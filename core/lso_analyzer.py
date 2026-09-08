import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import yfinance as yf

# Hand-researched risk tags per ticker: {"CCJ": ["ai-capex-power", ...], ...}.
# A ticker mapped to an empty list has been researched and carries no durable
# tag; a ticker absent from the file has not been researched yet, and the two
# are reported differently so the second can be worked through.
DURABLE_TAGS_FILE = "durable-tags.json"

_tags_cache: tuple[float, dict[str, list[str]]] | None = None

# Litigation is deliberately NOT a durable tag. Durable tags describe what a
# company *is* — a China ADR, a crypto proxy, an AI-capex name — and those hold
# for years. A docket is a dated fact: cases are filed, dismissed, settled and
# appealed, and an assessment written today is wrong within a quarter or two.
# durable-tags.json has nowhere to record when a fact was established, so a
# litigation entry kept there would rot silently while still gating a symbol.
# This store carries the date and the source alongside the severity.
LITIGATION_FILE = "litigation.json"

_litigation_cache: tuple[float, dict[str, dict]] | None = None

# How long an assessment is trusted. Docket facts move on filing events, so the
# review cadence follows the quarterly report that discloses them.
_LITIGATION_REVIEW_DAYS = 100

# Four outcomes, only two of which act. `clear` and `review` exist so an
# automated pass can record what it found without inventing a verdict: `clear`
# is "checked, nothing broken out", `review` is "there is legal text here but
# nothing decisive" — flagged for a human, deliberately not acted on, since
# defaulting those to overhang would cap the tier on most large filers.
_LITIGATION_SEVERITY: dict[str, str] = {
    "clear":
        "Checked against the latest filing — no broken-out legal matter",
    "review":
        "Automated review found legal text but nothing decisive — read the "
        "latest 10-Q Part II Item 1 and set a severity by hand",
    "existential":
        "Litigation that threatens the business itself — a ruling can gap the "
        "stock overnight, and the loss is not bounded by anything the chain "
        "prices; read it before writing anything against this name",
    "overhang":
        "Litigation overhang — a known case above the industry's background "
        "rate; a cash cost and a lid on the multiple, not a threat to the "
        "franchise; size it smaller rather than passing",
}

# Sector score adjustments and explanatory notes
_SECTOR_SCORES: dict[str, tuple[int, str]] = {
    "Utilities":             (+10, "Very stable demand; ideal wheel candidate"),
    "Consumer Defensive":   (+8,  "Stable consumer demand; good for wheel"),
    "Real Estate":          (+5,  "Income-oriented; generally stable"),
    "Industrials":          (+2,  "Moderate stability; watch macro cycle"),
    "Consumer Cyclical":    (0,   "Economic cycle exposure; monitor earnings"),
    "Communication Services":(0,  "Mixed — some stable, some high-growth volatile"),
    "Technology":           (0,   ""),
    "Financial Services":   (-8,  "Interest-rate sensitive; credit cycle exposure"),
    "Healthcare":           (-10, "Large-pharma stable; biotech is binary-event risk"),
    "Basic Materials":      (-15, "Commodity-price exposure; cyclical"),
    "Energy":               (-25, "Oil/gas price volatility; elevated geopolitical risk "
                                  "(Iran-Israel conflict, OPEC policy, Russia sanctions)"),
}

# Tags that disqualify outright, whatever the chain looks like. These are the
# strategy's "auto-disqualify" risks: binary or un-priceable, so no amount of
# cushion or position sizing compensates. They come from durable-tags.json
# rather than sector/industry, because sector doesn't identify them — a recent
# IPO looks like any other name in its sector.
_HARD_GATE_TAGS: dict[str, str] = {
    "biotech-binary":
        "FDA/trial-catalyst risk — readouts gap the stock overnight regardless "
        "of cushion",
}

# The IPO gate is derived, not tagged. "<12 months" is a fact with an expiry
# date, and a hand-written tag has no way to reach it — four of the six tickers
# once tagged `recent-ipo` had aged out while still being force-rejected. The
# listing date rides along in the yfinance `info` already fetched, so the gate
# computes its own age and stops firing on its own. The tag survives only as a
# fallback for when that field is missing.
_IPO_WINDOW_DAYS = 365
_IPO_TAG         = "recent-ipo"
_IPO_GATE_REASON = ("Recent IPO (<12 months) — RSI/BB% compute on partial "
                    "history, there is no confirmed floor, and lockup expiry "
                    "looms")

# Tags that cap the risk tier at High without disqualifying: the position is
# tradeable, it just can't be sized as anything better than the riskiest tier.
_TIER_CAP_TAGS: dict[str, str] = {
    "crypto-linked":
        "Crypto-linked — the floor is an external asset's sentiment, not the "
        "company's own fundamentals; size as a directional crypto bet",
}

# Hand-assigned tags for the IREN pattern: a core segment declining while a
# newer one tries to replace it, funded by dilution. Neither is derivable from
# yfinance (see analyze_symbol), so they are curated in durable-tags.json.
_DECLINING_TAG = "core-revenue-declining"
_DILUTION_TAG  = "active-dilution"

# Risk tier → max allocation, from the strategy's sizing table.
_TIER_ALLOCATION = {
    "Low":    "35–40%",
    "Medium": "25–30%",
    "High":   "15–20%",
}
_TIER_ORDER = {"Low": 0, "Medium": 1, "High": 2}

