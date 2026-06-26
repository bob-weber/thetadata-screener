import re
import time
import json
import requests
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

THETA_BASE = "http://127.0.0.1:25503"


def _us_market_holidays(year: int) -> set[date]:
    """Return NYSE/Nasdaq market holidays for the given year."""
    def nth_weekday(y, m, wd, n):
        first = date(y, m, 1)
        delta = (wd - first.weekday()) % 7
        return first + timedelta(days=delta + 7 * (n - 1))

    def last_monday(y, m):
        last = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
        return last - timedelta(days=(last.weekday()) % 7)

    def observed(d):
        if d.weekday() == 5: return d - timedelta(days=1)
        if d.weekday() == 6: return d + timedelta(days=1)
        return d

    # Easter (Anonymous Gregorian algorithm) → Good Friday
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day   = (h + ll - 7 * m + 114) % 31 + 1
    good_friday = date(year, month, day) - timedelta(days=2)

    return {
        observed(date(year, 1, 1)),          # New Year's Day
        nth_weekday(year, 1, 0, 3),          # MLK Day        (3rd Mon Jan)
        nth_weekday(year, 2, 0, 3),          # Presidents Day (3rd Mon Feb)
        good_friday,                          # Good Friday
        last_monday(year, 5),                 # Memorial Day   (last Mon May)
        observed(date(year, 6, 19)),          # Juneteenth
        observed(date(year, 7, 4)),           # Independence Day
        nth_weekday(year, 9, 0, 1),          # Labor Day      (1st Mon Sep)
        nth_weekday(year, 11, 3, 4),         # Thanksgiving   (4th Thu Nov)
        observed(date(year, 12, 25)),         # Christmas
    }


def _last_trading_day(ref: date) -> date:
    """Return ref if it is a trading day, otherwise the most recent trading day before ref."""
    holidays = _us_market_holidays(ref.year) | _us_market_holidays(ref.year - 1)
    d = ref
    while d.weekday() >= 5 or d in holidays:
        d -= timedelta(days=1)
    return d


def _prev_trading_day() -> date:
    """Return the most recent trading day strictly before today."""
    today    = date.today()
    holidays = _us_market_holidays(today.year) | _us_market_holidays(today.year - 1)
    d = today - timedelta(days=1)
    while d.weekday() >= 5 or d in holidays:
        d -= timedelta(days=1)
    return d


