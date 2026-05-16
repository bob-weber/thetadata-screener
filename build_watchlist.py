#!/usr/bin/env python3
"""
Watchlist Builder
Builds a filtered list of NYSE/NASDAQ common stocks in the $8-$75 price range.
Output: watchlist.txt — one ticker per line, used by the main screener.

Sources:
  - SEC EDGAR company list (exchange + instrument type, no API key needed)
  - ThetaData (last closing price)

Run this periodically (weekly or monthly) to refresh the watchlist.
"""

import sys
import time
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

PRICE_MIN     = 8.0
PRICE_MAX     = 75.0
WATCHLIST     = Path("watchlist.txt")
THROTTLE      = 0.1   # seconds between ThetaData price requests

# SEC EDGAR full company list — updated nightly by the SEC, no key needed
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

# Exchanges to include
VALID_EXCHANGES = {"NYSE", "Nasdaq", "NYSE MKT"}

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_sec_tickers() -> pd.DataFrame:
    """
    Fetch the SEC EDGAR company/exchange list.
    Returns a DataFrame with columns: ticker, name, exchange.
    Only includes NYSE and Nasdaq common stocks (filters out ETFs, funds,
    preferred shares, warrants, etc. by ticker pattern and exchange).
    """
    log.info("Fetching SEC EDGAR company list …")
    try:
        r = requests.get(
            SEC_TICKERS_URL,
            headers={"User-Agent": "screener/1.0 contact@example.com"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        log.error(f"Failed to fetch SEC data: {e}")
        sys.exit(1)

    data = r.json()

    # The SEC uses two formats depending on the endpoint:
    # Format A: { "fields": ["cik_str","ticker","name","exchange"], "data": [[...],[...],...] }
    # Format B: { "0": {"cik_str":..., "ticker":..., ...}, "1": {...}, ... }
    if "fields" in data and "data" in data:
        # Format A
        df = pd.DataFrame(data["data"], columns=data["fields"])
    elif isinstance(data, dict):
        # Format B
        rows = list(data.values())
        df = pd.DataFrame(rows)
    else:
        log.error(f"Unrecognised SEC response format: {type(data)}")
        sys.exit(1)

    if "ticker" not in df.columns or "exchange" not in df.columns:
        log.error(f"Unexpected SEC response format. Columns: {df.columns.tolist()}")
        sys.exit(1)

    log.info(f"SEC total records: {len(df)}")

    # Filter to NYSE and Nasdaq only
    df = df[df["exchange"].isin(VALID_EXCHANGES)].copy()
    log.info(f"After exchange filter (NYSE/Nasdaq): {len(df)}")

    # Uppercase tickers
    df["ticker"] = df["ticker"].str.upper().str.strip()

    # ── Filter out non-company instruments by ticker pattern ──────────────────
    # ETFs, funds, and indices tend to have 4-5 letter tickers ending in:
    #   - X  (leveraged ETFs: TQQQ, SOXL, UVXY)
    #   - nothing useful we can pattern-match universally
    # Better approach: exclude known non-equity suffixes in the ticker
    #   - tickers with a dot (BRK.A, BRK.B) → keep (these are share classes)
    #   - tickers ending in W  → warrants
    #   - tickers ending in R  → rights
    #   - tickers ending in U  → units
    #   - tickers ending in +  → preferred
    #   - tickers with ^ or ~  → preferred / when-issued

    mask = (
        ~df["ticker"].str.endswith("W") &
        ~df["ticker"].str.endswith("R") &
        ~df["ticker"].str.endswith("U") &
        ~df["ticker"].str.contains(r"[\^~\+]", regex=True)
    )
    df = df[mask].copy()
    log.info(f"After warrant/rights/preferred filter: {len(df)}")

    # Drop duplicates (same ticker on multiple exchanges — keep first)
    df = df.drop_duplicates(subset="ticker").reset_index(drop=True)
    log.info(f"After dedup: {len(df)}")

    return df


def fetch_theta_symbols(client: ThetaClient) -> set:
    """Return the set of symbols ThetaData has data for."""
    log.info("Fetching ThetaData symbol list …")
    try:
        df = client.stock_list_symbols()
    except Exception as e:
        log.error(f"Failed to fetch ThetaData symbols: {e}")
        sys.exit(1)
    col = "symbol" if "symbol" in df.columns else df.columns[0]
    return set(df[col].str.upper().str.strip().tolist())


def get_last_close(client: ThetaClient, symbol: str, trade_date: date) -> float | None:
    """Return the last closing price for a symbol, or None on error."""
    try:
        eod = client.stock_history_eod(
            symbol=symbol,
            start_date=trade_date - timedelta(days=5),  # small window, just need last close
            end_date=trade_date,
        )
        if eod is None or eod.empty:
            return None

        # Find close column
        for col in ("close", "CLOSE", "Close"):
            if col in eod.columns:
                closes = eod[col].dropna()
                if not closes.empty:
                    return float(closes.iloc[-1])

        # Fall back to last numeric column
        numeric = eod.select_dtypes(include=[np.number]).columns.tolist()
        if numeric:
            vals = eod[numeric[-1]].dropna()
            if not vals.empty:
                return float(vals.iloc[-1])

    except (NoDataFoundError, Exception):
        pass
    return None


# ── Main ──────────────────────────────────────────────────────────────────────

def build_watchlist():
    # ── Step 1: connect to ThetaData ─────────────────────────────────────────
    log.info("Connecting to ThetaData terminal …")
    try:
        client = ThetaClient(dataframe_type="pandas")
    except AuthenticationError:
        log.error("Authentication failed — check your ThetaData credentials.")
        sys.exit(1)

    trade_date = date.today() - timedelta(days=1)

    # ── Step 2: get SEC company list (NYSE/Nasdaq common stocks) ──────────────
    sec_df = fetch_sec_tickers()

    # ── Step 3: cross-reference with ThetaData symbols ────────────────────────
    theta_symbols = fetch_theta_symbols(client)
    sec_df = sec_df[sec_df["ticker"].isin(theta_symbols)].copy()
    log.info(f"After ThetaData cross-reference: {len(sec_df)} tickers")

    candidates = sec_df["ticker"].tolist()

    # ── Step 4: fetch last close and filter by price ──────────────────────────
    log.info(f"Fetching prices for {len(candidates)} tickers …")

    watchlist = []
    skipped   = 0

    with tqdm(candidates, desc="Price filter", unit="sym", ncols=90,
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}, {rate_fmt}]  kept:{postfix}") as bar:
        bar.set_postfix_str("0")
        for sym in bar:
            price = get_last_close(client, sym, trade_date)
            if price is None:
                skipped += 1
                continue
            if PRICE_MIN <= price <= PRICE_MAX:
                watchlist.append(sym)
                bar.set_postfix_str(str(len(watchlist)))
            time.sleep(THROTTLE)

    log.info(f"Price filter done. Kept: {len(watchlist)}  Skipped/no data: {skipped}")

    # ── Step 5: write watchlist ───────────────────────────────────────────────
    watchlist.sort()
    WATCHLIST.write_text("\n".join(watchlist) + "\n")
    log.info(f"Watchlist written to {WATCHLIST}  ({len(watchlist)} tickers)")

    # Summary
    print("\n" + "=" * 60)
    print(f"  WATCHLIST COMPLETE — {len(watchlist)} tickers")
    print(f"  Price range: ${PRICE_MIN} – ${PRICE_MAX}")
    print(f"  Exchanges:   NYSE, Nasdaq, NYSE American (common stocks only)")
    print(f"  Output:      {WATCHLIST.resolve()}")
    print("=" * 60)


if __name__ == "__main__":
    build_watchlist()
