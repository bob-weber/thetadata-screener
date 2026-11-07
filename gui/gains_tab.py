import csv
import io
import json
import re
from collections import Counter
from datetime import datetime, date, timedelta
from pathlib import Path

import matplotlib
matplotlib.use("QtAgg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QCheckBox,
)

# Time-range presets: (label, value). int → days back from last data point;
# None → all time; "custom" → use the From/To date pickers.
_RANGE_OPTIONS = [
    ("1 Week",   7),
    ("3 Weeks",  21),
    ("3 Months", 91),
    ("1 Year",   365),
    ("All Time", None),
    ("Custom…",  "custom"),
]

from .account_store import events_path, gains_path, list_accounts

_DEPOSIT_TYPES    = {"CRC"}
_WITHDRAWAL_TYPES = {"CDB"}
_CB_SECTION_STOPS = {"Account Trade History", "Profits and Losses",
                     "Account Summary", "Futures Statements", "Forex Statements"}

# Direct stock trade in the TRD rows, e.g. "BOT +20 IBM @221.50"
_STK_TRD_RE = re.compile(
    r'^(BOT|SOLD)\s+([+-]?\d+(?:\.\d+)?)\s+([A-Z]+)\s+@', re.IGNORECASE)
# Stock assignment/exercise in the EXP rows, e.g. "BOT 100.0 SPXU UPON ..."
# A called-away sale is signed: "SOLD -400.0 GAP UPON ...".
_STK_EXP_RE = re.compile(
    r'^(BOT|SOLD)\s+([+-]?\d+(?:\.\d+)?)\s+([A-Z]+)\s+UPON', re.IGNORECASE)


def _parse_amount(raw: str) -> float | None:
    s = raw.strip().replace(",", "").replace("$", "")
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        val = float(s)
        return -val if negative else val
    except ValueError:
        return None


