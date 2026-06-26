import re
import time
import json
import threading
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import numpy as np


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
UNIVERSE_FILE    = "universe.json"           # persisted scan universe; refreshed on demand
DROPPED_FILE     = "universe_dropped.json"   # diagnostic: EDGAR names Schwab didn't price

# Per-symbol Schwab fetches (Pass 2 history, option chains) run concurrently
# behind a self-tuning rate limiter (no GUI knob). Start conservatively below
# Schwab's account-wide ceiling: starting at the ceiling provokes 429/403s, and
# each hit triggers a multi-second cool-off, so a gentler start avoids the long
# stalls. Back off further on 429/403 and recover automatically.
_FETCH_WORKERS    = 8
_FETCH_START_RATE = 7    # requests/sec

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


def _fmt_elapsed(seconds: float) -> str:
    """Human-readable elapsed time, e.g. '45.2s' or '1m 23s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s:02d}s"


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
    Each frame carries strike/bid/ask/delta columns. Returns {} for a genuine
    no-chain response; HTTP errors propagate so the caller's rate limiter can see
    429/403. (We never pass from_date — Schwab 400s when it equals today; the near
    end is filtered by the caller from the returned expirations.)
    """
    from core import schwab_client
    ct = (client.Options.ContractType.PUT if right == "P"
          else client.Options.ContractType.CALL)
    data = schwab_client.option_chain(client, symbol, contract_type=ct, to_date=to_date)
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


def _fetch_edgar_symbols(on_log=None) -> list[str]:
    """NYSE/Nasdaq common-stock tickers from SEC EDGAR, ETFs/funds filtered out."""
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
    symbols = sorted(df["ticker"].tolist())
    if on_log:
        on_log(f"SEC company universe: {len(symbols)} tickers after ETF/fund filter")
    return symbols


def _resolve_priceable(client, edgar_symbols: list[str], on_log=None, on_progress=None,
                       chunk: int = 250) -> dict:
    """Map each EDGAR ticker Schwab can price to its Schwab symbol.

    Direct hits map to themselves. Dual-class names lost to the EDGAR '-'/'.'
    vs Schwab '/' separator (BRK-B → BRK/B) are recovered with a normalized
    retry, and stored under their Schwab form. Returns {edgar: schwab}.
    """
    from core import schwab_client
    resolved: dict[str, str] = {}
    missing: list[str] = []
    total = len(edgar_symbols)
    for i in range(0, total, chunk):
        batch = edgar_symbols[i:i + chunk]
        try:
            data = schwab_client.quotes(client, batch)
            # Recognized symbols are top-level keys; unknowns are in errors.invalidSymbols.
            for s in batch:
                (resolved.__setitem__(s, s) if s in data else missing.append(s))
        except Exception:
            for s in batch:      # on a request error, keep the batch rather than drop it
                resolved[s] = s
        if on_progress:
            on_progress(min(i + chunk, total), total)

    # Recover dual-class commons via separator normalization (BRK-B → BRK/B).
    candidates = [s for s in missing if _classify_dropped(s) == "class_share"]
    for i in range(0, len(candidates), chunk):
        batch = candidates[i:i + chunk]
        alt = {s: re.sub(r"[-.]", "/", s) for s in batch}
        try:
            data = schwab_client.quotes(client, list(alt.values()))
        except Exception:
            continue
        for edgar_sym, schwab_sym in alt.items():
            if schwab_sym in data:
                resolved[edgar_sym] = schwab_sym

    if on_log:
        recovered = sum(1 for e, s in resolved.items() if e != s)
        extra = f" ({recovered} dual-class recovered)" if recovered else ""
        on_log(f"Priceable in Schwab: {len(resolved)}/{total}{extra}")
    return resolved


def _classify_dropped(sym: str) -> str:
    """Bucket a dropped EDGAR ticker by its suffix after a -/./ separator.

    class_share names (e.g. BRK-B) are the ones likely lost only to the EDGAR
    '-' vs Schwab '/' format difference; the rest are genuinely out of scope.
    """
    parts = re.split(r"[-./]", sym, maxsplit=1)
    if len(parts) < 2:
        return "unlisted"                 # no separator → Schwab simply has no quote
    suffix = parts[1]
    if suffix.startswith("P"):
        return "preferred"                # ABR-PD, AGM-PE, …
    if suffix in ("W", "WT", "WS"):
        return "warrant"                  # ACHR-WT
    if suffix in ("R", "RT"):
        return "rights"
    if len(suffix) == 1 and suffix.isalpha():
        return "class_share"              # BRK-B → likely BRK/B in Schwab
    return "other"


