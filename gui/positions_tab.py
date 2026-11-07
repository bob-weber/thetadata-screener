import calendar
import csv
import io
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal, QThread
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QGroupBox,
    QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QFileDialog, QMessageBox,
    QAbstractItemView, QApplication,
)

from core.lso_analyzer import tags_for

from .account_store import DEFAULT_ACCT, events_path, safe_token
from .sharebook import ShareBook

POSITIONS_FILE   = Path("my_option_positions.json")     # legacy cache, rebuilt from events
STOCKS_FILE      = Path("my_stock_positions.json")       # legacy cache, rebuilt from events
SUPERSEDED_FILE  = Path("my_option_superseded.json")     # legacy (pre-lifecycle), cleaned up

# ── Options column layout ──────────────────────────────────────────────────────
# (field, header, kind)   kind: "edit" = stored & editable, "calc" = computed,
#                                "hidden" = stored but not shown (used for calc)
_OPT_COLUMNS = [
    ("symbol",     "Symbol",            "edit"),
    ("market",     "Market",            "calc"),   # live underlying price (yfinance)
    ("opened",     "Opened",            "edit"),
    ("lifecycle",  "Lifecycle",         "edit"),   # Put → Assigned → Sold, etc.
    ("qty",        "Qty",               "edit"),
    ("unit",       "Unit",              "calc"),   # what Qty counts: contracts or shares
    ("strike",     "Strike",            "edit"),
    ("capital",    "Capital",           "calc"),   # at risk now; live rows only
    ("capital_pct","% of Book",         "calc"),
    ("premium",    "Premium ($)",       "edit"),   # total option premium collected (puts + calls)
    ("expiration", "Expiration",        "edit"),
    ("status",     "Status",            "edit"),
    ("weeks_held", "Weeks Held",        "calc"),
    ("fees",       "Fees",              "edit"),
    ("weekly_ror", "Avg Weekly RoR",    "calc"),
    # Target prices for a position that is (or may become) shares:
    #   Assn Price — what the shares cost at assignment (avg strike paid)
    #   Cost Basis — assignment price less the put premium
    #   Recovery   — cost basis less the call premium: the break-even exit price
    ("assn_price", "Assn/Buy Price",    "calc"),
    ("cost_basis", "Cost Basis",        "calc"),
    ("recovery",   "Recovery",          "calc"),
    # Last, so the stretched final column absorbs variable-length tag lists.
    ("tags",       "Tags",              "calc"),   # durable-tags.json risk tags
]
_OPT_FIELDS = [c[0] for c in _OPT_COLUMNS]

# Closed tab: finished cycles only. Market price, capital and the target prices
# describe a position you still hold, so they are left off; the same cost_basis
# field carries total P&L for a completed row.
_CLOSED_COLUMNS = [
    ("symbol",     "Symbol"),
    ("opened",     "Opened"),
    ("lifecycle",  "Lifecycle"),
    ("qty",        "Qty"),
    ("unit",       "Unit"),
    ("strike",     "Strike"),
    ("premium",    "Premium ($)"),
    ("expiration", "Ended"),
    ("status",     "Status"),
    ("weeks_held", "Weeks Held"),
    ("fees",       "Fees"),
    ("weekly_ror", "Avg Weekly RoR"),
    ("cost_basis", "P&L"),
    ("tags",       "Tags"),
]
_CLOSED_FIELDS = [c[0] for c in _CLOSED_COLUMNS]
_C_STATUS_COL  = _CLOSED_FIELDS.index("status")
_O_HDRS     = [c[1] for c in _OPT_COLUMNS]
_OPT_HIDDEN_COLS = [i for i, c in enumerate(_OPT_COLUMNS) if c[2] == "hidden"]

def _oc(field: str) -> int:
    return _OPT_FIELDS.index(field)

_O_SYM_COL       = _oc("symbol")
_O_MARKET_COL    = _oc("market")
_O_LIFECYCLE_COL = _oc("lifecycle")
_O_STRIKE_COL    = _oc("strike")
_O_CB_COL        = _oc("cost_basis")
_O_OPENED_COL    = _oc("opened")
_O_EXP_COL       = _oc("expiration")
_O_STATUS_COL    = _oc("status")
_O_WEEKS_COL     = _oc("weeks_held")
_O_ROR_COL       = _oc("weekly_ror")
_O_PREMIUM_COL   = _oc("premium")

# Live statuses share a rank so a symbol's puts, shares/calls and summary row
# read as one group; _LIVE_LEG_SORT then orders them within the symbol.
_STATUS_SORT = {"Open": 0, "Holding": 0, "Blended": 0, "Closed": 2,
                "Called away": 3, "Assigned": 4, "Sold": 5, "Transferred": 5,
                "Expired": 6}
_LIVE_LEG_SORT = {"Open": 0, "Holding": 1, "Blended": 2}


def _row_sort_key(r: dict) -> tuple:
    """Wheel-cycle progression, then ticker, then a live symbol's legs in order."""
    status = r.get("status", "")
    # "opened" is MM/DD/YYYY; as a string 01/05/2027 sorts before 12/15/2026.
    return (_STATUS_SORT.get(status, 99), r.get("symbol", ""),
            _LIVE_LEG_SORT.get(status, 0),
            _parse_mdy(r.get("opened", "")) or date.min)


class _StatusItem(QTableWidgetItem):
    """Table item that sorts Status by wheel-cycle progression, not alphabetically.

    Carries the whole row key rather than just the status: Qt's sort is stable,
    so ties would otherwise fall back on whatever column was sorted last, and
    clicking the header could not put a symbol's legs back together.
    """
    def __init__(self, text: str, sort_key: tuple):
        super().__init__(text)
        self.sort_key = sort_key

    def __lt__(self, other):
        if isinstance(other, _StatusItem):
            return self.sort_key < other.sort_key
        return super().__lt__(other)


class _SymbolItem(QTableWidgetItem):
    """Closed-tab Symbol cell: sorts by ticker, then oldest cycle first.

    Sorting on the ticker text alone leaves a symbol's rows in whatever order
    the previous sort left them. Within one ticker the order stays oldest first
    whichever way the column is sorted: Qt sorts descending by swapping the
    comparison, so the within-ticker part is swapped back. (The Positions table
    doesn't use Qt sorting at all — see _grouped_order.)
    """
    def __init__(self, text: str, row: dict):
        super().__init__(text)
        opened = _parse_mdy(row.get("opened", ""))
        self.ticker  = row.get("symbol", "")
        # Blended's "— blended —" has no date; it sorts by its leg rank alone.
        self.leg_key = (_LIVE_LEG_SORT.get(row.get("status", ""), 0),
                        opened or date.min)

    def __lt__(self, other):
        if not isinstance(other, _SymbolItem):
            return super().__lt__(other)
        if self.ticker != other.ticker:
            return self.ticker < other.ticker
        table = self.tableWidget()
        desc = (table is not None and table.horizontalHeader().sortIndicatorOrder()
                == Qt.SortOrder.DescendingOrder)
        if desc:
            return other.leg_key < self.leg_key
        return self.leg_key < other.leg_key


def _sort_value(field: str, row: dict, prices: dict):
    """Comparable value of one cell for the grouped live sort; None = blank.

    Cells are display strings ('$28,800', '7.8%', '+12.00', '08/14/2026'), so
    they are read back as dates or numbers where they parse; text sorts after
    numbers and case-blind. '—' and empty cells are blank and always go last.
    """
    if field == "market":
        price = prices.get(row.get("symbol", ""))
        return (0, float(price)) if isinstance(price, (int, float)) else None
    if field == "status":
        return (0, _LIVE_LEG_SORT.get(row.get("status", ""), 0))
    raw = str(row.get(field, "")).strip()
    if not raw or raw.startswith("—"):
        return None
    d = _parse_mdy(raw)
    if d is not None:
        return (0, d.toordinal())
    try:
        return (0, float(raw.replace("$", "").replace(",", "")
                         .replace("%", "").replace("+", "")))
    except ValueError:
        return (1, raw.lower())


def _grouped_order(rows: list[dict], field: str, desc: bool,
                   prices: dict) -> list[list[dict]]:
    """Live rows as ticker groups, sorted on ``field`` without splitting a ticker.

    Within a group the legs sort on the column (blanks last) and Blended stays
    at the bottom — it summarises the rows above it. Groups order by their
    leading row, so sorting on Expiration puts the ticker with the nearest
    expiration first; tickers break ties, and a group with no value goes last.
    Sorting on Symbol itself orders the groups by ticker and keeps each one's
    legs in their natural order (puts → shares → Blended, oldest first).
    """
    groups: dict[str, list[dict]] = {}
    for r in sorted(rows, key=_row_sort_key):
        groups.setdefault(r.get("symbol", ""), []).append(r)

    keyed = []
    for sym, g in groups.items():
        legs    = [r for r in g if r.get("status") != "Blended"]
        blended = [r for r in g if r.get("status") == "Blended"]
        if field == "symbol":
            keyed.append(((0, sym.lower()), sym, legs + blended))
            continue
        vals    = [(r, _sort_value(field, r, prices)) for r in legs]
        present = [rv for rv in vals if rv[1] is not None]
        present.sort(key=lambda rv: rv[1], reverse=desc)
        missing = [r for r, v in vals if v is None]
        head    = present[0][1] if present else None
        keyed.append((head, sym, [r for r, _ in present] + missing + blended))

    with_val = [k for k in keyed if k[0] is not None]
    no_val   = sorted((k for k in keyed if k[0] is None), key=lambda k: k[1])
    # Two stable passes: ticker always ascending as the tie-break, then value.
    with_val.sort(key=lambda k: k[1])
    with_val.sort(key=lambda k: k[0], reverse=desc)
    return [k[2] for k in with_val + no_val]


def _fmt_price(price) -> str:
    """Format a live underlying price; '…' means a fetch is still pending."""
    if price is None:
        return "…"
    try:
        return f"${float(price):,.2f}"
    except (TypeError, ValueError):
        return "…"


class _PriceWorker(QThread):
    """Fetch current underlying prices via yfinance off the UI thread."""
    done = pyqtSignal(int, dict)   # (generation, {symbol: price})

    def __init__(self, symbols: list[str], generation: int):
        super().__init__()
        self._symbols    = symbols
        self._generation = generation

    def run(self):
        from core.screener import fetch_stock_prices
        try:
            rows = fetch_stock_prices(self._symbols)
        except Exception:
            rows = []
        self.done.emit(self._generation, {r["symbol"]: r["price"] for r in rows})