# An overhang resting on a dollar figure alone has to clear this fraction of
# market cap before it caps a position. A quantified amount is the filer's own
# statement that a matter is real, but not that it matters: AXP books a $12.5m
# accrual at 0.006% of cap, three orders of magnitude below the $971m Boeing
# carries at 0.58%. An absolute floor gets this backwards, since dollar size and
# materiality run in different directions — ALGN's $31.8m is a quarter of RCL's
# $130m and half again as material, 0.28% of an $11b company against 0.18% of a
# $71b one. A caption, a class action or a derivative suit still act on their
# own; this bar applies only when the amount is all there is.
_EXPOSURE_MATERIAL_PCT = 0.0025

_MONEY_SCALE = {"billion": 1_000_000_000, "million": 1_000_000, "": 1}


def _money_value(text: str) -> float:
    """Dollars from a stated figure ("$1.2 million" → 1200000.0), 0.0 if unread.

    Lives here rather than in ``litigation_review`` because the materiality test
    below is its only consumer that needs a number; the review module imports it
    back, which is the direction the dependency already runs.
    """
    m = re.search(r"\$\s?([\d,]*(?:\.\d+)?)\s*(million|billion)?", text, re.I)
    if not m or not m.group(1).strip(","):
        return 0.0
    try:
        amount = float(m.group(1).replace(",", ""))
    except ValueError:
        return 0.0
    return amount * _MONEY_SCALE[(m.group(2) or "").lower()]


# Additional penalty when the industry sub-type is especially risky
_INDUSTRY_EXTRA: dict[str, tuple[int, str]] = {
    "biotechnology":          (-20, "FDA/clinical-trial binary events — large overnight gaps likely"),
    "drug manufacturers":     (-15, "Regulatory binary risk; earnings driven by pipeline news"),
    "oil & gas":              (-5,  "Direct crude-price exposure"),
    "coal":                   (-10, "Structural decline; regulatory risk"),
    "uranium":                (-10, "Regulatory and geopolitical sensitivity"),
}


def load_durable_tags() -> dict[str, list[str]]:
    """Ticker → risk tags from ``durable-tags.json`` (empty dict if unreadable).

    Re-read whenever the file's mtime changes, so tags added during a session
    show up on the next analysis without restarting the app.
    """
    global _tags_cache
    path = Path(DURABLE_TAGS_FILE)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _tags_cache = None
        return {}
    if _tags_cache is not None and _tags_cache[0] == mtime:
        return _tags_cache[1]
    try:
        raw = json.loads(path.read_text())
    except Exception:
        _tags_cache = None
        return {}
    tags = {
        str(sym).strip().upper(): [str(t) for t in (val or [])]
        for sym, val in raw.items()
    }
    _tags_cache = (mtime, tags)
    return tags


def load_litigation() -> dict[str, dict]:
    """Ticker → litigation entry from ``litigation.json`` (empty if unreadable).

    Same mtime-cached read as the durable tags, so an entry edited mid-session
    takes effect on the next analysis. An entry may be a bare severity string,
    which is accepted but carries no date — and therefore counts as stale.
    """
    global _litigation_cache
    path = Path(LITIGATION_FILE)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _litigation_cache = None
        return {}
    if _litigation_cache is not None and _litigation_cache[0] == mtime:
        return _litigation_cache[1]
    try:
        raw = json.loads(path.read_text())
    except Exception:
        _litigation_cache = None
        return {}
    entries = {
        str(sym).strip().upper(): ({"severity": val} if isinstance(val, str)
                                   else dict(val or {}))
        for sym, val in raw.items()
    }
    _litigation_cache = (mtime, entries)
    return entries


def litigation_for(symbol: str) -> dict | None:
    """The litigation entry for a symbol, or None if it has none on file."""
    return load_litigation().get((symbol or "").strip().upper())


def _parse_iso(value) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except Exception:
        return None


def _litigation_state(entry: dict | None, today: date | None = None) -> dict:
    """``{severity, stale, review_by, reason}`` for one litigation entry.

    Staleness never silently flips the verdict either way. An expired entry keeps
    applying and is flagged instead, because the two failure modes are not
    symmetric: a gate that wrongly stops firing can put you short puts into a
    live disaster, while one that wrongly keeps firing only costs a trade. An
    entry with no usable date is stale from the start — undated is unverified.
    """
    if not entry:
        return {"severity": None, "stale": False, "review_by": None,
                "reason": "", "exposure": None, "exposure_only": False}
    today = today or date.today()

    severity = str(entry.get("severity", "")).strip().lower()
    unknown  = severity not in _LITIGATION_SEVERITY
    if unknown:
        # An unreadable severity is a data error, not a clean bill of health —
        # but it is also not evidence of a case, so it lands on `review`:
        # visible and unacted-on, rather than silently capping or silently
        # clearing on a malformed entry.
        severity = "review"

    review_by = _parse_iso(entry.get("review_by"))
    if review_by is None:
        asof = _parse_iso(entry.get("asof"))
        review_by = (asof + timedelta(days=_LITIGATION_REVIEW_DAYS)) if asof else None
    stale = unknown or review_by is None or today > review_by

    reason = _LITIGATION_SEVERITY[severity]
    if entry.get("case"):
        reason += f" [{entry['case']}]"
    if entry.get("note"):
        reason += f" — {entry['note']}"
    return {"severity": severity, "stale": stale,
            "review_by": review_by.isoformat() if review_by else None,
            "reason": reason,
            # Passed through for the materiality test in _risk_tier, which is
            # where the market cap to measure the amount against is known.
            "exposure": entry.get("exposure"),
            "exposure_only": bool(entry.get("exposure_only"))}