def _record_dropped(dropped: list[str], on_log=None,
                    dropped_file: str | Path = DROPPED_FILE) -> None:
    """Persist the dropped tickers + a breakdown so exclusions are inspectable."""
    from collections import Counter
    kinds = Counter(_classify_dropped(s) for s in dropped)
    class_shares = sorted(s for s in dropped if _classify_dropped(s) == "class_share")
    Path(dropped_file).write_text(json.dumps({
        "updated":      date.today().isoformat(),
        "count":        len(dropped),
        "by_kind":      dict(kinds),
        "class_share":  class_shares,     # candidates lost to -/. vs / format
        "dropped":      dropped,
    }, indent=2))
    if on_log:
        eg = ", ".join(class_shares[:3])
        on_log(
            f"Dropped {len(dropped)}: {len(class_shares)} dual-class"
            f"{f' (e.g. {eg})' if eg else ''}, {kinds.get('preferred', 0)} preferred, "
            f"{kinds.get('warrant', 0)} warrant, {kinds.get('rights', 0)} rights, "
            f"{kinds.get('unlisted', 0)} unlisted — see {dropped_file}"
        )


def build_universe(on_log=None, on_progress=None,
                   universe_file: str | Path = UNIVERSE_FILE,
                   validate: bool = True) -> dict:
    """Fetch the EDGAR universe, drop names Schwab can't price, and persist it.

    Returns the saved dict ``{updated, source, count, symbols}``. When Schwab is
    unavailable (no cached token), saves the unvalidated EDGAR list instead of
    failing, so the stock scanner still has a universe to work from. The names
    Schwab rejects are written to DROPPED_FILE with a breakdown by kind.
    """
    edgar = _fetch_edgar_symbols(on_log)
    if not edgar:
        raise ScreenerError("SEC EDGAR returned no symbols.")

    symbols = edgar
    source  = "SEC EDGAR"
    if validate:
        try:
            from core import schwab_client
            client = schwab_client.get_client(interactive=False)
        except Exception as e:
            client = None
            if on_log:
                on_log(f"Schwab unavailable ({e}) — saving unvalidated EDGAR list")
        if client is not None:
            resolved = _resolve_priceable(client, edgar, on_log, on_progress)
            symbols  = sorted(resolved.values())   # Schwab symbols (recovered forms included)
            source   = "SEC EDGAR + Schwab priceable"
            _record_dropped(sorted(set(edgar) - set(resolved)), on_log)

    data = {
        "updated": date.today().isoformat(),
        "source":  source,
        "count":   len(symbols),
        "symbols": symbols,
    }
    Path(universe_file).write_text(json.dumps(data, indent=2))
    if on_log:
        on_log(f"Universe saved: {len(symbols)} tickers → {universe_file}")
    return data


def load_universe(universe_file: str | Path = UNIVERSE_FILE) -> list[str] | None:
    """Return the persisted universe symbols, or None if there's no usable file."""
    p = Path(universe_file)
    if not p.exists():
        return None
    try:
        symbols = json.loads(p.read_text()).get("symbols")
        return symbols or None
    except Exception:
        return None


class _RateLimiter:
    """Self-tuning global pacer for concurrent Schwab calls.

    Starts at ``start_rate`` req/s and holds the aggregate rate just under
    Schwab's account-wide limit: Schwab 429/403s with no Retry-After, so on a
    rate-limit hit we pause every worker and slow down, then recover toward the
    start rate on sustained success. No upward probing → we don't provoke limits.
    """
    def __init__(self, start_rate: float):
        self._lock      = threading.Lock()
        self._start     = 1.0 / start_rate   # fastest allowed interval (seconds)
        self.interval   = self._start        # current interval; grows on limit
        self._next_slot = 0.0
        self._pause     = 0.0                # global cool-off after a hit
        self._last_grow = 0.0
        self.hits       = 0

    def pace(self):
        with self._lock:
            now  = time.monotonic()
            slot = max(now, self._next_slot, self._pause)
            self._next_slot = slot + self.interval
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def hit(self, attempt: int):
        with self._lock:
            self.hits += 1
            now = time.monotonic()
            if now - self._last_grow > 1.0:        # grow at most once/sec (don't over-correct a burst)
                self.interval = min(self.interval * 1.5, 1.0)
                self._last_grow = now
            self._pause = max(self._pause, now + min(0.5 * 2 ** attempt, 8.0))

    def recover(self):
        with self._lock:
            if self.interval > self._start:
                self.interval = max(self._start, self.interval * 0.9)

    @property
    def rate(self) -> float:
        return 1.0 / self.interval


