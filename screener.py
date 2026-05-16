#!/usr/bin/env python3
"""
Put Selling Screener
Screens all US stocks for cash-secured put candidates using ThetaData.

Criteria:
  - Stock price: $10 - $75
  - RSI(14) < 40
  - Bollinger %B < 33  (price in lower third of 20-day band)
  - DTE: 4 - 21 days
  - Put premium yield: 0.9% - 1.1%  (bid / stock price)

Uses EOD data — compatible with ThetaData free tier.
Saves progress after the technical scan so options fetch can resume
if interrupted.
"""

import sys
import time
import json
import logging
import requests
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np
from tqdm import tqdm
from thetadata import ThetaClient
from thetadata.errors import AuthenticationError, NoDataFoundError

# ── Configuration ─────────────────────────────────────────────────────────────

STOCK_PRICE_MIN  = 10.0
STOCK_PRICE_MAX  = 75.0
DTE_MIN          = 4
DTE_MAX          = 21
YIELD_MIN        = 0.009    # 0.9%
YIELD_MAX        = 0.011    # 1.1%
RSI_PERIOD       = 14
BB_PERIOD        = 20
BB_STD_MULT      = 2.0
RSI_THRESHOLD    = 40.0
BB_PCT_THRESHOLD = 33.0

# Free tier: 1 concurrent request. Throttle between options chain calls.
# Increase if you get rate-limit errors; decrease if you have a paid tier.
OPTIONS_THROTTLE = 0.5      # seconds between options requests
STOCK_THROTTLE   = 0.1      # seconds between stock history requests

# ThetaData REST base URL (v2 EOD endpoint — works on free tier)
THETA_BASE       = "http://127.0.0.1:25510"

# Progress cache — lets us skip the slow stock scan on resume
CACHE_FILE       = Path("tech_candidates_cache.json")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Technical indicator helpers ───────────────────────────────────────────────

def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    """Return the most recent RSI value. Uses Wilder smoothing (matches TOS)."""
    delta    = closes.diff().dropna()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    rsi      = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def calc_bb_pct(closes: pd.Series, period: int = 20, std_mult: float = 2.0) -> float:
    """
    Return Bollinger %B for the most recent bar.
    %B = (price - lower_band) / (upper_band - lower_band) * 100
    """
    sma   = closes.rolling(period).mean()
    std   = closes.rolling(period).std(ddof=0)
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    price = closes.iloc[-1]
    u, l  = upper.iloc[-1], lower.iloc[-1]
    if (u - l) == 0:
        return 50.0
    return float((price - l) / (u - l) * 100)


# ── Column name resolver ──────────────────────────────────────────────────────

def find_close_col(df: pd.DataFrame) -> str | None:
    """Find the close price column regardless of how ThetaData names it."""
    for name in ("close", "CLOSE", "Close", "DataType.CLOSE"):
        if name in df.columns:
            return name
    # Fall back to last numeric column
    numeric = df.select_dtypes(include=[np.number]).columns.tolist()
    return numeric[-1] if numeric else None


# ── EOD options chain via REST ────────────────────────────────────────────────

