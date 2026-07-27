import time
from datetime import date, timedelta

import yfinance as yf

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

# Additional penalty when the industry sub-type is especially risky
_INDUSTRY_EXTRA: dict[str, tuple[int, str]] = {
    "biotechnology":          (-20, "FDA/clinical-trial binary events — large overnight gaps likely"),
    "drug manufacturers":     (-15, "Regulatory binary risk; earnings driven by pipeline news"),
    "oil & gas":              (-5,  "Direct crude-price exposure"),
    "coal":                   (-10, "Structural decline; regulatory risk"),
    "uranium":                (-10, "Regulatory and geopolitical sensitivity"),
}


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
    return {
        **result,
        "score": new_score,
        "grade": _score_to_grade(new_score),
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

        # ── Clamp and grade ───────────────────────────────────────────────
        score = max(0, min(100, score))
        grade = _score_to_grade(score)

        return {
            "symbol":             symbol,
            "grade":              grade,
            "score":              score,
            "sector":             sector,
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
        return {
            "symbol":             symbol,
            "grade":              "?",
            "score":              50,
            "sector":             "Error",
            "industry":           "",
            "beta":               None,
            "mkt_cap_b":          None,
            "earnings_date":      "",
            "earnings_in_period": False,
            "flags":              "DATA ERROR",
            "notes":              str(e),
        }


def analyze_symbols(
    symbols: list[str],
    expiration: date,
    on_log=None,
    on_progress=None,
    stop_flag=None,
    throttle: float = 0.3,
) -> list[dict]:
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