def _concurrent_fetch(keys, call_one, *, workers, start_rate,
                      on_progress=None, stop_flag=None):
    """Fetch ``call_one(key)`` for every key concurrently under a shared limiter.

    ``call_one`` makes ONE Schwab request and returns its result, raising on HTTP
    error (429/403 → rate-limit backoff; other errors → give up after 2 tries).
    Returns ``(results, limiter)`` where results maps key → value for everything
    that returned (None values are dropped). ``results`` is None if the user
    stopped. Raises ScreenerError if hard-blocked with no progress for 30s.
    """
    limiter = _RateLimiter(start_rate)
    results: dict = {}

    def _worker(key):
        other_errors = 0
        for attempt in range(8):
            limiter.pace()
            try:
                value = call_one(key)
                limiter.recover()
                return key, value
            except Exception as e:
                status = getattr(getattr(e, "response", None), "status_code", None)
                if status in (429, 403):           # 429 burst, or 403 once Schwab escalates
                    limiter.hit(attempt)
                else:
                    other_errors += 1
                    if other_errors >= 2:          # genuine failure (not rate limit) → give up
                        break
                    time.sleep(0.3)
        return key, None

    total = len(keys)
    done  = 0
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_worker, k) for k in keys]
        for fut in as_completed(futures):
            if stop_flag and stop_flag():
                for f in futures:
                    f.cancel()
                return None, limiter
            # Circuit breaker: rate-limited with no progress after 30s = hard block.
            if not results and limiter.hits and time.monotonic() - start > 30:
                for f in futures:
                    f.cancel()
                raise ScreenerError(
                    "Schwab is blocking requests (rate limit / temporary cool-off). "
                    "Wait a few minutes, then retry. If it persists, re-run the "
                    "Schwab login.")
            key, value = fut.result()
            done += 1
            if value is not None:
                results[key] = value
            if on_progress:
                on_progress(done, total)
    return results, limiter


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
    on_found=None,
    stop_flag=None,
    watchlist_file: str | Path | None = None,
    price_cache_file: str | Path = "price_screen_cache.json",
    use_cache: bool = True,
) -> list[dict]:
    """Pass 1 — screen the symbol universe down to those inside the price range.

    Writes ``price_screen_cache.json`` and returns ``[{"symbol", "price"}, …]``.
    This is the slow, rarely-changing pass; the GUI drives it from its own button.
    ``on_found(rows)`` is called with each batch's newly qualified rows so the GUI
    can populate the list as the scan runs.
    """
    price_min      = config.get("price_min",      10.0)
    price_max      = config.get("price_max",     500.0)

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

    from core import schwab_client
    try:
        client = schwab_client.get_client(interactive=False)
    except (FileNotFoundError, RuntimeError) as e:
        raise ScreenerError(
            "Schwab authentication required for the price screen. Run "
            "'gui-env/bin/python -m core.schwab_client login' once, then retry."
        ) from e

    # ── Symbol universe ───────────────────────────────────────────────────────
    # Precedence: manual watchlist → saved universe.json → bootstrap from EDGAR.
    if watchlist_file and Path(watchlist_file).exists():
        all_symbols = [t.strip() for t in Path(watchlist_file).read_text().splitlines() if t.strip()]
        if on_log:
            on_log(f"Using watchlist: {len(all_symbols)} symbols")
    else:
        all_symbols = load_universe()
        if all_symbols is None:
            if on_log:
                on_log("No saved universe — building from SEC EDGAR (use Update Universe to refresh) …")
            all_symbols = build_universe(on_log=on_log).get("symbols", [])
        elif on_log:
            on_log(f"Using saved universe: {len(all_symbols)} tickers")

    if on_log:
        on_log(f"Pass 1: price screen — {len(all_symbols)} symbols (batched quotes) …")
    price_qualified: list[dict] = []
    total   = len(all_symbols)
    skipped = 0
    chunk   = 250

    for i in range(0, total, chunk):
        if stop_flag and stop_flag():
            if on_log:
                on_log("Stopped by user.")
            return []

        batch = all_symbols[i:i + chunk]
        try:
            data = schwab_client.quotes(client, batch)
        except Exception:
            data = {}

        batch_qualified: list[dict] = []
        for sym in batch:
            price = data.get(sym, {}).get("quote", {}).get("lastPrice")
            if price is None:
                skipped += 1
            elif price_min <= price <= price_max:
                batch_qualified.append({"symbol": sym, "price": round(float(price), 2)})

        price_qualified.extend(batch_qualified)
        if on_found and batch_qualified:
            on_found(batch_qualified)
        if on_progress:
            on_progress(min(i + chunk, total), total)
        time.sleep(0.1)   # gentle pace between the ~20 batched quote calls

    if on_log:
        on_log(f"Pass 1 done — {len(price_qualified)} in ${price_min}–${price_max}, {skipped} skipped")

    price_path.write_text(json.dumps({
        **price_key,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "qualified":  price_qualified,
    }))
    return price_qualified


