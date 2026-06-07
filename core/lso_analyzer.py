import time
from datetime import date

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


def apply_contract_adjustments(result: dict, otm_pct: float | None) -> dict:
    """Re-score and re-grade a symbol result for a specific contract's OTM%."""
    if otm_pct is None:
        return result

    score     = result.get("score", 50)
    flags_str = result.get("flags", "—")
    notes_str = result.get("notes", "")

    flag_list = [] if flags_str == "—" else flags_str.split(" | ")
    note_list = [] if notes_str == "No major concerns" else notes_str.split(" • ")

    adj, note, flag = _score_otm(otm_pct)
    if flag:
        flag_list.append(flag)
    note_list.append(note)

    new_score = max(0, min(100, score + adj))
    return {
        **result,
        "score": new_score,
        "grade": _score_to_grade(new_score),
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
                elif expiration < ed <= date(expiration.year, expiration.month + 1
                                             if expiration.month < 12 else 1,
                                             expiration.day):
                    # Within ~30 days after expiration — IV will be elevated
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