def fetch_option_eod_chain(symbol: str, exp: date, trade_date: date) -> pd.DataFrame | None:
    """
    Pull EOD data for all puts on a given expiration using the v2 REST API.
    Returns a DataFrame with columns: strike, bid, ask, delta (may be absent on free tier).
    Returns None on error or no data.

    The v2 endpoint requires a specific strike, so we first list all strikes
    for the expiration, then fetch EOD for each. On the free tier this is slow
    but it works.
    """
    exp_str        = exp.strftime("%Y%m%d")
    trade_date_str = trade_date.strftime("%Y%m%d")

    # Step 1: list all strikes for this symbol/expiration/right
    try:
        r = requests.get(
            f"{THETA_BASE}/v2/list/strikes",
            params={"root": symbol, "exp": exp_str},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        strikes = data.get("response", [])
        if not strikes:
            return None
    except Exception:
        return None

    rows = []
    for strike_raw in strikes:
        try:
            r = requests.get(
                f"{THETA_BASE}/v2/hist/option/eod",
                params={
                    "root":       symbol,
                    "exp":        exp_str,
                    "strike":     strike_raw,
                    "right":      "P",
                    "start_date": trade_date_str,
                    "end_date":   trade_date_str,
                },
                timeout=10,
            )
            if r.status_code != 200:
                continue
            data = r.json()
            header   = data.get("header", {}).get("format", [])
            response = data.get("response", [])
            if not response or not header:
                continue

            row = dict(zip(header, response[0]))
            row["strike_raw"] = strike_raw
            rows.append(row)
        except Exception:
            continue
        time.sleep(0.05)   # small pause between per-strike requests

    if not rows:
        return None

    df = pd.DataFrame(rows)
    df.columns = [c.lower() for c in df.columns]
    return df


# ── Expiration list ───────────────────────────────────────────────────────────

def valid_expirations(today: date) -> list[date]:
    return [today + timedelta(days=d) for d in range(DTE_MIN, DTE_MAX + 1)]


# ── Main screener ─────────────────────────────────────────────────────────────

def run_screener():
    log.info("Connecting to ThetaData terminal …")
    try:
        client = ThetaClient(dataframe_type="pandas")
    except AuthenticationError:
        log.error("Authentication failed — check your ThetaData credentials.")
        sys.exit(1)

    today      = date.today()
    trade_date = today - timedelta(days=1)   # last completed trading day
    hist_start = today - timedelta(days=45)  # enough for BB(20) + RSI(14)

    # ── Step 1: get full symbol list ──────────────────────────────────────────
    log.info("Fetching symbol list …")
    try:
        symbols_df = client.stock_list_symbols()
    except Exception as e:
        log.error(f"Failed to fetch symbol list: {e}")
        sys.exit(1)

    col = "symbol" if "symbol" in symbols_df.columns else symbols_df.columns[0]
    all_symbols = symbols_df[col].tolist()
    log.info(f"Total symbols: {len(all_symbols)}")

    # ── Step 2: price + technical filter (with cache) ─────────────────────────

    # If we have a cache from today, skip the slow stock scan
    tech_candidates = []
    if CACHE_FILE.exists():
        try:
            cached = json.loads(CACHE_FILE.read_text())
            if cached.get("date") == today.isoformat():
                tech_candidates = cached["candidates"]
                log.info(f"Loaded {len(tech_candidates)} candidates from cache — skipping stock scan.")
        except Exception:
            pass

    if not tech_candidates:
        log.info("Running price / RSI / BB filter …")
        skipped = 0

        with tqdm(all_symbols, desc="Stock scan", unit="sym", ncols=90,
                  bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]  hits:{postfix}") as bar:
            bar.set_postfix_str("0")
            for sym in bar:
                try:
                    eod = client.stock_history_eod(
                        symbol=sym,
                        start_date=hist_start,
                        end_date=trade_date,
                    )
                except (NoDataFoundError, Exception):
                    skipped += 1
                    continue

                if eod is None or len(eod) < BB_PERIOD + 2:
                    skipped += 1
                    continue

                close_col = find_close_col(eod)
                if close_col is None:
                    skipped += 1
                    continue

                closes = eod[close_col].dropna().astype(float)
                if len(closes) < BB_PERIOD + 2:
                    skipped += 1
                    continue

                last_price = float(closes.iloc[-1])
                if not (STOCK_PRICE_MIN <= last_price <= STOCK_PRICE_MAX):
                    continue

                rsi = calc_rsi(closes, RSI_PERIOD)
                if rsi >= RSI_THRESHOLD:
                    continue

                bb_pct = calc_bb_pct(closes, BB_PERIOD, BB_STD_MULT)
                if bb_pct >= BB_PCT_THRESHOLD:
                    continue

                tech_candidates.append({
                    "symbol": sym,
                    "price":  last_price,
                    "rsi":    round(rsi, 1),
                    "bb_pct": round(bb_pct, 1),
                })
                bar.set_postfix_str(str(len(tech_candidates)))

                time.sleep(STOCK_THROTTLE)

        log.info(f"Technical filter done. Candidates: {len(tech_candidates)}  Skipped: {skipped}")

        # Save cache so options scan can resume if interrupted
        CACHE_FILE.write_text(json.dumps({"date": today.isoformat(), "candidates": tech_candidates}))
        log.info(f"Candidate list cached to {CACHE_FILE}")

    if not tech_candidates:
        log.info("No candidates passed the technical filter.")
        return

    # ── Step 3: options chain filter ─────────────────────────────────────────
    log.info(f"Pulling EOD options chains for {len(tech_candidates)} candidates …")
    log.info("Note: free tier rate limit means this will be slow. Leave it running.")

    target_expirations = valid_expirations(today)
    results = []

    with tqdm(tech_candidates, desc="Options scan", unit="ticker", ncols=90,
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]  hits:{postfix}") as bar:
        bar.set_postfix_str("0")
        for row in bar:
            sym         = row["symbol"]
            stock_price = row["price"]
            bar.set_description(f"Options scan [{sym:<6}]")

            for exp in target_expirations:
                chain = fetch_option_eod_chain(sym, exp, trade_date)
                if chain is None or chain.empty:
                    continue

                # Need at least bid to compute yield
                if "bid" not in chain.columns:
                    continue

                chain = chain[chain["bid"].notna()].copy()
                chain["bid"] = pd.to_numeric(chain["bid"], errors="coerce")
                chain = chain[chain["bid"] > 0].copy()

                if chain.empty:
                    continue

                chain["yield_pct"] = chain["bid"] / stock_price
                chain = chain[
                    (chain["yield_pct"] >= YIELD_MIN) &
                    (chain["yield_pct"] <= YIELD_MAX)
                ].copy()

                if chain.empty:
                    continue

                dte = (exp - today).days

                for _, opt in chain.iterrows():
                    # ThetaData strike encoding: $170.00 → 170000 (1/10th of a cent)
                    strike_raw = opt.get("strike_raw", None)
                    try:
                        strike = float(strike_raw) / 1000.0
                    except (TypeError, ValueError):
                        strike = None

                    delta = opt.get("delta", None)
                    if delta is not None:
                        try:
                            delta = round(float(delta), 3)
                        except (TypeError, ValueError):
                            delta = None

                    results.append({
                        "symbol":     sym,
                        "price":      round(stock_price, 2),
                        "rsi":        row["rsi"],
                        "bb_pct":     row["bb_pct"],
                        "expiration": exp.strftime("%Y-%m-%d"),
                        "dte":        dte,
                        "strike":     round(strike, 2) if strike else None,
                        "bid":        round(float(opt["bid"]), 2),
                        "yield_pct":  round(float(opt["yield_pct"]) * 100, 2),
                        "delta":      delta,
                    })

            bar.set_postfix_str(str(len(results)))
            time.sleep(OPTIONS_THROTTLE)

    # ── Step 4: display results ───────────────────────────────────────────────
    if not results:
        log.info("No options met all criteria.")
        return

    df = pd.DataFrame(results)
    df = df.sort_values(["expiration", "yield_pct"], ascending=[True, False])

    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 120)
    pd.set_option("display.float_format", "{:.2f}".format)

    print("\n" + "=" * 100)
    print(f"  PUT SELLING CANDIDATES   {today.strftime('%Y-%m-%d')}   "
          f"({len(df)} contracts  /  {df['symbol'].nunique()} tickers)")
    print("=" * 100)
    print(df.to_string(index=False))
    print("=" * 100)

    out_file = f"puts_{today.strftime('%Y%m%d')}.csv"
    df.to_csv(out_file, index=False)
    log.info(f"Results saved to {out_file}")


if __name__ == "__main__":
    run_screener()
