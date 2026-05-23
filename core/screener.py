import io
import re
import time
import json
import requests
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

THETA_BASE = "http://127.0.0.1:25503"


def _last_trading_day(ref: date) -> date:
    """Return the most recent weekday strictly before ref."""
    d = ref - timedelta(days=1)
    while d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        d -= timedelta(days=1)
    return d

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
VALID_EXCHANGES  = {"NYSE", "Nasdaq", "NYSE MKT"}

_FUND_RE = re.compile(
    r"\betf\b"
    r"|\bfunds?\b"
    r"|\bspdr\b"
    r"|\bishares\b"
    r"|\bproshares\b"
    r"|\bdirexion\b"
    r"|\bwisdomtree\b"
    r"|exchange.traded",
    re.IGNORECASE,
)


def _is_fund(name: str) -> bool:
    return bool(_FUND_RE.search(name))


class ScreenerError(Exception):
    pass


def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    delta    = closes.diff().dropna()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    rsi      = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])


def calc_bb_pct(closes: pd.Series, period: int = 20, std_mult: float = 2.0) -> float:
    sma   = closes.rolling(period).mean()
    std   = closes.rolling(period).std(ddof=0)
    upper = sma + std_mult * std
    lower = sma - std_mult * std
    price = closes.iloc[-1]
    u, l  = upper.iloc[-1], lower.iloc[-1]
    if (u - l) == 0:
        return 50.0
    return float((price - l) / (u - l) * 100)


def find_close_col(df: pd.DataFrame) -> str | None:
    for name in ("close", "CLOSE", "Close", "DataType.CLOSE"):
        if name in df.columns:
            return name
    numeric = df.select_dtypes(include=[np.number]).columns.tolist()
    return numeric[-1] if numeric else None


def fetch_stock_prices(
    symbols: list[str],
    on_log=None,
    on_progress=None,
    stop_flag=None,
    throttle: float = 0.1,
) -> list[dict]:
    """Fetch the latest EOD close price for each symbol. Returns [{symbol, price}, ...]."""
    from thetadata import ThetaClient
    from thetadata.errors import AuthenticationError

    if on_log:
        on_log("Connecting to ThetaData terminal …")
    try:
        client = ThetaClient(dataframe_type="pandas")
    except AuthenticationError:
        raise ScreenerError("Authentication failed — check your ThetaData credentials.")

    today      = date.today()
    trade_date = _last_trading_day(today)
    results    = []
    total      = len(symbols)

    for i, sym in enumerate(symbols):
        if stop_flag and stop_flag():
            if on_log:
                on_log("Stopped by user.")
            break
        try:
            eod = client.stock_history_eod(symbol=sym, start_date=trade_date, end_date=trade_date)
            if eod is not None and not eod.empty:
                close_col = find_close_col(eod)
                if close_col:
                    closes = eod[close_col].dropna().astype(float)
                    if not closes.empty:
                        results.append({"symbol": sym, "price": float(closes.iloc[-1])})
                        if on_log:
                            on_log(f"  {sym}: ${closes.iloc[-1]:.2f}")
        except Exception:
            if on_log:
                on_log(f"  {sym}: no data")
        if on_progress:
            on_progress(i + 1, total)
        time.sleep(throttle)

    return results


def fetch_option_eod_chain(symbol: str, exp: date, trade_date: date, right: str = "P") -> pd.DataFrame | None:
    exp_str        = exp.strftime("%Y%m%d")
    trade_date_str = trade_date.strftime("%Y%m%d")
    right_str      = "put" if right == "P" else "call"

    try:
        r = requests.get(
            f"{THETA_BASE}/v3/option/history/eod",
            params={
                "symbol":     symbol,
                "expiration": exp_str,
                "strike":     "*",
                "right":      right_str,
                "start_date": trade_date_str,
                "end_date":   trade_date_str,
            },
            timeout=15,
        )
        if r.status_code != 200 or not r.text.strip():
            return None
        text = r.text.strip()
        if text.startswith("No data"):
            return None
        df = pd.read_csv(io.StringIO(text))
        df.columns = [c.strip().lower() for c in df.columns]
        return df if not df.empty else None
    except Exception:
        return None


