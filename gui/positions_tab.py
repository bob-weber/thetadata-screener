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
    QWidget, QVBoxLayout,
    QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QFileDialog, QMessageBox,
    QAbstractItemView, QApplication,
)

from .account_store import DEFAULT_ACCT, events_path

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
    ("strike",     "Strike",            "edit"),
    ("premium",    "Premium ($)",       "edit"),   # total option premium collected (puts + calls)
    ("expiration", "Expiration",        "edit"),
    ("status",     "Status",            "edit"),
    ("weeks_held", "Weeks Held",        "calc"),
    ("fees",       "Fees",              "edit"),
    ("weekly_ror", "Avg Weekly RoR",    "calc"),
    ("cost_basis", "Cost Basis/Recovery/P&L",  "calc"),
]
_OPT_FIELDS = [c[0] for c in _OPT_COLUMNS]
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

# "Blended" shares Open's rank so a symbol's summary row stays with its legs
# instead of collecting at one end of the table.
_STATUS_SORT = {"Open": 0, "Blended": 0, "Holding": 1, "Closed": 2,
                "Called away": 3, "Assigned": 4, "Sold": 5, "Expired": 6}


class _StatusItem(QTableWidgetItem):
    """Table item that sorts Status by wheel-cycle progression, not alphabetically."""
    def __lt__(self, other):
        if isinstance(other, _StatusItem):
            return (_STATUS_SORT.get(self.text(), 99) <
                    _STATUS_SORT.get(other.text(), 99))
        return super().__lt__(other)


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
    status_changed  = pyqtSignal(str)        # status bar text

    def __init__(self):
        super().__init__()
        self._events, self._outcomes, self._account = [], {}, ""
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
                return data.get("events", []), outcomes
            except Exception:
                pass
        return [], {}

    def _save_events(self):
        events_path(self._account).write_text(json.dumps({
            "account":  self._account,
            "events":   self._events,
            "outcomes": {_outcome_key(k): v for k, v in self._outcomes.items()},
        }, indent=2))

    def load_account(self, acct: str):
        """Switch the active account: load its events and rebuild the table."""
        self._account = acct
        self._events, self._outcomes = self._read_events(acct) if acct else ([], {})
        self._rebuild_options_table()
        self.account_changed.emit(self._account)

    def apply_range(self, sel, date_from: str | None = None, date_to: str | None = None):
        """Shared time-range filter (sel: None=all, int=days back, 'custom')."""
        self._range_sel  = sel
        self._range_from = date_from
        self._range_to   = date_to
        self._rebuild_options_table()

    def _rebuild_options_table(self):
        from datetime import timedelta
        rows = _reconstruct_wheels(self._events, self._outcomes)

        # Date-range filter — active (Open/Holding) positions always shown
        sel = self._range_sel
        active = ("Open", "Holding", "Blended")
        if sel == "custom" and self._range_from and self._range_to:
            rows = [r for r in rows
                    if r["status"] in active
                    or self._range_from <= r["opened"] <= self._range_to]
        elif isinstance(sel, int):
            cutoff = (date.today() - timedelta(days=sel)).isoformat()
            rows = [r for r in rows
                    if r["status"] in active or r["opened"] >= cutoff]

        rows.sort(key=lambda r: (_STATUS_SORT.get(r["status"], 99), r["symbol"], r["opened"]))
        self._opt_table.setSortingEnabled(False)
        self._opt_table.blockSignals(True)
        self._opt_table.setRowCount(0)
        for r in rows:
            self._insert_option_row(r)
        self._opt_table.blockSignals(False)
        self._opt_table.setSortingEnabled(True)
        self._opt_table.sortByColumn(_O_STATUS_COL, Qt.SortOrder.AscendingOrder)
        self._opt_table.resizeColumnsToContents()
        self._refresh_market_prices()

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
        self._opt_table.setSortingEnabled(False)
        for r in range(self._opt_table.rowCount()):
            sym_it = self._opt_table.item(r, _O_SYM_COL)
            if not sym_it:
                continue
            price = self._price_cache.get(sym_it.text().strip().upper())
            mkt_it = self._opt_table.item(r, _O_MARKET_COL)
            if mkt_it is not None:
                mkt_it.setText(_fmt_price(price))
        self._opt_table.setSortingEnabled(True)
        self._opt_table.resizeColumnToContents(_O_MARKET_COL)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        hint = QLabel(
            "One line per wheel cycle (Put → Assigned → … ). "
            "P&L and RoR include all premiums + stock outcome, net of fees. "
            "Derived from imported TOS statements."
        )
        hint.setStyleSheet("color: grey; font-size: 11px;")
        root.addWidget(hint)

        self._opt_table = QTableWidget(0, len(_O_HDRS))
        self._opt_table.setHorizontalHeaderLabels(_O_HDRS)
        self._opt_table.setSortingEnabled(True)
        self._opt_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._opt_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        _oh = self._opt_table.horizontalHeader()
        _oh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        _oh.setStretchLastSection(True)
        self._opt_table.verticalHeader().setVisible(False)
        for ci in _OPT_HIDDEN_COLS:
            self._opt_table.setColumnHidden(ci, True)
        root.addWidget(self._opt_table, 1)

    # ── shared helpers ────────────────────────────────────────────────────────

    def _make_item(self, text: str, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    # ── Options rows ──────────────────────────────────────────────────────────

    def _insert_option_row(self, data: dict | None = None) -> int:
        """Insert one derived lifecycle row (all fields supplied; read-only)."""
        row = self._opt_table.rowCount()
        self._opt_table.insertRow(row)
        d = data or {}
        for ci, field in enumerate(_OPT_FIELDS):
            if field == "market":
                price = self._price_cache.get(d.get("symbol", ""))
                item = self._make_item(_fmt_price(price), editable=False)
                item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            elif field == "status":
                val = str(d.get(field, ""))
                item = _StatusItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            else:
                item = self._make_item(str(d.get(field, "")), editable=False)
            self._opt_table.setItem(row, ci, item)
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
                missing = [k for k in ("action", "roll") if k in e and k not in old]
                if missing:
                    old.update({k: e[k] for k in missing})
                    upgraded += 1

        # Accumulate cash events across imports by MAX multiplicity per uid.
        # A statement is complete for its date range, so identical partial fills
        # within one statement are kept, while overlapping statements (which
        # repeat the same fills) don't double-count.
        existing_counts = Counter(_event_uid(e) for e in self._events)
        new_counts = Counter()
        templates: dict[tuple, dict] = {}
        for e in new_events:
            uid = _event_uid(e)
            new_counts[uid] += 1
            templates.setdefault(uid, e)

        added_evs = 0
        for uid, n in new_counts.items():
            deficit = n - existing_counts.get(uid, 0)
            for _ in range(deficit):
                self._events.append(templates[uid])
                added_evs += 1

        self._outcomes.update(new_outcomes)
        self._save_events()
        self._rebuild_options_table()

        opt_rows = self._opt_table.rowCount()
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
        self._opt_table.blockSignals(True)
        self._opt_table.setRowCount(0)
        self._opt_table.blockSignals(False)
        if self._account:
            events_path(self._account).unlink(missing_ok=True)
        POSITIONS_FILE.unlink(missing_ok=True)
        STOCKS_FILE.unlink(missing_ok=True)
        SUPERSEDED_FILE.unlink(missing_ok=True)
        self._events, self._outcomes = [], {}

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
    r'/\d{1,2}\s+[A-Z]{3}\s+\d{2,4}'        # TO-CLOSE expiration (skip)
    r'\s+([\d.]+)\s+(PUT|CALL)',
    re.IGNORECASE,
)
# Diagonal roll — like a calendar but the strike moves too, so it carries a
# to-open and a to-close strike:
# "SOLD -2 DIAGONAL ICE 100 18 DEC 26/31 JUL 26 140/130 CALL @2.00 CBOE"
_TRD_DIAG_RE = re.compile(
    r'(?:SOLD|BOT)\s+\S+\s+DIAGONAL\s+([A-Z]{1,10})\s+100'
    r'.*?(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'   # TO-OPEN expiration
    r'/\d{1,2}\s+[A-Z]{3}\s+\d{2,4}'        # TO-CLOSE expiration (skip)
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
            close_strike = None
            cal  = _TRD_CAL_RE.search(desc)
            diag = None if cal else _TRD_DIAG_RE.search(desc)
            vert = None if (cal or diag) else _TRD_VERT_RE.search(desc)
            m = cal or diag or vert or _TRD_SINGLE_RE.search(desc)
            if not m:
                continue
            if diag or vert:
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


def _reconstruct_wheels(events: list[dict], outcomes: dict[tuple, str]) -> list[dict]:
    """Group per-symbol events into wheel cycles → one display row per cycle."""
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
            c = {"sym": sym, "open_date": open_date, "premium": 0.0, "fees": 0.0,
                 "stock_pnl": 0.0, "stages": [], "put_strike": None, "put_exp": None,
                 "qty_by_exp": {}, "end_date": None, "shares": 0.0, "total_cost": 0.0,
                 "has_option": False, "has_put": False, "open_qty": 0,
                 "roll_group": None, "qty_known": True, "closed_date": None}
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

        for e in evs:
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
                                       if c["open_qty"] == 0
                                       and c["closed_date"] == e["date"]), None)
                        if rolled is not None:
                            _retire(rolled["put_strike"])
                            live[key] = rolled
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
                    signed = -e["qty"] if action == "BOT" else e["qty"]
                    cycle["qty_by_exp"][e["exp"]] = max(
                        cycle["qty_by_exp"].get(e["exp"], 0) + signed, 0)
                    cycle["stages"].append("Put")
                    cycle["end_date"] = e["exp"]
                    # A diagonal moved this position off another strike; that
                    # cycle is closed by the same row, so retire it rather than
                    # leaving it open forever alongside its own replacement.
                    closed = e.get("close_strike")
                    if closed and closed != key and closed in live:
                        live[closed]["closed_date"] = e["date"]
                        live[closed]["open_qty"]    = 0
                        _retire(closed)
                else:
                    # A call belongs to whichever position holds the shares it
                    # covers; with none assigned it rides the newest cycle.
                    cycle = _holder() or (mine[-1] if mine else _new(e["strike"], e["date"]))
                    cycle["has_option"] = True
                    cycle["premium"]   += e["amount"]
                    cycle["fees"]      += e["fees"]
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
                        cycle = cycle or _holder()
                    if cycle is None:
                        # A direct stock buy with no live cycle is a standalone
                        # position — it lives in the Long Stock table, not here.
                        cycle = _holder()
                        if cycle is None:
                            continue
                    cycle["fees"]       += e["fees"]
                    cycle["shares"]     += e["shares"]
                    cycle["total_cost"] += abs(e["amount"])
                    cycle["stages"].append("Assigned" if e["assigned"] else "Bought")
                else:  # SOLD
                    cycle = _holder()
                    if cycle is None:
                        continue
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
                    cycle["stages"].append("Called away" if e["assigned"] else "Sold")
                    cycle["end_date"] = e["date"]
                    if cycle["shares"] <= 0:
                        _retire(cycle["put_strike"])

    # Only wheel cycles (those with at least one option leg) belong here;
    # pure stock buys/sells live in the Long Stock Positions table.
    rows = [_cycle_to_row(c, outcomes) for c in cycles if c["has_option"]]
    return rows + _blended_rows(rows)


