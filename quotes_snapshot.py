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

Short PUTS that need a roll also get a "roll_target" (skip with --no-targets):
a put >= 5% ITM, or one expiring this week that is ITM or within 3% OTM, is
quoted at the same strike on the roll-band date (5-10% ITM -> 1 week past this
Friday, 10-15% -> 2, 15-20% -> 3, >20% -> 4 = the cap; expiring lines under
5% -> 1 week). The earliest listed expiry on or after that date (a holiday
Friday's Thursday counts) is used, so a strike missing from a monthly falls
through to the next weekly — one week at most. A target past the 4-week cap
(only possible that way) is flagged "past_cap". Lines already at or past
their band date get no target. Each target carries bid/ask/mark plus the roll
credit three ways: natural (new bid - old ask, worst case), mid (new mid - old
mid, ToS's default), and est (new bid - a realistic close cost: max(old mid,
intrinsic) when the old quote is sound — no one sells a put back below
intrinsic — else max(intrinsic, 0.02)). est is the limit to enter.
"old_quote_unreliable" flags a zero bid or a spread wider than half the mark —
e.g. a 0.00/1.00 placeholder on an expiring OTM put, which drags ToS's default
mid down. weeks_added counts Fridays (a Mon/Wed or holiday-Thursday expiry
belongs to its week's Friday), at least 1; min_credit is the
0.3%-of-strike-per-week marker for those weeks.

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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))       # main() also chdirs here; importing doesn't

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


# ── Roll targets for short puts ────────────────────────────────────────────────

MARKER = 0.003          # minimum roll credit: 0.3% of strike per week added
CAP_WEEKS = 4           # roll cap: 4 Fridays past this coming Friday
NEAR_OTM = 0.03         # expiring lines this close OTM still get a target


def _this_friday(today: date) -> date:
    return today + timedelta(days=(4 - today.weekday()) % 7)


def _band_weeks(pct_itm: float) -> int:
    """Weeks past this Friday the roll bands call for (% ITM as a fraction)."""
    if pct_itm < 0.10:
        return 1
    if pct_itm < 0.15:
        return 2
    if pct_itm < 0.20:
        return 3
    return CAP_WEEKS


def _needs_target(row: dict, friday: date) -> int | None:
    """Weeks past this Friday to quote, or None when no roll target is needed."""
    if row["type"] != "P" or row["underlying_last"] in (None, 0):
        return None
    price = row["underlying_last"]
    pct_itm = (row["strike"] - price) / price            # price-denominated
    expiry = date.fromisoformat(row["expiry"])
    if pct_itm >= 0.05:
        weeks = _band_weeks(pct_itm)
    elif expiry <= friday and pct_itm >= -NEAR_OTM:
        weeks = 1
    else:
        return None
    # Already at its band: any expiry in the band week counts (Mon/Wed weeklies,
    # a holiday Thursday), so compare the Friday of the expiry's week.
    if _this_friday(expiry) >= friday + timedelta(weeks=weeks):
        return None
    return weeks


def _unreliable(bid, ask, mark) -> bool:
    if not bid or ask is None or not mark:
        return True
    return (ask - bid) > 0.5 * mark


def _fetch_put(client, underlying: str, strike: float, earliest: date):
    """Earliest listed put at this strike expiring on/after `earliest` (≤ 1 wk later)."""
    resp = client.get_option_chain(
        underlying,
        contract_type=client.Options.ContractType.PUT,
        strike=strike,
        from_date=earliest,
        to_date=earliest + timedelta(days=8))     # band Thursday → next Friday
    resp.raise_for_status()
    best = None
    for key, strikes in resp.json().get("putExpDateMap", {}).items():
        exp = date.fromisoformat(key.split(":")[0])
        for k, contracts in strikes.items():
            if abs(float(k) - strike) < 1e-6 and contracts and exp >= earliest:
                if best is None or exp < best[0]:
                    best = (exp, contracts[0])
    return best


def _roll_target(client, row: dict, weeks: int, friday: date) -> dict:
    target_date = friday + timedelta(weeks=weeks)
    try:
        found = _fetch_put(client, row["underlying"], row["strike"],
                           target_date - timedelta(days=1))   # holiday Thursday
    except Exception as exc:
        if is_auth_error(exc):
            raise
        return {"error": str(exc)[:200]}
    if not found:
        return {"error": f"no {row['strike']:g}P listed within a week of {target_date}"}
    exp, c = found
    old_exp = date.fromisoformat(row["expiry"])
    if exp <= old_exp:                                   # never "roll" into itself
        return {"error": f"no {row['strike']:g}P listed after {old_exp} near {target_date}"}
    bid, ask, mark = c.get("bid"), c.get("ask"), c.get("mark")
    o_bid, o_ask, o_mark = row["bid"], row["ask"], row["mark"]
    bad_old = _unreliable(o_bid, o_ask, o_mark)
    intrinsic = row["intrinsic"] or 0.0
    # Nobody sells a put back below intrinsic, so a mid under it understates the close.
    close_est = max(intrinsic, 0.02) if bad_old else max(o_mark, intrinsic)
    weeks_added = max(1, (_this_friday(exp) - _this_friday(old_exp)).days // 7)

    def r(x):
        return None if x is None else round(x, 2)

    return {
        "symbol": c.get("symbol"), "expiry": exp.isoformat(),
        "weeks_added": weeks_added,
        "bid": bid, "ask": ask, "mark": mark,
        "credit_natural": r(bid - o_ask) if None not in (bid, o_ask) else None,
        "credit_mid": r(mark - o_mark) if None not in (mark, o_mark) else None,
        "credit_est": r(bid - close_est) if bid is not None else None,
        "old_quote_unreliable": bad_old,
        "past_cap": exp > friday + timedelta(weeks=CAP_WEEKS),
        "min_credit": round(MARKER * row["strike"] * weeks_added, 3),
    }


def add_roll_targets(client, options: list[dict], today: date) -> None:
    """Attach "roll_target" to each short put needing a roll, in place.

    One chain call per (underlying, strike, expiry, weeks); accounts holding the
    same line share the result. Auth errors propagate; others land in the target.
    """
    friday = _this_friday(today)
    cache: dict = {}
    for row in options:
        weeks = _needs_target(row, friday)
        if weeks is None:
            continue
        key = (row["underlying"], row["strike"], row["expiry"], weeks)
        if key not in cache:
            cache[key] = _roll_target(client, row, weeks, friday)
        row["roll_target"] = cache[key]


def main() -> int:
    os.chdir(ROOT)                  # creds/token paths are repo-relative
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("symbols", nargs="*", help="extra tickers")
    ap.add_argument("-f", "--file", action="append", default=[],
                    help="ticker file (repeatable)")
    ap.add_argument("-o", "--out", default=str(ROOT / "quotes_snapshot.json"),
                    help="output file (default: quotes_snapshot.json in the repo folder)")
    ap.add_argument("--stdout", action="store_true", help="print the JSON instead")
    ap.add_argument("--no-targets", action="store_true",
                    help="skip roll-target quotes for short puts")
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

    if not args.no_targets:
        try:
            add_roll_targets(client, result["options"], today)
        except Exception as exc:
            if is_auth_error(exc):
                print(f"Schwab login expired — from {ROOT} run:\n"
                      f"  {_python_cmd()} -m core.schwab_client login",
                      file=sys.stderr)
                return 2
            raise

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