def _current_trade_date() -> date:
    """Return today if the market has closed (4:00 PM ET), otherwise the previous trading day."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ET    = ZoneInfo("America/New_York")
    today = date.today()
    if _last_trading_day(today) == today:
        close_et = datetime(today.year, today.month, today.day, 16, 0, tzinfo=ET)
        if datetime.now(ET) >= close_et:
            return today
    return _prev_trading_day()

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


def _current_price(eod: pd.DataFrame) -> float | None:
    """Return bid/ask midpoint when both are positive, otherwise the close price."""
    try:
        bid = float(eod["bid"].iloc[-1])
        ask = float(eod["ask"].iloc[-1])
        if bid > 0 and ask > 0:
            return (bid + ask) / 2
    except (KeyError, IndexError, ValueError):
        pass
    close_col = find_close_col(eod)
    if close_col:
        closes = eod[close_col].dropna().astype(float)
        if not closes.empty:
            return float(closes.iloc[-1])
    return None


def fetch_stock_prices(
    symbols: list[str],
    on_log=None,
    on_progress=None,
    stop_flag=None,
    throttle: float = 0.1,
) -> list[dict]:
    """Fetch the latest price for each symbol via yfinance. Returns [{symbol, price}, ...]."""
    import yfinance as yf

    results = []
    total   = len(symbols)

    for i, sym in enumerate(symbols):
        if stop_flag and stop_flag():
            if on_log:
                on_log("Stopped by user.")
            break
        try:
            price = yf.Ticker(sym).fast_info.last_price
            if price and price > 0:
                results.append({"symbol": sym, "price": price})
                if on_log:
                    on_log(f"  {sym}: ${price:.2f}")
            else:
                if on_log:
                    on_log(f"  {sym}: no data")
        except Exception:
            if on_log:
                on_log(f"  {sym}: no data")
        if on_progress:
            on_progress(i + 1, total)

    return results


def _fetch_schwab_chain(client, symbol: str, right: str, to_date: date) -> dict:
    """Real-time option chain from Schwab as {expiration_date: DataFrame}.

    A single API call per symbol covers every expiration through ``to_date``.
    Each frame carries strike/bid/ask/delta columns. Returns {} on any error or
    empty chain so the caller can simply skip the symbol. (We never pass
    from_date — Schwab 400s when it equals today; the near end is filtered by
    the caller from the returned expirations.)
    """
    from core import schwab_client
    ct = (client.Options.ContractType.PUT if right == "P"
          else client.Options.ContractType.CALL)
    try:
        data = schwab_client.option_chain(client, symbol, contract_type=ct, to_date=to_date)
    except Exception:
        return {}
    if data.get("status") != "SUCCESS":
        return {}

    exp_map = data.get("putExpDateMap" if right == "P" else "callExpDateMap", {})
    chains: dict[date, pd.DataFrame] = {}
    for exp_key, strikes in exp_map.items():
        # keys look like "2026-06-26:1" (date:days-to-expiration)
        try:
            exp_d = date.fromisoformat(exp_key.split(":")[0])
        except ValueError:
            continue
        rows = [
            {"strike": c.get("strikePrice"), "bid": c.get("bid"),
             "ask": c.get("ask"), "delta": c.get("delta")}
            for contracts in strikes.values() for c in contracts
        ]
        if rows:
            chains[exp_d] = pd.DataFrame(rows)
    return chains


def snap_expiration(requested: date, available: list[date]) -> date | None:
    """Snap a requested expiration to the closest listed one on or before it.

    Options expire only on dates the exchange lists; when the requested date is a
    weekend or holiday (e.g. Juneteenth 6/19) it won't appear, so we fall back to
    the nearest earlier listed expiration. Returns None if none qualifies.
    """
    on_or_before = [e for e in available if e <= requested]
    return max(on_or_before) if on_or_before else None


def _has_weeklies(expirations: list[date], ref: date, horizon_days: int = 70) -> bool:
    """True if the symbol offers weekly options.

    Monthly-only names list expirations roughly a month apart (3rd Fridays);
    weeklies add an expiration nearly every Friday. We flag weeklies when two
    listed expirations within the horizon fall 10 or fewer days apart.
    """
    near = sorted(e for e in expirations if ref <= e <= ref + timedelta(days=horizon_days))
    if len(near) < 2:
        return False
    return any((b - a).days <= 10 for a, b in zip(near, near[1:]))


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
        # Single-char suffixes W/R denote warrants/rights at any length.
        # U denotes SPAC units only when appended to a 4-char base (5+ chars total);
        # shorter tickers like LULU/ROKU are legitimate standalone symbols.
        df = df[
            ~df["ticker"].str.endswith("W") &
            ~df["ticker"].str.endswith("R") &
            ~(df["ticker"].str.endswith("U") & (df["ticker"].str.len() >= 5)) &
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


def _connect_client(on_log=None):
    from thetadata import ThetaClient
    from thetadata.errors import AuthenticationError

    if on_log:
        on_log("Connecting to ThetaData terminal …")
    try:
        return ThetaClient(dataframe_type="pandas")
    except AuthenticationError:
        raise ScreenerError("Authentication failed — check your ThetaData credentials.")


def _price_key(config: dict) -> dict:
    return {
        "date":      date.today().isoformat(),
        "price_min": config.get("price_min", 10.0),
        "price_max": config.get("price_max", 500.0),
    }


def _full_key(config: dict) -> dict:
    return {
        **_price_key(config),
        "rsi_threshold":    config.get("rsi_threshold",    40.0),
        "bb_pct_threshold": config.get("bb_pct_threshold", 33.0),
        "rsi_period":       config.get("rsi_period",       14),
        "bb_period":        config.get("bb_period",        20),
    }


def run_price_screen(
    config: dict,
    on_log=None,
    on_progress=None,
    stop_flag=None,
    watchlist_file: str | Path | None = None,
    price_cache_file: str | Path = "price_screen_cache.json",
    use_cache: bool = True,
) -> list[dict]:
    """Pass 1 — screen the symbol universe down to those inside the price range.

    Writes ``price_screen_cache.json`` and returns ``[{"symbol", "price"}, …]``.
    This is the slow, rarely-changing pass; the GUI drives it from its own button.
    """
    price_min      = config.get("price_min",      10.0)
    price_max      = config.get("price_max",     500.0)
    stock_throttle = config.get("stock_throttle",  0.1)

    trade_date = _current_trade_date()
    price_key  = _price_key(config)
    price_path = Path(price_cache_file)

    if use_cache and price_path.exists():
        try:
            cached = json.loads(price_path.read_text())
            if all(cached.get(k) == v for k, v in price_key.items()):
                qualified = cached["qualified"]
                if on_log:
                    on_log(f"Price screen cache hit — {len(qualified)} symbols in ${price_min}–${price_max}")
                return qualified
        except Exception:
            pass

    client = _connect_client(on_log)

    # ── Symbol universe ───────────────────────────────────────────────────────
    if watchlist_file and Path(watchlist_file).exists():
        all_symbols = [t.strip() for t in Path(watchlist_file).read_text().splitlines() if t.strip()]
        if on_log:
            on_log(f"Using watchlist: {len(all_symbols)} symbols")
    else:
        all_symbols = _get_company_symbols(client, on_log)

    if on_log:
        on_log(f"Pass 1: price screen — {len(all_symbols)} symbols …")
    price_qualified: list[dict] = []
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
            if on_progress:
                on_progress(i + 1, total)
            continue

        if eod is None or eod.empty:
            skipped += 1
            if on_progress:
                on_progress(i + 1, total)
            continue

        last_price = _current_price(eod)
        if last_price is None:
            skipped += 1
            if on_progress:
                on_progress(i + 1, total)
            continue

        if price_min <= last_price <= price_max:
            price_qualified.append({"symbol": sym, "price": round(last_price, 2)})

        if on_progress:
            on_progress(i + 1, total)

        time.sleep(stock_throttle)

    if on_log:
        on_log(f"Pass 1 done — {len(price_qualified)} in ${price_min}–${price_max}, {skipped} skipped")

    from datetime import datetime
    price_path.write_text(json.dumps({
        **price_key,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "qualified":  price_qualified,
    }))
    return price_qualified


def run_technical_filter(
    config: dict,
    price_qualified: list[dict],
    on_log=None,
    on_progress=None,
    stop_flag=None,
    history_cache_file: str | Path = "tech_history_cache.json",
    candidates_cache_file: str | Path = "tech_candidates_cache.json",
    use_cache: bool = True,
) -> list[dict]:
    """Pass 2 — fetch 45-day history for the price-qualified list and apply RSI/BB%.

    Writes ``tech_candidates_cache.json`` and returns
    ``[{"symbol", "price", "rsi", "bb_pct"}, …]``. Takes the price-screened list
    (from :func:`run_price_screen`) as input so it can be re-run on its own.
    """
    rsi_period       = config.get("rsi_period",       14)
    bb_period        = config.get("bb_period",        20)
    bb_std_mult      = config.get("bb_std_mult",       2.0)
    rsi_threshold    = config.get("rsi_threshold",    40.0)
    bb_pct_threshold = config.get("bb_pct_threshold", 33.0)
    stock_throttle   = config.get("stock_throttle",    0.1)

    today      = date.today()
    trade_date = _current_trade_date()
    hist_start = today - timedelta(days=45)

    price_key = _price_key(config)
    full_key  = _full_key(config)

    if not price_qualified:
        if on_log:
            on_log("No price-screened symbols — run the Price Scan first.")
        return []

    # ── Pass 2: fetch 45-day history (cache keyed on price params only) ────────
    hist_path = Path(history_cache_file)
    histories: dict[str, list] | None = None
    if use_cache and hist_path.exists():
        try:
            cached = json.loads(hist_path.read_text())
            if all(cached.get(k) == v for k, v in price_key.items()):
                histories = cached["histories"]
                if on_log:
                    on_log(f"History cache hit — {len(histories)} close series loaded, computing indicators …")
        except Exception:
            pass

    if histories is None:
        client = _connect_client(on_log)
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
                if on_progress:
                    on_progress(i + 1, total)
                continue

            if eod is None or len(eod) < bb_period + 2:
                skipped += 1
                if on_progress:
                    on_progress(i + 1, total)
                continue

            close_col = find_close_col(eod)
            if close_col is None:
                skipped += 1
                if on_progress:
                    on_progress(i + 1, total)
                continue

            closes = eod[close_col].dropna().astype(float)
            if len(closes) < bb_period + 2:
                skipped += 1
                if on_progress:
                    on_progress(i + 1, total)
                continue

            histories[sym] = closes.tolist()

            if on_progress:
                on_progress(i + 1, total)

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
    Path(candidates_cache_file).write_text(json.dumps({
        **full_key,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "candidates": tech_candidates,
    }))
    return tech_candidates


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
    """Convenience wrapper: price screen (Pass 1) then technical filter (Pass 2)."""
    full_key  = _full_key(config)
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

    price_qualified = run_price_screen(
        config,
        on_log=on_log,
        on_progress=on_pass1_progress,
        stop_flag=stop_flag,
        watchlist_file=watchlist_file,
        price_cache_file=price_cache_file,
    )
    if not price_qualified:
        if on_log:
            on_log("No symbols passed the price filter.")
        return []

    return run_technical_filter(
        config,
        price_qualified,
        on_log=on_log,
        on_progress=on_pass2_progress,
        stop_flag=stop_flag,
        history_cache_file=history_cache_file,
        candidates_cache_file=candidates_cache_file,
    )


def run_options_filter(
    candidates: list[dict],
    config: dict,
    on_log=None,
    on_progress=None,
    stop_flag=None,
) -> list[dict]:
    yield_min        = config.get("yield_min",        0.009)
    yield_max        = config.get("yield_max",        0.020)
    options_throttle = config.get("options_throttle", 0.5)
    right            = config.get("right",            "P")    # "P" or "C"
    side             = config.get("side",             "sell") # "sell" or "buy"
    weeklies_only    = config.get("weeklies_only",    False)
    price_col        = "bid" if side == "sell" else "ask"

    today = date.today()

    exp_date_str  = config.get("expiration_date")
    requested_exp = date.fromisoformat(exp_date_str) if exp_date_str else None
    dte_min = config.get("dte_min",  4)
    dte_max = config.get("dte_max", 21)
    results = []
    total   = len(candidates)

    # Option chains come from Schwab (real-time); authenticate once. The scanner
    # runs off the main thread, so never block on stdin — require a cached token.
    from core import schwab_client
    try:
        client = schwab_client.get_client(interactive=False)
    except (FileNotFoundError, RuntimeError) as e:
        raise ScreenerError(
            "Schwab authentication required for option chains. Run "
            "'gui-env/bin/python -m core.schwab_client login' once, then retry."
        ) from e

    if on_log:
        label = f"{'Put' if right == 'P' else 'Call'} {'sells' if side == 'sell' else 'buys'}"
        on_log(f"Scanning {label} chains (Schwab real-time) for {total} candidates …")

    for i, row in enumerate(candidates):
        if stop_flag and stop_flag():
            if on_log:
                on_log("Stopped by user.")
            break

        sym         = row["symbol"]
        stock_price = row["price"]
        sym_hits    = 0

        # One Schwab call per symbol returns every expiration through the horizon.
        horizon = requested_exp if requested_exp is not None else today + timedelta(days=dte_max)
        chains  = _fetch_schwab_chain(client, sym, right, horizon)
        if not chains:
            if on_log:
                on_log(f"  {sym}: no chain data")
            if on_progress:
                on_progress(i + 1, total)
            time.sleep(options_throttle)
            continue

        available = sorted(chains)

        if weeklies_only and not _has_weeklies(available, today):
            if on_log:
                on_log(f"  {sym}: no weekly options — skipped")
            if on_progress:
                on_progress(i + 1, total)
            time.sleep(options_throttle)
            continue

        if requested_exp is not None:
            exp = snap_expiration(requested_exp, available)
            if exp is None:
                if on_log:
                    on_log(f"  {sym}: no listed expiration on or before {requested_exp}")
                if on_progress:
                    on_progress(i + 1, total)
                time.sleep(options_throttle)
                continue
            if exp != requested_exp and on_log:
                on_log(f"  {sym}: {requested_exp} not listed — using {exp}")
            expirations = [exp]
        else:
            expirations = [e for e in available if dte_min <= (e - today).days <= dte_max]

        for exp in expirations:
            chain = chains[exp]
            if price_col not in chain.columns:
                continue
            chain = chain.copy()
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