def _evaluate_candidate(sym, closes_list, price_lookup, *, rsi_period, bb_period,
                        bb_std_mult, rsi_threshold, bb_pct_threshold,
                        append_live=True) -> dict | None:
    """Apply the RSI/BB% filter to one close series; return a candidate row or None.

    ``closes_list`` holds daily closes *through yesterday*. When ``append_live`` is
    set (a live trading session), the symbol's current quote from ``price_lookup``
    is appended as today's close so RSI/BB% reflect the exact live price rather than
    the prior session's close. ``closes_list`` already excludes today's bar, so this
    never double-counts.
    """
    live = price_lookup.get(sym)
    closes_list = list(closes_list)
    if append_live and live is not None:
        closes_list.append(live)
    closes = pd.Series(closes_list, dtype=float)
    if len(closes) < bb_period + 2:
        return None
    rsi = calc_rsi(closes, rsi_period)
    if rsi >= rsi_threshold:
        return None
    bb_pct = calc_bb_pct(closes, bb_period, bb_std_mult)
    if bb_pct >= bb_pct_threshold:
        return None
    return {
        "symbol": sym,
        "price":  round(live if live is not None else float(closes.iloc[-1]), 2),
        "rsi":    round(rsi, 1),
        "bb_pct": round(bb_pct, 1),
    }


