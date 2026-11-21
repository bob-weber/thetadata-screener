#!/usr/bin/env python3
"""Dump Schwab stock quotes and open short options as JSON — for roll checks
and the morning brief.

Uses the same cached token as the screener (``core.schwab_client``) and never
prompts for login. Open positions come from Schwab's Accounts API (all
accounts the token can see), so there is no position list to maintain:

    <venv python> quotes_snapshot.py                      # → quotes_snapshot.json
    <venv python> quotes_snapshot.py MU UPST VST          # plus extra tickers
    <venv python> quotes_snapshot.py --stdout             # print instead of writing

Output goes to quotes_snapshot.json in the repo folder (overwritten each run;
-o picks another path). Attach that file to the chat.

<venv python> is gui-env/bin/python on Linux, venv\\Scripts\\python on Windows.

"quotes" holds every ticker given, every held stock, and every option's
underlying. "options" holds each SHORT option (long options such as LEAPs are
skipped) with intrinsic and time value:
put intrinsic = max(0, strike - underlying last), call = max(0, last - strike),
time_value = mark - intrinsic (not clamped; can dip below 0 on deep ITM lines).
If the positions call fails, the quotes are still written and the reason is
in "positions_error".

Symbol files: one or more tickers per line, '#' starts a comment.
Exit codes: 0 ok, 1 other failure, 2 Schwab login expired (re-run
``<venv python> -m core.schwab_client login`` from the repo root; the
message printed on exit 2 gives the exact command for this machine).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)                      # creds/token paths are repo-relative
sys.path.insert(0, str(ROOT))

from core.schwab_client import get_client, is_auth_error, quotes  # noqa: E402

BATCH = 250                         # same batch size the price screen uses

# Account names by the last 3 digits of the account number, as schwab.com
# shows them ("...576"). Unlisted accounts are reported as "...NNN".
ACCOUNT_NAMES = {"576": "IRA", "060": "LSO", "463": "Michelle LSO", "891": "Stocks"}
ACCOUNT_ORDER = list(ACCOUNT_NAMES.values())


def _read_symbols(paths: list[str]) -> list[str]:
    out: list[str] = []
    for p in paths:
        for line in Path(p).read_text().splitlines():
            out.extend(line.split("#", 1)[0].replace(",", " ").split())
    return out


def _ms_to_iso(ms) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _python_cmd() -> str:
    """This interpreter as a command that pastes cleanly into the user's shell.

    Relative to the repo root when the venv lives inside it (venv\\Scripts\\
    python.exe on Windows, gui-env/bin/python on Linux). A path with spaces is
    quoted, and on Windows prefixed with PowerShell's '&' call operator —
    without it PowerShell parses a quoted path as a string, not a command.
    """
    exe = Path(sys.executable)
    try:
        cmd = str(exe.relative_to(ROOT))
    except ValueError:
        cmd = str(exe)
    if " " in cmd:
        cmd = f'& "{cmd}"' if os.name == "nt" else f'"{cmd}"'
    return cmd


def _slim(entry: dict) -> dict:
    """Keep the fields a roll check / brief actually uses."""
    q = entry.get("quote", {})
    return {
        "last":          q.get("lastPrice"),               # incl. extended hours
        "regular_last":  q.get("regularMarketLastPrice"),  # regular session only
        "bid":           q.get("bidPrice"),
        "ask":           q.get("askPrice"),
        "mark":          q.get("mark"),
        "prev_close":    q.get("closePrice"),
        "change_pct":    q.get("netPercentChange"),
        "high_52w":      q.get("52WeekHigh"),
        "low_52w":       q.get("52WeekLow"),
        "volume":        q.get("totalVolume"),
        "quote_time":    _ms_to_iso(q.get("quoteTime")),
    }


# ── Positions (Accounts API) ───────────────────────────────────────────────────

def _parse_occ(sym: str) -> tuple[str, date, str, float]:
    """'ASTS  261120P00027000' → ('ASTS', 2026-11-20, 'P', 27.0)."""
    return (sym[:6].strip(),
            datetime.strptime(sym[6:12], "%y%m%d").date(),
            sym[12],
            int(sym[13:]) / 1000)


def _fetch_positions(client) -> tuple[list[str], list[dict]]:
    """Held stock tickers and short option positions across every account."""
    resp = client.get_accounts(fields=[client.Account.Fields.POSITIONS])
    resp.raise_for_status()
    stocks: list[str] = []
    options: list[dict] = []
    for acct in resp.json():
        sa = acct.get("securitiesAccount", {})
        num = str(sa.get("accountNumber", ""))
        name = ACCOUNT_NAMES.get(num[-3:], f"...{num[-3:]}")
        for p in sa.get("positions", []):
            inst = p.get("instrument", {})
            kind = inst.get("assetType")
            if kind in ("EQUITY", "COLLECTIVE_INVESTMENT") and p.get("longQuantity", 0) > 0:
                stocks.append(inst["symbol"])
            elif kind == "OPTION" and p.get("shortQuantity", 0) > 0:
                sym = inst["symbol"]
                und, expiry, cp, strike = _parse_occ(sym)
                options.append({
                    "account": name, "symbol": sym,
                    "underlying": inst.get("underlyingSymbol") or und,
                    "type": cp, "strike": strike, "expiry": expiry,
                    "qty": -int(p["shortQuantity"]),
                })
    rank = {n: i for i, n in enumerate(ACCOUNT_ORDER)}
    options.sort(key=lambda o: (rank.get(o["account"], 99), o["account"],
                                o["expiry"], o["underlying"], o["strike"]))
    return stocks, options


def _option_row(pos: dict, oq: dict | None, und_last, today: date) -> dict:
    q = (oq or {}).get("quote", {})
    bid, ask, mark = q.get("bidPrice"), q.get("askPrice"), q.get("mark")
    intrinsic = time_value = None
    if und_last is not None:
        diff = pos["strike"] - und_last if pos["type"] == "P" else und_last - pos["strike"]
        intrinsic = round(max(0.0, diff), 4)
        if mark is not None:
            time_value = round(mark - intrinsic, 4)
    return {
        "account": pos["account"], "symbol": pos["symbol"],
        "underlying": pos["underlying"], "type": pos["type"],
        "strike": pos["strike"], "expiry": pos["expiry"].isoformat(),
        "qty": pos["qty"], "bid": bid, "ask": ask, "mark": mark,
        "underlying_last": und_last, "intrinsic": intrinsic,
        "time_value": time_value, "dte": (pos["expiry"] - today).days,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", help="extra tickers")
    ap.add_argument("-f", "--file", action="append", default=[],
                    help="ticker file (repeatable)")
    ap.add_argument("-o", "--out", default=str(ROOT / "quotes_snapshot.json"),
                    help="output file (default: quotes_snapshot.json in the repo folder)")
    ap.add_argument("--stdout", action="store_true", help="print the JSON instead")
    args = ap.parse_args()

    result = {"as_of": datetime.now(timezone.utc).isoformat(),
              "quotes": {}, "missing": [], "options": []}
    try:
        client = get_client(interactive=False)
        try:
            held, positions = _fetch_positions(client)
        except Exception as exc:
            if is_auth_error(exc):
                raise
            held, positions = [], []
            result["positions_error"] = str(exc)[:300]
            print(f"Positions fetch failed, quotes only: {exc}", file=sys.stderr)

        # Stocks: explicit tickers, held shares, every option underlying —
        # upper-cased, de-duplicated, first-seen order.
        stocks = list(dict.fromkeys(s.upper() for s in
                                    args.symbols + _read_symbols(args.file) +
                                    held + [p["underlying"] for p in positions]))
        contracts = list(dict.fromkeys(p["symbol"] for p in positions))
        wanted = stocks + contracts
        if not wanted:
            print("No tickers given and no positions found.", file=sys.stderr)
            return 1
        raw: dict = {}
        for i in range(0, len(wanted), BATCH):
            raw.update(quotes(client, wanted[i:i + BATCH]))
    except Exception as exc:
        if is_auth_error(exc) or "No Schwab token" in str(exc):
            print(f"Schwab login expired — from {ROOT} run:\n"
                  f"  {_python_cmd()} -m core.schwab_client login",
                  file=sys.stderr)
            return 2
        print(f"Quote fetch failed: {exc}", file=sys.stderr)
        return 1

    for s in stocks:
        if s in raw and "quote" in raw[s]:
            result["quotes"][s] = _slim(raw[s])
        else:
            result["missing"].append(s)

    today = date.today()
    for pos in positions:
        oq = raw.get(pos["symbol"])
        if not (oq and "quote" in oq) and pos["symbol"] not in result["missing"]:
            result["missing"].append(pos["symbol"])
        und_last = result["quotes"].get(pos["underlying"], {}).get("last")
        result["options"].append(_option_row(pos, oq, und_last, today))

    text = json.dumps(result, indent=1)
    if args.stdout:
        print(text)
    else:
        tmp = f"{args.out}.tmp"
        Path(tmp).write_text(text)
        os.replace(tmp, args.out)
        accts = sorted({o["account"] for o in result["options"]})
        print(f"Wrote {args.out}: {len(result['quotes'])} stocks, "
              f"{len(result['options'])} short options ({', '.join(accts) or 'none'})"
              + (f", MISSING {len(result['missing'])}" if result["missing"] else "")
              + (", POSITIONS ERROR" if "positions_error" in result else ""),
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