class PortfolioTab(QWidget):
    csv_imported    = pyqtSignal(str, str)   # (file path, account) after a successful import
    account_changed = pyqtSignal(str)        # account label text
    closed_count_changed = pyqtSignal(int)   # rows on the Closed tab, for its title
    status_changed  = pyqtSignal(str)        # status bar text

    def __init__(self):
        super().__init__()
        self._events, self._outcomes, self._account = [], {}, ""
        self._holdings: dict = {}   # newest statement's Equities snapshot
        self._exposure   = {}     # last _annotate_tags_and_capital() result
        self._range_sel  = None   # None → all time; int → days back; "custom"
        self._range_from = None   # ISO date string when custom
        self._range_to   = None
        self._price_cache   = {}    # {symbol: price} for the session
        self._price_workers = []    # keep refs so running threads aren't GC'd mid-run
        self._price_gen     = 0     # bumped per fetch; stale results are ignored
        self._setup_ui()

        # Wait out any in-flight price fetch on exit so a still-running QThread
        # isn't destroyed mid-run (which aborts the process).
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_price_workers)

    def _stop_price_workers(self):
        for w in list(self._price_workers):
            w.wait(3000)

    def _read_events(self, acct: str) -> tuple[list[dict], dict]:
        """Load one account's stored events/outcomes (empty if it has none yet)."""
        path = events_path(acct)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                outcomes = {_outcome_unkey(k): v for k, v in data.get("outcomes", {}).items()}
                events = data.get("events", [])
                _drop_split_roll_duplicates(events)
                self._holdings = data.get("holdings", {})
                return events, outcomes
            except Exception:
                pass
        self._holdings = {}
        return [], {}

    def _save_events(self):
        events_path(self._account).write_text(json.dumps({
            "account":  self._account,
            "events":   self._events,
            "outcomes": {_outcome_key(k): v for k, v in self._outcomes.items()},
            "holdings": self._holdings,
        }, indent=2))

    def load_account(self, acct: str):
        """Switch the active account: load its events and rebuild the table."""
        self._account = acct
        if acct:
            self._events, self._outcomes = self._read_events(acct)
        else:
            self._events, self._outcomes, self._holdings = [], {}, {}
        self._rebuild_options_table()
        self.account_changed.emit(self._account)

    def apply_range(self, sel, date_from: str | None = None, date_to: str | None = None):
        """Shared time-range filter (sel: None=all, int=days back, 'custom')."""
        # The same range again (an account switch re-applies it right after the
        # load already drew with it) would only redo the work.
        def _key(s, f, t):
            return (s, f, t) if s == "custom" else (s,)
        if _key(sel, date_from, date_to) == _key(self._range_sel, self._range_from, self._range_to):
            return
        self._range_sel  = sel
        self._range_from = date_from
        self._range_to   = date_to
        self._rebuild_options_table()

    def _rebuild_options_table(self):
        from datetime import timedelta
        rows = _reconstruct_wheels(self._events, self._outcomes, self._holdings)

        # Date-range filter — active (Open/Holding) positions always shown.
        # "opened" is MM/DD/YYYY and the bounds are ISO, so compare as ISO:
        # comparing the raw strings put every closed row outside any range.
        sel = self._range_sel
        active = ("Open", "Holding", "Blended")

        def _opened_iso(r):
            d = _parse_mdy(r["opened"])
            return d.isoformat() if d else ""

        if sel == "custom" and self._range_from and self._range_to:
            rows = [r for r in rows
                    if r["status"] in active
                    or self._range_from <= _opened_iso(r) <= self._range_to]
        elif isinstance(sel, int):
            cutoff = (date.today() - timedelta(days=sel)).isoformat()
            rows = [r for r in rows
                    if r["status"] in active or _opened_iso(r) >= cutoff]

        # Exposure is measured on what's on screen — with a range filter active
        # the table is the book you're looking at, and the panel must agree with
        # it. Live positions are never filtered out, so the total is unaffected.
        self._exposure = _annotate_tags_and_capital(rows)
        self._render_exposure()

        rows.sort(key=_row_sort_key)
        live   = [r for r in rows if r["status"] in active]
        closed = [r for r in rows if r["status"] not in active]
        self._live_rows = live
        self._render_live()
        self._fill_table(self._closed_table, _CLOSED_FIELDS, closed, _C_STATUS_COL)
        self.closed_count_changed.emit(len(closed))
        self._refresh_market_prices()

    def _fill_table(self, table, fields, rows, status_col):
        """Refill a Qt-sorted table, keeping whatever sort the user chose.

        Status ascending is the default only until a header is clicked; a
        rebuild (range change, import) re-applies the current indicator.
        """
        header = table.horizontalHeader()
        if header.isSortIndicatorShown() and getattr(table, "_sorted_once", False):
            col, order = header.sortIndicatorSection(), header.sortIndicatorOrder()
        else:
            col, order = status_col, Qt.SortOrder.AscendingOrder
            table._sorted_once = True
        table.setSortingEnabled(False)
        table.blockSignals(True)
        table.setRowCount(0)
        for r in rows:
            self._insert_option_row(r, table, fields)
        table.blockSignals(False)
        header.setSortIndicator(col, order)
        table.setSortingEnabled(True)       # sorts once, on the indicator
        table.resizeColumnsToContents()

    def _render_live(self):
        """Fill the Positions table in grouped order, one Symbol cell per ticker.

        Qt's own sorting is off for this table: it orders rows one at a time
        and cannot keep a ticker's rows together, and it would scatter the row
        spans. The header still shows and flips the indicator; each change
        re-renders from the row dicts.
        """
        table  = self._opt_table
        header = table.horizontalHeader()
        field  = _OPT_FIELDS[header.sortIndicatorSection()]
        desc   = header.sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        groups = _grouped_order(self._live_rows, field, desc, self._price_cache)

        table.blockSignals(True)
        table.clearSpans()
        table.setRowCount(0)
        for g in groups:
            start = table.rowCount()
            for r in g:
                self._insert_option_row(r, table, _OPT_FIELDS)
            if len(g) > 1:
                # Every row keeps its own Symbol/Market item (export and the
                # price refresh read them per row); the span shows the first.
                for col in (_O_SYM_COL, _O_MARKET_COL):
                    table.setSpan(start, col, len(g), 1)
            for col in (_O_SYM_COL, _O_MARKET_COL):
                it = table.item(start, col)
                if it is not None:
                    it.setTextAlignment((Qt.AlignmentFlag.AlignLeft if col == _O_SYM_COL
                                         else Qt.AlignmentFlag.AlignRight)
                                        | Qt.AlignmentFlag.AlignVCenter)
            sym_it = table.item(start, _O_SYM_COL)
            if sym_it is not None:
                f = sym_it.font()
                f.setBold(True)
                sym_it.setFont(f)
        table.blockSignals(False)
        table.resizeColumnsToContents()

    def _on_live_sort_changed(self, _section: int, _order):
        self._render_live()

    def closed_widget(self) -> QWidget:
        """The Closed tab's page. Owned here, since both tables come from one
        reconstruction; the window only decides where it is shown."""
        return self._closed_page

    # ── Tag exposure ──────────────────────────────────────────────────────────

    def _render_exposure(self):
        exp = self._exposure or {}
        total = exp.get("total", 0.0)
        if not total:
            self._exposure_label.setText(
                "<i>No live positions — nothing at risk to concentrate.</i>")
            return

        # A symbol can carry several tags, so its capital counts under each and
        # the percentages deliberately sum past 100%. Said outright, because a
        # column of percentages that doesn't add up otherwise reads as a bug.
        parts = []
        for tag, cap, pct in exp.get("by_tag", []):
            colour = "#8a0000" if pct >= 30 else ("#8a3300" if pct >= 20 else "#333333")
            parts.append(
                f"<span style='color:{colour}'><b>{tag}</b> "
                f"${cap:,.0f} ({pct:.1f}%)</span>"
            )
        html = (f"<b>${total:,.0f}</b> at risk across "
                f"{len(exp.get('by_symbol', []))} symbols &nbsp;·&nbsp; "
                + " &nbsp;|&nbsp; ".join(parts))
        if exp.get("untagged"):
            html += ("<br><span style='color:#8a3300'>Not in durable-tags.json: "
                     + ", ".join(exp["untagged"]) + "</span>")
        html += ("<br><span style='color:grey;font-size:11px'>Tags overlap, so "
                 "these sum past 100% — each is the share of capital carrying "
                 "that tag.</span>")
        self._exposure_label.setText(html)

    def export_for_chat(self):
        """Write live positions + tag exposure to a CSV, for uploading to a chat.

        One file, two sections separated by a blank line — the same shape TOS's
        own statements use. Numbers are written bare (no $, no %, no thousands
        separators) so they arrive as numbers rather than strings; the on-screen
        formatting is for reading, and a CSV is for parsing.
        """
        exp = self._exposure or {}
        total = exp.get("total", 0.0)

        default = (f"positions-{safe_token(self._account)}-{date.today()}.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export positions for chat", default,
            "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return

        def _cell(row, field):
            it = self._opt_table.item(row, _oc(field))
            return it.text() if it else ""

        def _num(s):
            """'$28,800' / '7.8%' → '28800' / '7.8'; anything else unchanged."""
            return s.replace("$", "").replace(",", "").replace("%", "").strip()

        live = []
        for r in range(self._opt_table.rowCount()):
            status = self._opt_table.item(r, _O_STATUS_COL)
            if status is None or status.text() not in _LIVE_STATUSES:
                continue
            live.append([
                _cell(r, "symbol"),
                _cell(r, "qty"),
                _cell(r, "unit"),
                _num(_cell(r, "strike")),
                _num(_cell(r, "capital")),
                _num(_cell(r, "capital_pct")),
                _num(_cell(r, "premium")),
                _cell(r, "expiration"),
                _cell(r, "status"),
                _num(_cell(r, "assn_price")),
                _num(_cell(r, "cost_basis")),
                _num(_cell(r, "recovery")),
                _cell(r, "tags"),
            ])

        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh)
                w.writerow(["Positions", self._account or "account",
                            str(date.today())])
                w.writerow([])
                w.writerow(["Symbol", "Qty", "Unit", "Strike", "Capital", "Pct of Book",
                            "Premium", "Expiration", "Status", "Assn/Buy Price",
                            "Basis", "Recovery", "Tags"])
                w.writerows(live)

                w.writerow([])
                w.writerow(["Tag Exposure", f"{total:.0f}", "at risk"])
                w.writerow([])
                w.writerow(["Tag", "Capital", "Pct of Book"])
                for t, c, p in exp.get("by_tag", []):
                    w.writerow([t, f"{c:.0f}", f"{p:.1f}"])
                w.writerow([])
                w.writerow(["Note", "Tags overlap, so Pct of Book sums past 100%"])
                if exp.get("untagged"):
                    w.writerow(["Not in durable-tags.json",
                                ", ".join(exp["untagged"])])
        except OSError as e:
            QMessageBox.warning(self, "Export", f"Could not write {path}:\n{e}")
            return

        self.status_changed.emit(
            f"Exported {len(live)} live position(s) and tag exposure to {path}")

    # ── Live market prices ──────────────────────────────────────────────────────

    def _refresh_market_prices(self):
        """Fetch current underlying prices (uncached symbols only) in the background."""
        symbols = set()
        for r in range(self._opt_table.rowCount()):
            it = self._opt_table.item(r, _O_SYM_COL)
            if it and it.text().strip():
                symbols.add(it.text().strip().upper())
        missing = sorted(s for s in symbols if s not in self._price_cache)
        if not missing:
            return
        self._price_gen += 1
        worker = _PriceWorker(missing, self._price_gen)
        worker.done.connect(self._on_prices_loaded)
        worker.finished.connect(lambda w=worker: self._price_workers.remove(w)
                                if w in self._price_workers else None)
        self._price_workers.append(worker)
        worker.start()

    def _on_prices_loaded(self, generation: int, prices: dict):
        if generation != self._price_gen:
            return                                  # superseded by a newer fetch
        self._price_cache.update(prices)
        if _OPT_FIELDS[self._opt_table.horizontalHeader().sortIndicatorSection()] == "market":
            self._render_live()           # the order itself depends on the prices
            return
        for r in range(self._opt_table.rowCount()):
            sym_it = self._opt_table.item(r, _O_SYM_COL)
            if not sym_it:
                continue
            price = self._price_cache.get(sym_it.text().strip().upper())
            mkt_it = self._opt_table.item(r, _O_MARKET_COL)
            if mkt_it is not None:
                mkt_it.setText(_fmt_price(price))
        self._opt_table.resizeColumnToContents(_O_MARKET_COL)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        hint = QLabel(
            "Live wheel cycles (Put → Assigned → … ), one per line; finished ones "
            "are on the Closed tab. RoR includes all premiums net of fees. "
            "Derived from imported TOS statements."
        )
        hint.setStyleSheet("color: grey; font-size: 11px;")
        root.addWidget(hint)

        self._opt_table = self._make_table(_O_HDRS)
        for ci in _OPT_HIDDEN_COLS:
            self._opt_table.setColumnHidden(ci, True)
        # Grouped sort (see _render_live): Qt sorting off, header indicator kept.
        self._live_rows: list[dict] = []
        self._opt_table.setSortingEnabled(False)
        _oh = self._opt_table.horizontalHeader()
        _oh.setSectionsClickable(True)
        _oh.setSortIndicatorShown(True)
        _oh.setSortIndicator(_O_SYM_COL, Qt.SortOrder.AscendingOrder)
        _oh.sortIndicatorChanged.connect(self._on_live_sort_changed)
        root.addWidget(self._opt_table, 1)

        # The Closed tab's page — finished cycles, kept off the live table.
        self._closed_page = QWidget()
        cl = QVBoxLayout(self._closed_page)
        cl.setSpacing(6)
        chint = QLabel(
            "Completed wheel cycles: expired, closed, sold or called away. "
            "P&L is all premiums + stock outcome, net of fees. "
            "Filtered by the Range selector on the date each cycle opened."
        )
        chint.setStyleSheet("color: grey; font-size: 11px;")
        cl.addWidget(chint)
        self._closed_table = self._make_table([c[1] for c in _CLOSED_COLUMNS])
        cl.addWidget(self._closed_table, 1)

        self._exposure_box = QGroupBox("Tag exposure — live positions")
        ebox = QVBoxLayout(self._exposure_box)
        ebox.setContentsMargins(8, 4, 8, 6)
        self._exposure_label = QLabel("—")
        self._exposure_label.setTextFormat(Qt.TextFormat.RichText)
        self._exposure_label.setWordWrap(True)
        ebox.addWidget(self._exposure_label)
        root.addWidget(self._exposure_box)

    # ── shared helpers ────────────────────────────────────────────────────────

    def _make_table(self, headers: list[str]) -> QTableWidget:
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSortingEnabled(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hdr.setStretchLastSection(True)
        table.verticalHeader().setVisible(False)
        return table

    def _make_item(self, text: str, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    # ── Options rows ──────────────────────────────────────────────────────────

    def _insert_option_row(self, data: dict | None = None, table=None,
                           fields=None) -> int:
        """Insert one derived lifecycle row (all fields supplied; read-only)."""
        table  = table if table is not None else self._opt_table
        fields = fields or _OPT_FIELDS
        row = table.rowCount()
        table.insertRow(row)
        d = data or {}
        for ci, field in enumerate(fields):
            if field == "market":
                price = self._price_cache.get(d.get("symbol", ""))
                item = self._make_item(_fmt_price(price), editable=False)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            elif field == "status":
                val = str(d.get(field, ""))
                item = _StatusItem(val, _row_sort_key(d))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            elif field == "symbol":
                item = _SymbolItem(str(d.get(field, "")), d)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            else:
                item = self._make_item(str(d.get(field, "")), editable=False)
            table.setItem(row, ci, item)
        return row

    # ── persistence ───────────────────────────────────────────────────────────

    # ── CSV import ────────────────────────────────────────────────────────────

    def import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import TOS Account Statement", "",
            "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return

        rows = list(csv.reader(io.StringIO(
            Path(path).read_text(encoding="utf-8-sig", errors="replace"))))
        account      = (_parse_account(rows) or "").strip()
        new_events   = _collect_cash_events(rows)
        new_outcomes = _build_rad_outcomes(rows)
        _, opt_warns, stks, stk_warns = _parse_tos_csv(Path(path))

        if not new_events and not stks:
            all_warns = opt_warns + stk_warns
            msg = "No option or equity positions found."
            if all_warns:
                msg += "\n\n" + "\n".join(all_warns[:5])
            QMessageBox.warning(self, "Import", msg)
            return

        # Route to the statement's own account bucket. Importing a different
        # account's statement loads (or creates) that account and merges into it,
        # never discarding the account we were previously viewing.
        target = account or self._account or DEFAULT_ACCT
        switched = bool(self._account and target != self._account)
        if target != self._account:
            self._account = target
            self._events, self._outcomes = self._read_events(target)

        def _cell(table, row, col):
            it = table.item(row, col)
            return it.text().strip() if it else ""

        # Events stored before a field existed (action/roll) can't be repaired by
        # re-importing — the uid is unchanged, so the merge below adds nothing.
        # Patch them in place from their incoming twin instead.
        by_uid: dict[tuple, list[dict]] = {}
        for e in self._events:
            by_uid.setdefault(_event_uid(e), []).append(e)
        upgraded = 0
        for e in new_events:
            for old in by_uid.get(_event_uid(e), ()):
                missing = [k for k in ("action", "roll", "close_exp")
                           if k in e and k not in old]
                if missing:
                    old.update({k: e[k] for k in missing})
                    upgraded += 1

        # Accumulate cash events across imports by MAX multiplicity per uid.
        # A statement is complete for its date range, so identical partial fills
        # within one statement are kept, while overlapping statements (which
        # repeat the same fills) don't double-count.
        # Identical fills can still carry different fees (one pays the
        # commission, its twin doesn't), so every fill is kept in order rather
        # than copying the first one — and stored twins take the statement's
        # fees fill for fill, which repairs histories imported before this.
        existing: dict[tuple, list[dict]] = {}
        for e in self._events:
            existing.setdefault(_event_uid(e), []).append(e)
        incoming: dict[tuple, list[dict]] = {}
        for e in new_events:
            incoming.setdefault(_event_uid(e), []).append(e)

        added_evs = 0
        for uid, fills in incoming.items():
            have = existing.get(uid, [])
            for old, new in zip(have, fills):
                if old.get("fees") != new.get("fees"):
                    old["fees"] = new.get("fees", 0.0)
                    upgraded += 1
            for new in fills[len(have):]:
                self._events.append(new)
                added_evs += 1

        # A roll shown as one row here and as two legs in an overlapping
        # statement has different uids, so the merge above keeps both.
        added_evs -= _drop_split_roll_duplicates(self._events)

        self._outcomes.update(new_outcomes)
        # Holdings as of the statement's end date. Only a newer statement
        # replaces them: importing an old one must not roll the snapshot back.
        asof = _parse_statement_end(rows)
        cur  = _parse_mdy(self._holdings.get("asof", "")) if self._holdings else None
        if asof and (cur is None or asof >= cur):
            equities = {}
            for h in stks:
                try:
                    equities[h["symbol"]] = {"shares": float(h["shares"]),
                                             "price":  float(h["cost_basis"])}
                except (KeyError, ValueError):
                    continue
            self._holdings = {"asof": asof.strftime("%m/%d/%Y"), "equities": equities}
        self._save_events()
        self._rebuild_options_table()

        opt_rows = self._opt_table.rowCount() + self._closed_table.rowCount()
        note = f"Imported {added_evs} new event(s) → {opt_rows} cycle(s)."
        if upgraded:
            note += f" Upgraded {upgraded} stored event(s)."
        if switched:
            note = f"Switched to account {self._account}. " + note
        self.status_changed.emit(note)
        self.account_changed.emit(self._account)
        self.csv_imported.emit(path, self._account)

    def _reset_data(self):
        """Wipe the active account's stored events (no confirmation)."""
        self._live_rows = []
        self._opt_table.clearSpans()
        for table in (self._opt_table, self._closed_table):
            table.blockSignals(True)
            table.setRowCount(0)
            table.blockSignals(False)
        self.closed_count_changed.emit(0)
        if self._account:
            events_path(self._account).unlink(missing_ok=True)
        POSITIONS_FILE.unlink(missing_ok=True)
        STOCKS_FILE.unlink(missing_ok=True)
        SUPERSEDED_FILE.unlink(missing_ok=True)
        self._events, self._outcomes, self._holdings = [], {}, {}
        self._exposure = {}
        self._render_exposure()

    def clear_all(self):
        """Erase the active account's stored history."""
        acct = self._account
        self._reset_data()
        self.status_changed.emit(f"Cleared account {acct}." if acct else "Cleared.")


# ── TOS CSV parsing ───────────────────────────────────────────────────────────

# Month abbreviation → number  (JAN→1 … DEC→12)
_MONTH = {m.upper(): i for i, m in enumerate(calendar.month_abbr) if m}

# Section headers that end the Options block
_SECTION_BREAKS = {"Profits and Losses", "Account Summary", "Equities",
                   "Futures Statements", "Forex Statements"}

# Cash Balance TRD description patterns
# Calendar roll: "SOLD -4 CALENDAR GAP 100 (Weeklys) 29 MAY 26/22 MAY 26 22 PUT @.67 CBOE"
_TRD_CAL_RE = re.compile(
    r'(?:SOLD|BOT)\s+\S+\s+CALENDAR\s+([A-Z]{1,10})\s+100'
    r'.*?(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'   # TO-OPEN expiration
    r'/(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'      # TO-CLOSE expiration
    r'\s+([\d.]+)\s+(PUT|CALL)',
    re.IGNORECASE,
)
# Diagonal roll — like a calendar but the strike moves too, so it carries a
# to-open and a to-close strike:
# "SOLD -2 DIAGONAL ICE 100 18 DEC 26/31 JUL 26 140/130 CALL @2.00 CBOE"
_TRD_DIAG_RE = re.compile(
    r'(?:SOLD|BOT)\s+\S+\s+DIAGONAL\s+([A-Z]{1,10})\s+100'
    r'.*?(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'   # TO-OPEN expiration
    r'/(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'      # TO-CLOSE expiration
    r'\s+([\d.]+)/([\d.]+)\s+(PUT|CALL)',   # TO-OPEN / TO-CLOSE strike
    re.IGNORECASE,
)
# Vertical spread — one expiration, two strikes:
# "BOT +3 VERTICAL DVN 100 (Weeklys) 26 JUN 26 43/43.5 CALL @.26 CBOE"
_TRD_VERT_RE = re.compile(
    r'(?:SOLD|BOT)\s+\S+\s+VERTICAL\s+([A-Z]{1,10})\s+100'
    r'.*?(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'
    r'\s+([\d.]+)/([\d.]+)\s+(PUT|CALL)',
    re.IGNORECASE,
)
# Single leg: "SOLD -2 KTOS 100 (Weeklys) 22 MAY 26 50 PUT @.71 CBOE"
_TRD_SINGLE_RE = re.compile(
    r'(?:SOLD|BOT)\s+\S+\s+([A-Z]{1,10})\s+100'
    r'.*?(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'
    r'\s+([\d.]+)\s+(PUT|CALL)',
    re.IGNORECASE,
)


def _parse_mdy(s: str) -> date | None:
    """Parse 'MM/DD/YYYY' (or M/D/YY) → date, or None."""
    parts = s.strip().split("/")
    if len(parts) != 3:
        return None
    try:
        m, d, y = (int(p) for p in parts)
        if y < 100:
            y += 2000
        return date(y, m, d)
    except ValueError:
        return None


def _norm_strike(s: str) -> str:
    """Normalize strike to a canonical string ('22.0' and '22' both → '22')."""
    try:
        return str(float(s.strip())).rstrip("0").rstrip(".")
    except ValueError:
        return s.strip()


def _parse_exp(exp: str) -> str:
    """Convert TOS expiration to MM/DD/YYYY.
    '29 MAY 26' → '05/29/2026'   '5/22/26' → '05/22/2026'
    """
    parts = exp.strip().split()
    if len(parts) == 3:
        day, mon, yr = parts
        mn = _MONTH.get(mon.upper())
        if mn:
            yr_i = int(yr)
            if yr_i < 100:
                yr_i += 2000
            return f"{mn:02d}/{int(day):02d}/{yr_i}"
    slash = exp.split("/")
    if len(slash) == 3 and len(slash[2]) == 2:
        slash[2] = "20" + slash[2]
    return "/".join(slash) if len(slash) == 3 else exp


def _build_equity_date_lookup(rows: list[list[str]]) -> dict[str, str]:
    """Return {SYMBOL: oldest_open_date} for equity BUY TO OPEN from Account Trade History.

    History is newest-first, so overwriting on each match leaves the oldest date.
    """
    lookup: dict[str, str] = {}
    in_section = False
    col: dict[str, int] = {}

    for row in rows:
        if not row:
            continue
        cells = [c.strip().strip('"') for c in row]
        first = cells[0]

        if first == "Account Trade History":
            in_section = True
            col = {}
            continue

        if not in_section:
            continue
        if first:
            break
        if not col:
            col = {h.strip().lower(): i for i, h in enumerate(cells)}
            continue

        try:
            exec_time  = cells[col["exec time"]].strip()
            side       = cells[col["side"]].strip()
            pos_effect = cells[col["pos effect"]].strip()
            sym        = cells[col["symbol"]].strip().upper()
            exp        = cells[col["exp"]].strip()
            typ        = cells[col["type"]].strip().upper()
        except (KeyError, IndexError):
            continue

        if side != "BUY" or pos_effect != "TO OPEN":
            continue
        if typ in ("PUT", "CALL") or exp:   # skip options
            continue
        if not sym or not exec_time:
            continue

        date_part = exec_time.split()[0]
        parts = date_part.split("/")
        if len(parts) == 3:
            try:
                m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
                if y < 100:
                    y += 2000
                lookup[sym] = f"{m:02d}/{d:02d}/{y}"   # overwrite → keeps oldest
            except ValueError:
                pass

    return lookup


def _scan_option_legs(rows: list[list[str]]) -> dict:
    """Scan Account Trade History SELL TO OPEN option legs and roll provenance.

    Returns dict with:
      prices  — {key: net credit/share for that leg}      (first = most recent)
      opens   — {key: open date MM/DD/YYYY}               (last write = oldest)
      qty     — {key: contract count}
      rolled_from — {new_key: old_key}  roll provenance

    History is newest-first.  A calendar (roll) trade is a SELL TO OPEN row
    followed by a BUY TO CLOSE continuation row (empty Exec Time) naming the
    expiration it rolled *from*.  key = (SYM, exp_MM/DD/YYYY, strike, TYPE).
    """
    prices:      dict[tuple, float] = {}
    opens:       dict[tuple, str]   = {}
    qty:         dict[tuple, int]   = {}
    rolled_from: dict[tuple, tuple] = {}

    in_section = False
    col: dict[str, int] = {}
    last_cal_key: tuple | None = None

    for row in rows:
        if not row:
            continue
        cells = [c.strip().strip('"') for c in row]
        first = cells[0]

        if first == "Account Trade History":
            in_section = True
            col = {}
            last_cal_key = None
            continue
        if not in_section:
            continue
        if first:           # non-empty first cell = next section header
            break
        if not col:
            col = {h.strip().lower(): i for i, h in enumerate(cells)}
            continue

        try:
            exec_time  = cells[col["exec time"]].strip()
            spread     = cells[col["spread"]].strip().upper()
            side       = cells[col["side"]].strip()
            pos_effect = cells[col["pos effect"]].strip()
            sym        = cells[col["symbol"]].strip().upper()
            exp        = cells[col["exp"]].strip()
            strike     = cells[col["strike"]].strip()
            typ        = cells[col["type"]].strip().upper()
            net_price  = cells[col["net price"]].strip()
            qty_raw    = cells[col["qty"]].strip()
        except (KeyError, IndexError):
            last_cal_key = None
            continue

        if typ not in ("PUT", "CALL"):
            last_cal_key = None
            continue

        key = (sym, _parse_exp(exp), _norm_strike(strike), typ)

        if side == "SELL" and pos_effect == "TO OPEN":
            try:
                price = float(net_price)
            except ValueError:
                last_cal_key = None
                continue

            opened    = ""
            date_part = exec_time.split()[0] if exec_time else ""
            d = _parse_mdy(date_part)
            if d:
                opened = f"{d.month:02d}/{d.day:02d}/{d.year}"

            if key not in prices:                # first = most recent leg credit
                prices[key] = price
            if key not in qty:
                try:
                    qty[key] = abs(int(float(qty_raw)))
                except ValueError:
                    qty[key] = 0
            if opened:
                opens[key] = opened              # overwrite → last = oldest

            last_cal_key = key if spread == "CALENDAR" else None

        elif (side == "BUY" and pos_effect == "TO CLOSE"
              and last_cal_key is not None and not exec_time):
            rolled_from[last_cal_key] = key
            last_cal_key = None
        else:
            last_cal_key = None

    return {"prices": prices, "opens": opens, "qty": qty, "rolled_from": rolled_from}


def _walk_chain(head: tuple, legs: dict, fees: dict) -> dict:
    """Walk a roll chain back from its head, summing premium & fees, oldest open date."""
    cur, seen = head, set()
    total_prem = 0.0
    total_fee  = 0.0
    opened     = ""
    while True:
        total_prem += legs["prices"].get(cur, 0.0)
        total_fee  += fees.get(cur, 0.0)
        if cur in legs["opens"]:
            opened = legs["opens"][cur]
        if cur not in legs["rolled_from"] or cur in seen:
            break
        seen.add(cur)
        cur = legs["rolled_from"][cur]
    return {"premium": round(total_prem, 4), "fees": round(total_fee, 2), "opened": opened}


def _build_premium_lookup(rows: list[list[str]]) -> dict[tuple, dict]:
    """Return {key: {"price": str cumulative credit/share, "opened": MM/DD/YYYY}}
    for every sold-to-open option leg, premium summed across its roll chain."""
    legs = _scan_option_legs(rows)
    lookup: dict[tuple, dict] = {}
    for key in legs["prices"]:
        chain = _walk_chain(key, legs, {})
        lookup[key] = {
            "price":  str(chain["premium"]),
            "opened": chain["opened"] or legs["opens"].get(key, ""),
        }
    return lookup


def _build_rad_outcomes(rows: list[list[str]]) -> dict[tuple, str]:
    """Parse Cash Balance RAD rows → {key: 'Expired' | 'Assigned'}."""
    rad_re = re.compile(
        r'Removed due to (Assignment|Expiration) (PUT|CALL).*?\$([\d.]+) '
        r'EXP (\d{1,2}/\d{1,2}/\d{2}):\s*(?:ASG|EXP):?\s*[\d.]+\s+([A-Z]+)\s+100',
        re.IGNORECASE)
    out: dict[tuple, str] = {}
    in_cb = False
    for row in rows:
        cells = [c.strip().strip('"') for c in row]
        if not cells:
            continue
        if cells[0] == "Cash Balance":
            in_cb = True
            continue
        if cells[0] == "Account Trade History":
            break
        if in_cb and len(cells) > 4 and cells[2] == "RAD":
            m = rad_re.search(cells[4])
            if m:
                outcome, typ, strike, exp, sym = m.groups()
                key = (sym.upper(), _parse_exp(exp), _norm_strike(strike), typ.upper())
                out[key] = "Assigned" if outcome.upper() == "ASSIGNMENT" else "Expired"
    return out


# ── Wheel lifecycle reconstruction ──────────────────────────────────────────────

_OPT_QTY_RE  = re.compile(r'\b(SOLD|BOT)\s+([+-]?\d+)', re.IGNORECASE)
_STK_DESC_RE = re.compile(
    r'(BOT|SOLD)\s+([+-]?\d+(?:\.\d+)?)\s+([A-Z]+)\s+(@|UPON)', re.IGNORECASE)


def _money(s: str) -> float:
    s = s.strip().replace(",", "").replace("$", "")
    if not s:
        return 0.0
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        return -float(s) if neg else float(s)
    except ValueError:
        return 0.0


def _collect_cash_events(rows: list[list[str]]) -> list[dict]:
    """Parse Cash Balance into per-symbol option-premium and stock events (dollars)."""
    events: list[dict] = []
    in_cb = False
    for row in rows:
        cells = [c.strip().strip('"') for c in row]
        if not cells:
            continue
        if cells[0] == "Cash Balance":
            in_cb = True
            continue
        if cells[0] == "Account Trade History":
            break
        if not in_cb or len(cells) < 8:
            continue

        d = _parse_mdy(cells[0])
        if d is None:
            continue
        date_str = f"{d.month:02d}/{d.day:02d}/{d.year}"
        typ    = cells[2]
        desc   = cells[4] if len(cells) > 4 else ""
        fees   = (abs(_money(cells[5])) + abs(_money(cells[6]))) if len(cells) > 6 else 0.0
        amount = _money(cells[7])

        if typ == "TRD" and re.search(r'\b(PUT|CALL)\b', desc, re.IGNORECASE):
            # Specific spread shapes first: the single-leg pattern can't match
            # them (the symbol slot would have to be DIAGONAL/VERTICAL), so
            # without these the row is skipped and its cash never lands.
            close_strike = close_exp = None
            cal  = _TRD_CAL_RE.search(desc)
            diag = None if cal else _TRD_DIAG_RE.search(desc)
            vert = None if (cal or diag) else _TRD_VERT_RE.search(desc)
            m = cal or diag or vert or _TRD_SINGLE_RE.search(desc)
            if not m:
                continue
            if diag:
                sym, exp, close_exp, strike, close_strike, otyp = m.groups()
            elif cal:
                sym, exp, close_exp, strike, otyp = m.groups()
            elif vert:
                sym, exp, strike, close_strike, otyp = m.groups()
            else:
                sym, exp, strike, otyp = m.groups()
            roll = cal or diag           # both close one leg and open another
            qm  = _OPT_QTY_RE.search(desc)
            qty = abs(int(qm.group(2))) if qm else 0
            ev = {
                "sym": sym.upper(), "date": date_str, "kind": "opt",
                "otype": otyp.upper(), "amount": round(amount, 2), "fees": round(fees, 2),
                "strike": _norm_strike(strike), "exp": _parse_exp(exp.strip()), "qty": qty,
                # Direction and roll-ness decide whether a leg opens or closes
                # contracts; without them a buy-to-close reads as another sale.
                "action": qm.group(1).upper() if qm else "SOLD",
                "roll": roll is not None,
            }
            if close_exp is not None:
                ev["close_exp"] = _parse_exp(close_exp.strip())
            # A diagonal also retires a leg at a *different* strike, which lives
            # under its own cycle and would otherwise never learn it was closed.
            if close_strike is not None:
                cs = _norm_strike(close_strike)
                if cs != ev["strike"]:
                    ev["close_strike"] = cs
            events.append(ev)
        elif typ in ("TRD", "EXP"):
            m = _STK_DESC_RE.search(desc)
            if not m:
                continue
            action, sh, sym, how = m.groups()
            events.append({
                "sym": sym.upper(), "date": date_str, "kind": "stock",
                "action": action.upper(), "shares": abs(float(sh)),
                "amount": round(amount, 2), "fees": round(fees, 2),
                "assigned": how.upper() == "UPON",
            })
    return events


def _drop_split_roll_duplicates(events: list[dict]) -> int:
    """Remove leg pairs that repeat a CALENDAR/DIAGONAL event, in place.

    Statements don't agree on how to show a roll: one lists it as a single
    CALENDAR/DIAGONAL row with its net credit, an overlapping one lists the
    same order as its buy-to-close and sell-to-open legs. Their uids differ,
    so both survive the merge and the roll's premium counts twice. A leg pair
    is dropped only when it matches a roll's date, symbol, type, quantity,
    strikes and (when the roll recorded it) to-close expiration, and its
    amounts add up to the roll's. Returns the number of events removed.
    """
    singles: dict[tuple, list[int]] = {}
    rolls: list[dict] = []
    for i, e in enumerate(events):
        if e.get("kind") != "opt" or "action" not in e:
            continue
        if e.get("roll"):
            rolls.append(e)
        elif not e.get("close_strike"):          # verticals carry one too
            key = (e["date"], e["sym"], e["otype"], e["action"], e["qty"], e["strike"])
            singles.setdefault(key, []).append(i)

    drop: set[int] = set()
    for r in rolls:
        opp = "BOT" if r["action"] == "SOLD" else "SOLD"
        opens  = singles.get((r["date"], r["sym"], r["otype"], r["action"],
                              r["qty"], r["strike"]), [])
        closes = singles.get((r["date"], r["sym"], r["otype"], opp, r["qty"],
                              r.get("close_strike", r["strike"])), [])
        pair = next(((a, b) for a in opens if a not in drop
                     for b in closes if b not in drop
                     if events[a]["exp"] == r["exp"]
                     and events[b]["exp"] == r.get("close_exp", events[b]["exp"])
                     and abs(events[a]["amount"] + events[b]["amount"]
                             - r["amount"]) < 0.05), None)
        if pair:
            drop.update(pair)

    if drop:
        events[:] = [e for i, e in enumerate(events) if i not in drop]
    return len(drop)


def _parse_statement_end(rows: list[list[str]]) -> date | None:
    """The 'through M/D/YY' date in the statement header."""
    for row in rows[:3]:
        if row:
            m = re.search(r'through\s+(\d{1,2}/\d{1,2}/\d{2,4})', row[0])
            if m:
                return _parse_mdy(m.group(1))
    return None


def _parse_account(rows: list[list[str]]) -> str:
    """Extract the account label from the statement header line."""
    for row in rows[:3]:
        if not row:
            continue
        m = re.search(r'Account Statement for (.+?)\s+since', row[0])
        if m:
            return m.group(1).strip()
    return ""


def _event_uid(e: dict) -> tuple:
    if e["kind"] == "opt":
        return (e["sym"], e["date"], "opt", e["otype"], e["strike"], e["exp"],
                e["qty"], e["amount"])
    return (e["sym"], e["date"], "stock", e["action"], e["shares"], e["amount"])


def _outcome_key(k: tuple) -> str:
    return "|".join(k)


def _outcome_unkey(s: str) -> tuple:
    return tuple(s.split("|"))


def _normalize_verticals(events: list[dict]) -> list[dict]:
    """Read a vertical spread row as the roll it is.

    A vertical closes a short at one strike and opens one at another, same
    expiration — a diagonal roll without the change of date. It's stored as
    parsed ("BOT +3 VERTICAL DVN … 43/43.5 CALL": a BOT at 43 naming 43.5),
    which reads backwards: the BOT closes the 43 and the 43.5 is sold. A
    "SOLD" vertical opens its first strike and closes the second, which is
    already the diagonal shape. Both become a roll that sells the new strike
    and closes the old one. Stored events are not changed — the strike is
    part of their merge identity.
    """
    out = []
    for e in events:
        if (e.get("kind") == "opt" and not e.get("roll") and e.get("close_strike")
                and not e.get("close_exp")):
            if e.get("action") == "BOT":
                e = {**e, "action": "SOLD", "strike": e["close_strike"],
                     "close_strike": e["strike"]}
            e = {**e, "roll": True, "close_exp": e["exp"]}
        out.append(e)
    return out


def _reconcile_holdings(events: list[dict], holdings: dict | None) -> list[dict]:
    """The events plus the transfers that square them with the statement."""
    seeded, moved = holdings_adjustments(events, holdings)
    return seeded + events + moved


def holdings_adjustments(events: list[dict], holdings: dict | None
                         ) -> tuple[list[dict], list[dict]]:
    """Transfers that square the share count from the trades with the statement.

    Returns (transfers in, transfers out) as synthetic stock events. Shared
    with the P&L charts, which need the same shares in the same account.

    The trades miss shares in both directions. Shares held from before the
    earliest imported statement (or moved in from another account) have no
    BOT row, so without an opening lot they don't exist here — and neither
    does the position their covered calls belong to. Shares moved out to
    another account have no SOLD row, so they'd be held here forever.

    The newest statement's Equities section lists every holding with its
    average cost. Shares it lists that the trades can't account for become one
    opening lot at the start of the history; shares the trades hold that it
    doesn't list leave on its date as a transfer at average cost — no gain or
    loss, since nothing was sold. The stored events are not changed.
    """
    if not holdings or "equities" not in holdings or not events:
        return [], []                      # no snapshot yet: trades only
    snap = holdings["equities"]
    asof = _parse_mdy(holdings.get("asof", "")) or date.max
    first = min((_parse_mdy(e["date"]) or date.max) for e in events)
    book = ShareBook()                          # shares and cost from events
    bought: dict[str, list] = {}                # sym → [outright shares, last buy date]
    for e in sorted(events, key=lambda e: _parse_mdy(e["date"]) or date.min):
        if e["kind"] != "stock" or (_parse_mdy(e["date"]) or date.min) > asof:
            continue
        if e["action"] == "BOT" and not e["assigned"]:
            b = bought.setdefault(e["sym"], [0.0, e["date"]])
            b[0] += e["shares"]
            b[1] = e["date"]
        book.apply(e["sym"], e["action"], e["shares"], e["amount"])
    held = {sym: [sh, tc] for sym, sh, tc in book.held()}
    seeded = []
    for sym, h in snap.items():
        shares, price = float(h.get("shares", 0)), float(h.get("price", 0))
        have, have_cost = held.get(sym, [0.0, 0.0])
        missing = shares - have
        if missing <= 0.0001 or price <= 0:
            continue
        # The snapshot's price averages every share held; the lot carries
        # whatever part of that total the known purchases don't.
        lot_cost = shares * price - have_cost
        if lot_cost <= 0:
            lot_cost = missing * price
        seeded.append({"sym": sym, "date": first.strftime("%m/%d/%Y"),
                       "kind": "stock", "action": "BOT", "shares": missing,
                       "amount": -round(lot_cost, 2), "fees": 0.0,
                       "assigned": False, "pre_history": True})
    # When shares left isn't on the statement. Shares bought outright and moved
    # to another account usually go soon after purchase, and leaving them on
    # the books until the statement date would let them claim covered calls
    # written on the shares that stayed — so outright shares leave right after
    # the last outright buy, and only any excess beyond them on the statement
    # date. The amount is filled in at the lot's own cost when it's applied.
    moved = []
    for sym, (have, _) in held.items():
        listed = float((snap.get(sym) or {}).get("shares", 0))
        excess = have - listed
        if excess <= 0.0001 or have <= 0:
            continue
        out_sh, out_date = bought.get(sym, [0.0, None])
        early = min(excess, out_sh)
        for n, d in ((early, out_date), (excess - early, asof.strftime("%m/%d/%Y"))):
            if n > 0.0001 and d:
                moved.append({"sym": sym, "date": d, "kind": "stock",
                              "action": "SOLD", "shares": n, "amount": 0.0,
                              "fees": 0.0, "assigned": False, "transfer_out": True})

    # What each transfer out carried: the average cost of the shares held when
    # it left. (The Positions tab prices it off the lot it leaves instead; the
    # two agree when, as usual, only outright-bought shares were held.)
    book = ShareBook()
    for e in sorted(seeded + events + moved, key=lambda e: _parse_mdy(e["date"]) or date.min):
        if e["kind"] != "stock":
            continue
        _, removed = book.apply(e["sym"], e["action"], e["shares"], e["amount"],
                                bool(e.get("transfer_out")))
        if e.get("transfer_out"):
            e["cost"] = round(removed, 2)
    return seeded, moved


def _reconstruct_wheels(events: list[dict], outcomes: dict[tuple, str],
                        holdings: dict | None = None) -> list[dict]:
    """Group per-symbol events into positions → one display row per position.

    Wheel cycles start from a short put; shares bought outright start a long
    stock position (their covered calls join it); a bought option to open is a
    long option position of its own.
    """
    events = _reconcile_holdings(_normalize_verticals(events), holdings)
    # Shares bought outright are shown only once a statement's holdings have
    # been imported: without them, shares moved to another account look held
    # here forever (no statement row records a transfer), so an account with
    # no snapshot yet keeps the old behaviour and skips outright buys.
    track_bought = bool(holdings and "equities" in holdings)
    by_sym: dict[str, list[dict]] = {}
    for e in events:
        by_sym.setdefault(e["sym"], []).append(e)

    cycles: list[dict] = []
    for sym, evs in by_sym.items():
        evs.sort(key=lambda e: _parse_mdy(e["date"]) or date.min)
        # Concurrent short puts on one symbol are separate positions with their
        # own strike, open date and premium, so each gets its own cycle keyed by
        # strike. A roll keeps its strike and stays on the same cycle; a roll
        # that changes strike is still the same position moving, so a sale that
        # lands the same day a cycle went flat adopts that cycle under its new
        # strike rather than starting a fresh line.
        live: dict[str, dict] = {}          # strike → open cycle
        mine: list[dict] = []               # this symbol's cycles, in open order

        def _new(key, open_date):
            # premium/fees cover every leg; call_premium/call_fees are the
            # calls' part of them, kept apart because only put premium lowers
            # the cost basis of assigned shares — calls lower the recovery price.
            c = {"sym": sym, "open_date": open_date, "premium": 0.0, "fees": 0.0,
                 "call_premium": 0.0, "call_fees": 0.0,
                 "stock_pnl": 0.0, "stages": [], "put_strike": None, "put_exp": None,
                 "qty_by_exp": {}, "end_date": None, "shares": 0.0, "total_cost": 0.0,
                 "has_option": False, "has_put": False, "open_qty": 0,
                 "roll_group": None, "qty_known": True, "closed_date": None,
                 "live_key": key, "stock_qty": 0, "calls": {}}
            live[key] = c
            mine.append(c)
            cycles.append(c)
            return c

        def _retire(key):
            live.pop(key, None)

        def _holder():
            """The cycle carrying the shares (assigned stock and its calls)."""
            for c in live.values():
                if c["shares"] > 0:
                    return c
            return None

        def _holders():
            return [c for c in live.values() if c["shares"] > 0]

        def _open_calls(c, on):
            """Contracts of calls still open on this cycle as of `on`. Expiring
            worthless leaves no event, so a call past its expiration is over."""
            return sum(q for (_, exp), q in c["calls"].items()
                       if q > 0 and (_parse_mdy(exp) or date.max) >= on)

        def _call_holder(e):
            """The share cycle a call belongs to, when more than one holds shares.

            A call can't say which lot it covers, so closing one goes to the
            cycle that has it open, and a new one goes to the cycle with
            uncovered shares — the one whose free contracts match exactly,
            if any does.
            """
            holders = _holders()
            if len(holders) <= 1:
                return holders[0] if holders else None
            on = _parse_mdy(e["date"]) or date.min
            if e.get("action") == "BOT" or e.get("roll"):
                # A roll's to-close leg says which call it replaces.
                leg = ((e.get("close_strike") or e["strike"], e.get("close_exp"))
                       if e.get("roll") else (e["strike"], e["exp"]))
                for c in holders:
                    if c["calls"].get(leg, 0) > 0:
                        return c
                if e.get("action") == "BOT":
                    return holders[0]
            free = [(int(c["shares"] // 100) - _open_calls(c, on), c) for c in holders]
            for f, c in free:
                if f == e["qty"]:
                    return c
            return next((c for f, c in free if f >= e["qty"]),
                        next((c for f, c in free if f > 0), holders[0]))

        def _sale_holder(e):
            """The share cycle a stock sale comes out of. Called away settles at
            the call's strike, which finds the cycle that wrote that call."""
            holders = _holders()
            if len(holders) <= 1 or not e["assigned"] or not e["shares"]:
                return holders[0] if holders else None
            px = e["amount"] / e["shares"]
            wrote = [c for c in holders
                     if any(q > 0 and abs(float(k) - px) < 0.005
                            for (k, _), q in c["calls"].items())]
            return next((c for c in wrote if c["shares"] >= e["shares"]),
                        wrote[0] if wrote else holders[0])

        # Contracts moved by each diagonal put roll, summed over its fills (one
        # order often fills in pieces). Comparing that with what is open at
        # the old strike tells a whole-position roll from a partial one.
        def _diag(e):
            return (e["date"], e["exp"], e["strike"], e.get("close_strike"))
        diag_qty: dict[tuple, int] = {}
        for e in evs:
            if (e["kind"] == "opt" and e.get("otype") == "PUT" and e.get("roll")
                    and e.get("close_strike") and e["close_strike"] != e["strike"]):
                diag_qty[_diag(e)] = diag_qty.get(_diag(e), 0) + e["qty"]
        diag_done: set = set()

        def _short_open(e) -> bool:
            """Is a short contract open on this exact leg (so a BOT closes it)?"""
            leg = (e["strike"], e["exp"])
            if e["otype"] == "CALL":
                return any(c["calls"].get(leg, 0) > 0 for c in mine)
            c = live.get(e["strike"])
            return bool(c and c["qty_by_exp"].get(e["exp"], 0) > 0)

        def _exercise_long_call(e, px):
            """Shares bought UPON exercise of a long call at ``px``, if one is open.

            The call becomes the shares: the row carries on as a holding, at
            the strike, with what the call cost still in its premium — so Cost
            Basis is strike + call cost per share, and Recovery stays honest.
            Part of a position exercised splits off pro rata, the rest stays
            a long call.
            """
            c = next((c for c in live.values()
                      if c.get("long") and c["long"]["type"] == "CALL"
                      and c.get("long_qty", 0) > 0
                      and abs(float(c["long"]["strike"]) - px) < 0.005), None)
            if c is None:
                return None
            n = min(int(e["shares"] // 100), c["long_qty"])
            if n <= 0:
                return None
            if n < c["long_qty"]:
                part = n / c["long_qty"]
                stock = _new(f"{c['live_key']}#ex{e['date']}", c["open_date"])
                stock["has_option"] = True
                stock["stages"]     = list(c["stages"])
                for k in ("premium", "fees"):
                    moved = round(c[k] * part, 2)
                    stock[k] += moved
                    c[k]     -= moved
                c["long_cost"]   = c["long_cost"] * (1 - part)
                c["long_qty"]   -= n
                c["long_bought"] = max(c["long_bought"] - n, c["long_qty"])
            else:
                _retire(c["live_key"])
                c.pop("long")
                stock = c
                stock["live_key"] = f"{c['live_key']}#ex"
                live[stock["live_key"]] = stock
            stock["bought"] = True
            stock["stages"].append("Exercised")
            return stock

        for e in evs:
            if e["kind"] == "opt" and not e.get("roll"):
                # Long options: a buy that closes no short contract opens a
                # long position, and a sale of that same contract closes it.
                lk = f"long#{e['otype']}#{e['strike']}#{e['exp']}"
                if e.get("action") == "SOLD" and lk in live:
                    c = live[lk]
                    c["premium"]  += e["amount"]
                    c["fees"]     += e["fees"]
                    c["long_qty"]  = max(c["long_qty"] - e["qty"], 0)
                    c["stages"].append("Sold")
                    if c["long_qty"] == 0:
                        c["end_date"] = e["date"]
                        _retire(lk)
                    continue
                if e.get("action") == "BOT" and not _short_open(e):
                    c = live.get(lk) or _new(lk, e["date"])
                    c["long"]        = {"type": e["otype"], "strike": e["strike"],
                                        "exp": e["exp"]}
                    c["has_option"]  = True
                    c["premium"]    += e["amount"]
                    c["fees"]       += e["fees"]
                    c["long_qty"]    = c.get("long_qty", 0) + e["qty"]
                    c["long_bought"] = c.get("long_bought", 0) + e["qty"]
                    c["long_cost"]   = c.get("long_cost", 0.0) + abs(e["amount"]) + e["fees"]
                    if not c["stages"]:
                        c["stages"].append(f"Long {e['otype'].lower()}")
                    c["end_date"] = e["exp"]
                    continue
            if e["kind"] == "opt":
                if e["otype"] == "PUT":
                    key    = e["strike"]
                    action = e.get("action")
                    cycle  = live.get(key)
                    if cycle is not None and action == "SOLD" and cycle["open_qty"] == 0:
                        # Flat already: same day means this is the far leg of a
                        # hand-rolled position, a later day means a brand new one.
                        if cycle["closed_date"] != e["date"]:
                            _retire(key)
                            cycle = None
                    if cycle is None and action == "SOLD":
                        # A cycle that went flat today at another strike is this
                        # position rolled — carry it over under the new strike.
                        rolled = next((c for k, c in live.items()
                                       if c["open_qty"] == 0 and c["shares"] <= 0
                                       and c["closed_date"] == e["date"]), None)
                        # A diagonal roll closes and reopens in one row, so the
                        # old strike is still open here — its to-close leg names
                        # it. Without this the new strike starts a fresh cycle
                        # and the old one's premium never reaches the shares.
                        # Only a roll of the whole position carries the cycle
                        # over; rolling part of it leaves the rest open at the
                        # old strike (handled below) and starts a new line.
                        closing = e.get("close_strike")
                        if (rolled is None and e.get("roll") and closing
                                and closing != key and closing in live
                                and live[closing]["shares"] <= 0
                                and diag_qty.get(_diag(e), 0)
                                    >= live[closing]["open_qty"]):
                            rolled = live[closing]
                        if rolled is not None:
                            _retire(rolled["live_key"])
                            live[key] = rolled
                            rolled["live_key"] = key
                            cycle = rolled
                    if cycle is None:
                        cycle = _new(key, e["date"])
                    cycle["has_option"] = True
                    cycle["has_put"]    = True
                    cycle["premium"]   += e["amount"]
                    cycle["fees"]      += e["fees"]
                    cycle["put_strike"] = e["strike"]
                    cycle["put_exp"]    = e["exp"]
                    # Track contracts actually outstanding. A buy-to-close
                    # retires them; a roll replaces the open quantity outright
                    # (its fills share a date and to-open expiration, so they
                    # accumulate within the group rather than each overwriting
                    # the last); a plain sale adds to it.
                    if action is None:
                        cycle["qty_known"] = False      # pre-upgrade event
                    elif e.get("roll") and action == "SOLD":
                        group = (e["date"], e["exp"])
                        base  = cycle["open_qty"] if group == cycle["roll_group"] else 0
                        cycle["open_qty"]   = base + e["qty"]
                        cycle["roll_group"] = group
                    elif action == "SOLD":
                        cycle["open_qty"]  += e["qty"]
                        cycle["roll_group"] = None
                    else:                               # BOT — buy to close
                        cycle["open_qty"]   = max(cycle["open_qty"] - e["qty"], 0)
                        cycle["roll_group"] = None
                    # Flat means closed as of this leg — and reopening clears it.
                    cycle["closed_date"] = e["date"] if cycle["open_qty"] == 0 else None
                    # Most contracts ever open: what a bought-back row reports
                    # once its open count is back to zero.
                    cycle["peak_qty"] = max(cycle.get("peak_qty", 0), cycle["open_qty"])
                    signed = -e["qty"] if action == "BOT" else e["qty"]
                    cycle["qty_by_exp"][e["exp"]] = max(
                        cycle["qty_by_exp"].get(e["exp"], 0) + signed, 0)
                    cycle["stages"].append("Put")
                    cycle["end_date"] = e["exp"]
                    # A diagonal moved this position off another strike; that
                    # cycle is closed by the same row, so retire it rather than
                    # leaving it open forever alongside its own replacement.
                    # A partial diagonal takes only its contracts off the old
                    # strike, once per order however many fills it came in.
                    closed = e.get("close_strike")
                    if closed and closed != key and closed in live:
                        old   = live[closed]
                        moved = diag_qty.get(_diag(e), 0) if e.get("roll") else 0
                        if moved and moved < old["open_qty"]:
                            if _diag(e) not in diag_done:
                                diag_done.add(_diag(e))
                                old["open_qty"] -= moved
                                exp = old["put_exp"]
                                if exp in old["qty_by_exp"]:
                                    old["qty_by_exp"][exp] = max(
                                        old["qty_by_exp"][exp] - moved, 0)
                        else:
                            old["closed_date"] = e["date"]
                            old["open_qty"]    = 0
                            _retire(closed)
                            if old is not cycle and old["shares"] <= 0:
                                # Rolled whole into a position already open at
                                # the new strike (IREN 38.5 → 38 on 10/14): the
                                # two are one position now, so the old premium
                                # lowers its basis instead of reading as profit
                                # booked on a row of its own.
                                for k in ("premium", "fees",
                                          "call_premium", "call_fees"):
                                    cycle[k] += old[k]
                                od = _parse_mdy(old["open_date"])
                                cd = _parse_mdy(cycle["open_date"])
                                if od and cd and od < cd:
                                    cycle["open_date"] = old["open_date"]
                                old["merged"] = True
                else:
                    # A call belongs to whichever position holds the shares it
                    # covers; with none assigned it rides the newest cycle.
                    wheel = [c for c in mine if not c.get("long")]
                    cycle = _call_holder(e) or (wheel[-1] if wheel else _new(e["strike"], e["date"]))
                    # Open calls per (strike, exp), so a later close, roll or
                    # called-away sale can find the cycle that wrote them.
                    if e.get("action") in ("SOLD", "BOT"):
                        leg = (e["strike"], e["exp"])
                        if e.get("roll") and e.get("close_exp"):
                            old = (e.get("close_strike") or e["strike"], e["close_exp"])
                            cycle["calls"][old] = max(cycle["calls"].get(old, 0) - e["qty"], 0)
                        elif e.get("roll") and e.get("action") == "SOLD":
                            # Events stored before close_exp was recorded name
                            # the closing strike at most. Without retiring the
                            # leg it replaced, every rolled call stays "open"
                            # forever (SNPS showed its 11/20 call, rolled away
                            # in September, as the nearest expiration). Take
                            # the open leg at the closing strike that expires
                            # soonest — what a roll out in time replaces. A leg
                            # already past expiration is dead, not the one rolled.
                            k = e.get("close_strike") or e["strike"]
                            on = _parse_mdy(e["date"]) or date.min
                            olds = sorted((l for l, q in cycle["calls"].items()
                                           if q > 0 and l[0] == k and l != leg
                                           and (_parse_mdy(l[1]) or date.max) >= on),
                                          key=lambda l: _parse_mdy(l[1]) or date.max)
                            left = e["qty"]
                            for old in olds:
                                if not left:
                                    break
                                used = min(cycle["calls"][old], left)
                                cycle["calls"][old] -= used
                                left -= used
                        signed = -e["qty"] if e["action"] == "BOT" else e["qty"]
                        cycle["calls"][leg] = max(cycle["calls"].get(leg, 0) + signed, 0)
                    cycle["has_option"] = True
                    cycle["premium"]   += e["amount"]
                    cycle["fees"]      += e["fees"]
                    cycle["call_premium"] += e["amount"]
                    cycle["call_fees"]    += e["fees"]
                    cycle["stages"].append("Call")
                    cycle["end_date"] = e["exp"]
            else:  # stock
                if e["action"] == "BOT":
                    # An assignment settles at the put's strike, which is how it
                    # finds its way back to the position that produced it.
                    px = abs(e["amount"]) / e["shares"] if e["shares"] else 0.0
                    cycle = None
                    if e["assigned"]:
                        cycle = next((c for c in live.values()
                                      if c["put_strike"] and
                                      abs(float(c["put_strike"]) - px) < 0.005), None)
                        if cycle is None:
                            cycle = _exercise_long_call(e, px)
                        cycle = cycle or _holder()
                    if cycle is None:
                        # Shares bought outright with none already held start
                        # a long stock position; its covered calls join it as
                        # they would assigned shares.
                        cycle = _holder()
                        if cycle is None:
                            if not track_bought:
                                continue
                            cycle = _new(f"stock#{e['date']}#{len(mine)}", e["date"])
                            cycle["bought"]      = True
                            cycle["pre_history"] = bool(e.get("pre_history"))
                    if e["assigned"] and cycle.get("open_qty"):
                        # Assignment retires the contracts it exercised — 300
                        # shares is three of them. Without this the puts stay on
                        # the books after the shares arrive, and the row counts
                        # capital that is no longer at risk.
                        taken = min(int(e["shares"] // 100), cycle["open_qty"])
                        cycle["open_qty"] -= taken
                        exp = cycle["put_exp"]
                        if exp in cycle["qty_by_exp"]:
                            cycle["qty_by_exp"][exp] = max(
                                cycle["qty_by_exp"][exp] - taken, 0)
                        # The shares live under their own key, so a new put
                        # sold at this strike starts a fresh cycle instead of
                        # finding the share holder flat and retiring it.
                        stock_key = f"{cycle['live_key']}#stock"
                        stock = live.get(stock_key)
                        if taken and stock is None and cycle["open_qty"] == 0:
                            # Fully assigned: the whole position is shares now.
                            _retire(cycle["live_key"])
                            cycle["live_key"] = stock_key
                            live[stock_key] = cycle
                        elif taken:
                            # Only part of the position was assigned (or an
                            # earlier part already was), so it is two things at
                            # one strike: the shares (which will carry the
                            # covered calls) and the puts still short. One row
                            # cannot say both — status, expiration and capital
                            # would each have to describe one and lie about the
                            # other — so the shares go to one share cycle and
                            # the puts keep this one. Premium follows the
                            # contracts pro rata: the assigned ones' share
                            # reduces the stock's basis, the rest stays open.
                            share = taken / (taken + cycle["open_qty"])
                            if stock is None:
                                stock = _new(stock_key, e["date"])
                                stock["has_option"] = True
                                stock["has_put"]    = True
                                stock["stages"]     = list(cycle["stages"])
                                stock["open_date"]  = cycle["open_date"]
                            stock["put_strike"] = cycle["put_strike"]
                            stock["put_exp"]    = cycle["put_exp"]
                            moved_prem = round(cycle["premium"] * share, 2)
                            moved_fees = round(cycle["fees"] * share, 2)
                            stock["premium"] += moved_prem
                            stock["fees"]    += moved_fees
                            cycle["premium"] -= moved_prem
                            cycle["fees"]    -= moved_fees
                            for k in ("call_premium", "call_fees"):
                                moved = round(cycle[k] * share, 2)
                                stock[k] += moved
                                cycle[k] -= moved
                            if cycle["open_qty"] == 0:
                                # The last of the puts joined the shares; this
                                # row has nothing left to describe.
                                _retire(cycle["live_key"])
                                cycle["merged"] = True
                            else:
                                cycle["closed_date"] = None
                            cycle = stock
                    cycle["fees"]       += e["fees"]
                    cycle["shares"]     += e["shares"]
                    cycle["shares_in"]   = cycle.get("shares_in", 0) + e["shares"]
                    cycle["total_cost"] += abs(e["amount"])
                    # Contracts-equivalent of the stock taken on. Retiring the
                    # assigned puts above empties qty_by_exp, so this is what the
                    # row counts once the position is shares rather than options.
                    cycle["stock_qty"]  = cycle.get("stock_qty", 0) + int(e["shares"] // 100)
                    if not (e.get("pre_history") and cycle["stages"]) \
                            and cycle["stages"][-1:] != ["Exercised"]:
                        cycle["stages"].append("Assigned" if e["assigned"] else "Bought")
                else:  # SOLD
                    cycle = _sale_holder(e)
                    if e.get("transfer_out"):
                        cycle = next((c for c in _holders() if c.get("bought")), cycle)
                        if cycle is not None and cycle["shares"] > 0:
                            # Moved, not sold: out at cost, no gain or loss.
                            q = min(e["shares"], cycle["shares"])
                            e = {**e, "amount": cycle["total_cost"] / cycle["shares"] * q}
                    if cycle is None:
                        continue
                    if e["assigned"] and e["shares"]:
                        # The exercised calls are spent.
                        px, left = e["amount"] / e["shares"], int(e["shares"] // 100)
                        for leg, q in cycle["calls"].items():
                            if left and q > 0 and abs(float(leg[0]) - px) < 0.005:
                                used = min(q, left)
                                cycle["calls"][leg] -= used
                                left -= used
                    cycle["fees"] += e["fees"]
                    shares, total_cost = cycle["shares"], cycle["total_cost"]
                    if shares > 0:
                        avg = total_cost / shares
                        q   = min(e["shares"], shares)
                        cycle["stock_pnl"]  += e["amount"] - avg * q
                        cycle["total_cost"]  = total_cost - avg * q
                        cycle["shares"]      = shares - q
                    else:
                        cycle["stock_pnl"] += e["amount"]
                    cycle["stages"].append("Transferred out" if e.get("transfer_out")
                                           else "Called away" if e["assigned"] else "Sold")
                    cycle["end_date"] = e["date"]
                    if cycle["shares"] <= 0:
                        _retire(cycle["live_key"])

    # Only wheel cycles (those with at least one option leg) belong here;
    # pure stock buys/sells live in the Long Stock Positions table.
    rows = [_cycle_to_row(c, outcomes) for c in cycles
            if (c["has_option"] or c.get("bought")) and not c.get("merged")]
    return rows + _blended_rows(rows)


def _unit(unit: str, n: float) -> str:
    """'contracts'/'shares', singular for exactly one."""
    return unit[:-1] if n == 1 else unit


def _long_option_row(c: dict) -> dict:
    """Display row for a bought option (a LEAP, or any long call/put).

    Cash-basis like everything else: the purchase is a realized debit and the
    sale a realized credit, so Premium is their net and the P&L follows. While
    open, Cost Basis is what one contract cost per share (fees in) and
    Recovery is the underlying price at which the option is worth that at
    expiration — strike + cost for a call, strike − cost for a put. Neither is
    a share price, so the row stays out of the symbol's blended basis.
    """
    L       = c["long"]
    strike  = float(L["strike"])
    premium = round(c["premium"], 2)
    fees    = round(c["fees"], 2)
    pnl     = round(premium - fees, 2)
    bought  = c.get("long_bought", 0) or 1
    open_q  = c.get("long_qty", 0)
    cost    = c.get("long_cost", 0.0)
    per_sh  = cost / (bought * 100)
    exp_d   = _parse_mdy(L["exp"])
    today   = date.today()
    if open_q > 0:
        status = "Expired" if exp_d and exp_d < today else "Open"
    else:
        status = "Sold"
    live = status == "Open"

    open_d = _parse_mdy(c["open_date"])
    end_d  = (today if live else
              exp_d if status == "Expired" else _parse_mdy(c["end_date"] or "")) or today
    days   = (end_d - open_d).days if open_d else 0
    ror = ""
    if not live and cost > 0 and days > 0:
        ror = f"{(pnl / cost) * (7 / days) * 100:.2f}%"
    stages = c["stages"] + ([] if c["stages"][-1:] == ["Sold"] else [status])
    capital = cost * open_q / bought if live else cost
    recovery = strike + per_sh if L["type"] == "CALL" else strike - per_sh
    return {
        "symbol":     c["sym"],
        "opened":     c["open_date"],
        "lifecycle":  " → ".join(stages),
        "qty":        str(open_q if live else bought),
        "unit":       _unit("contracts", open_q if live else bought),
        "strike":     L["strike"],
        "premium":    f"{premium:.2f}",
        "expiration": L["exp"] if status != "Sold" else (c["end_date"] or L["exp"]),
        "status":     status,
        "weeks_held": f"{days / 7:.1f}" if days >= 0 else "",
        "fees":       f"{fees:.2f}",
        "weekly_ror": ror,
        "assn_price": "",
        "cost_basis": f"{per_sh:.2f}" if live else f"{pnl:+,.2f}",
        "recovery":   f"{recovery:.2f}" if live else "",
        "_basis":     None,
        "_recovery":  None,
        "_shares":    0.0,
        "_capital":   capital,
    }


def _cycle_to_row(c: dict, outcomes: dict[tuple, str]) -> dict:
    if c.get("long"):
        return _long_option_row(c)
    strike = float(c["put_strike"]) if c["put_strike"] else 0.0
    qty    = c["qty_by_exp"].get(c["put_exp"], 0) or c.get("stock_qty", 0)
    premium    = round(c["premium"], 2)
    fees       = round(c["fees"], 2)
    stock_pnl  = round(c["stock_pnl"], 2)
    total_pnl  = round(premium + stock_pnl - fees, 2)

    # Status
    if c["shares"] > 0:
        status = "Holding"
    elif c["stages"][-1:] == ["Transferred out"]:
        status = "Transferred"
    elif "Sold" in c["stages"]:
        status = "Sold"
    elif "Called away" in c["stages"]:
        status = "Called away"
    elif c.get("has_put") and c.get("qty_known", True) and c.get("open_qty", 0) == 0:
        # Bought back to flat — the contracts are gone, whatever the calendar says.
        status = "Closed"
    else:
        key = (c["sym"], c["put_exp"], c["put_strike"], "PUT")
        status = outcomes.get(key)
        if status is None:
            # No RAD row for this leg. TOS emits those zero-amount "Removed due
            # to Expiration/Assignment" lines on short statement ranges but
            # drops them from a long full-history export, so their absence says
            # nothing about whether the position is still live — falling back to
            # "Open" leaves months of settled cycles sitting in the table. An
            # assignment always attaches stock (a "BOT n SYM UPON" row), which
            # lands this cycle in the Holding/Sold/Called away branches above,
            # so a cycle that reaches here with its expiration behind it kept
            # the premium and closed out.
            exp_d = _parse_mdy(c["put_exp"]) if c["put_exp"] else None
            status = "Expired" if exp_d and exp_d < date.today() else "Open"

    # Lifecycle path (dedup consecutive)
    path = []
    for s in c["stages"]:
        if not path or path[-1] != s:
            path.append(s)
    if path and path[-1] not in ("Sold", "Called away", "Transferred out") and status not in path:
        path.append(status)
    if c.get("pre_history") and path and path[0] == "Bought":
        path[0] = "Transferred in"
    # Share rows count shares; option rows count contracts (a bought-back
    # put reports the most it had open, not the zero it ended at).
    if c.get("shares_in"):
        shown_qty = c["shares"] if c["shares"] > 0 else c["shares_in"]
    else:
        shown_qty = qty or c.get("peak_qty", 0)

    if status == "Holding" and path and path[-1] == "Holding" and "Call" in path:
        # No call open on the shares any more: say how the last one ended,
        # or the row reads as if a call were still written against them.
        calls = c.get("calls", {})
        if calls and not any(q > 0 and (_parse_mdy(exp) or date.max) >= date.today()
                             for (_, exp), q in calls.items()):
            ended = ("Call expired" if any(q > 0 for q in calls.values())
                     else "Call closed")
            i = len(path) - 1 - path[::-1].index("Call")
            path[i] = ended
    lifecycle = " → ".join(path)

    completed = status in ("Sold", "Called away", "Expired", "Closed", "Transferred")

    # Timing. A bought-back cycle ends the day it went flat, not on the
    # expiration its last contract would have run to.
    open_d = _parse_mdy(c["open_date"])
    end_str = c["closed_date"] if status == "Closed" and c.get("closed_date") else c["end_date"]
    close_d = _parse_mdy(end_str) if end_str else None

    # Held shares show the nearest call still open, not the last call written:
    # a call that expires worthless leaves no row on the statement (TOS posts
    # its removal days later, if at all), so its date would sit here as if the
    # shares were still covered. Past its expiration it's over. With no call
    # open the shares are uncovered — capital is still at work through today.
    # A bought-back cycle ended the day it went flat (the Closed tab's "Ended").
    exp_cell = end_str or ""
    if status == "Holding":
        today = date.today()
        open_exps = sorted(d for (_, exp), q in c.get("calls", {}).items()
                           if q > 0 and (d := _parse_mdy(exp)) and d >= today)
        if open_exps:
            exp_cell = open_exps[0].strftime("%m/%d/%Y")
            # Capital stays committed until the last open call expires; the
            # last call *written* may be an earlier one, or already over.
            close_d  = open_exps[-1]
        else:
            exp_cell = "uncovered" if c.get("calls") else ""
            close_d  = None

    # Weeks Held = actual elapsed time (open → today for live, open → close for done)
    held_end  = date.today() if status in ("Open", "Holding") else (close_d or date.today())
    days_held = (held_end - open_d).days if open_d else 0
    weeks     = f"{days_held / 7:.1f}" if days_held >= 0 else ""
    if c.get("pre_history"):
        # The lot's real purchase date predates the statements, so elapsed
        # time — and any rate built on it — would be invented.
        weeks = ""

    # RoR denominator = full capital commitment: open → expiration for live positions,
    # open → close date for completed ones. Capital is locked until expiration, so
    # dividing by only days-held would overstate the annualised rate.
    ror_end  = close_d if completed else (close_d or date.today())
    days_ror = (ror_end - open_d).days if open_d else 0

    # Weekly RoR on deployed capital. Once assigned, the capital at work is the
    # stock held, not the collateral the put reserved — the put is gone, and its
    # contract count can legitimately be zero if it was bought back.
    capital = c["total_cost"] if c["shares"] > 0 else strike * 100 * qty
    ror = ""
    if capital > 0 and days_ror > 0 and not c.get("pre_history"):
        ror = f"{(total_pnl / capital) * (7 / days_ror) * 100:.2f}%"

    # Cost Basis (open/holding) vs Total P&L (completed)
    # basis/shares are also kept numerically so concurrent positions on one
    # symbol can be blended into a summary row.
    #
    # Three target prices for shares:
    #   assn     — the price paid at assignment (average, if assigned in parts)
    #   basis    — assn less the put premium, net of fees: what the shares cost
    #              (also the IRS treatment — put premium reduces stock basis)
    #   recovery — basis less the call premium, net of fees: the exit price that
    #              gets the whole cycle back to even. Fees on the stock legs
    #              themselves sit in fees with the puts, raising basis.
    # An open put shows basis only: it is the price paid if assigned now, and
    # with no shares there are no calls to bring a recovery below it.
    assn = basis = recovery = None
    shares_eq = 0.0
    call_net = c.get("call_premium", 0.0) - c.get("call_fees", 0.0)
    put_net  = (premium - fees) - call_net
    if status == "Holding" and c["shares"] > 0:
        assn      = c["total_cost"] / c["shares"]
        basis     = (c["total_cost"] - put_net) / c["shares"]
        recovery  = basis - call_net / c["shares"]
        shares_eq = c["shares"]
        pnl_cell  = f"{basis:.2f}"
    elif status == "Open" and capital > 0:
        basis     = strike - (premium - fees) / (qty * 100)
        recovery  = basis
        shares_eq = qty * 100
        pnl_cell  = f"{basis:.2f}"
    else:
        pnl_cell = f"{total_pnl:+,.2f}"

    return {
        "symbol":     c["sym"],
        "opened":     "transferred" if c.get("pre_history") else c["open_date"],
        "lifecycle":  lifecycle,
        # Share rows count shares; option rows count contracts.
        "qty":        f"{shown_qty:g}",
        "unit":       _unit("shares" if c.get("shares_in") else "contracts", shown_qty),
        "strike":     c["put_strike"] or "",
        "premium":    f"{premium:.2f}",
        "expiration": exp_cell,
        "status":     status,
        "weeks_held": weeks,
        "fees":       f"{fees:.2f}",
        "weekly_ror": ror,
        "assn_price": f"{assn:.2f}" if assn is not None else "",
        "cost_basis": pnl_cell,
        # Open puts carry recovery numerically (for the blend) but don't show
        # it: it would only repeat the basis beside it.
        "recovery":   f"{recovery:.2f}" if assn is not None else "",
        "_basis":     basis,
        "_recovery":  recovery,
        "_shares":    shares_eq,
        "_capital":   capital,
    }


def _blended_rows(rows: list[dict]) -> list[dict]:
    """One summary row per symbol carrying more than one live position.

    Concurrent short puts at different strikes each have their own basis, but
    what they come to together — the price per share you'd end up paying if all
    of them were assigned — appears on none of the individual lines, so it gets
    a row of its own. Assigned stock counts too, weighted by its shares.
    """
    by_sym: dict[str, list[dict]] = {}
    for r in rows:
        if (r["status"] in ("Open", "Holding")
                and r.get("_basis") is not None and r.get("_shares")):
            by_sym.setdefault(r["symbol"], []).append(r)

    out: list[dict] = []
    for sym, live in sorted(by_sym.items()):
        if len(live) < 2:
            continue
        shares = sum(r["_shares"] for r in live)
        value  = sum(r["_basis"] * r["_shares"] for r in live)
        recov  = sum(r["_recovery"] * r["_shares"] for r in live)
        held   = [r for r in live if r.get("assn_price")]
        held_sh = sum(r["_shares"] for r in held)
        assn   = (sum(float(r["assn_price"]) * r["_shares"] for r in held) / held_sh
                  if held_sh else None)
        out.append({
            "symbol":     sym,
            "opened":     "— blended —",
            "lifecycle":  "",
            # All options: contracts. Any shares: the share count the blended
            # basis is averaged over (open puts as the shares they'd bring).
            "qty":        (f"{sum(float(r['qty']) for r in live):g}"
                           if all(r.get("unit", "").startswith("contract") for r in live)
                           else f"{sum(r['_shares'] for r in live):g}"),
            "unit":       ("contracts" if all(r.get("unit", "").startswith("contract")
                                               for r in live) else "shares"),
            "strike":     "—",
            "premium":    f"{sum(float(r['premium']) for r in live):.2f}",
            "expiration": "—",
            "status":     "Blended",
            "weeks_held": "—",
            "fees":       f"{sum(float(r['fees']) for r in live):.2f}",
            "weekly_ror": "—",
            # Assn Price blends only the shares actually held; basis and
            # recovery blend everything, as if the open puts were assigned too.
            "assn_price": f"{assn:.2f}" if assn is not None else "",
            "cost_basis": f"{value / shares:.2f}",
            "recovery":   f"{recov / shares:.2f}",
            # Summary of rows already counted individually — carried for display
            # only. Tag exposure skips Blended so capital isn't counted twice.
            "_capital":   sum(r.get("_capital") or 0.0 for r in live),
        })
    return out


# Statuses that still tie up capital. "Blended" is a per-symbol summary of rows
# already in this set, so it displays capital but never contributes to exposure.
_LIVE_STATUSES = ("Open", "Holding")


def _annotate_tags_and_capital(rows: list[dict]) -> dict:
    """Fill each row's tags / capital / % of book in place; return the exposure.

    Capital is only shown where it is still at risk — a closed cycle's
    collateral has been released, and showing it would inflate every
    concentration read. Returned dict:

        {"total": float,
         "by_tag": [(tag, capital, pct), …]  (largest first),
         "by_symbol": [(symbol, capital, pct, tags), …],
         "untagged": [symbol, …]}
    """
    total = sum(r.get("_capital") or 0.0
                for r in rows if r["status"] in _LIVE_STATUSES)

    by_tag: dict[str, float] = {}
    by_symbol: dict[str, float] = {}
    sym_tags: dict[str, list[str]] = {}
    untagged: set[str] = set()

    for r in rows:
        sym = r["symbol"]
        tags, researched = tags_for(sym)
        sym_tags[sym] = tags
        r["tags"] = ", ".join(tags) if researched else "UNTAGGED"
        if researched and not tags:
            r["tags"] = "—"

        cap = r.get("_capital") or 0.0
        live = r["status"] in _LIVE_STATUSES
        if live or r["status"] == "Blended":
            r["capital"] = f"${cap:,.0f}" if cap else ""
            r["capital_pct"] = f"{cap / total * 100:.1f}%" if (cap and total) else ""
        else:
            r["capital"] = ""
            r["capital_pct"] = ""

        if not live or not cap:
            continue
        by_symbol[sym] = by_symbol.get(sym, 0.0) + cap
        if not researched:
            untagged.add(sym)
        for t in tags:
            by_tag[t] = by_tag.get(t, 0.0) + cap

    def _pct(v):
        return (v / total * 100) if total else 0.0

    return {
        "total": total,
        "by_tag": [(t, v, _pct(v))
                   for t, v in sorted(by_tag.items(), key=lambda kv: -kv[1])],
        "by_symbol": [(s, v, _pct(v), sym_tags.get(s, []))
                      for s, v in sorted(by_symbol.items(), key=lambda kv: -kv[1])],
        "untagged": sorted(untagged),
    }


def _build_fee_lookup(rows: list[list[str]]) -> dict[tuple, float]:
    """Scan Cash Balance TRD rows and return total fees keyed by
    (SYMBOL, exp_MM/DD/YYYY, strike_str, TYPE_UPPER).
    """
    lookup: dict[tuple, float] = {}

    for row in rows:
        cells = [c.strip().strip('"') for c in row]
        # Cash Balance columns:
        # DATE, TIME, TYPE, REF#, DESCRIPTION, Misc Fees, Commissions & Fees, AMOUNT, BALANCE
        if len(cells) < 7 or cells[2] != "TRD":
            continue

        desc     = cells[4] if len(cells) > 4 else ""
        misc_raw = cells[5] if len(cells) > 5 else ""
        comm_raw = cells[6] if len(cells) > 6 else ""

        try:
            misc = abs(float(misc_raw.replace(",", ""))) if misc_raw else 0.0
        except ValueError:
            misc = 0.0
        try:
            comm = abs(float(comm_raw.replace(",", ""))) if comm_raw else 0.0
        except ValueError:
            comm = 0.0

        total = round(misc + comm, 2)
        if total == 0.0:
            continue

        # Calendar roll — use the TO-OPEN (first) expiration as the key
        if "CALENDAR" in desc.upper():
            m = _TRD_CAL_RE.search(desc)
            if m:
                sym, exp, _close_exp, strike, typ = m.groups()
                key = (sym.upper(), _parse_exp(exp.strip()), _norm_strike(strike), typ.upper())
                lookup[key] = round(lookup.get(key, 0.0) + total, 2)
        else:
            m = _TRD_SINGLE_RE.search(desc)
            if m:
                sym, exp, strike, typ = m.groups()
                key = (sym.upper(), _parse_exp(exp.strip()), _norm_strike(strike), typ.upper())
                lookup[key] = round(lookup.get(key, 0.0) + total, 2)

    return lookup


def _parse_tos_csv(
    path: Path,
) -> tuple[list[dict], list[str], list[dict], list[str]]:
    """Parse a TOS Account Statement CSV. Returns (options, opt_warnings, equities, eq_warnings)."""
    opt_results:  list[dict] = []
    opt_warnings: list[str]  = []
    eq_results:   list[dict] = []
    eq_warnings:  list[str]  = []

    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return [], [str(e)], [], []

    rows = list(csv.reader(io.StringIO(text)))

    fee_lookup        = _build_fee_lookup(rows)
    premium_lookup    = _build_premium_lookup(rows)
    equity_date_lookup = _build_equity_date_lookup(rows)

    # ── Options section ───────────────────────────────────────────────────────
    in_options = False
    col: dict[str, int] = {}

    for row in rows:
        if not row:
            continue
        cells = [c.strip().strip('"') for c in row]
        first = cells[0]

        if first == "Options":
            in_options = True
            col = {}
            continue

        if not in_options:
            continue

        if not col:
            col = {h.strip().lower(): i for i, h in enumerate(cells)}
            continue

        if first in _SECTION_BREAKS:
            break

        joined = " ".join(cells)
        if "OVERALL" in joined.upper() or not first:
            continue

        try:
            sym    = cells[col["symbol"]].upper()
            exp    = cells[col["exp"]]
            strike = cells[col["strike"]]
            typ    = cells[col["type"]].capitalize()
            qty    = cells[col["qty"]]
            price  = cells[col["trade price"]]
        except (KeyError, IndexError) as e:
            opt_warnings.append(f"Skipped row (missing column {e}): {cells[:5]}")
            continue

        if not sym or sym in ("SYMBOL",):
            continue

        exp_fmt      = _parse_exp(exp)
        contract_key = (sym, exp_fmt, _norm_strike(strike), typ.upper())
        fees         = f"{fee_lookup[contract_key]:.2f}" if contract_key in fee_lookup else ""
        opened       = ""
        if contract_key in premium_lookup:
            entry  = premium_lookup[contract_key]
            price  = entry["price"]
            opened = entry["opened"]

        opt_results.append({
            "symbol":     sym,
            "type":       typ,
            "expiration": exp_fmt,
            "strike":     strike,
            "qty":        qty,
            "premium":    price,
            "fees":       fees,
            "opened":     opened,
        })

    # ── Equities section ──────────────────────────────────────────────────────
    _EQUITY_BREAKS = {
        "Options", "Profits and Losses", "Account Summary",
        "Futures Statements", "Forex Statements",
        "Cash Balance", "Account Trade History",
    }

    in_equities = False
    col = {}

    for row in rows:
        if not row:
            continue
        cells = [c.strip().strip('"') for c in row]
        first = cells[0]

        if first == "Equities":
            in_equities = True
            col = {}
            continue

        if not in_equities:
            continue

        if not col:
            col = {h.strip().lower(): i for i, h in enumerate(cells)}
            continue

        if first in _EQUITY_BREAKS:
            break

        joined = " ".join(cells)
        if "OVERALL" in joined.upper() or not first:
            continue

        try:
            sym = cells[col["symbol"]].upper().strip()
        except (KeyError, IndexError):
            continue

        if not sym or sym == "SYMBOL":
            continue

        try:
            qty_raw = cells[col["qty"]].strip().lstrip("+").replace(",", "")
            qty_f   = float(qty_raw)
            if qty_f <= 0:
                continue
            shares = str(int(qty_f)) if qty_f == int(qty_f) else str(qty_f)
        except (KeyError, IndexError, ValueError):
            eq_warnings.append(f"Skipped {sym}: could not parse qty")
            continue

        cost_basis = ""
        try:
            cost_basis = cells[col["trade price"]].strip().replace("$", "").replace(",", "")
        except (KeyError, IndexError):
            pass

        current_price = ""
        for name in ("mark", "close price", "last price", "last"):
            if name in col:
                try:
                    val = cells[col[name]].strip().replace("$", "").replace(",", "")
                    if val:
                        current_price = val
                        break
                except IndexError:
                    pass

        eq_results.append({
            "symbol":        sym,
            "shares":        shares,
            "cost_basis":    cost_basis,
            "current_price": current_price,
            "opened":        equity_date_lookup.get(sym, ""),
        })

    return opt_results, opt_warnings, eq_results, eq_warnings
