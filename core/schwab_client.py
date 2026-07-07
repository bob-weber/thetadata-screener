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

import json
import os
import webbrowser
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


def _read_token_file() -> dict:
    with open(SCHWAB_TOKEN_FILE) as f:
        return json.load(f)


def _write_token_file(token: dict, *args, **kwargs) -> None:
    """Persist the (metadata-wrapped) token atomically, never losing the refresh
    token. Schwab's ~7-day refresh token must survive every access-token refresh;
    if a refresh response ever omits it, carry forward the one already on disk.
    Temp-file + rename keeps a concurrent scan's refreshes from corrupting it.
    """
    inner = token.get("token") if isinstance(token.get("token"), dict) else None
    if inner is not None and not inner.get("refresh_token"):
        try:
            prev_rt = (_read_token_file().get("token") or {}).get("refresh_token")
            if prev_rt:
                inner["refresh_token"] = prev_rt
        except Exception:
            pass
    tmp = f"{SCHWAB_TOKEN_FILE}.tmp"
    with open(tmp, "w") as f:
        json.dump(token, f)
    os.replace(tmp, SCHWAB_TOKEN_FILE)


def get_client(*, interactive: bool = True, force_login: bool = False) -> Client:
    """Return an authenticated read-only Schwab client.

    Uses the cached token when present (schwab-py auto-refreshes the 30-minute
    access token from the refresh token). With no usable token and
    ``interactive=True``, runs the browser login; with ``interactive=False`` it
    raises instead of blocking on stdin — use that from the GUI, where a token
    must already exist. ``force_login=True`` skips the cached token entirely and
    re-authenticates (used to refresh an expired/revoked refresh token).
    """
    creds = load_credentials()
    if force_login:
        if not interactive:
            raise RuntimeError("force_login requires an interactive session.")
        return login()
    if SCHWAB_TOKEN_FILE.exists():
        # Custom read/write funcs (vs client_from_token_file) so refreshes go
        # through _write_token_file, which preserves the refresh token.
        return auth.client_from_access_functions(
            creds["app_key"], creds["app_secret"],
            _read_token_file, _write_token_file)
    if not interactive:
        raise RuntimeError(
            f"No Schwab token at {SCHWAB_TOKEN_FILE}; run "
            "'python -m core.schwab_client login' once to authenticate."
        )
    return login()


def login() -> Client:
    """Run the interactive browser login and (over)write the token file.

    Always performs a fresh OAuth authorization — it never reuses the cached
    token — so it doubles as the weekly refresh once the refresh token expires;
    no need to delete the token file first. Builds the Schwab authorization URL,
    opens it in the default browser, and prints it prominently as a fallback,
    then exchanges the pasted redirect URL for a new token.

    A single auth context is used end to end because its ``state`` is validated
    when the redirect is exchanged.
    """
    creds = load_credentials()
    ctx = auth.get_auth_context(creds["app_key"], creds["callback_url"])
    url = ctx.authorization_url

    bar = "=" * 72
    print(f"\n{bar}\n  SCHWAB LOGIN — authorize this app in your browser\n{bar}\n")
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    print("A browser window was opened for you." if opened
          else "Could not open a browser automatically.")
    print("If it didn't open (or you're on another machine), copy this URL:\n")
    print(f"    {url}\n")
    print("STEP 1 — Log in with your schwab.com BROKERAGE credentials (the same")
    print("Login ID you use for the website / thinkorswim), then click Allow.")
    print("If Schwab says 'Invalid login ID or password', that's a REAL login")
    print("failure — fix the credentials (watch for stale autofill). It is the")
    print("only error that means something is wrong.\n")
    print(f"STEP 2 — Your browser then jumps to a '{creds['callback_url']}/...' address.")
    print("Depending on the browser it may show a connection error, a blank page,")
    print("or just flash by — all fine, there's no server there. What matters is")
    print("the ADDRESS BAR: copy the FULL 'https://127.0.0.1/...?code=...' URL.\n")

    received = input(
        "Paste the https://127.0.0.1/...?code=... address here, then press Enter> "
    ).strip()

    client = auth.client_from_received_url(
        creds["app_key"], creds["app_secret"], ctx, received, _write_token_file)

    # Schwab's Market Data Production API issues only a 1-hour access token —
    # no refresh token. Check what we got and warn the user accordingly.
    try:
        inner = _read_token_file().get("token") or {}
        saved_rt = inner.get("refresh_token")
        expires_in = int(inner.get("expires_in", 3600))
    except Exception:
        saved_rt = None
        expires_in = 3600

    if saved_rt:
        print(f"\nLogin successful. Refresh token saved — session lasts ~7 days.")
    else:
        import math
        hours = math.floor(expires_in / 3600)
        mins  = (expires_in % 3600) // 60
        dur   = f"{hours}h {mins}m" if hours else f"{mins}m"
        print(f"\nLogin successful. Access token expires in ~{dur}.")
        print("This app is registered as Market Data Production, which does not")
        print("issue refresh tokens. Re-run login once the session expires.")

    return client


# ── Data helpers (return parsed JSON, raise on HTTP error) ─────────────────────

try:  # authlib raises this when the refresh token is expired/revoked
    from authlib.integrations.base_client.errors import OAuthError as _OAuthError
except Exception:  # pragma: no cover — defensive if authlib's layout changes
    _OAuthError = ()

_AUTH_MARKERS = (
    "invalid_grant", "invalid, expired or revoked",
    "unsupported_token_type", "401 unauthorized",
)


def is_auth_error(exc: Exception) -> bool:
    """True if ``exc`` is a Schwab OAuth/authentication failure.

    Fires when the cached refresh token has expired or been revoked (needs a
    fresh ``python -m core.schwab_client login``). Matches by exception type and,
    defensively, by message text so it survives authlib internals changing.
    """
    if _OAuthError and isinstance(exc, _OAuthError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _AUTH_MARKERS)


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
    # `login` always forces a fresh browser auth (overwriting any stale token);
    # any other arg just uses/refreshes the cached token.
    client = login() if cmd == "login" else get_client(interactive=True)
    print(f"Authenticated. Token cached at {SCHWAB_TOKEN_FILE}.")

    if cmd == "login":
        # Quick read to prove the token works end to end.
        data = quotes(client, "AAPL")
        q = data.get("AAPL", {}).get("quote", {})
        print(f"AAPL last={q.get('lastPrice')} bid={q.get('bidPrice')} "
              f"ask={q.get('askPrice')}")