def _parse_date(raw: str) -> date | None:
    for fmt in ("%m/%d/%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _clean_ref(raw: str) -> str:
    s = raw.strip().strip('"')
    if s.startswith('="'):
        s = s[2:].rstrip('"')
    return s


def _is_option(desc: str) -> bool:
    return bool(re.search(r'\b(PUT|CALL)\b', desc, re.IGNORECASE))


def parse_statement(path: Path) -> dict:
    """
    Parse a TOS Account Statement CSV's Cash Balance section.
    Returns dict with deposits, balances, opt_trades, stk_trades.
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        raise ValueError(str(e))

    rows       = list(csv.reader(io.StringIO(text)))
    deposits:   list[dict] = []
    balances:   list[dict] = []
    opt_trades: list[dict] = []
    stk_trades: list[dict] = []
    opening:    dict | None = None   # cash before the statement's first row
    day_balance: dict[str, float] = {}
    in_cb = False

    for row in rows:
        cells = [c.strip().strip('"') for c in row]
        if not cells:
            continue
        first = cells[0]

        if first == "Cash Balance":
            in_cb = True
            continue
        if first in _CB_SECTION_STOPS:
            break
        if not in_cb or len(cells) < 9:
            continue

        row_date = _parse_date(first)
        if row_date is None:
            continue

        row_type = cells[2]
        amount   = _parse_amount(cells[7])
        balance  = _parse_amount(cells[8])
        # Misc Fees and Commissions & Fees sit beside AMOUNT, not inside it:
        # the row's cash effect is all three (they're negative).
        fees     = round((_parse_amount(cells[5]) or 0.0)
                         + (_parse_amount(cells[6]) or 0.0), 2)
        ref      = _clean_ref(cells[3]) if len(cells) > 3 else ""
        desc     = cells[4] if len(cells) > 4 else ""
        diso     = row_date.isoformat()

        # Every row carries the running balance, so the first one tells what
        # the account held before anything on this statement happened.
        if opening is None and balance is not None:
            opening = {"date": diso,
                       "balance": round(balance - (amount or 0.0) - fees, 2)}

        # End-of-day cash: the last running balance of each date (rows are
        # in time order). It's what capital in is measured from.
        if balance is not None:
            day_balance[diso] = balance

        # Separate from the balance capture above: every row has a balance,
        # so chaining these onto it would skip every trade.
        if row_type in _DEPOSIT_TYPES and amount is not None and amount > 0:
            deposits.append({"ref": ref, "date": diso, "amount": amount})

        elif row_type in _WITHDRAWAL_TYPES and amount is not None and amount < 0:
            deposits.append({"ref": ref, "date": diso, "amount": amount})

        elif row_type in ("TRD", "EXP") and (amount is not None or fees):
            # A fill at $0.00 (a zero-credit roll) prints a blank AMOUNT but
            # still pays fees.
            amount = amount or 0.0
            stk = _STK_TRD_RE.match(desc) or _STK_EXP_RE.match(desc)
            if row_type == "TRD" and _is_option(desc):
                opt_trades.append({"date": diso, "amount": amount, "fees": fees,
                                   "desc": desc})
            elif stk:
                action = stk.group(1).upper()
                shares = abs(float(stk.group(2)))
                symbol = stk.group(3).upper()
                stk_trades.append({
                    "date": diso, "action": action, "symbol": symbol,
                    "shares": shares, "amount": amount, "fees": fees, "desc": desc,
                })

    balances = [{"date": d, "balance": b} for d, b in sorted(day_balance.items())]
    return {"deposits": deposits, "balances": balances,
            "opt_trades": opt_trades, "stk_trades": stk_trades,
            "opening": opening}


def _blank_history() -> dict:
    return {"deposits": [], "balances": [], "opt_trades": [], "stk_trades": []}


def _load_history(path: Path) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text())
            for k in ("deposits", "balances", "opt_trades", "stk_trades"):
                data.setdefault(k, [])
            _drop_split_roll_duplicates(data["opt_trades"])
            return data
        except Exception:
            pass
    return _blank_history()


def _save_history(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


def _merge(existing: list[dict], new: list[dict], key_fn) -> int:
    """Merge new items into existing in place, dedup by key_fn. Returns count added."""
    seen = {key_fn(e) for e in existing}
    added = 0
    for e in new:
        k = key_fn(e)
        if k not in seen:
            existing.append(e)
            seen.add(k)
            added += 1
    existing.sort(key=lambda e: e["date"])
    return added


def _merge_counted(existing: list[dict], new: list[dict], key_fn) -> int:
    """Merge by multiplicity rather than presence. Returns count added.

    One order can fill in pieces that share a date, description and amount —
    "SOLD -2 CALENDAR RUN ... @.33" twice is two fills, not a repeat. A
    statement is complete for its date range, so every fill it lists is kept,
    while an overlapping statement (which repeats fills already stored) only
    tops the count up to the higher of the two instead of double-counting.
    """
    have = Counter(key_fn(e) for e in existing)
    fills: dict = {}
    for e in new:
        fills.setdefault(key_fn(e), []).append(e)

    added = 0
    for k, rows in fills.items():
        # Each missing fill is the statement's own row: identical fills can
        # differ in fees, so copying the first would misstate the rest.
        for e in rows[have.get(k, 0):]:
            existing.append(dict(e))
            added += 1
    existing.sort(key=lambda e: e["date"])
    return added


def _backfill_fees(existing: list[dict], new: list[dict], key_fn) -> None:
    """Give stored trades the statement's fees, fill for fill.

    The merge key (date, description, amount) leaves fees out, so a re-import
    can't correct them by itself: each stored trade takes the fees of its twin
    in the statement — filling them in for trades stored before fees were
    recorded, and repairing identical fills that were stored with the first
    one's fees.
    """
    pending: dict = {}
    for e in new:
        pending.setdefault(key_fn(e), []).append(e.get("fees", 0.0))
    for e in existing:
        fees = pending.get(key_fn(e))
        if fees:
            e["fees"] = fees.pop(0)


def _merge_balances(existing: list[dict], new: list[dict]) -> int:
    """End-of-day balances by date; the incoming statement's value wins (a
    statement pulled mid-day can hold a balance the day later moved past)."""
    by_date = {b["date"]: b for b in existing}
    added = sum(1 for b in new if b["date"] not in by_date)
    by_date.update({b["date"]: b for b in new})
    existing[:] = sorted(by_date.values(), key=lambda b: b["date"])
    return added


def merge_statement(data: dict, parsed: dict) -> dict:
    """Merge a parsed statement into accumulated history. Returns counts added."""
    trade_key = lambda e: (e["date"], e["desc"], e["amount"])
    # The earliest statement's opening balance is the account's starting cash.
    op = parsed.get("opening")
    if op and (not data.get("opening") or op["date"] < data["opening"]["date"]):
        data["opening"] = op
    _backfill_fees(data["opt_trades"], parsed["opt_trades"], trade_key)
    _backfill_fees(data["stk_trades"], parsed["stk_trades"], trade_key)
    counts = {
        "deposits":   _merge(data["deposits"],   parsed["deposits"],
                             lambda e: e["ref"] or (e["date"], e["amount"])),
        "balances":   _merge_balances(data["balances"], parsed["balances"]),
        "opt_trades": _merge_counted(data["opt_trades"], parsed["opt_trades"],
                                     lambda e: (e["date"], e["desc"], e["amount"])),
        "stk_trades": _merge_counted(data["stk_trades"], parsed["stk_trades"],
                                     lambda e: (e["date"], e["desc"], e["amount"])),
    }
    # A roll shown as one row here and as two legs in an overlapping
    # statement merges as two different trades.
    counts["opt_trades"] -= _drop_split_roll_duplicates(data["opt_trades"])
    return counts


_PRICE_CACHE: dict[tuple, dict] = {}   # (symbol, start, end) → closes


def _fetch_price_history(symbols: list[str], start: date, end: date) -> dict:
    """Return {symbol: {iso_date: close}} via yfinance, cached for the session.

    Every redraw (a checkbox, the range selector, an account switch) used to
    download each symbol's whole history again on the GUI thread.
    """
    if not symbols:
        return {}
    missing = [s for s in symbols if (s, start, end) not in _PRICE_CACHE]
    if missing:
        fetched = _download_price_history(missing, start, end)
        for s in missing:
            _PRICE_CACHE[(s, start, end)] = fetched.get(s, {})
    return {s: _PRICE_CACHE[(s, start, end)] for s in symbols
            if _PRICE_CACHE.get((s, start, end))}


def _download_price_history(symbols: list[str], start: date, end: date) -> dict:
    """{symbol: {iso_date: close}} straight from yfinance."""
    try:
        import yfinance as yf
    except Exception:
        return {}
    out = {}
    for sym in symbols:
        try:
            hist = yf.Ticker(sym).history(
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
            )
        except Exception:
            continue
        if hist.empty:
            continue
        out[sym] = {ts.date().isoformat(): float(r["Close"])
                    for ts, r in hist.iterrows()}
    return out


def _price_on(hist: dict, date_str: str) -> float | None:
    """Most recent close on or before date_str."""
    candidates = [d for d in hist if d <= date_str]
    return hist[max(candidates)] if candidates else None


def _account_transfers(acct: str) -> tuple[list[dict], list[dict]]:
    """(transfers in, transfers out) for one account, from its positions data.

    The statement records no row when shares move between accounts, so the
    trades alone leave them held in the old account forever and sold from the
    new one with no cost at all. The Positions tab squares each account with
    the holdings its newest statement lists; this reuses that.
    """
    from .positions_tab import holdings_adjustments
    try:
        d = json.loads(events_path(acct).read_text())
    except Exception:
        return [], []
    return holdings_adjustments(d.get("events", []), d.get("holdings"))


def _iso(mdy: str) -> str:
    return datetime.strptime(mdy, "%m/%d/%Y").date().isoformat()


def with_fees(data: dict) -> dict:
    """``data`` with each trade's fees folded into its amount (in memory).

    Stored amounts stay as the statement prints them — they're part of the
    merge key — so every calculation works on this net view instead.
    """
    def net(ts):
        return [{**t, "amount": t["amount"] + t.get("fees", 0.0)} for t in ts]
    return {**data, "opt_trades": net(data.get("opt_trades", [])),
            "stk_trades": net(data.get("stk_trades", []))}


def prepared(data: dict, acct: str | None) -> dict:
    """What the charts calculate on: fees netted in, transfers applied."""
    return with_transfers(with_fees(data), acct)


def with_transfers(data: dict, acct: str | None) -> dict:
    """``data`` with share transfers between accounts added as stock trades.

    In memory only — the stored history is never changed. A transfer moves
    shares at cost: in, it gives them a basis here; out, it removes them at
    average cost without realizing anything. A sale of shares this account
    never held (moved in, then sold before the newest statement could list
    them — STRK in the Stocks account) takes its cost from the matching
    transfer out of another account, so the realized gain is the real one
    rather than the whole sale proceeds.
    """
    if not acct:
        return data
    seeded, moved = _account_transfers(acct)
    ins  = [{"date": _iso(e["date"]), "action": "BOT", "symbol": e["sym"],
             "shares": e["shares"], "amount": e["amount"],
             "desc": "transfer in", "transfer": True} for e in seeded]
    outs = [{"date": _iso(e["date"]), "action": "SOLD", "symbol": e["sym"],
             "shares": e["shares"], "amount": e.get("cost", 0.0),
             "desc": "transfer out", "transfer": True} for e in moved]

    # Sales the account can't cover from what it held.
    held: dict[str, float] = {}
    others = None
    for t in sorted(ins + data.get("stk_trades", []) + outs, key=lambda t: t["date"]):
        sym = t["symbol"]
        sh  = held.get(sym, 0.0)
        if t["action"] == "BOT":
            held[sym] = sh + t["shares"]
            continue
        short = t["shares"] - sh
        if not t.get("transfer") and short > 0.0001:
            if others is None:
                others = [dict(e) for a in list_accounts() if a != acct
                          for e in _account_transfers(a)[1]]
            for lot in others:
                if short <= 0.0001:
                    break
                if lot["sym"] != sym or lot["shares"] <= 0.0001:
                    continue
                n = min(short, lot["shares"])
                per = lot.get("cost", 0.0) / lot["shares"] if lot["shares"] else 0.0
                d = min(_iso(lot["date"]), t["date"])
                ins.append({"date": d, "action": "BOT", "symbol": sym, "shares": n,
                            "amount": -round(per * n, 2),
                            "desc": "transfer in", "transfer": True})
                lot["cost"]   = lot.get("cost", 0.0) - per * n
                lot["shares"] -= n
                short -= n
                sh    += n
        held[sym] = max(sh - t["shares"], 0.0)

    if not ins and not outs:
        return data
    # Ins first and outs last, so on a shared date the shares arrive before
    # anything is sold from them and leave only after the day's own trades.
    return {**data, "stk_trades": ins + data.get("stk_trades", []) + outs}


def _capital_flows(data: dict) -> list[dict]:
    """Everything that put capital into the account or took it out.

    Deposits and withdrawals, plus two things TOS doesn't write as either:
    the cash the account already held before its earliest imported statement
    (an IRA rollover, money moved in from another account), and shares moved
    between accounts — counted at cost, in for the account receiving them and
    out for the one giving them up, so each account's RoR is measured on what
    it actually had to work with.
    """
    flows = list(data.get("deposits", []))
    op = data.get("opening")
    if op and abs(op.get("balance", 0.0)) > 0.005:
        flows.append({"date": op["date"], "amount": op["balance"], "kind": "opening"})
    for t in data.get("stk_trades", []):
        if t.get("transfer"):
            amt = abs(t["amount"])
            flows.append({"date": t["date"], "kind": "transfer",
                          "amount": amt if t["action"] == "BOT" else -amt})
    return flows


def reconstruct(data: dict, with_unrealized: bool = True) -> dict:
    """
    Walk trades chronologically with average-cost basis.
    Returns time series aligned to balance dates:
      dates, deposits, realized, total, and final summary values.

    Pass ``with_unrealized=False`` to skip the (networked) yfinance price
    fetch when only realized P&L is needed; ``total`` then equals ``realized``.
    """
    balances = sorted(data["balances"], key=lambda b: b["date"])

    # Combine and sort all trades chronologically
    opt = [{**t, "kind": "opt"} for t in data["opt_trades"]]
    stk = [{**t, "kind": "stk"} for t in data["stk_trades"]]
    trades = sorted(opt + stk, key=lambda t: t["date"])

    deposits = sorted(_capital_flows(data), key=lambda d: d["date"])

    # Timeline = every date that has an event (balance, trade, or deposit), so a
    # trading day still plots even when its end-of-day BAL row hasn't posted yet.
    timeline = sorted({b["date"] for b in balances}
                      | {t["date"] for t in trades}
                      | {d["date"] for d in deposits})
    if not timeline:
        return {}

    start_d = date.fromisoformat(timeline[0])
    end_d   = date.fromisoformat(timeline[-1])

    # All symbols ever traded → fetch price history once (skipped if realized-only)
    if with_unrealized:
        symbols = sorted({t["symbol"] for t in data["stk_trades"]})
        price_hist = _fetch_price_history(symbols, start_d, end_d)
    else:
        price_hist = {}

    # Capital in. The statement's deposit rows are incomplete — a full-history
    # export omits them, and transfers between accounts never appear — but
    # every row's running balance is exact. So with balances, capital in up
    # to a day is that day's cash less all trade cash so far (premiums, stock
    # bought and sold, fees): whatever is left came from outside. Shares moved
    # in or out at cost are added on top. Without balances (an old history),
    # it falls back to the deposit rows plus the opening balance.
    bals = sorted(balances, key=lambda b: b["date"])
    use_balances = bool(bals)
    opening = (data.get("opening") or {}).get("balance")
    bi = 0
    last_bal = opening
    cum_cash = 0.0
    cum_moved = 0.0

    holdings: dict[str, list[float]] = {}   # symbol → [shares, total_cost]
    cum_opt = 0.0
    cum_stk_realized = 0.0
    ti = 0   # trade index
    di = 0   # deposit index
    dep_cum = 0.0

    dep_series, realized_series, total_series = [], [], []
    fees_series: list[float] = []
    cum_fees = 0.0   # already inside realized; tracked so it can be shown

    for bd in timeline:
        # advance deposits
        while di < len(deposits) and deposits[di]["date"] <= bd:
            dep_cum += deposits[di]["amount"]
            di += 1
        # advance trades
        while ti < len(trades) and trades[ti]["date"] <= bd:
            t = trades[ti]
            cum_fees += -t.get("fees", 0.0)
            if t.get("transfer"):
                cum_moved += abs(t["amount"]) * (1 if t["action"] == "BOT" else -1)
            else:
                cum_cash += t["amount"]
            if t["kind"] == "opt":
                cum_opt += t["amount"]
            else:
                sym = t["symbol"]
                sh, tc = holdings.get(sym, [0.0, 0.0])
                if t["action"] == "BOT":
                    sh += t["shares"]
                    tc += abs(t["amount"])
                    holdings[sym] = [sh, tc]
                else:  # SOLD — realize against average cost
                    if sh > 0:
                        avg = tc / sh
                        q   = min(t["shares"], sh)
                        if not t.get("transfer"):      # moved, not sold
                            cum_stk_realized += t["amount"] - avg * q
                        sh -= q
                        tc -= avg * q
                        holdings[sym] = [sh, tc]
                    else:
                        # short / no basis — treat proceeds as realized
                        # (a transfer out moved shares; it realizes nothing)
                        if not t.get("transfer"):
                            cum_stk_realized += t["amount"]
            ti += 1

        realized = cum_opt + cum_stk_realized

        # unrealized = market value of current holdings − their cost
        unreal = 0.0
        for sym, (sh, tc) in holdings.items():
            if sh <= 0:
                continue
            p = _price_on(price_hist.get(sym, {}), bd)
            if p is not None:
                unreal += p * sh - tc

        if use_balances:
            while bi < len(bals) and bals[bi]["date"] <= bd:
                last_bal = bals[bi]["balance"]
                bi += 1
            if last_bal is not None:
                dep_cum = last_bal - cum_cash + cum_moved
        dep_series.append(dep_cum)
        fees_series.append(cum_fees)
        realized_series.append(realized)
        total_series.append(realized + unreal)

    return {
        "dates":    [datetime.fromisoformat(d) for d in timeline],
        "deposits": dep_series,
        "realized": realized_series,
        "total":    total_series,
        "fees":     fees_series,
    }


# Option leg description parsers (capture: action, qty, symbol, to-open
# expiration, strike, type). Calendar/diagonal rolls list the to-open
# expiration first, then /to-close; diagonals also carry a to-close strike
# (captured separately since a strike change means a different collateral key).
_OPT_CAL_RE = re.compile(
    r'\b(?P<action>SOLD|BOT)\s+(?P<qty>[+-]?\d+)\s+CALENDAR\s+(?P<symbol>\S+)\s+100.*?'
    r'(?P<exp>\d{1,2}\s+[A-Z]{3}\s+\d{2,4})/(?P<close_exp>\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'
    r'\s+(?P<strike>[\d.]+)\s+(?P<type>PUT|CALL)', re.IGNORECASE)
_OPT_DIAG_RE = re.compile(
    r'\b(?P<action>SOLD|BOT)\s+(?P<qty>[+-]?\d+)\s+DIAGONAL\s+(?P<symbol>\S+)\s+100.*?'
    r'(?P<exp>\d{1,2}\s+[A-Z]{3}\s+\d{2,4})/(?P<close_exp>\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'
    r'\s+(?P<strike>[\d.]+)/(?P<close_strike>[\d.]+)\s+(?P<type>PUT|CALL)', re.IGNORECASE)
_OPT_VERT_RE = re.compile(
    r'\b(?P<action>SOLD|BOT)\s+(?P<qty>[+-]?\d+)\s+VERTICAL\s+(?P<symbol>\S+)\s+100.*?'
    r'(?P<exp>\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'
    r'\s+(?P<strike>[\d.]+)/[\d.]+\s+(?P<type>PUT|CALL)', re.IGNORECASE)
_OPT_SINGLE_RE = re.compile(
    r'\b(?P<action>SOLD|BOT)\s+(?P<qty>[+-]?\d+)\s+(?P<symbol>\S+)\s+100.*?'
    r'(?P<exp>\d{1,2}\s+[A-Z]{3}\s+\d{2,4})\s+(?P<strike>[\d.]+)\s+(?P<type>PUT|CALL)', re.IGNORECASE)


def _parse_exp_date(s: str) -> date | None:
    for fmt in ("%d %b %y", "%d %b %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_opt_leg(desc: str) -> dict | None:
    m = _OPT_CAL_RE.search(desc)
    is_roll = m is not None
    if m is None:
        m = _OPT_DIAG_RE.search(desc)
        is_roll = m is not None
    if m is None:
        m = _OPT_VERT_RE.search(desc)
    if m is None:
        m = _OPT_SINGLE_RE.search(desc)
    if not m:
        return None
    exp = _parse_exp_date(m.group("exp"))
    if exp is None:
        return None
    leg = {
        "action":  m.group("action").upper(),
        "qty":     int(m.group("qty")),
        "symbol":  m.group("symbol").upper(),
        "exp":     exp,
        "strike":  float(m.group("strike")),
        "type":    m.group("type").upper(),
        "is_roll": is_roll,
    }
    groups = m.groupdict()
    if groups.get("close_strike"):
        leg["close_strike"] = float(groups["close_strike"])
    if groups.get("close_exp"):
        leg["close_exp"] = _parse_exp_date(groups["close_exp"])
    return leg


def _drop_split_roll_duplicates(opt_trades: list[dict]) -> int:
    """Remove leg pairs that repeat a CALENDAR/DIAGONAL row, in place.

    Statements don't agree on how to show a roll: one lists it as a single
    CALENDAR/DIAGONAL row with its net credit, an overlapping one lists the
    same order as its buy-to-close and sell-to-open legs. The descriptions
    differ, so the merge can't see they're the same order, and the roll is
    stored twice. A leg pair is dropped only when it matches a roll row's
    date, symbol, type, quantity, both expirations and both strikes, and its
    amounts add up to the roll's. Returns the number of rows removed.
    """
    singles: dict[tuple, list[int]] = {}
    combos: list[tuple[dict, dict]] = []
    for i, t in enumerate(opt_trades):
        leg = _parse_opt_leg(t.get("desc", ""))
        if not leg:
            continue
        if leg["is_roll"]:
            if leg.get("close_exp"):
                combos.append((t, leg))
        else:
            key = (t["date"], leg["symbol"], leg["type"], leg["action"],
                   leg["qty"], leg["exp"], leg["strike"])
            singles.setdefault(key, []).append(i)

    drop: set[int] = set()
    for t, leg in combos:
        # The roll's first-listed leg carries its action and quantity; the
        # to-close leg is the opposite side.
        opp = "BOT" if leg["action"] == "SOLD" else "SOLD"
        open_key = (t["date"], leg["symbol"], leg["type"], leg["action"],
                    leg["qty"], leg["exp"], leg["strike"])
        close_key = (t["date"], leg["symbol"], leg["type"], opp, -leg["qty"],
                     leg["close_exp"], leg.get("close_strike", leg["strike"]))
        pair = next(((a, b)
                     for a in singles.get(open_key, []) if a not in drop
                     for b in singles.get(close_key, []) if b not in drop
                     if abs(opt_trades[a]["amount"] + opt_trades[b]["amount"]
                            - t["amount"]) < 0.05), None)
        if pair:
            drop.update(pair)

    if drop:
        opt_trades[:] = [t for i, t in enumerate(opt_trades) if i not in drop]
    return len(drop)


def _net_same_day_rolls(legs: list[tuple[date, dict]]) -> None:
    """Fold a same-day closing BOT into the SOLD leg that replaces it, in place.

    A roll placed as one order arrives as a single CALENDAR/DIAGONAL row that
    already carries its net credit, but the same roll placed as two orders
    arrives as a separate buy-to-close and sell-to-open. Left alone the debit
    books entirely on its trade day (a close that leaves nothing open projects
    nothing forward) while the credit paying for it accrues out to the new
    expiration — so rolling far out reads as a large loss this week and a
    windfall weeks later. Pairing them by symbol and option type, preferring an
    exact quantity match, books the roll as the one net credit or debit it
    actually was, over the new leg's window. Strike is deliberately not part of
    the match: a roll down or up is still a roll. A close with no same-day open
    is a genuine exit and keeps booking on its own day.
    """
    groups: dict[tuple, list[dict]] = {}
    for trade_d, leg in legs:
        if leg["is_roll"]:
            continue                     # already a net roll row
        groups.setdefault((trade_d, leg["symbol"], leg["type"]), []).append(leg)

    for group in groups.values():
        closes = [l for l in group if l["action"] == "BOT"]
        opens  = [l for l in group if l["action"] == "SOLD"]
        if not closes or not opens:
            continue
        for close in closes:
            # Partial fills mean several closes can legitimately fold into one
            # open, so an open is never consumed by the first close it matches.
            match = next((o for o in opens if abs(o["qty"]) == abs(close["qty"])), None)
            if match is None:
                match = max(opens, key=lambda o: abs(o["amount"]))
            match["amount"] += close["amount"]
            close["amount"] = 0.0
            close["carry_to"] = match    # where the closed leg's unaccrued premium goes


def _bdays(start: date, end: date) -> list[date]:
    """Business days (Mon-Fri) in [start, end] inclusive; holidays ignored."""
    if end < start:
        end = start
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out or [start]


def _stock_realized_by_date(data: dict) -> dict[date, float]:
    """Realized P&L from stock sales (avg-cost basis), booked on the sale day."""
    trades = sorted(data.get("stk_trades", []), key=lambda t: t["date"])
    holdings: dict[str, list[float]] = {}   # symbol → [shares, total_cost]
    out: dict[date, float] = {}
    for t in trades:
        sym = t["symbol"]
        sh, tc = holdings.get(sym, [0.0, 0.0])
        if t["action"] == "BOT":
            sh += t["shares"]
            tc += abs(t["amount"])
            holdings[sym] = [sh, tc]
        else:  # SOLD — realize proceeds against average cost
            if sh > 0:
                avg = tc / sh
                q   = min(t["shares"], sh)
                realized = 0.0 if t.get("transfer") else t["amount"] - avg * q
                sh -= q
                tc -= avg * q
                holdings[sym] = [sh, tc]
            else:
                # short / no basis; a transfer out realizes nothing
                realized = 0.0 if t.get("transfer") else t["amount"]
            try:
                d = date.fromisoformat(t["date"])
            except Exception:
                continue
            out[d] = out.get(d, 0.0) + realized
    return out


def _weekly_allocated_ror(data: dict, sel, range_from: str | None,
                          range_to: str | None) -> dict | None:
    """Weekly return on capital actually deployed in trades.

    Both premium accrual and capital-at-risk are tracked per (symbol, strike,
    type) chain over time, walked chronologically: each event's window ends
    where the next event for that same chain begins, instead of running to its
    own original/far expiration — so a leg rolled again next week stops
    accruing premium and collateral once it's actually rolled, rather than
    both stacking with whatever replaced it. Only the most recent event in a
    chain (nothing rolled/closed it yet) still projects forward to its own
    parsed expiration, since we don't know if/when it'll be rolled again.
    Premium is accrued evenly across a window's business days; capital at risk
    is short-put collateral (strike×100×qty) on each open day. A week's
    allocated is the average over days a position is open. Weekly RoR = weekly
    realized / weekly allocated. A roll placed as two orders is netted into one
    event first — see ``_net_same_day_rolls``.

    A leg that's superseded before its expiration books only the days it was
    held, at the rate it would have earned to expiration; the rest carries
    into whatever superseded it. Booking all of it into the held days instead
    front-loads income — the roll that replaces it pays the buyback out of its
    own net credit, which accrues forward — and means importing next week's
    trades rewrites this week's figure. With the carry, a day's premium never
    changes once later trades arrive.
    """
    parsed: list[tuple[date, dict]] = []
    for t in data.get("opt_trades", []):
        leg = _parse_opt_leg(t.get("desc", ""))
        if not leg:
            continue
        try:
            trade_d = date.fromisoformat(t["date"])
        except Exception:
            continue
        leg["amount"] = t["amount"]
        parsed.append((trade_d, leg))

    # Before chaining: a buy-to-close and the sell-to-open that replaces it on
    # the same day are one roll, and must accrue over one window.
    _net_same_day_rolls(parsed)

    legs_by_key: dict[tuple[str, float, str], list[tuple[date, dict]]] = {}
    for trade_d, leg in parsed:
        legs_by_key.setdefault((leg["symbol"], leg["strike"], leg["type"]), []).append(
            (trade_d, leg))
        # A diagonal roll also closes out the to-close leg's *different*
        # strike, which lives under a separate key and would otherwise never
        # learn its position was rolled away — inject a synthetic close (no
        # premium of its own; the row's amount is already booked above) so
        # that chain's window ends here instead of stacking with the new leg.
        close_strike = leg.get("close_strike")
        if close_strike is not None and close_strike != leg["strike"]:
            closing_leg = {
                "action": "BOT", "qty": leg["qty"], "symbol": leg["symbol"],
                "exp": trade_d, "strike": close_strike, "type": leg["type"],
                "is_roll": False, "amount": 0.0, "carry_to": leg,
            }
            legs_by_key.setdefault(
                (leg["symbol"], close_strike, leg["type"]), []).append(
                (trade_d, closing_leg))

    # Walk each (symbol, strike, type) chain chronologically. A roll
    # (CALENDAR/DIAGONAL) closes the prior leg and opens the new one in the
    # same order, so it replaces the open quantity outright rather than adding
    # to it; a plain SOLD/BOT adjusts the running quantity instead.
    # One roll can fill in pieces, though, and each fill is its own row — so
    # fills sharing a trade date and to-open expiration are one roll and
    # accumulate, replacing the chain's open quantity only as a group. Without
    # that, six contracts rolled as [2, 2, 1, 1] would carry the last fill's
    # single contract of collateral, understating capital at risk sixfold.
    daily_prem:   dict[date, float] = {}
    daily_collat: dict[date, float] = {}
    # id(leg) → (leg, days it books premium on, days it would accrue over)
    plans: dict[int, tuple[dict, list[date], int]] = {}
    succ:  dict[int, dict] = {}      # id(leg) → leg its unaccrued premium carries to
    for events in legs_by_key.values():
        events.sort(key=lambda e: e[0])
        open_qty = 0
        roll_group = None
        for i, (ev_date, leg) in enumerate(events):
            is_roll_open = leg["is_roll"] and leg["action"] == "SOLD"
            group = (ev_date, leg["exp"]) if is_roll_open else None
            if is_roll_open:
                open_qty = (open_qty if group == roll_group else 0) + abs(leg["qty"])
            elif leg["action"] == "SOLD":
                open_qty += abs(leg["qty"])
            else:  # BOT — closes contracts
                open_qty = max(open_qty - abs(leg["qty"]), 0)
            roll_group = group

            next_date = events[i + 1][0] if i + 1 < len(events) else None
            same_day_superseded = next_date is not None and next_date <= ev_date
            # A leg accrues to its expiration while it's open. One that leaves
            # nothing open (a close) has nothing to project forward and books
            # on its own trade date — unless it was rolled into a leg on
            # another strike, which then takes everything it was carrying.
            planned = _bdays(ev_date, leg["exp"] if open_qty > 0 else ev_date)
            target  = leg.get("carry_to")
            if (target is not None and open_qty <= 0
                    and all(l is not target for _, l in events[:i])):
                held = []
                succ[id(leg)] = target
            elif next_date is None:
                held = planned
            else:
                held = [] if same_day_superseded else [d for d in planned if d < next_date]
                succ[id(leg)] = events[i + 1][1]
            plans[id(leg)] = (leg, held, len(planned))

            # Collateral, unlike premium, is skipped for a same-day-superseded
            # leg — it never carried overnight risk, so it shouldn't stack with
            # whatever replaced it on that same day.
            if same_day_superseded or open_qty <= 0 or leg["type"] != "PUT":
                continue
            window_end = (min(next_date - timedelta(days=1), leg["exp"])
                          if next_date is not None else leg["exp"])
            collat = leg["strike"] * 100 * open_qty
            for d in _bdays(ev_date, window_end):
                daily_collat[d] = daily_collat.get(d, 0.0) + collat

    # Book premium in carry order: a leg is booked only once everything
    # carrying into it has been, so its total includes what it inherited.
    carry: dict[int, float] = {k: 0.0 for k in plans}
    waiting = Counter(id(t) for t in succ.values())
    ready = [k for k in plans if not waiting[k]]
    while ready:
        k = ready.pop()
        leg, held, n_planned = plans.pop(k)
        total   = leg["amount"] + carry[k]
        per_day = total / n_planned
        for d in held:
            daily_prem[d] = daily_prem.get(d, 0.0) + per_day
        rest = total - per_day * len(held)
        nxt = id(succ[k]) if k in succ else None
        if nxt in carry:
            carry[nxt] += rest
            waiting[nxt] -= 1
            if not waiting[nxt]:
                ready.append(nxt)
        elif rest:
            day = held[-1] if held else leg["exp"]
            daily_prem[day] = daily_prem.get(day, 0.0) + rest
    # Nothing should be left, but never drop cash if the carries ever loop.
    for k, (leg, held, _) in plans.items():
        day = held[0] if held else leg["exp"]
        daily_prem[day] = daily_prem.get(day, 0.0) + leg["amount"] + carry[k]

    # Stock realized gain/loss, all on the day of sale.
    for d, r in _stock_realized_by_date(data).items():
        daily_prem[d] = daily_prem.get(d, 0.0) + r

    if not daily_prem:
        return None

    days = sorted(set(daily_prem) | set(daily_collat))
    first, last = days[0], days[-1]

    if sel == "custom" and range_from and range_to:
        win_start = date.fromisoformat(range_from)
        win_end   = date.fromisoformat(range_to)
    elif isinstance(sel, int):
        # Count back from the last real trade, like the P&L tab — not from the
        # furthest day premium is projected to accrue.
        real = [date.fromisoformat(t["date"])
                for t in data.get("opt_trades", []) + data.get("stk_trades", [])]
        win_end   = min(max(real), last) if real else last
        win_start = win_end - timedelta(days=sel)
    else:
        win_start, win_end = first, last

    wk = win_start - timedelta(days=win_start.weekday())
    last_wk = win_end - timedelta(days=win_end.weekday())
    weeks, ror, realized, allocated = [], [], [], []
    while wk <= last_wk:
        wdays  = [wk + timedelta(days=i) for i in range(5)]   # Mon-Fri
        r      = sum(daily_prem.get(d, 0.0) for d in wdays)
        active = [daily_collat[d] for d in wdays if daily_collat.get(d, 0.0) > 0]
        alloc  = sum(active) / len(active) if active else 0.0
        weeks.append(datetime(wk.year, wk.month, wk.day))
        realized.append(r)
        allocated.append(alloc)
        ror.append((r / alloc * 100) if alloc else 0.0)
        wk += timedelta(days=7)
    return {"weeks": weeks, "ror": ror, "realized": realized, "allocated": allocated}


class GainsTab(QWidget):
    def __init__(self):
        super().__init__()
        self._account = ""
        self._data = _blank_history()
        self._range_sel  = None   # None → all time; int → days back; "custom"
        self._range_from = None   # ISO date string when custom
        self._range_to   = None
        self._setup_ui()
        self._refresh_chart()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        summary = QHBoxLayout()
        self._lbl_dep      = QLabel("Net deposits: —")
        self._lbl_realized = QLabel("Realized: —")
        self._lbl_total    = QLabel("Total: —")
        self._lbl_fees     = QLabel("Fees: —")
        self._lbl_fees.setToolTip("Commissions and misc fees paid, from the statement's "
                                  "fee columns. Already subtracted from Realized and Total.")
        for lbl in (self._lbl_dep, self._lbl_realized, self._lbl_total, self._lbl_fees):
            lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
            summary.addWidget(lbl)
        summary.addStretch()

        self._cb_deposits = QCheckBox("Show deposits")
        self._cb_pnl      = QCheckBox("Show gains/losses")
        self._cb_deposits.setChecked(True)
        self._cb_pnl.setChecked(True)
        for cb in (self._cb_deposits, self._cb_pnl):
            cb.toggled.connect(self._refresh_chart)
            summary.addWidget(cb)
        root.addLayout(summary)

        self._fig, (self._ax1, self._ax2) = plt.subplots(
            2, 1, figsize=(10, 7), sharex=True)
        self._fig.subplots_adjust(hspace=0.12)
        self._canvas = FigureCanvasQTAgg(self._fig)
        root.addWidget(self._canvas, 1)

    def load_account(self, acct: str):
        """Switch to an account's gains history and redraw."""
        self._account = acct
        self._data = _load_history(gains_path(acct)) if acct else _blank_history()
        self._refresh_chart()

    def process_csv(self, path: str, account: str):
        """Merge an imported statement into the given account's gains history."""
        try:
            parsed = parse_statement(Path(path))
        except ValueError:
            return
        self._account = account
        self._data = _load_history(gains_path(account))
        merge_statement(self._data, parsed)
        _save_history(gains_path(account), self._data)
        self._refresh_chart()

    def clear_history(self):
        """Erase the active account's gains history."""
        if self._account:
            gains_path(self._account).unlink(missing_ok=True)
        self._data = _blank_history()
        self._refresh_chart()

    def apply_range(self, sel, date_from: str | None = None, date_to: str | None = None):
        """Shared time-range filter (sel: None=all, int=days back, 'custom')."""
        self._range_sel  = sel
        self._range_from = date_from
        self._range_to   = date_to
        self._refresh_chart()

    def data_date_bounds(self) -> tuple[str | None, str | None]:
        """(min, max) ISO date of all events, for defaulting custom pickers."""
        dates = ([b["date"] for b in self._data.get("balances", [])]
                 + [t["date"] for t in self._data.get("opt_trades", [])]
                 + [t["date"] for t in self._data.get("stk_trades", [])]
                 + [d["date"] for d in self._data.get("deposits", [])])
        return (min(dates), max(dates)) if dates else (None, None)

    def _filtered_series(self, series: dict) -> dict | None:
        """Slice the time series to the selected range. None → no points in range."""
        dates = series["dates"]
        sel = self._range_sel
        if sel is None or not dates:        # All Time
            return series

        if sel == "custom":
            if not (self._range_from and self._range_to):
                return series
            lo = datetime.fromisoformat(self._range_from)
            hi = datetime.fromisoformat(self._range_to) + timedelta(days=1)
        else:                               # int days back from last data point
            hi = dates[-1] + timedelta(days=1)
            lo = dates[-1] - timedelta(days=sel)

        idx = [i for i, d in enumerate(dates) if lo <= d < hi]
        if not idx:
            return None
        n = len(dates)
        return {
            k: ([v[i] for i in idx] if isinstance(v, list) and len(v) == n else v)
            for k, v in series.items()
        }

    def _refresh_chart(self):
        for ax in (self._ax1, self._ax2):
            ax.clear()

        full = reconstruct(prepared(self._data, self._account))
        if not full:
            for ax in (self._ax1, self._ax2):
                ax.text(0.5, 0.5, "Import a TOS CSV in the Positions tab",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=11, color="gray")
            self._canvas.draw()
            for lbl in (self._lbl_dep, self._lbl_realized, self._lbl_total, self._lbl_fees):
                lbl.setText(lbl.text().split(":")[0] + ": —")
            return

        series = self._filtered_series(full)
        if series is None:
            for ax in (self._ax1, self._ax2):
                ax.text(0.5, 0.5, "No data in selected range",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=11, color="gray")
            self._canvas.draw()
            for lbl in (self._lbl_dep, self._lbl_realized, self._lbl_total, self._lbl_fees):
                lbl.setText(lbl.text().split(":")[0] + ": —")
            return

        dt   = series["dates"]
        dep  = series["deposits"]
        real = series["realized"]
        tot  = series["total"]

        net_dep   = dep[-1]
        realized  = real[-1]
        total_pnl = tot[-1]
        real_ror  = (realized  / net_dep * 100) if net_dep else 0.0
        tot_ror   = (total_pnl / net_dep * 100) if net_dep else 0.0

        show_dep = self._cb_deposits.isChecked()
        show_pnl = self._cb_pnl.isChecked()
        _fill_pnl(self._ax1, dt, dep, real,
                  f"Realized P&L  —  ${realized:+,.0f}  ({real_ror:+.1f}%)",
                  show_deposits=show_dep, show_pnl=show_pnl)
        _fill_pnl(self._ax2, dt, dep, tot,
                  f"Total P&L (Realized + Unrealized)  —  ${total_pnl:+,.0f}  ({tot_ror:+.1f}%)",
                  show_deposits=show_dep, show_pnl=show_pnl)

        for ax in (self._ax1, self._ax2):
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
            ax.set_ylabel("Value ($)")
            if ax.get_legend_handles_labels()[1]:
                ax.legend(loc="upper left", fontsize=8)
            ax.grid(True, alpha=0.3)

        locator = mdates.AutoDateLocator()
        self._ax2.xaxis.set_major_locator(locator)
        self._ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        self._fig.autofmt_xdate(rotation=30, ha="right")
        self._fig.tight_layout(pad=1.5)
        self._canvas.draw()

        self._lbl_dep.setText(f"Net deposits: ${net_dep:,.2f}")
        self._lbl_realized.setStyleSheet(
            f"font-weight: bold; font-size: 13px; color: {'green' if realized >= 0 else 'red'};")
        self._lbl_realized.setText(f"Realized: ${realized:+,.2f} ({real_ror:+.1f}%)")
        self._lbl_total.setStyleSheet(
            f"font-weight: bold; font-size: 13px; color: {'green' if total_pnl >= 0 else 'red'};")
        self._lbl_total.setText(f"Total: ${total_pnl:+,.2f} ({tot_ror:+.1f}%)")
        self._lbl_fees.setText(f"Fees: ${series['fees'][-1]:,.2f}")