def _ipo_gate_reason(info: dict, tags: list[str], today: date | None = None) -> str:
    """Reason string if the symbol is inside the IPO window, else ``""``.

    Derived from the listing date in ``info`` when it is there — that expires by
    itself — and only falling back to the hand-written tag when it is not.
    """
    today = today or date.today()
    # Read the unit off the field name rather than the magnitude. Sniffing by
    # size gets it wrong for anything listed before ~1973: PEP's 1972 date is
    # 76,253,400,000 ms, which is small enough to pass for a seconds value and
    # lands in the year 4386 — reading as "listed in the future", i.e. gated.
    # Both fields are also negative for pre-1970 listings (KO, 1962), so the
    # epoch is offset explicitly instead of via date.fromtimestamp().
    listed = None
    for field, unit in (("firstTradeDateMilliseconds", "milliseconds"),
                        ("firstTradeDateEpochUtc",     "seconds")):
        raw = info.get(field)
        if raw is None:
            continue
        try:
            listed = (datetime(1970, 1, 1) + timedelta(**{unit: int(raw)})).date()
            break
        except Exception:
            listed = None
    if listed is None:
        return _IPO_GATE_REASON if _IPO_TAG in tags else ""
    age = (today - listed).days
    if age > _IPO_WINDOW_DAYS:
        return ""
    return f"{_IPO_GATE_REASON} — listed {listed.isoformat()}, {age} days ago"


def tags_for(symbol: str) -> tuple[list[str], bool]:
    """``(tags, researched)`` for a symbol. Not in the file → ``([], False)``."""
    tags = load_durable_tags()
    key = (symbol or "").strip().upper()
    if key not in tags:
        return [], False
    return tags[key], True


def _ttm_operating_income(qis) -> float | None:
    """Trailing four quarters of operating income, or None if unavailable."""
    try:
        if qis is None or qis.empty or "Operating Income" not in qis.index:
            return None
        s = qis.loc["Operating Income"].dropna()[:4]
        return float(s.sum()) if len(s) else None
    except Exception:
        return None


def _profitability(info: dict, ttm_operating: float | None = None) -> tuple[str, bool, str]:
    """``(state, growing, note)`` where state is profitable/unprofitable/unknown.

    Operating income leads, because the bottom line lies. IREN's trailing EPS
    reads +$0.77 on a one-off gain booked below the operating line, while its
    TTM operating income is −$221M — the loss the strategy actually cares about.
    Trailing EPS and net income are then checked as a backstop, either one
    negative counting as unprofitable: yfinance disagrees with itself often
    enough (MSTR reports profitMargins 0.0 alongside a −$31B net income) that no
    single field can be trusted alone.

    ``growing`` is revenue growth above zero, the closest available stand-in for
    the strategy's "growing with a credible path". It does not distinguish a
    real product-market fit from a one-off revenue bump.
    """
    eps = info.get("trailingEps")
    net = info.get("netIncomeToCommon")
    growth = info.get("revenueGrowth")
    growing = bool(growth is not None and growth > 0)

    losing, why = False, ""
    if ttm_operating is not None and ttm_operating < 0:
        losing = True
        why = f"operating loss ${abs(ttm_operating)/1e6:,.0f}M TTM"
    known = [v for v in (eps, net) if v is not None]
    if any(v < 0 for v in known):
        losing = True
        why = why or "negative trailing EPS / net income"

    if not losing:
        if ttm_operating is None and not known:
            return "unknown", growing, ""
        return "profitable", growing, ""

    note = f"Unprofitable ({why})"
    if growth is not None:
        note += (f", revenue growing {growth*100:.0f}%" if growing
                 else f", revenue declining {growth*100:.0f}%")
    return "unprofitable", growing, note + " — capped at High risk tier"


def _bs_series(bs, row: str):
    """A balance-sheet row as a newest-first series with gaps dropped."""
    try:
        if bs is None or bs.empty or row not in bs.index:
            return None
        s = bs.loc[row].dropna().sort_index(ascending=False)
        return s if len(s) else None
    except Exception:
        return None


def _external_funding(bs, market_cap: int | None) -> tuple[bool, bool, str]:
    """``(diluting, levering, detail)`` — is the buildout funded by issuing paper?

    Both routes are checked because they substitute for each other: IREN did
    both, MSTR only diluted, CLSK only borrowed. Requiring both would miss two
    of the three.

    New debt is measured against market cap rather than against prior debt —
    RBRK's debt grew 243% off a tiny base, which is 5% of its market cap and
    means nothing. Dilution must also be *sustained* (rising in at least three
    of the last four quarters): an ATM program drips quarter after quarter,
    while a stock-funded acquisition lands in one step. Synopsys issued 23% in
    the single quarter to 2025-07-31 and was flat either side; that shape is a
    deal, not a funding problem.
    """
    shares = _bs_series(bs, "Ordinary Shares Number")
    if shares is None or len(shares) < 2:
        return False, False, ""

    back     = min(4, len(shares) - 1)
    base     = shares.iloc[back]
    dilution = (shares.iloc[0] - base) / base if base else 0.0

    # Consecutive quarter-on-quarter increases above 1% (noise floor for
    # buybacks and stock comp netting out).
    rising = sum(1 for a, b in zip(shares.iloc[:-1], shares.iloc[1:])
                 if b and (a - b) / b > 0.01)
    steady = rising >= 3

    debt_added = 0.0
    debt = _bs_series(bs, "Total Debt")
    if debt is not None and len(debt) > 1 and market_cap:
        d_back = min(4, len(debt) - 1)
        debt_added = (debt.iloc[0] - debt.iloc[d_back]) / market_cap

    diluting = dilution >= 0.20 and steady
    levering = debt_added >= 0.15
    if not (diluting or levering):
        return False, False, ""

    bits = []
    if diluting:
        bits.append(f"share count {dilution*100:+.0f}% over {back} quarters, "
                    f"rising in {rising} of the last {len(shares)-1}")
    if levering:
        bits.append(f"net new debt worth {debt_added*100:.0f}% of market cap")
    return diluting, levering, "; ".join(bits)


