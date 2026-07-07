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
    return _last_session_before(date.today())


def _last_session_before(ref: date) -> date:
    """Return the most recent trading day strictly before ``ref``."""
    holidays = _us_market_holidays(ref.year) | _us_market_holidays(ref.year - 1)
    d = ref - timedelta(days=1)
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


_SCHWAB_AUTH_MSG = (
    "Schwab login expired — the refresh token is invalid or has been revoked. "
    "Re-run 'gui-env/bin/python -m core.schwab_client login', then retry."
)


def _fmt_elapsed(seconds: float) -> str:
    """Human-readable elapsed time, e.g. '45.2s' or '1m 23s'."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s:02d}s"


def calc_rsi_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI as a full series (NaN until `period` bars have accumulated)."""
    delta    = closes.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_bb_bands(closes: pd.Series, period: int = 20,
                  std_mult: float = 2.0) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Bollinger Bands as (middle SMA, upper, lower) series."""
    sma   = closes.rolling(period).mean()
    std   = closes.rolling(period).std(ddof=0)
    return sma, sma + std_mult * std, sma - std_mult * std


def calc_rsi(closes: pd.Series, period: int = 14) -> float:
    return float(calc_rsi_series(closes.dropna(), period).iloc[-1])


def calc_bb_pct(closes: pd.Series, period: int = 20, std_mult: float = 2.0) -> float:
    _, upper, lower = calc_bb_bands(closes, period, std_mult)
    price = closes.iloc[-1]
    u, l  = upper.iloc[-1], lower.iloc[-1]
    if (u - l) == 0:
        return 50.0
    return float((price - l) / (u - l) * 100)


def fetch_history_df(symbol: str, days: int = 180,
                     intraday_minutes: int | None = None) -> pd.DataFrame:
    """OHLCV history for one symbol as a DataFrame indexed by timestamp.

    Used by the chart window. ``intraday_minutes`` (1/5/10/15/30) requests
    intraday candles for the short timeframes; None gives daily candles. Pulls
    ``days`` of lookback so the 20-bar Bollinger Bands and 14-bar RSI can warm
    up. Requires a cached Schwab token (no interactive login on the GUI thread).
    Raises ScreenerError if the token is missing or the symbol returns no candles.
    """
    from core import schwab_client
    try:
        client = schwab_client.get_client(interactive=False)
    except (FileNotFoundError, RuntimeError) as e:
        raise ScreenerError(
            "Schwab authentication required. Run "
            "'gui-env/bin/python -m core.schwab_client login' once, then retry."
        ) from e

    start = datetime.now() - timedelta(days=days)
    if intraday_minutes:
        data = schwab_client.price_history_intraday(
            client, symbol, minutes=intraday_minutes, start=start)
    else:
        data = schwab_client.price_history_daily(client, symbol, start=start)
    candles = [c for c in data.get("candles", []) if c.get("close") is not None]
    if not candles:
        raise ScreenerError(f"No price history for {symbol}.")

    df = pd.DataFrame(candles)
    # Schwab stamps candles in UTC epoch ms; show them in Eastern wall-clock time
    # (so intraday axes read 9:30–16:00), then drop the tz for clean plotting.
    df["dt"] = (pd.to_datetime(df["datetime"], unit="ms", utc=True)
                  .dt.tz_convert("America/New_York").dt.tz_localize(None))
    return df.set_index("dt")[["open", "high", "low", "close", "volume"]].sort_index()


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
             "ask": c.get("ask"), "delta": c.get("delta"),
             "iv": c.get("volatility")}          # annualized IV %, per contract
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


_WEEKLIES_HORIZON_DAYS = 70


def _has_weeklies(expirations: list[date], ref: date,
                  horizon_days: int = _WEEKLIES_HORIZON_DAYS) -> bool:
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
    # Drop the weekly-options flags so they get recomputed against the new
    # universe on the next options scan (listings change rarely, but a universe
    # rebuild is the natural point to refresh them).
    Path(WEEKLIES_CACHE_FILE).unlink(missing_ok=True)
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


# Persistent per-symbol daily-close store for Pass 2. Each entry holds the close
# series *through the last completed session* (today's bar is never stored — the
# live quote stands in for it). Symbols already current are skipped on the next
# scan; stale ones are fully refetched, which also self-heals split/dividend
# re-adjustments. Keyed per symbol, so it survives price-range/threshold changes.
HISTORY_STORE_FILE = "history_store_cache.json"


def _load_history_store(path: str | Path = HISTORY_STORE_FILE) -> dict:
    """Load the per-symbol close store as {symbol: {"last": iso_date, "closes": [...]}}."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_history_store(store: dict, path: str | Path = HISTORY_STORE_FILE) -> None:
    Path(path).write_text(json.dumps(store))


