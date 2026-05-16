import time
import requests
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

VALID_EXCHANGES  = {"NYSE", "Nasdaq", "NYSE MKT"}
SEC_TICKERS_URL  = "https://www.sec.gov/files/company_tickers_exchange.json"


class WatchlistError(Exception):
    pass


def fetch_sec_tickers(on_log=None) -> pd.DataFrame:
    if on_log:
        on_log("Fetching SEC EDGAR company list …")
    try:
        r = requests.get(
            SEC_TICKERS_URL,
            headers={"User-Agent": "screener/1.0 contact@example.com"},
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        raise WatchlistError(f"Failed to fetch SEC data: {e}")

    data = r.json()

    if "fields" in data and "data" in data:
        df = pd.DataFrame(data["data"], columns=data["fields"])
    elif isinstance(data, dict):
        df = pd.DataFrame(list(data.values()))
    else:
        raise WatchlistError(f"Unrecognised SEC response format: {type(data)}")

    if "ticker" not in df.columns or "exchange" not in df.columns:
        raise WatchlistError(f"Unexpected SEC columns: {df.columns.tolist()}")

    if on_log:
        on_log(f"SEC total records: {len(df)}")

    df = df[df["exchange"].isin(VALID_EXCHANGES)].copy()
    if on_log:
        on_log(f"After exchange filter (NYSE/Nasdaq): {len(df)}")

    df["ticker"] = df["ticker"].str.upper().str.strip()

    mask = (
        ~df["ticker"].str.endswith("W") &
        ~df["ticker"].str.endswith("R") &
        ~df["ticker"].str.endswith("U") &
        ~df["ticker"].str.contains(r"[\^~\+]", regex=True)
    )
    df = df[mask].copy()
    if on_log:
        on_log(f"After warrant/rights/preferred filter: {len(df)}")

    df = df.drop_duplicates(subset="ticker").reset_index(drop=True)
    if on_log:
        on_log(f"After dedup: {len(df)}")

    return df


def fetch_theta_symbols(client, on_log=None) -> set:
    if on_log:
        on_log("Fetching ThetaData symbol list …")
    try:
        df = client.stock_list_symbols()
    except Exception as e:
        raise WatchlistError(f"Failed to fetch ThetaData symbols: {e}")
    col = "symbol" if "symbol" in df.columns else df.columns[0]
    return set(df[col].str.upper().str.strip().tolist())


def get_last_close(client, symbol: str, trade_date: date) -> float | None:
    from thetadata.errors import NoDataFoundError
    try:
        eod = client.stock_history_eod(
            symbol=symbol,
            start_date=trade_date - timedelta(days=5),
            end_date=trade_date,
        )
        if eod is None or eod.empty:
            return None
        for col in ("close", "CLOSE", "Close"):
            if col in eod.columns:
                closes = eod[col].dropna()
                if not closes.empty:
                    return float(closes.iloc[-1])
        numeric = eod.select_dtypes(include=[np.number]).columns.tolist()
        if numeric:
            vals = eod[numeric[-1]].dropna()
            if not vals.empty:
                return float(vals.iloc[-1])
    except (NoDataFoundError, Exception):
        pass
    return None


def build_watchlist(
    price_min: float,
    price_max: float,
    output_path: str | Path,
    throttle: float = 0.1,
    on_log=None,
    on_progress=None,
    stop_flag=None,
) -> list[str]:
    from thetadata import ThetaClient
    from thetadata.errors import AuthenticationError

    if on_log:
        on_log("Connecting to ThetaData terminal …")
    try:
        client = ThetaClient(dataframe_type="pandas")
    except AuthenticationError:
        raise WatchlistError("Authentication failed — check your ThetaData credentials.")

    trade_date = date.today() - timedelta(days=1)

    sec_df = fetch_sec_tickers(on_log=on_log)

    theta_symbols = fetch_theta_symbols(client, on_log=on_log)
    sec_df = sec_df[sec_df["ticker"].isin(theta_symbols)].copy()
    if on_log:
        on_log(f"After ThetaData cross-reference: {len(sec_df)} tickers")

    candidates = sec_df["ticker"].tolist()
    total = len(candidates)
    if on_log:
        on_log(f"Fetching prices for {total} tickers …")

    watchlist, skipped = [], 0

    for i, sym in enumerate(candidates):
        if stop_flag and stop_flag():
            if on_log:
                on_log("Stopped by user.")
            break

        price = get_last_close(client, sym, trade_date)
        if price is None:
            skipped += 1
        elif price_min <= price <= price_max:
            watchlist.append(sym)

        if on_progress:
            on_progress(i + 1, total)

        time.sleep(throttle)

    if on_log:
        on_log(f"Price filter done. Kept: {len(watchlist)}  Skipped/no data: {skipped}")

    watchlist.sort()
    Path(output_path).write_text("\n".join(watchlist) + "\n")
    if on_log:
        on_log(f"Watchlist written: {len(watchlist)} tickers → {output_path}")

    return watchlist