def _immaterial_exposure(litigation: dict, market_cap: float | None) -> str | None:
    """Why an exposure-only overhang isn't capping the tier, or None if it is.

    Only entries flagged ``exposure_only`` are eligible: the amount is the whole
    case for the overhang, so if it is immaterial there is nothing left. An
    unknown cap or an unparseable figure means the test can't be run, and an
    untested overhang keeps applying — the asymmetry the litigation store is
    built on, where a cap that wrongly fires costs a trade and one that wrongly
    stands down costs a position.
    """
    if not litigation.get("exposure_only") or not market_cap:
        return None
    exposure = litigation.get("exposure") or ""
    value = _money_value(exposure)
    if not value or value >= market_cap * _EXPOSURE_MATERIAL_PCT:
        return None
    return (f"Litigation overhang rests on a quantified {exposure.strip()} alone, "
            f"{value / market_cap:.3%} of market cap — below the "
            f"{_EXPOSURE_MATERIAL_PCT:.3%} materiality bar, so it is recorded "
            f"but does not cap sizing")


def _risk_tier(score: int, tags: list[str], profit_state: str,
               funded_by_paper: bool = False,
               litigation: dict | None = None,
               market_cap: float | None = None) -> tuple[str, str, list[str]]:
    """``(tier, max_allocation, notes)`` for a symbol.

    The base tier follows the graded score, since the score already weighs the
    beta / market-cap / cushion inputs the strategy's tier table describes.
    Crypto-linkage, unprofitability and litigation then *cap* the tier at High —
    they are sizing inputs, not gates, so they never reject a candidate, they
    only stop it being sized as anything safer. ``review`` and ``clear`` don't
    size at all; ``existential`` caps like ``overhang`` rather than harder,
    because it is a flag for you to read, not a verdict the analyzer acts on.
    """
    tier = "Low" if score >= 85 else ("Medium" if score >= 70 else "High")
    notes: list[str] = []

    for tag, why in _TIER_CAP_TAGS.items():
        if tag in tags:
            if _TIER_ORDER[tier] < _TIER_ORDER["High"]:
                notes.append(f"{why} (tier capped from {tier} to High)")
            else:
                notes.append(why)
            tier = "High"

    if litigation and litigation.get("severity") in ("overhang", "existential"):
        immaterial = _immaterial_exposure(litigation, market_cap)
        if immaterial:
            notes.append(immaterial)
        else:
            why = litigation["reason"]
            if _TIER_ORDER[tier] < _TIER_ORDER["High"]:
                notes.append(f"{why} (tier capped from {tier} to High)")
            else:
                notes.append(why)
            tier = "High"

    if profit_state == "unprofitable":
        tier = "High"

    allocation = _TIER_ALLOCATION[tier]

    # The IREN pattern is a drift problem, not a variance problem — wider
    # cushion doesn't compensate, so it sits at the bottom of High or passes.
    # Dilution is measured (see _external_funding); the declining core segment
    # needs a revenue split yfinance doesn't carry, so it stays hand-tagged.
    diluting = funded_by_paper or _DILUTION_TAG in tags
    if _DECLINING_TAG in tags and diluting:
        tier = "High"
        allocation = "15% or pass"
        notes.append(
            "Core segment declining while funded by issuing paper (IREN "
            "pattern) — structural, not variance; bottom of the High range "
            "or pass")
    return tier, allocation, notes


def _score_beta(beta: float | None) -> tuple[int, str]:
    if beta is None:
        return 0, ""
    if beta < 0.5:
        return +8,  f"Beta {beta:.2f} — very low volatility"
    if beta < 0.8:
        return +5,  f"Beta {beta:.2f} — below-market volatility"
    if beta < 1.2:
        return 0, ""
    if beta < 1.5:
        return 0, f"Beta {beta:.2f} — elevated volatility; size conservatively"
    if beta < 2.0:
        return 0, f"Beta {beta:.2f} — high volatility; use wider OTM cushion"
    return 0, f"Beta {beta:.2f} — very high volatility; high risk tier sizing"


def _score_market_cap(cap: int | None) -> tuple[int, str]:
    if cap is None:
        return 0, ""
    b = cap / 1e9
    if b >= 10:
        return +5, f"Large cap ${b:.1f}B — liquid options market"
    if b >= 2:
        return 0,  ""
    if b >= 0.5:
        return -10, f"Small cap ${b:.1f}B — option liquidity may be thin"
    return -20, f"Micro cap ${b:.2f}B — wide spreads; assignment risk"


