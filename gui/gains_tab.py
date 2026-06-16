import csv
import io
import json
import re
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

GAINS_FILE = Path("gains_history.json")

_DEPOSIT_TYPES    = {"CRC"}
_WITHDRAWAL_TYPES = {"CDB"}
_CB_SECTION_STOPS = {"Account Trade History", "Profits and Losses",
                     "Account Summary", "Futures Statements", "Forex Statements"}

# Direct stock trade in the TRD rows, e.g. "BOT +20 IBM @221.50"
_STK_TRD_RE = re.compile(
    r'^(BOT|SOLD)\s+([+-]?\d+(?:\.\d+)?)\s+([A-Z]+)\s+@', re.IGNORECASE)
# Stock assignment/exercise in the EXP rows, e.g. "BOT 100.0 SPXU UPON ..."
_STK_EXP_RE = re.compile(
    r'^(BOT|SOLD)\s+(\d+(?:\.\d+)?)\s+([A-Z]+)\s+UPON', re.IGNORECASE)


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
        ref      = _clean_ref(cells[3]) if len(cells) > 3 else ""
        desc     = cells[4] if len(cells) > 4 else ""
        diso     = row_date.isoformat()

        if row_type == "BAL" and balance is not None:
            balances.append({"date": diso, "balance": balance})

        elif row_type in _DEPOSIT_TYPES and amount is not None and amount > 0:
            deposits.append({"ref": ref, "date": diso, "amount": amount})

        elif row_type in _WITHDRAWAL_TYPES and amount is not None and amount < 0:
            deposits.append({"ref": ref, "date": diso, "amount": amount})

        elif row_type in ("TRD", "EXP") and amount is not None:
            stk = _STK_TRD_RE.match(desc) or _STK_EXP_RE.match(desc)
            if row_type == "TRD" and _is_option(desc):
                opt_trades.append({"date": diso, "amount": amount, "desc": desc})
            elif stk:
                action = stk.group(1).upper()
                shares = abs(float(stk.group(2)))
                symbol = stk.group(3).upper()
                stk_trades.append({
                    "date": diso, "action": action, "symbol": symbol,
                    "shares": shares, "amount": amount, "desc": desc,
                })

    return {"deposits": deposits, "balances": balances,
            "opt_trades": opt_trades, "stk_trades": stk_trades}


def _load_history() -> dict:
    if GAINS_FILE.exists():
        try:
            data = json.loads(GAINS_FILE.read_text())
            for k in ("deposits", "balances", "opt_trades", "stk_trades"):
                data.setdefault(k, [])
            return data
        except Exception:
            pass
    return {"deposits": [], "balances": [], "opt_trades": [], "stk_trades": []}


def _save_history(data: dict) -> None:
    GAINS_FILE.write_text(json.dumps(data, indent=2))


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


def merge_statement(data: dict, parsed: dict) -> dict:
    """Merge a parsed statement into accumulated history. Returns counts added."""
    return {
        "deposits":   _merge(data["deposits"],   parsed["deposits"],
                             lambda e: e["ref"] or (e["date"], e["amount"])),
        "balances":   _merge(data["balances"],   parsed["balances"],
                             lambda e: e["date"]),
        "opt_trades": _merge(data["opt_trades"], parsed["opt_trades"],
                             lambda e: (e["date"], e["desc"], e["amount"])),
        "stk_trades": _merge(data["stk_trades"], parsed["stk_trades"],
                             lambda e: (e["date"], e["desc"], e["amount"])),
    }


def _fetch_price_history(symbols: list[str], start: date, end: date) -> dict:
    """Return {symbol: {iso_date: close}} via yfinance."""
    if not symbols:
        return {}
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


def reconstruct(data: dict) -> dict:
    """
    Walk trades chronologically with average-cost basis.
    Returns time series aligned to balance dates:
      dates, deposits, realized, total, and final summary values.
    """
    balances = sorted(data["balances"], key=lambda b: b["date"])

    # Combine and sort all trades chronologically
    opt = [{**t, "kind": "opt"} for t in data["opt_trades"]]
    stk = [{**t, "kind": "stk"} for t in data["stk_trades"]]
    trades = sorted(opt + stk, key=lambda t: t["date"])

    deposits = sorted(data["deposits"], key=lambda d: d["date"])

    # Timeline = every date that has an event (balance, trade, or deposit), so a
    # trading day still plots even when its end-of-day BAL row hasn't posted yet.
    timeline = sorted({b["date"] for b in balances}
                      | {t["date"] for t in trades}
                      | {d["date"] for d in deposits})
    if not timeline:
        return {}

    start_d = date.fromisoformat(timeline[0])
    end_d   = date.fromisoformat(timeline[-1])

    # All symbols ever traded → fetch price history once
    symbols = sorted({t["symbol"] for t in data["stk_trades"]})
    price_hist = _fetch_price_history(symbols, start_d, end_d)

    holdings: dict[str, list[float]] = {}   # symbol → [shares, total_cost]
    cum_opt = 0.0
    cum_stk_realized = 0.0
    ti = 0   # trade index
    di = 0   # deposit index
    dep_cum = 0.0

    dep_series, realized_series, total_series = [], [], []

    for bd in timeline:
        # advance deposits
        while di < len(deposits) and deposits[di]["date"] <= bd:
            dep_cum += deposits[di]["amount"]
            di += 1
        # advance trades
        while ti < len(trades) and trades[ti]["date"] <= bd:
            t = trades[ti]
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
                        cum_stk_realized += t["amount"] - avg * q
                        sh -= q
                        tc -= avg * q
                        holdings[sym] = [sh, tc]
                    else:
                        # short / no basis — treat proceeds as realized
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

        dep_series.append(dep_cum)
        realized_series.append(realized)
        total_series.append(realized + unreal)

    return {
        "dates":    [datetime.fromisoformat(d) for d in timeline],
        "deposits": dep_series,
        "realized": realized_series,
        "total":    total_series,
    }


class GainsTab(QWidget):
    def __init__(self):
        super().__init__()
        self._data = _load_history()
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
        for lbl in (self._lbl_dep, self._lbl_realized, self._lbl_total):
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

    def process_csv(self, path: str):
        """Called by PortfolioTab after a successful CSV import."""
        try:
            parsed = parse_statement(Path(path))
        except ValueError:
            return
        merge_statement(self._data, parsed)
        _save_history(self._data)
        self._refresh_chart()

    def clear_history(self):
        """Called by PortfolioWindow when the user clears all data."""
        self._data = {"deposits": [], "balances": [], "opt_trades": [], "stk_trades": []}
        _save_history(self._data)
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

        idx = [i for i, d in enumerate(dates) if lo <= d <= hi]
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

        series = reconstruct(self._data)
        if not series:
            for ax in (self._ax1, self._ax2):
                ax.text(0.5, 0.5, "Import a TOS CSV in the Positions tab",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=11, color="gray")
            self._canvas.draw()
            for lbl in (self._lbl_dep, self._lbl_realized, self._lbl_total):
                lbl.setText(lbl.text().split(":")[0] + ": —")
            return

        series = self._filtered_series(series)
        if series is None:
            for ax in (self._ax1, self._ax2):
                ax.text(0.5, 0.5, "No data in selected range",
                        ha="center", va="center", transform=ax.transAxes,
                        fontsize=11, color="gray")
            self._canvas.draw()
            for lbl in (self._lbl_dep, self._lbl_realized, self._lbl_total):
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