WEEKLIES_CACHE_FILE = "weeklies_cache.json"


def _load_weeklies_cache(path: str | Path = WEEKLIES_CACHE_FILE) -> dict[str, bool]:
    """Load the persisted {symbol: has_weeklies} flags.

    Weekly-vs-monthly is a stable property of a stock, so we compute it once per
    symbol (from a chain wide enough for _has_weeklies to read Friday spacing) and
    reuse it. Cleared by build_universe so flags refresh when the universe is rebuilt.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return {k: bool(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_weeklies_cache(flags: dict[str, bool], path: str | Path = WEEKLIES_CACHE_FILE) -> None:
    Path(path).write_text(json.dumps(flags))


# ── Implied volatility: expected move (σ) and a self-calibrating IV-percentile store ─
def _clean_iv(v) -> float | None:
    """Coerce a Schwab per-contract IV to a usable annualized %, else None.

    Schwab returns a sentinel (e.g. -999) for no-data and skew-inflated values in
    the far-OTM tails (e.g. ~490% deep OTM); keep only plausible readings.
    """
    try:
        iv = float(v)
    except (TypeError, ValueError):
        return None
    return iv if 0 < iv <= 300 else None


def _period_sigma_pct(iv_pct: float, dte: int) -> float | None:
    """The underlying's expected move over ``dte`` days, as a %: IV × √(DTE/365)."""
    if not iv_pct or dte <= 0:
        return None
    return iv_pct * (dte / 365.0) ** 0.5


def _atm_iv(chain: pd.DataFrame, stock_price: float) -> float | None:
    """Near-ATM IV of a chain — the strike closest to spot. Used as the underlying's
    IV level (comparable across candidates), not the skew-inflated far-OTM IV."""
    if "iv" not in chain.columns or not stock_price:
        return None
    df = chain.copy()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df = df.dropna(subset=["strike"])
    if df.empty:
        return None
    idx = (df["strike"] - stock_price).abs().idxmin()
    return _clean_iv(df.loc[idx, "iv"])


IV_HISTORY_FILE = "iv_history_cache.json"
_IV_HISTORY_DAYS = 365          # trailing window for the IV percentile
_IV_PCTILE_MIN_OBS = 5          # below this the percentile is too thin to trust