def _bar_width(x: list[float]) -> float:
    """Bar width (in matplotlib date units) = 80% of the smallest gap between points."""
    gaps = [b - a for a, b in zip(x[:-1], x[1:]) if b > a]
    return min(gaps) * 0.8 if gaps else 1.0


def _fill_pnl(ax, dt_series, dep_series, pnl_series, title: str,
              show_deposits: bool = True, show_pnl: bool = True):
    """Draw deposits (blue base) as a fill and/or P&L as per-date bars.

    When deposits are hidden the P&L bars sit on a zero baseline, so the chart
    shows pure gain/loss (green up / red down) with deposits removed.
    """
    base = dep_series if show_deposits else [0.0] * len(dep_series)

    if show_deposits:
        ax.fill_between(dt_series, 0, dep_series,
                        color="#4a90d9", alpha=0.8, label="Net Deposits")
    if show_pnl:
        x = mdates.date2num(dt_series)
        width = _bar_width(x)
        colors = ["#5cb85c" if p >= 0 else "#d9534f" for p in pnl_series]
        ax.bar(x, pnl_series, bottom=base, width=width, color=colors,
               alpha=0.85, linewidth=0, align="center")
        # Proxy entries so the legend shows Gain/Loss for the mixed-colour bars.
        ax.bar([], [], color="#5cb85c", label="Gain")
        ax.bar([], [], color="#d9534f", label="Loss")
    ax.set_title(title, fontsize=10, pad=4)


