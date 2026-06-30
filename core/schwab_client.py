"""Schwab Market Data API client (read-only).

Thin wrapper over schwab-py for the screener's pricing needs: stock quotes,
option chains, and price history. Authentication is OAuth 2.0; the access token
is cached in ``SCHWAB_TOKEN_FILE`` and auto-refreshed by schwab-py. The refresh
token expires after ~7 days — once it does, the next API call fails with an auth
error and you re-run the interactive login (``python -m core.schwab_client login``,
or delete the token file and call ``get_client()``).

Credentials live in ``SCHWAB_CREDS_FILE`` (gitignored), as key=value lines:

    app_key=YOUR_APP_KEY
    app_secret=YOUR_APP_SECRET
    callback_url=https://127.0.0.1   # optional; this is the default

The app is registered for Market Data Production only, so these tokens cannot
touch accounts or place trades — read-only by construction.
"""

from __future__ import annotations

from pathlib import Path

from schwab import auth
from schwab.client import Client

SCHWAB_CREDS_FILE = Path("schwab_creds.txt")
SCHWAB_TOKEN_FILE = Path("schwab_token.json")
DEFAULT_CALLBACK  = "https://127.0.0.1"


# ── Credentials & client ──────────────────────────────────────────────────────

def load_credentials(path: Path = SCHWAB_CREDS_FILE) -> dict:
    """Read app_key / app_secret / callback_url from the gitignored creds file."""
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Create it with:\n"
            "  app_key=YOUR_APP_KEY\n"
            "  app_secret=YOUR_APP_SECRET\n"
            "  callback_url=https://127.0.0.1"
        )
    creds: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        creds[key.strip()] = val.strip()

    missing = {"app_key", "app_secret"} - creds.keys()
    if missing:
        raise ValueError(f"{path} is missing: {', '.join(sorted(missing))}")
    creds.setdefault("callback_url", DEFAULT_CALLBACK)
    return creds


def get_client(*, interactive: bool = True) -> Client:
    """Return an authenticated read-only Schwab client.

    Uses the cached token when present (schwab-py auto-refreshes the 30-minute
    access token from the refresh token). With no usable token and
    ``interactive=True``, runs the manual browser-paste login; with
    ``interactive=False`` it raises instead of blocking on stdin — use that from
    the GUI, where a token must already exist.
    """
    creds = load_credentials()
    if SCHWAB_TOKEN_FILE.exists():
        return auth.client_from_token_file(
            SCHWAB_TOKEN_FILE, creds["app_key"], creds["app_secret"])
    if not interactive:
        raise RuntimeError(
            f"No Schwab token at {SCHWAB_TOKEN_FILE}; run "
            "'python -m core.schwab_client login' once to authenticate."
        )
    # Manual flow: prints the authorize URL, then prompts for the pasted
    # https://127.0.0.1/?code=... redirect and writes the token file.
    return auth.client_from_manual_flow(
        creds["app_key"], creds["app_secret"],
        creds["callback_url"], SCHWAB_TOKEN_FILE)


# ── Data helpers (return parsed JSON, raise on HTTP error) ─────────────────────

def _json(resp):
    resp.raise_for_status()
    return resp.json()


def quotes(client: Client, symbols, *, fields=None) -> dict:
    """Quote(s) for one symbol or a list → {symbol: {quote, reference, ...}}."""
    if isinstance(symbols, str):
        symbols = [symbols]
    return _json(client.get_quotes(symbols, fields=fields))


def option_chain(client: Client, symbol: str, *,
                 contract_type=Client.Options.ContractType.ALL, **kwargs) -> dict:
    """Full option chain for a symbol (bid/ask, greeks, etc.).

    Extra schwab-py kwargs pass through: strike_count, strike_range, from_date,
    to_date, days_to_expiration, strategy, include_underlying_quote, ...
    """
    return _json(client.get_option_chain(
        symbol, contract_type=contract_type, **kwargs))


def price_history_daily(client: Client, symbol: str, *,
                        start=None, end=None) -> dict:
    """Daily candles for a symbol (start/end are datetimes; None → API default)."""
    return _json(client.get_price_history_every_day(
        symbol, start_datetime=start, end_datetime=end))


_INTRADAY_METHODS = {
    1:  "get_price_history_every_minute",
    5:  "get_price_history_every_five_minutes",
    10: "get_price_history_every_ten_minutes",
    15: "get_price_history_every_fifteen_minutes",
    30: "get_price_history_every_thirty_minutes",
}


def price_history_intraday(client: Client, symbol: str, *, minutes: int = 30,
                           start=None, end=None, extended_hours=False) -> dict:
    """Intraday candles at a 1/5/10/15/30-minute interval (start/end are datetimes)."""
    try:
        method = getattr(client, _INTRADAY_METHODS[minutes])
    except KeyError:
        raise ValueError(f"Unsupported intraday interval: {minutes} min "
                         f"(use one of {sorted(_INTRADAY_METHODS)})")
    return _json(method(symbol, start_datetime=start, end_datetime=end,
                        need_extended_hours_data=extended_hours))


# ── First-run login / smoke test ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    client = get_client(interactive=True)
    print(f"Authenticated. Token cached at {SCHWAB_TOKEN_FILE}.")

    if cmd == "login":
        # Quick read to prove the token works end to end.
        data = quotes(client, "AAPL")
        q = data.get("AAPL", {}).get("quote", {})
        print(f"AAPL last={q.get('lastPrice')} bid={q.get('bidPrice')} "
              f"ask={q.get('askPrice')}")