def _load_iv_store(path: str | Path = IV_HISTORY_FILE) -> dict:
    """Load the per-symbol near-ATM IV history as {symbol: {iso_date: iv_pct}}."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_iv_store(store: dict, path: str | Path = IV_HISTORY_FILE) -> None:
    Path(path).write_text(json.dumps(store))


def _record_iv(store: dict, symbol: str, iv_pct: float, ref: date) -> None:
    """Record one near-ATM IV reading for today and prune anything older than a year."""
    cutoff = (ref - timedelta(days=_IV_HISTORY_DAYS)).isoformat()
    hist = {d: v for d, v in store.get(symbol, {}).items() if d >= cutoff}
    hist[ref.isoformat()] = round(iv_pct, 2)
    store[symbol] = hist


def _iv_percentile(store: dict, symbol: str, iv_pct: float) -> float | None:
    """Percent of the symbol's trailing-year IV readings at or below ``iv_pct``.

    Self-calibrating: it only becomes meaningful once several scans have
    accumulated, so returns None until there are at least a handful of readings.
    """
    hist = store.get(symbol, {})
    vals = list(hist.values())
    if len(vals) < _IV_PCTILE_MIN_OBS:
        return None
    at_or_below = sum(1 for v in vals if v <= iv_pct)
    return round(at_or_below / len(vals) * 100, 1)


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
    from core import schwab_client
    limiter = _RateLimiter(start_rate)
    results: dict = {}
    auth_error = None   # set by any worker that hits an expired/revoked token

    def _worker(key):
        nonlocal auth_error
        other_errors = 0
        for attempt in range(8):
            limiter.pace()
            try:
                value = call_one(key)
                limiter.recover()
                return key, value
            except Exception as e:
                if schwab_client.is_auth_error(e):  # expired token → fatal, stop the scan
                    auth_error = e
                    break
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
            if auth_error is not None:             # expired token → surface it, don't return empty
                for f in futures:
                    f.cancel()
                raise ScreenerError(_SCHWAB_AUTH_MSG) from auth_error
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
        except Exception as e:
            if schwab_client.is_auth_error(e):
                raise ScreenerError(_SCHWAB_AUTH_MSG) from e
            data = {}   # tolerate a transient per-batch failure

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
    history_store_file: str | Path = HISTORY_STORE_FILE,
    candidates_cache_file: str | Path = "tech_candidates_cache.json",
    use_cache: bool = True,
) -> list[dict]:
    """Pass 2 — get 45-day history for the price-qualified list and apply RSI/BB%.

    Writes ``tech_candidates_cache.json`` and returns
    ``[{"symbol", "price", "rsi", "bb_pct"}, …]``. Takes the price-screened list
    (from :func:`run_price_screen`) as input so it can be re-run on its own.
    ``on_found(rows)`` is called with each candidate as it's evaluated so the GUI
    can populate the list as the scan runs.

    Daily history is served from a persistent per-symbol store
    (:data:`HISTORY_STORE_FILE`): symbols already current through the last
    completed session are reused with no fetch; the rest are fetched in full and
    the store is updated. ``use_cache=False`` forces a full refetch of every
    symbol (still updating the store).
    """
    rsi_period       = config.get("rsi_period",       14)
    bb_period        = config.get("bb_period",        20)
    bb_std_mult      = config.get("bb_std_mult",       2.0)
    rsi_threshold    = config.get("rsi_threshold",    40.0)
    bb_pct_threshold = config.get("bb_pct_threshold", 33.0)

    today      = date.today()
    hist_start = today - timedelta(days=45)

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

    # ── Per-symbol history store: reuse what's current, fetch only the rest ────
    # The store holds daily closes through the last completed session; today's bar
    # is always synthesized from the live quote, so a "current" symbol needs no
    # fetch (its daily closes can't change intraday). Stale/new symbols are fetched
    # in full, which also re-syncs any split/dividend re-adjustment.
    last_session_str = _last_session_before(et_today).isoformat()
    store_path = Path(history_store_file)
    store      = _load_history_store(store_path)

    symbols = [item["symbol"] for item in price_qualified]
    fresh: dict[str, list] = {}
    stale: list[str]       = []
    for sym in symbols:
        entry = store.get(sym)
        if use_cache and entry and entry.get("last") == last_session_str and entry.get("closes"):
            fresh[sym] = entry["closes"]
        else:
            stale.append(sym)

    # Already-current symbols stream in immediately — no fetch needed for these.
    if on_found:
        for sym, closes in fresh.items():
            cand = _evaluate_candidate(sym, closes, price_lookup, **filter_kw)
            if cand:
                on_found([cand])

    n_total = len(symbols)
    if on_progress and fresh:
        on_progress(len(fresh), n_total)
    if on_log:
        on_log(f"Pass 2: {len(fresh)} symbols current in store, fetching {len(stale)} …")

    fetched: dict[str, tuple] = {}
    elapsed = 0.0
    if stale:
        from core import schwab_client
        try:
            client = schwab_client.get_client(interactive=False)
        except (FileNotFoundError, RuntimeError) as e:
            raise ScreenerError(
                "Schwab authentication required for technical history. Run "
                "'gui-env/bin/python -m core.schwab_client login' once, then retry."
            ) from e
        hist_start_dt = datetime.combine(hist_start, datetime.min.time())
        # End a day out so today's bar is always returned regardless of the
        # machine's timezone vs. ET; we strip it below and use the live quote.
        end_dt = datetime.combine(today + timedelta(days=1), datetime.min.time())
        ET = ZoneInfo("America/New_York")

        # Schwab's price-history endpoint is single-symbol, so we can't batch — but
        # the calls are independent network I/O, so fetch them concurrently behind
        # the shared self-tuning limiter.
        def _fetch_history(sym):
            data = schwab_client.price_history_daily(
                client, sym, start=hist_start_dt, end=end_dt)
            # Keep daily closes *through yesterday* (ET); today's still-forming bar
            # is dropped here and replaced by the live quote in _evaluate_candidate,
            # so the indicators track the exact current price without double-counting.
            kept = [
                c for c in data.get("candles", [])
                if c.get("close") is not None
                and datetime.fromtimestamp(c["datetime"] / 1000, ET).date() < et_today
            ]
            closes    = [c["close"] for c in kept]
            last_date = (datetime.fromtimestamp(kept[-1]["datetime"] / 1000, ET)
                         .date().isoformat()) if kept else None
            # Evaluate as the history arrives so qualifying symbols stream into the
            # GUI list instead of waiting for every fetch.
            if on_found:
                cand = _evaluate_candidate(sym, closes, price_lookup, **filter_kw)
                if cand:
                    on_found([cand])
            return closes, last_date

        def _prog(done, _total):
            if on_progress:
                on_progress(len(fresh) + done, n_total)

        fetch_start = time.monotonic()
        fetched, limiter = _concurrent_fetch(
            stale, _fetch_history,
            workers=_FETCH_WORKERS, start_rate=_FETCH_START_RATE,
            on_progress=_prog, stop_flag=stop_flag)
        if fetched is None:                          # user stopped
            if on_log:
                on_log("Stopped by user.")
            return []
        elapsed = time.monotonic() - fetch_start
        if limiter.hits and on_log:
            on_log(f"Schwab rate-limited {limiter.hits}× — auto-throttled to "
                   f"~{limiter.rate:.0f} req/s (recovers automatically).")

    # ── Merge reused + freshly fetched closes; persist the store ──────────────
    # Stored closes run through yesterday; the live bar is appended at evaluation,
    # so one fewer stored close is needed for a full window.
    histories: dict[str, list] = dict(fresh)
    for sym, (closes, last_date) in fetched.items():
        if len(closes) >= bb_period + 1:
            histories[sym] = closes
            if last_date:
                store[sym] = {"last": last_date, "closes": closes}
    _save_history_store(store, store_path)

    if on_log:
        rate = f" ({len(stale) / max(elapsed, 1e-3):.1f} fetched/s)" if stale else ""
        on_log(f"Pass 2 done — {len(histories)} histories "
               f"({len(fresh)} reused, {len(fetched)} fetched) in {_fmt_elapsed(elapsed)}{rate}")

    # ── Apply RSI / BB% filter in memory ──────────────────────────────────────
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
    history_store_file: str | Path = HISTORY_STORE_FILE,
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
        history_store_file=history_store_file,
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

    # Weekly-vs-monthly is a stable per-symbol property, cached across scans
    # (build_universe clears it). Drop known monthly-only names up front so we
    # never even fetch their chains; the rest are fetched and scanned below.
    weekly_flags = _load_weeklies_cache() if weeklies_only else {}
    weekly_dirty = False
    iv_store = _load_iv_store()          # self-calibrating near-ATM IV history
    iv_dirty = False
    if weeklies_only:
        n_before = len(candidates)
        candidates = [r for r in candidates if weekly_flags.get(r["symbol"]) is not False]
        skipped = n_before - len(candidates)
        if skipped and on_log:
            on_log(f"  Skipped {skipped} known monthly-only names (cached); "
                   f"fetching {len(candidates)} with weeklies.")
    total = len(candidates)

    # One Schwab call per symbol covers every expiration through the horizon;
    # fetch all candidates concurrently behind the shared self-tuning limiter.
    base_horizon = requested_exp if requested_exp is not None else today + timedelta(days=dte_max)
    # Symbols whose weekly status is still unknown need a wider chain so
    # _has_weeklies can read Friday-to-Friday spacing; otherwise targeting the
    # nearest expiration truncates the chain to a single date — e.g. the Thu 7/2
    # expiration when 7/3 is the observed Independence Day holiday — and weekly
    # names get wrongly rejected. Cached names just need the scan window.
    wide_horizon = max(base_horizon, today + timedelta(days=_WEEKLIES_HORIZON_DAYS))

    def _horizon_for(sym: str) -> date:
        if weeklies_only and sym not in weekly_flags:
            return wide_horizon
        return base_horizon

    fetch_start = time.monotonic()
    fetched, limiter = _concurrent_fetch(
        [row["symbol"] for row in candidates],
        lambda sym: _fetch_schwab_chain(client, sym, right, _horizon_for(sym)),
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
        if weeklies_only:
            has_w = weekly_flags.get(sym)
            if has_w is None:                       # first time we've seen this symbol
                has_w = _has_weeklies(available, today)
                weekly_flags[sym] = has_w
                weekly_dirty = True
            if not has_w:
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

        sym_iv_pctile = None            # per-symbol IV percentile (recorded once, nearest exp)
        sym_iv_recorded = False
        for exp in expirations:
            full_chain = chains[exp]
            if price_col not in full_chain.columns:
                continue
            dte = (exp - today).days

            # Near-ATM IV (from the full chain, before yield filtering): the
            # underlying's expected move σ and its IV level for grading/percentile.
            atm_iv    = _atm_iv(full_chain, stock_price)
            sigma_pct = _period_sigma_pct(atm_iv, dte) if atm_iv else None
            if atm_iv and not sym_iv_recorded:
                sym_iv_pctile = _iv_percentile(iv_store, sym, atm_iv)  # vs prior readings
                _record_iv(iv_store, sym, atm_iv, today)
                iv_dirty = True
                sym_iv_recorded = True

            chain = full_chain.copy()
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

                # Cushion in σ at this strike: OTM% / the underlying's expected move.
                cushion_sigma = (round(otm_pct / sigma_pct, 2)
                                 if sigma_pct and otm_pct is not None else None)

                results.append({
                    "symbol":     sym,
                    "expiration": exp.strftime("%Y-%m-%d"),
                    "dte":        dte,
                    "strike":     strike,
                    "otm_pct":    otm_pct,
                    "premium":    round(float(opt[price_col]), 2),
                    "yield_pct":  round(float(opt["yield_pct"]) * 100, 2),
                    "delta":      delta,
                    "iv":            round(atm_iv, 1) if atm_iv else None,
                    "sigma_pct":     round(sigma_pct, 2) if sigma_pct else None,
                    "cushion_sigma": cushion_sigma,
                    "iv_pctile":     sym_iv_pctile,
                })

    if weekly_dirty:
        _save_weeklies_cache(weekly_flags)
    if iv_dirty:
        _save_iv_store(iv_store)
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