def _score_to_grade(score: int) -> str:
    if score >= 85: return "A"
    if score >= 70: return "B"
    if score >= 55: return "C"
    if score >= 40: return "D"
    return "F"


def _score_otm(otm_pct: float) -> tuple[int, str, str | None]:
    """Return (score_adj, note, flag_or_None) for OTM% on a short put."""
    if otm_pct < 0:
        return -100, (
            f"Strike is {abs(otm_pct):.1f}% ITM — put is in-the-money; "
            "immediate assignment risk"
        ), "ITM STRIKE"
    if otm_pct < 2:
        return -30, (
            f"Strike {otm_pct:.1f}% OTM — market pricing real downside risk; "
            "insufficient cushion for 1% rule"
        ), "NEAR ATM"
    if otm_pct < 4:
        return -10, (
            f"Strike {otm_pct:.1f}% OTM — marginal cushion; "
            "only acceptable for low-beta, high-dividend names"
        ), "MARGINAL OTM"
    if otm_pct < 8:
        return +3, f"Strike {otm_pct:.1f}% OTM — good cushion", None
    if otm_pct < 12:
        return +5, f"Strike {otm_pct:.1f}% OTM — excellent cushion; strong downside protection", None
    if otm_pct <= 16:
        return -5, f"Strike {otm_pct:.1f}% OTM — wide; verify premium is still meaningful", None
    return -30, (
        f"Strike {otm_pct:.1f}% OTM — market warning label; "
        "wide OTM for 1% premium signals severe downside priced in"
    ), "WIDE OTM"


def _score_sigma_cushion(cushion_sigma: float | None, gappy: bool) -> tuple[int, str, str | None]:
    """Volatility-adjusted distance to the strike, in expected moves (σ).

    Graded by how short the cushion actually is rather than gated on one line.
    ``need`` is the adequate mark — 1σ, or 1.5σ for gappy names (earnings in the
    period, or the geopolitical-commodity sectors) — and the bands step half a σ
    either side of it.

    Half an expected move from the strike is reckless and takes a real penalty.
    The band just under adequate is where most standard wheel strikes live: a
    0.5σ cushion is roughly a 0.28-delta put, which is a mainstream trade rather
    than a disqualifying one, so it costs a tilt and competes on its premium and
    technicals instead of being vetoed outright.
    """
    if cushion_sigma is None:
        # No IV came back for this symbol, so the primary gate can't be applied
        # at all — and neither can the IV band or IV/HV. Scoring that at zero
        # would let an unmeasured contract outrank a measured one, so an
        # unknown costs a little rather than nothing.
        return -5, (
            "σ-cushion unavailable — no IV data for this symbol, so the primary "
            "risk gate couldn't be applied; treat the grade as provisional"
        ), "IV UNAVAILABLE"
    need  = 1.5 if gappy else 1.0
    extra = " (gappy name)" if gappy else ""
    if cushion_sigma < need - 0.5:
        return -15, (
            f"Cushion {cushion_sigma:.2f}σ — inside half an expected move of the "
            f"strike{extra}; assignment takes less than a normal move"
        ), f"SUB-{need - 0.5:g}σ"
    if cushion_sigma < need:
        return -5, (
            f"Cushion {cushion_sigma:.2f}σ < {need:g}σ{extra} — short of adequate; "
            "the premium needs to be earning its keep"
        ), None
    if cushion_sigma < need + 0.5:
        return 0, f"Cushion {cushion_sigma:.2f}σ — adequate (≥{need:g}σ){extra}", None
    return +5, f"Cushion {cushion_sigma:.2f}σ — strong (≥{need + 0.5:g}σ){extra}", None


def _score_iv(iv_pct: float | None) -> tuple[int, str, str | None]:
    """Absolute IV band for management viability (roll/CC premium in every phase)."""
    if not iv_pct:
        return 0, "", None
    if iv_pct < 25:
        return -15, (
            f"IV {iv_pct:.0f}% < 25% — grinder: thin premium to enter, roll, "
            "and sell covered calls if assigned"
        ), "LOW IV"
    if iv_pct <= 80:
        return 0, f"IV {iv_pct:.0f}% — normal working range", None
    return -8, (
        f"IV {iv_pct:.0f}% > 80% — rich premium but size down and treat "
        "gappiness seriously"
    ), "HIGH IV"


def _score_iv_hv(iv_hv: float | None) -> tuple[int, str, str | None]:
    """Timing: is the option pricing more movement than the stock delivers?

    Implied volatility over the underlying's own realized volatility. Above 1 the
    market is charging more for the move than the stock has actually been making,
    which is the edge a premium seller is paid for; below 1 you are selling
    movement cheaper than it has been happening.
    """
    if iv_hv is None:
        return 0, "", None
    if iv_hv >= 1.3:
        return +3, (
            f"IV/HV {iv_hv:.2f} — options pricing well above the realized move; "
            "premium is rich"
        ), None
    if iv_hv >= 0.9:
        return 0, f"IV/HV {iv_hv:.2f} — implied roughly in line with realized movement", None
    return -3, (
        f"IV/HV {iv_hv:.2f} — implied below realized; you'd be selling the move "
        "cheaper than the stock has been making it"
    ), "IV BELOW REALIZED"