def _cycle_to_row(c: dict, outcomes: dict[tuple, str]) -> dict:
    strike = float(c["put_strike"]) if c["put_strike"] else 0.0
    qty    = c["qty_by_exp"].get(c["put_exp"], 0)
    premium    = round(c["premium"], 2)
    fees       = round(c["fees"], 2)
    stock_pnl  = round(c["stock_pnl"], 2)
    total_pnl  = round(premium + stock_pnl - fees, 2)

    # Status
    if c["shares"] > 0:
        status = "Holding"
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
    if path and path[-1] not in ("Sold", "Called away") and status not in path:
        path.append(status)
    lifecycle = " → ".join(path)

    completed = status in ("Sold", "Called away", "Expired", "Closed")

    # Timing. A bought-back cycle ends the day it went flat, not on the
    # expiration its last contract would have run to.
    open_d = _parse_mdy(c["open_date"])
    end_str = c["closed_date"] if status == "Closed" and c.get("closed_date") else c["end_date"]
    close_d = _parse_mdy(end_str) if end_str else None

    # Weeks Held = actual elapsed time (open → today for live, open → close for done)
    held_end  = date.today() if status in ("Open", "Holding") else (close_d or date.today())
    days_held = (held_end - open_d).days if open_d else 0
    weeks     = f"{days_held / 7:.1f}" if days_held >= 0 else ""

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
    if capital > 0 and days_ror > 0:
        ror = f"{(total_pnl / capital) * (7 / days_ror) * 100:.2f}%"

    # Cost Basis (open/holding) vs Total P&L (completed)
    # basis/shares are also kept numerically so concurrent positions on one
    # symbol can be blended into a summary row.
    basis = None
    shares_eq = 0.0
    if status == "Holding" and c["shares"] > 0:
        basis = (c["total_cost"] - (premium - fees)) / c["shares"]
        shares_eq = c["shares"]
        pnl_cell = f"{basis:.2f}"
    elif status == "Open" and capital > 0:
        basis = strike - (premium - fees) / (qty * 100)
        shares_eq = qty * 100
        pnl_cell = f"{basis:.2f}"
    else:
        pnl_cell = f"{total_pnl:+,.2f}"

    return {
        "symbol":     c["sym"],
        "opened":     c["open_date"],
        "lifecycle":  lifecycle,
        "qty":        str(qty),
        "strike":     c["put_strike"] or "",
        "premium":    f"{premium:.2f}",
        "expiration": c["end_date"] or "",
        "status":     status,
        "weeks_held": weeks,
        "fees":       f"{fees:.2f}",
        "weekly_ror": ror,
        "cost_basis": pnl_cell,
        "_basis":     basis,
        "_shares":    shares_eq,
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
        out.append({
            "symbol":     sym,
            "opened":     "— blended —",
            "lifecycle":  "",
            "qty":        str(sum(int(r["qty"]) for r in live)),
            "strike":     "—",
            "premium":    f"{sum(float(r['premium']) for r in live):.2f}",
            "expiration": "—",
            "status":     "Blended",
            "weeks_held": "—",
            "fees":       f"{sum(float(r['fees']) for r in live):.2f}",
            "weekly_ror": "—",
            "cost_basis": f"{value / shares:.2f}",
        })
    return out


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
                sym, exp, strike, typ = m.groups()
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