def run_technical_filter(
    config: dict,
    price_qualified: list[dict],
    on_log=None,
    on_progress=None,
    on_found=None,
    stop_flag=None,
    history_cache_file: str | Path = "tech_history_cache.json",
    candidates_cache_file: str | Path = "tech_candidates_cache.json",
    use_cache: bool = True,
) -> list[dict]:
    """Pass 2 — fetch 45-day history for the price-qualified list and apply RSI/BB%.

    Writes ``tech_candidates_cache.json`` and returns
    ``[{"symbol", "price", "rsi", "bb_pct"}, …]``. Takes the price-screened list
    (from :func:`run_price_screen`) as input so it can be re-run on its own.
    ``on_found(rows)`` is called with each candidate as its history arrives so the
    GUI can populate the list as the fetch runs.
    """
    rsi_period       = config.get("rsi_period",       14)
    bb_period        = config.get("bb_period",        20)
    bb_std_mult      = config.get("bb_std_mult",       2.0)
    rsi_threshold    = config.get("rsi_threshold",    40.0)
    bb_pct_threshold = config.get("bb_pct_threshold", 33.0)

    today      = date.today()
    hist_start = today - timedelta(days=45)

    price_key = _price_key(config)
    full_key  = _full_key(config)

    if not price_qualified:
        if on_log:
            on_log("No price-screened symbols — run the Price Scan first.")
        return []

    # "Today" is the current market (ET) date — the machine's local date can differ
    # near midnight. On a trading day we substitute the live quote for today's bar;
    # on a weekend/holiday there's no new bar, so the stored closes stand as-is.
    from zoneinfo import ZoneInfo
    et_today         = datetime.now(ZoneInfo("America/New_York")).date()
    is_trading_today = _last_trading_day(et_today) == et_today

    price_lookup = {item["symbol"]: item["price"] for item in price_qualified}
    filter_kw = dict(
        rsi_period=rsi_period, bb_period=bb_period, bb_std_mult=bb_std_mult,
        rsi_threshold=rsi_threshold, bb_pct_threshold=bb_pct_threshold,
        append_live=is_trading_today,
    )

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
        from core import schwab_client
        try:
            client = schwab_client.get_client(interactive=False)
        except (FileNotFoundError, RuntimeError) as e:
            raise ScreenerError(
                "Schwab authentication required for technical history. Run "
                "'gui-env/bin/python -m core.schwab_client login' once, then retry."
            ) from e
        if on_log:
            on_log(f"Pass 2: fetching 45-day history for {len(price_qualified)} symbols …")
        hist_start_dt = datetime.combine(hist_start, datetime.min.time())
        # End a day out so today's bar is always returned regardless of the
        # machine's timezone vs. ET; we strip it below and use the live quote.
        end_dt        = datetime.combine(today + timedelta(days=1), datetime.min.time())

        # Schwab's price-history endpoint is single-symbol, so we can't batch — but
        # the calls are independent network I/O, so fetch them concurrently behind
        # the shared self-tuning limiter.
        def _fetch_history(sym):
            data = schwab_client.price_history_daily(
                client, sym, start=hist_start_dt, end=end_dt)
            # Keep daily closes *through yesterday* (ET); today's still-forming bar
            # is dropped here and replaced by the live quote in _evaluate_candidate,
            # so the indicators track the exact current price without double-counting.
            closes = [
                c["close"] for c in data.get("candles", [])
                if c.get("close") is not None
                and datetime.fromtimestamp(c["datetime"] / 1000,
                                           ZoneInfo("America/New_York")).date() < et_today
            ]
            # Evaluate the indicator as the history arrives so qualifying symbols
            # can stream into the GUI list instead of waiting for every fetch.
            if on_found:
                cand = _evaluate_candidate(sym, closes, price_lookup, **filter_kw)
                if cand:
                    on_found([cand])
            return closes

        fetch_start = time.monotonic()
        fetched, limiter = _concurrent_fetch(
            [item["symbol"] for item in price_qualified], _fetch_history,
            workers=_FETCH_WORKERS, start_rate=_FETCH_START_RATE,
            on_progress=on_progress, stop_flag=stop_flag)
        if fetched is None:                          # user stopped
            if on_log:
                on_log("Stopped by user.")
            return []
        elapsed = time.monotonic() - fetch_start

        # Stored closes run through yesterday; the live bar is appended at
        # evaluation, so one fewer stored close is needed for a full window.
        histories = {s: c for s, c in fetched.items() if len(c) >= bb_period + 1}
        skipped   = len(price_qualified) - len(histories)
        if limiter.hits and on_log:
            on_log(f"Schwab rate-limited {limiter.hits}× — auto-throttled to "
                   f"~{limiter.rate:.0f} req/s (recovers automatically).")
        if on_log:
            on_log(f"Pass 2 done — history for {len(histories)} symbols, {skipped} skipped "
                   f"in {_fmt_elapsed(elapsed)} "
                   f"({len(price_qualified) / max(elapsed, 1e-3):.1f} symbols/s)")
        hist_path.write_text(json.dumps({**price_key, "histories": histories}))

    # ── Apply RSI / BB% filter in memory (instant from cache) ─────────────────
    tech_candidates: list[dict] = []

    for sym, closes_list in histories.items():
        cand = _evaluate_candidate(sym, closes_list, price_lookup, **filter_kw)
        if cand is not None:
            tech_candidates.append(cand)

    if on_log:
        on_log(f"Technical filter done — {len(tech_candidates)} candidates.")

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

    # One Schwab call per symbol covers every expiration through the horizon;
    # fetch all candidates concurrently behind the shared self-tuning limiter.
    horizon = requested_exp if requested_exp is not None else today + timedelta(days=dte_max)
    fetch_start = time.monotonic()
    fetched, limiter = _concurrent_fetch(
        [row["symbol"] for row in candidates],
        lambda sym: _fetch_schwab_chain(client, sym, right, horizon),
        workers=_FETCH_WORKERS, start_rate=_FETCH_START_RATE,
        on_progress=on_progress, stop_flag=stop_flag)
    if fetched is None:                              # user stopped
        if on_log:
            on_log("Stopped by user.")
        return []
    elapsed = time.monotonic() - fetch_start
    if limiter.hits and on_log:
        on_log(f"Schwab rate-limited {limiter.hits}× — auto-throttled to "
               f"~{limiter.rate:.0f} req/s (recovers automatically).")
    if on_log:
        on_log(f"Fetched {len(fetched)}/{total} chains in {_fmt_elapsed(elapsed)} "
               f"({total / max(elapsed, 1e-3):.1f} symbols/s)")

    # ── Filter the fetched chains in memory ───────────────────────────────────
    for row in candidates:
        sym         = row["symbol"]
        stock_price = row["price"]
        chains      = fetched.get(sym)
        if not chains:
            if on_log:
                on_log(f"  {sym}: no chain data")
            continue

        available = sorted(chains)
        if weeklies_only and not _has_weeklies(available, today):
            if on_log:
                on_log(f"  {sym}: no weekly options — skipped")
            continue

        if requested_exp is not None:
            exp = snap_expiration(requested_exp, available)
            if exp is None:
                if on_log:
                    on_log(f"  {sym}: no listed expiration on or before {requested_exp}")
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