def _score_rsi(rsi: float | None) -> tuple[int, str, str | None]:
    """Entry timing on momentum: oversold is the setup, capitulation is a trap.

    A short put is a bet on not falling much further, so a mild pullback is the
    entry and an extreme reading cuts both ways — the premium is richest exactly
    where assignment leaves you long a name in freefall.
    """
    if rsi is None:
        return 0, "", None
    if rsi < 20:
        return -5, (
            f"RSI {rsi:.0f} — capitulation, not a dip; assignment risks "
            "catching a falling knife"
        ), "RSI EXTREME"
    if rsi < 40:
        return +5, f"RSI {rsi:.0f} — oversold; the pullback the screen looks for", None
    if rsi < 60:
        return 0, f"RSI {rsi:.0f} — neutral momentum", None
    if rsi < 70:
        return -3, f"RSI {rsi:.0f} — extended; entering late in the move", None
    return -5, (
        f"RSI {rsi:.0f} — overbought; selling puts near a local high leaves "
        "little room before the mean catches up"
    ), "OVERBOUGHT"


def _score_bb_pct(bb_pct: float | None) -> tuple[int, str, str | None]:
    """Where price sits in its Bollinger range: 0 = lower band, 100 = upper."""
    if bb_pct is None:
        return 0, "", None
    if bb_pct < 0:
        return -5, (
            f"BB% {bb_pct:.1f} — below the lower band; a breakdown rather than "
            "a dip within the range"
        ), "BELOW BAND"
    if bb_pct < 33:
        return +5, f"BB% {bb_pct:.1f} — lower third of the range; good entry zone", None
    if bb_pct <= 67:
        return 0, f"BB% {bb_pct:.1f} — mid-range", None
    if bb_pct <= 100:
        return -3, f"BB% {bb_pct:.1f} — upper third; thin cushion for a short put", None
    return -5, (
        f"BB% {bb_pct:.1f} — above the upper band; extended, with the whole "
        "range to fall back through"
    ), "ABOVE BAND"


def _score_spread(spread_pct: float | None) -> tuple[int, str, str | None]:
    """Can you get back out? The bid-ask spread as a share of the mid.

    Every roll is a buy-to-close plus a sell-to-open, so the spread is the toll
    on managing the position — and it is worst on exactly the deep-ITM strikes
    where rolling is the thing you need. A wide enough market means the roll
    only ever existed on paper.
    """
    if spread_pct is None:
        return 0, "", None
    if spread_pct < 10:
        return +3, f"Spread {spread_pct:.0f}% of mid — tight; cheap to roll or close", None
    if spread_pct <= 25:
        return 0, f"Spread {spread_pct:.0f}% of mid — workable for a weekly", None
    if spread_pct <= 50:
        return -5, (
            f"Spread {spread_pct:.0f}% of mid — wide; a round trip gives back a "
            "meaningful slice of the premium"
        ), "WIDE SPREAD"
    return -15, (
        f"Spread {spread_pct:.0f}% of mid — no real market; the cost to get out "
        "can exceed the time value you're selling"
    ), "NO MARKET"


def apply_contract_adjustments(
    result: dict,
    otm_pct: float | None,
    *,
    iv: float | None = None,
    cushion_sigma: float | None = None,
    iv_hv: float | None = None,
    rsi: float | None = None,
    bb_pct: float | None = None,
    spread_pct: float | None = None,
    open_interest: int | None = None,
) -> dict:
    """Re-score and re-grade a symbol result for a specific contract.

    Layers the OTM% band, the σ-cushion gate (primary), the absolute IV band, the
    IV-vs-realized timing signal, the underlying's technical position (RSI and
    BB%, from the stock scan), and how tight the market is (bid-ask spread) on
    top of the symbol's fundamental score.
    """
    # A hard-gated symbol stays rejected whatever the contract looks like —
    # otherwise a generous chain would re-score it back above F.
    if result.get("reject"):
        return {
            **result,
            "iv": iv, "cushion_sigma": cushion_sigma, "iv_hv": iv_hv,
            "rsi": rsi, "bb_pct": bb_pct, "spread_pct": spread_pct,
            "open_interest": open_interest,
        }

    if otm_pct is None:
        return result

    score     = result.get("score", 50)
    flags_str = result.get("flags", "—")
    notes_str = result.get("notes", "")

    flag_list = [] if flags_str == "—" else flags_str.split(" | ")
    note_list = [] if notes_str == "No major concerns" else notes_str.split(" • ")

    # "Gappy" → require a wider (1.5σ) cushion: earnings inside the period, or a
    # geopolitical-commodity sector (per the strategy's gappiness overlay).
    gappy = bool(result.get("earnings_in_period")) or \
        result.get("sector") in ("Energy", "Basic Materials")

    total_adj = 0
    for adj, note, flag in (
        _score_otm(otm_pct),
        _score_sigma_cushion(cushion_sigma, gappy),
        _score_iv(iv),
        _score_iv_hv(iv_hv),
        _score_rsi(rsi),
        _score_bb_pct(bb_pct),
        _score_spread(spread_pct),
    ):
        total_adj += adj
        if flag:
            flag_list.append(flag)
        if note:
            note_list.append(note)

    new_score = max(0, min(100, score + total_adj))

    # Re-tier on the contract-level score: the tier follows the grade, and the
    # caps (crypto-linked, unprofitable, litigation overhang) re-apply on top of
    # it. The tier notes
    # are already in note_list from the symbol pass, so they aren't re-added.
    cap_b = result.get("mkt_cap_b")
    tier, allocation, _ = _risk_tier(new_score, result.get("tags") or [],
                                     result.get("profitability", "unknown"),
                                     bool(result.get("funded_by_paper")),
                                     result.get("litigation"),
                                     cap_b * 1e9 if cap_b else None)

    return {
        **result,
        "score": new_score,
        "grade": _score_to_grade(new_score),
        "risk_tier":      tier,
        "max_allocation": allocation,
        "iv":            iv,
        "cushion_sigma": cushion_sigma,
        "iv_hv":         iv_hv,
        "rsi":           rsi,
        "bb_pct":        bb_pct,
        "spread_pct":    spread_pct,
        "open_interest": open_interest,
        "flags": " | ".join(flag_list) if flag_list else "—",
        "notes": " • ".join(note_list) if note_list else "No major concerns",
    }