def _get_company_symbols(client, on_log=None) -> list[str]:
    """Return NYSE/Nasdaq company tickers, excluding ETFs and funds."""
    sec_symbols: set[str] = set()
    try:
        if on_log:
            on_log("Fetching SEC EDGAR company list …")
        r = requests.get(
            SEC_TICKERS_URL,
            headers={"User-Agent": "screener/1.0 contact@example.com"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if "fields" in data and "data" in data:
            df = pd.DataFrame(data["data"], columns=data["fields"])
        else:
            df = pd.DataFrame(list(data.values()))

        df = df[df["exchange"].isin(VALID_EXCHANGES)].copy()
        df["ticker"] = df["ticker"].str.upper().str.strip()
        df = df[
            ~df["ticker"].str.endswith("W") &
            ~df["ticker"].str.endswith("R") &
            ~df["ticker"].str.endswith("U") &
            ~df["ticker"].str.contains(r"[\^~\+]", regex=True)
        ]
        df = df[~df["name"].apply(_is_fund)]
        df = df.drop_duplicates(subset="ticker")
        sec_symbols = set(df["ticker"].tolist())
        if on_log:
            on_log(f"SEC company universe: {len(sec_symbols)} tickers after ETF/fund filter")
    except Exception as e:
        if on_log:
            on_log(f"SEC EDGAR unavailable ({e}) — falling back to full ThetaData list")

    try:
        theta_df = client.stock_list_symbols()
        col = "symbol" if "symbol" in theta_df.columns else theta_df.columns[0]
        theta_symbols = set(theta_df[col].str.upper().str.strip().tolist())
    except Exception as e:
        raise ScreenerError(f"Failed to fetch ThetaData symbol list: {e}")

    if sec_symbols:
        combined = sorted(sec_symbols & theta_symbols)
        if on_log:
            on_log(f"After ThetaData cross-reference: {len(combined)} symbols")
        return combined

    return sorted(theta_symbols)


def run_stock_filter(
    config: dict,
    on_log=None,
    on_pass1_progress=None,
    on_pass2_progress=None,
    stop_flag=None,
    price_cache_file: str | Path   = "price_screen_cache.json",
    history_cache_file: str | Path = "tech_history_cache.json",
    candidates_cache_file: str | Path = "tech_candidates_cache.json",
    watchlist_file: str | Path | None = None,
    skip_candidates_cache: bool = False,
) -> list[dict]:
    from thetadata import ThetaClient
    from thetadata.errors import AuthenticationError

    price_min        = config.get("price_min",        10.0)
    price_max        = config.get("price_max",        75.0)
    rsi_period       = config.get("rsi_period",       14)
    bb_period        = config.get("bb_period",        20)
    bb_std_mult      = config.get("bb_std_mult",       2.0)
    rsi_threshold    = config.get("rsi_threshold",    40.0)
    bb_pct_threshold = config.get("bb_pct_threshold", 33.0)
    stock_throttle   = config.get("stock_throttle",    0.1)

    today      = date.today()
    trade_date = _last_trading_day(today)
    hist_start = today - timedelta(days=45)

    price_key = {
        "date":      today.isoformat(),
        "price_min": price_min,
        "price_max": price_max,
    }
    full_key = {
        **price_key,
        "rsi_threshold":    rsi_threshold,
        "bb_pct_threshold": bb_pct_threshold,
        "rsi_period":       rsi_period,
        "bb_period":        bb_period,
    }

    # ── Candidates cache (all params match → return immediately) ──────────────
    cand_path = Path(candidates_cache_file)
    if not skip_candidates_cache and cand_path.exists():
        try:
            cached = json.loads(cand_path.read_text())
            if all(cached.get(k) == v for k, v in full_key.items()):
                if on_log:
                    on_log(f"Loaded {len(cached['candidates'])} candidates from cache.")
                return cached["candidates"]
        except Exception:
            pass

    if on_log:
        on_log("Connecting to ThetaData terminal …")
    try:
        client = ThetaClient(dataframe_type="pandas")
    except AuthenticationError:
        raise ScreenerError("Authentication failed — check your ThetaData credentials.")

    # ── Symbol universe ───────────────────────────────────────────────────────
    if watchlist_file and Path(watchlist_file).exists():
        all_symbols = [t.strip() for t in Path(watchlist_file).read_text().splitlines() if t.strip()]
        if on_log:
            on_log(f"Using watchlist: {len(all_symbols)} symbols")
    else:
        all_symbols = _get_company_symbols(client, on_log)

    # ── Pass 1: price screen (last price only) ────────────────────────────────
    price_path = Path(price_cache_file)
    price_qualified: list[dict] | None = None
    if price_path.exists():
        try:
            cached = json.loads(price_path.read_text())
            if all(cached.get(k) == v for k, v in price_key.items()):
                price_qualified = cached["qualified"]
                if on_log:
                    on_log(f"Price screen cache hit — {len(price_qualified)} symbols in ${price_min}–${price_max}")
        except Exception:
            pass

    if price_qualified is None:
        if on_log:
            on_log(f"Pass 1: price screen — {len(all_symbols)} symbols …")
        price_qualified = []
        total   = len(all_symbols)
        skipped = 0

        for i, sym in enumerate(all_symbols):
            if stop_flag and stop_flag():
                if on_log:
                    on_log("Stopped by user.")
                return []

            try:
                eod = client.stock_history_eod(
                    symbol=sym, start_date=trade_date, end_date=trade_date
                )
            except Exception:
                skipped += 1
                if on_pass1_progress:
                    on_pass1_progress(i + 1, total)
                continue

            if eod is None or eod.empty:
                skipped += 1
                if on_pass1_progress:
                    on_pass1_progress(i + 1, total)
                continue

            close_col = find_close_col(eod)
            if close_col is None:
                skipped += 1
                if on_pass1_progress:
                    on_pass1_progress(i + 1, total)
                continue

            closes = eod[close_col].dropna().astype(float)
            if closes.empty:
                skipped += 1
                if on_pass1_progress:
                    on_pass1_progress(i + 1, total)
                continue

            last_price = float(closes.iloc[-1])
            if price_min <= last_price <= price_max:
                price_qualified.append({"symbol": sym, "price": last_price})

            if on_pass1_progress:
                on_pass1_progress(i + 1, total)

            time.sleep(stock_throttle)

        if on_log:
            on_log(f"Pass 1 done — {len(price_qualified)} in ${price_min}–${price_max}, {skipped} skipped")
        price_path.write_text(json.dumps({**price_key, "qualified": price_qualified}))

    if not price_qualified:
        if on_log:
            on_log("No symbols passed the price filter.")
        return []

    # ── Pass 2: fetch 45-day history for price qualifiers ─────────────────────
    hist_path = Path(history_cache_file)
    histories: dict[str, list] | None = None
    if hist_path.exists():
        try:
            cached = json.loads(hist_path.read_text())
            if all(cached.get(k) == v for k, v in price_key.items()):
                histories = cached["histories"]
                if on_log:
                    on_log(f"History cache hit — {len(histories)} close series loaded, computing indicators …")
        except Exception:
            pass

    if histories is None:
        if on_log:
            on_log(f"Pass 2: fetching 45-day history for {len(price_qualified)} symbols …")
        histories = {}
        total   = len(price_qualified)
        skipped = 0

        for i, item in enumerate(price_qualified):
            sym = item["symbol"]
            if stop_flag and stop_flag():
                if on_log:
                    on_log("Stopped by user.")
                return []

            try:
                eod = client.stock_history_eod(
                    symbol=sym, start_date=hist_start, end_date=trade_date
                )
            except Exception:
                skipped += 1
                if on_pass2_progress:
                    on_pass2_progress(i + 1, total)
                continue

            if eod is None or len(eod) < bb_period + 2:
                skipped += 1
                if on_pass2_progress:
                    on_pass2_progress(i + 1, total)
                continue

            close_col = find_close_col(eod)
            if close_col is None:
                skipped += 1
                if on_pass2_progress:
                    on_pass2_progress(i + 1, total)
                continue

            closes = eod[close_col].dropna().astype(float)
            if len(closes) < bb_period + 2:
                skipped += 1
                if on_pass2_progress:
                    on_pass2_progress(i + 1, total)
                continue

            histories[sym] = closes.tolist()

            if on_pass2_progress:
                on_pass2_progress(i + 1, total)

            time.sleep(stock_throttle)

        if on_log:
            on_log(f"Pass 2 done — history for {len(histories)} symbols, {skipped} skipped")
        hist_path.write_text(json.dumps({**price_key, "histories": histories}))

    # ── Apply RSI / BB% filter in memory (instant from cache) ─────────────────
    price_lookup = {item["symbol"]: item["price"] for item in price_qualified}
    tech_candidates: list[dict] = []

    for sym, closes_list in histories.items():
        closes = pd.Series(closes_list, dtype=float)
        if len(closes) < bb_period + 2:
            continue
        rsi = calc_rsi(closes, rsi_period)
        if rsi >= rsi_threshold:
            continue
        bb_pct = calc_bb_pct(closes, bb_period, bb_std_mult)
        if bb_pct >= bb_pct_threshold:
            continue
        tech_candidates.append({
            "symbol": sym,
            "price":  round(price_lookup.get(sym, float(closes.iloc[-1])), 2),
            "rsi":    round(rsi, 1),
            "bb_pct": round(bb_pct, 1),
        })

    if on_log:
        on_log(f"Technical filter done — {len(tech_candidates)} candidates.")

    from datetime import datetime
    cand_path.write_text(json.dumps({
        **full_key,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "candidates": tech_candidates,
    }))
    return tech_candidates


def run_options_filter(
    candidates: list[dict],
    config: dict,
    on_log=None,
    on_progress=None,
    stop_flag=None,
) -> list[dict]:
    yield_min        = config.get("yield_min",        0.009)
    yield_max        = config.get("yield_max",        0.011)
    options_throttle = config.get("options_throttle", 0.5)
    right            = config.get("right",            "P")    # "P" or "C"
    side             = config.get("side",             "sell") # "sell" or "buy"
    price_col        = "bid" if side == "sell" else "ask"

    today      = date.today()
    trade_date = _last_trading_day(today)

    exp_date_str = config.get("expiration_date")
    if exp_date_str:
        expirations = [date.fromisoformat(exp_date_str)]
    else:
        dte_min     = config.get("dte_min",  4)
        dte_max     = config.get("dte_max", 21)
        expirations = [today + timedelta(days=d) for d in range(dte_min, dte_max + 1)]
    results = []
    total   = len(candidates)

    # Fail fast if the terminal isn't reachable
    try:
        requests.get(f"{THETA_BASE}/v2/list/roots", params={"sec": "stock"}, timeout=3)
    except Exception:
        raise ScreenerError(
            f"Cannot reach ThetaData terminal at {THETA_BASE}. "
            "Start ThetaTerminalv3.jar (run start_terminal.sh) and try again."
        )

    if on_log:
        label = f"{'Put' if right == 'P' else 'Call'} {'sells' if side == 'sell' else 'buys'}"
        on_log(f"Scanning {label} chains for {total} candidates …")

    for i, row in enumerate(candidates):
        if stop_flag and stop_flag():
            if on_log:
                on_log("Stopped by user.")
            break

        sym         = row["symbol"]
        stock_price = row["price"]
        sym_hits    = 0

        for exp in expirations:
            chain = fetch_option_eod_chain(sym, exp, trade_date, right=right)
            if chain is None or chain.empty or price_col not in chain.columns:
                if on_log:
                    on_log(f"  {sym}: no chain data for {exp}")
                continue

            chain[price_col] = pd.to_numeric(chain[price_col], errors="coerce")
            chain = chain[chain[price_col] > 0].dropna(subset=[price_col]).copy()
            if chain.empty:
                if on_log:
                    on_log(f"  {sym}: chain found but no positive {price_col}")
                continue

            chain["yield_pct"] = chain[price_col] / stock_price
            in_range = chain[
                (chain["yield_pct"] >= yield_min) &
                (chain["yield_pct"] <= yield_max)
            ]
            if on_log:
                on_log(
                    f"  {sym}: {len(chain)} strikes, "
                    f"{len(in_range)} in yield range "
                    f"({yield_min*100:.1f}%–{yield_max*100:.1f}%)"
                )
            chain = in_range.copy()
            if chain.empty:
                continue
            sym_hits += len(chain)

            dte = (exp - today).days

            for _, opt in chain.iterrows():
                try:
                    strike = round(float(opt["strike"]), 2)
                except (TypeError, ValueError, KeyError):
                    strike = None

                delta = opt.get("delta")
                try:
                    delta = round(float(delta), 3) if delta is not None else None
                except (TypeError, ValueError):
                    delta = None

                try:
                    otm_pct = round((stock_price - float(opt["strike"])) / stock_price * 100, 2)
                except (TypeError, ValueError):
                    otm_pct = None

                results.append({
                    "symbol":     sym,
                    "expiration": exp.strftime("%Y-%m-%d"),
                    "dte":        dte,
                    "strike":     strike,
                    "otm_pct":    otm_pct,
                    "premium":    round(float(opt[price_col]), 2),
                    "yield_pct":  round(float(opt["yield_pct"]) * 100, 2),
                    "delta":      delta,
                })

        if on_progress:
            on_progress(i + 1, total)

        time.sleep(options_throttle)

    return results


def run_screener(
    config: dict,
    on_log=None,
    on_stock_progress=None,
    on_options_progress=None,
    stop_flag=None,
    watchlist_file: str | Path | None = None,
) -> list[dict]:
    candidates = run_stock_filter(
        config,
        on_log=on_log,
        on_pass1_progress=on_stock_progress,
        on_pass2_progress=on_stock_progress,
        stop_flag=stop_flag,
        watchlist_file=watchlist_file,
    )
    if not candidates:
        return []
    return run_options_filter(candidates, config, on_log, on_options_progress, stop_flag)