class WeeklyRorTab(QWidget):
    """Per-week realized gain/loss ($, top) and return on allocated capital (%, bottom)."""

    def __init__(self):
        super().__init__()
        self._account = ""
        self._data = _blank_history()
        self._range_sel  = None
        self._range_from = None
        self._range_to   = None
        self._setup_ui()
        self._refresh()

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)
        self._summary = QLabel("Weekly RoR")
        self._summary.setStyleSheet("font-weight: bold; font-size: 13px;")
        root.addWidget(self._summary)

        self._fig, (self._ax_d, self._ax_r) = plt.subplots(
            2, 1, figsize=(10, 7), sharex=True)
        self._fig.subplots_adjust(hspace=0.12)
        self._canvas = FigureCanvasQTAgg(self._fig)
        root.addWidget(self._canvas, 1)

    # ── external hooks ──────────────────────────────────────────────────────
    def apply_range(self, sel, date_from: str | None = None, date_to: str | None = None):
        self._range_sel  = sel
        self._range_from = date_from
        self._range_to   = date_to
        self._refresh()

    def load_account(self, acct: str):
        """Switch to an account's gains history and redraw."""
        self._account = acct
        self._data = _load_history(gains_path(acct)) if acct else _blank_history()
        self._refresh()

    def reload(self, *_):
        """Re-read the active account's history (after import or clear) and redraw."""
        self._data = _load_history(gains_path(self._account)) if self._account else _blank_history()
        self._refresh()

    # ── drawing ─────────────────────────────────────────────────────────────
    def _empty(self, msg: str):
        for ax in (self._ax_d, self._ax_r):
            ax.text(0.5, 0.5, msg, ha="center", va="center",
                    transform=ax.transAxes, fontsize=11, color="gray")
        self._summary.setText(f"Weekly RoR — {msg.lower()}")
        self._canvas.draw()

    def _refresh(self):
        self._ax_d.clear()
        self._ax_r.clear()

        if not (self._data.get("opt_trades") or self._data.get("stk_trades")):
            self._empty("Import a TOS CSV in the Positions tab")
            return

        wk = _weekly_allocated_ror(
            prepared(self._data, self._account),
            self._range_sel, self._range_from, self._range_to)
        if not wk or not wk["weeks"]:
            self._empty("No data in selected range")
            return

        weeks, ror, realized, allocated = (
            wk["weeks"], wk["ror"], wk["realized"], wk["allocated"])

        # Past weeks are realized (green/red by sign); the current week is the
        # boundary (amber); weeks after it are forward-looking projections
        # (yellow), since their premium is still accruing on positions held now.
        today = date.today()
        cur_monday = today - timedelta(days=today.weekday())
        cur_dt = datetime(cur_monday.year, cur_monday.month, cur_monday.day)
        CUR    = "#f0ad4e"   # current week (amber)
        FUTURE = "#f5e16e"   # projected weeks (yellow)

        def _cat_color(w, val):
            if w == cur_dt:
                return CUR
            if w > cur_dt:
                return FUTURE
            return "#5cb85c" if val >= 0 else "#d9534f"

        x = mdates.date2num(weeks)
        colors = [_cat_color(w, v) for w, v in zip(weeks, realized)]

        # ── Top: realized gain/loss in dollars ──────────────────────────────
        self._ax_d.bar(x, realized, width=6.0, align="edge", color=colors,
                       alpha=0.85, linewidth=0)
        self._ax_d.bar([], [], color="#5cb85c", label="Gain")
        self._ax_d.bar([], [], color="#d9534f", label="Loss")
        if any(w == cur_dt for w in weeks):
            self._ax_d.bar([], [], color=CUR, label="Current week")
        if any(w > cur_dt for w in weeks):
            self._ax_d.bar([], [], color=FUTURE, label="Projected")
        self._ax_d.axhline(0, color="0.5", linewidth=0.6)
        self._ax_d.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"${v:,.0f}"))
        self._ax_d.set_ylabel("Realized ($)")
        self._ax_d.legend(loc="upper left", fontsize=8)
        self._ax_d.grid(True, axis="y", alpha=0.3)
        self._ax_d.set_title("Weekly Realized $  (top)  and  RoR on Allocated Capital  (bottom)",
                             fontsize=11)
        self._ax_d.tick_params(labelbottom=False)

        # ── Bottom: return on allocated capital (%) ─────────────────────────
        bars_r = self._ax_r.bar(x, ror, width=6.0, align="edge", color=colors,
                                alpha=0.85, linewidth=0)
        self._ax_r.axhline(0, color="0.5", linewidth=0.6)
        self._ax_r.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:+.1f}%"))
        self._ax_r.set_ylabel("RoR (% of allocated)")
        self._ax_r.grid(True, axis="y", alpha=0.3)
        loc = mdates.AutoDateLocator()
        self._ax_r.xaxis.set_major_locator(loc)
        self._ax_r.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))

        # Label each RoR bar with the capital allocated that week.
        lbl_color = plt.rcParams.get("xtick.color", "black")
        lbl_size  = plt.rcParams.get("xtick.labelsize", 10)
        for bar, alloc, r in zip(bars_r, allocated, ror):
            if alloc <= 0:
                continue
            label = f"${alloc/1000:.1f}k" if alloc >= 1000 else f"${alloc:.0f}"
            self._ax_r.annotate(
                label, (bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 4 if r >= 0 else -4), textcoords="offset points",
                ha="center", va="bottom" if r >= 0 else "top",
                fontsize=lbl_size, color=lbl_color)

        tot_real  = sum(realized)
        active    = [a for a in allocated if a > 0]
        avg_alloc = sum(active) / len(active) if active else 0.0
        avg_ror   = sum(r for r in ror if r) / len(active) if active else 0.0
        self._summary.setText(
            f"Weekly RoR — {len(weeks)} weeks · ${tot_real:+,.0f} realized · "
            f"avg {avg_ror:+.2f}%/wk on ~${avg_alloc:,.0f} allocated")

        self._fig.autofmt_xdate(rotation=30, ha="right")
        self._fig.tight_layout(pad=1.5)
        self._canvas.draw()