def analyze_symbol(symbol: str, expiration: date, on_log=None) -> dict:
    base   = 70
    score  = base
    flags  = []
    notes  = []

    # Tags and the litigation entry are local data, not market data — they
    # survive a yfinance failure, and the error path below honours them too.
    tags, tags_researched = tags_for(symbol)
    litigation = _litigation_state(litigation_for(symbol))

    try:
        ticker = yf.Ticker(symbol)
        info   = ticker.info or {}

        sector   = info.get("sector",    "Unknown")
        industry = (info.get("industry", "") or "").lower()
        beta     = info.get("beta")
        cap      = info.get("marketCap")
        div_rate = info.get("dividendRate") or 0

        # ── Sector ───────────────────────────────────────────────────────────
        if sector in _SECTOR_SCORES:
            adj, note = _SECTOR_SCORES[sector]
            score += adj
            if adj < 0:
                flags.append(sector.upper())
                notes.append(note)
            elif adj > 0:
                notes.append(note)

        # ── Industry sub-type ─────────────────────────────────────────────
        for keyword, (adj, note) in _INDUSTRY_EXTRA.items():
            if keyword in industry:
                score += adj
                flags.append(keyword.upper().replace(" & ", "/"))
                notes.append(note)
                break

        # ── Beta ──────────────────────────────────────────────────────────
        adj, note = _score_beta(beta)
        score += adj
        if note:
            if adj < 0:
                flags.append("HIGH BETA" if (beta or 0) >= 1.5 else "ELEVATED BETA")
            notes.append(note)

        # ── Market cap ────────────────────────────────────────────────────
        adj, note = _score_market_cap(cap)
        score += adj
        if note:
            if adj < 0:
                flags.append("SMALL CAP" if (cap or 0) >= 500_000_000 else "MICRO CAP")
            notes.append(note)

        # ── Dividend (bonus for wheel) ────────────────────────────────────
        if div_rate and div_rate > 0:
            score += 5
            notes.append(f"Pays dividend ${div_rate:.2f}/yr — favourable for wheel")

        # ── Earnings date ─────────────────────────────────────────────────
        today         = date.today()
        earnings_date = None
        earnings_in_period = False
        try:
            cal = ticker.calendar or {}
            raw_dates = cal.get("Earnings Date", [])
            if raw_dates:
                ed = raw_dates[0]
                if hasattr(ed, "date"):
                    ed = ed.date()
                earnings_date = ed
                if today < ed <= expiration:
                    earnings_in_period = True
                    score -= 40
                    flags.append("EARNINGS IN PERIOD")
                    notes.append(
                        f"Earnings {ed} falls within option period — "
                        "expect large IV move; high assignment risk"
                    )
                # Within 30 days after expiration — IV is already elevated going
                # in. Built by day arithmetic rather than incrementing the month:
                # month+1 wrapped December into the same year (so the branch
                # could never fire) and overflowed on month-end expirations
                # (31 Jan -> 31 Feb), raising a ValueError the except below
                # swallowed without trace.
                elif expiration < ed <= expiration + timedelta(days=30):
                    score -= 5
                    notes.append(f"Earnings {ed} shortly after expiration — IV may be elevated")
        except Exception:
            pass

        # ── Profitability and external funding (sizing inputs, never gates) ─
        # Two extra fetches per symbol. Each is guarded on its own: a missing
        # statement degrades that one signal rather than failing the analysis,
        # and _profitability falls back to the info fields it already has.
        try:
            ttm_operating = _ttm_operating_income(ticker.quarterly_income_stmt)
        except Exception:
            ttm_operating = None
        try:
            balance = ticker.quarterly_balance_sheet
        except Exception:
            balance = None

        profit_state, _growing, profit_note = _profitability(info, ttm_operating)
        if profit_note:
            notes.append(profit_note)
            flags.append("UNPROFITABLE")

        diluting, levering, funding_note = _external_funding(balance, cap)
        funded_by_paper = (diluting or levering) and profit_state == "unprofitable"
        if funded_by_paper:
            flags.append("DILUTING" if diluting else "LEVERING")
            notes.append(f"Loss-making and funded by issuing paper — {funding_note}")

        # ── Clamp and grade ───────────────────────────────────────────────
        score = max(0, min(100, score))
        grade = _score_to_grade(score)

        # ── Hard gates: binary risks no position size fixes ────────────────
        reject_reasons = [why for tag, why in _HARD_GATE_TAGS.items() if tag in tags]
        ipo_reason = _ipo_gate_reason(info, tags)
        if ipo_reason:
            reject_reasons.append(ipo_reason)
        # Litigation never rejects — it flags. A hard gate is self-defeating
        # for a risk you intend to read case by case: `Reject` zeroes the score
        # and apply_contract_adjustments() returns early, so the name never
        # reaches you with a workable contract and the review never happens.
        # The severities also come from a regex over one filing, and a machine
        # verdict is not a fact: PYPL graded F on "Civil Investigative Demand",
        # routine language for any consumer-finance filer. So the flag rides
        # into the LSO table and the CSV export, the tier cap sizes the name
        # down, and the decision stays yours.
        if litigation["severity"] == "existential":
            flags.append("LITIGATION SEVERE")
            notes.append(litigation["reason"])
        if litigation["severity"] == "overhang":
            flags.append("LITIGATION OVERHANG")
        if litigation["severity"] == "review":
            flags.append("LITIGATION UNCLEAR")
            notes.append(litigation["reason"])
        if litigation["stale"]:
            flags.append("LITIGATION REVIEW DUE")
            notes.append(
                "Litigation assessment is past its review date"
                + (f" ({litigation['review_by']})" if litigation["review_by"]
                   else " (undated)")
                + " — it still applies, but re-read the latest 10-Q")
        if reject_reasons:
            score = 0
            grade = "F"
            flags.append("HARD REJECT")
            notes.extend(reject_reasons)

        tier, allocation, tier_notes = _risk_tier(score, tags, profit_state,
                                                  funded_by_paper, litigation, cap)
        notes.extend(tier_notes)
        if reject_reasons:
            tier, allocation = "Reject", "0%"

        return {
            "symbol":             symbol,
            "grade":              grade,
            "score":              score,
            "sector":             sector,
            "tags":               tags,
            "tags_researched":    tags_researched,
            "profitability":      profit_state,
            "funded_by_paper":    funded_by_paper,
            "risk_tier":          tier,
            "max_allocation":     allocation,
            "reject":             bool(reject_reasons),
            "reject_reason":      " • ".join(reject_reasons),
            "litigation":         litigation,
            "industry":           info.get("industry", ""),
            "beta":               round(beta, 2) if beta is not None else None,
            "mkt_cap_b":          round((cap or 0) / 1e9, 2) if cap else None,
            "earnings_date":      str(earnings_date) if earnings_date else "",
            "earnings_in_period": earnings_in_period,
            "flags":              " | ".join(flags) if flags else "—",
            "notes":              " • ".join(notes) if notes else "No major concerns",
        }

    except Exception as e:
        if on_log:
            on_log(f"  {symbol}: data error — {e}")
        # The market data is gone but the local data isn't, so the litigation
        # flags and the tier cap still apply — they just never reject here
        # either.
        err_flags = ["DATA ERROR"]
        if litigation["severity"] == "existential":
            err_flags.append("LITIGATION SEVERE")
        if litigation["severity"] == "overhang":
            err_flags.append("LITIGATION OVERHANG")
        if litigation["severity"] == "review":
            err_flags.append("LITIGATION UNCLEAR")
        if litigation["stale"]:
            err_flags.append("LITIGATION REVIEW DUE")
        # No market cap on this path — yfinance is what failed — so an
        # exposure-only overhang goes untested and keeps capping.
        err_tier, err_alloc, _ = _risk_tier(50, tags, "unknown", False, litigation)
        return {
            "symbol":             symbol,
            "grade":              "?",
            "score":              50,
            "sector":             "Error",
            "tags":               tags,
            "tags_researched":    tags_researched,
            "profitability":      "unknown",
            "funded_by_paper":    False,
            "risk_tier":          err_tier,
            "max_allocation":     err_alloc,
            "reject":             False,
            "reject_reason":      "",
            "litigation":         litigation,
            "industry":           "",
            "beta":               None,
            "mkt_cap_b":          None,
            "earnings_date":      "",
            "earnings_in_period": False,
            "flags":              " | ".join(err_flags),
            # The litigation reason is local data and survives the failure —
            # it is the whole point of keeping the flags on this path.
            "notes":              " • ".join(
                [str(e)] + ([litigation["reason"]] if litigation["severity"]
                            in ("existential", "overhang", "review") else [])),
        }


def analyze_symbols(
    symbols: list[str],
    expiration: date,
    on_log=None,
    on_progress=None,
    stop_flag=None,
    throttle: float = 0.3,
    review_litigation: bool = True,
) -> list[dict]:
    # Litigation status is only actionable for names you might actually write
    # against, and it decays — so it is refreshed here, for exactly the symbols
    # being analysed, rather than pre-computed across the universe. Entries
    # inside their review window are reused, so a repeat analysis fetches
    # nothing and a quarterly refresh happens by itself.
    if review_litigation and symbols:
        try:
            from core import litigation_review
            litigation_review.refresh(symbols, on_log=on_log, stop_flag=stop_flag)
        except Exception as e:                # EDGAR is not required to grade
            if on_log:
                on_log(f"Litigation review unavailable ({e}) — using entries on file.")

    results = []
    total   = len(symbols)
    for i, sym in enumerate(symbols):
        if stop_flag and stop_flag():
            if on_log:
                on_log("Stopped by user.")
            break
        if on_log:
            on_log(f"Analyzing {sym} ({i+1}/{total}) …")
        results.append(analyze_symbol(sym, expiration, on_log))
        if on_progress:
            on_progress(i + 1, total)
        time.sleep(throttle)
    return results
