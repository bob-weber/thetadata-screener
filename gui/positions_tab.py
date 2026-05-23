import calendar
import csv
import io
import json
import re
from datetime import date
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QFileDialog,
    QAbstractItemView, QMessageBox,
)

POSITIONS_FILE = Path("my_option_positions.json")

# ── column layout ─────────────────────────────────────────────────────────────
# Editable columns the user fills in (manually or via CSV import)
_E = ["symbol", "type", "expiration", "strike", "qty", "premium", "fees", "opened"]
# Computed (read-only) columns
_C = ["5pct_below", "10pct_below", "weekly_ror", "weeks_held"]
_ALL_COLS = _E + _C
_HEADERS  = ["Symbol", "P/C", "Expiration", "Strike", "Qty",
             "Premium", "Fees", "Opened", "5% Below Strike", "10% Below Strike",
             "Weekly ROR", "Weeks Held"]

_STRIKE_COL      = _E.index("strike")       # col 3
_EXP_COL         = _E.index("expiration")   # col 2
_PREMIUM_COL     = _E.index("premium")      # col 5
_OPENED_COL      = _E.index("opened")       # col 7
_5PCT_COL        = len(_E)                  # col 8
_10PCT_COL       = len(_E) + 1             # col 9
_WEEKLY_ROR_COL  = len(_E) + 2             # col 10
_WEEKS_HELD_COL  = len(_E) + 3             # col 11


class PositionsTab(QWidget):
    def __init__(self):
        super().__init__()
        self._setup_ui()
        self._load_saved()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # toolbar
        toolbar = QHBoxLayout()
        import_btn = QPushButton("Import TOS CSV…")
        add_btn    = QPushButton("Add Row")
        del_btn    = QPushButton("Delete Selected")
        import_btn.clicked.connect(self._import_csv)
        add_btn.clicked.connect(lambda: self._add_row())
        del_btn.clicked.connect(self._delete_selected)
        toolbar.addWidget(import_btn)
        toolbar.addWidget(add_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch()
        self._status_label = QLabel("")
        toolbar.addWidget(self._status_label)
        root.addLayout(toolbar)

        # hint
        hint = QLabel(
            "Strike, 5%/10% below are in dollars.  "
            "Qty is negative for short (sold) positions."
        )
        hint.setStyleSheet("color: grey; font-size: 11px;")
        root.addWidget(hint)

        # table
        box = QGroupBox("Option Positions")
        bl  = QVBoxLayout(box)
        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked |
                                    QTableWidget.EditTrigger.SelectedClicked)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.itemChanged.connect(self._on_item_changed)
        bl.addWidget(self._table)
        root.addWidget(box, 1)

    # ── row helpers ───────────────────────────────────────────────────────────

    def _make_item(self, text: str, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setForeground(item.foreground())  # keep default colour
        return item

    def _add_row(self, data: dict | None = None):
        # Disable sorting before writing cells: Qt re-sorts the moment the sort
        # column's item is set, which shifts the row to a new index and makes
        # all subsequent setItem calls target the wrong row.
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        row = self._table.rowCount()
        self._table.insertRow(row)
        d = data or {}
        for ci, col in enumerate(_E):
            self._table.setItem(row, ci, self._make_item(str(d.get(col, ""))))
        self._table.setItem(row, _5PCT_COL,        self._make_item("", editable=False))
        self._table.setItem(row, _10PCT_COL,       self._make_item("", editable=False))
        self._table.setItem(row, _WEEKLY_ROR_COL,  self._make_item("", editable=False))
        self._table.setItem(row, _WEEKS_HELD_COL,  self._make_item("", editable=False))
        self._table.blockSignals(False)
        self._recompute_row(row)
        self._table.setSortingEnabled(True)
        self._save()

    def _recompute_row(self, row: int):
        def _float(col):
            it = self._table.item(row, col)
            if not it:
                return None
            try:
                return float(it.text().replace("$", "").replace(",", "").replace("%", ""))
            except ValueError:
                return None

        strike  = _float(_STRIKE_COL)
        premium = _float(_PREMIUM_COL)

        exp_text = ""
        it = self._table.item(row, _EXP_COL)
        if it:
            exp_text = it.text().strip()

        opened_text = ""
        it = self._table.item(row, _OPENED_COL)
        if it:
            opened_text = it.text().strip()

        self._table.blockSignals(True)

        if strike is not None:
            self._table.item(row, _5PCT_COL).setText(f"{strike * 0.95:.2f}")
            self._table.item(row, _10PCT_COL).setText(f"{strike * 0.90:.2f}")
        else:
            self._table.item(row, _5PCT_COL).setText("")
            self._table.item(row, _10PCT_COL).setText("")

        # Weekly ROR uses total days deployed (initial open → expiry), not just DTE
        # remaining, so near-expiry rolls don't show an artificially inflated rate.
        ror_text = ""
        if strike and strike > 0 and premium is not None and exp_text:
            try:
                em, ed, ey = (int(p) for p in exp_text.split("/"))
                dte = (date(ey, em, ed) - date.today()).days
                total_days = dte
                if opened_text:
                    om, od, oy = (int(p) for p in opened_text.split("/"))
                    days_held  = (date.today() - date(oy, om, od)).days
                    total_days = days_held + dte
                if total_days > 0:
                    ror_text = f"{(premium / strike) * (7 / total_days) * 100:.2f}%"
            except (ValueError, ZeroDivisionError):
                pass
        self._table.item(row, _WEEKLY_ROR_COL).setText(ror_text)

        weeks_text = ""
        if opened_text:
            try:
                m, d, y = (int(p) for p in opened_text.split("/"))
                days_held = (date.today() - date(y, m, d)).days
                if days_held >= 0:
                    weeks_text = f"{days_held / 7:.1f}"
            except ValueError:
                pass
        self._table.item(row, _WEEKS_HELD_COL).setText(weeks_text)

        self._table.blockSignals(False)

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() in (_STRIKE_COL, _EXP_COL, _PREMIUM_COL, _OPENED_COL):
            self._recompute_row(item.row())
        self._save()

    def _delete_selected(self):
        rows = sorted({i.row() for i in self._table.selectedItems()}, reverse=True)
        if not rows:
            return
        self._table.blockSignals(True)
        for r in rows:
            self._table.removeRow(r)
        self._table.blockSignals(False)
        self._save()
        self._status_label.setText(f"Deleted {len(rows)} row(s).")

    # ── persistence ───────────────────────────────────────────────────────────

    def _row_to_dict(self, row: int) -> dict:
        return {
            col: (self._table.item(row, ci).text()
                  if self._table.item(row, ci) else "")
            for ci, col in enumerate(_E)
        }

    def _save(self):
        rows = [self._row_to_dict(r) for r in range(self._table.rowCount())]
        POSITIONS_FILE.write_text(json.dumps(rows, indent=2))

    def _load_saved(self):
        if not POSITIONS_FILE.exists():
            return
        try:
            rows = json.loads(POSITIONS_FILE.read_text())
        except Exception:
            return
        for row in rows:
            self._add_row(row)
        if rows:
            self._status_label.setText(f"Loaded {len(rows)} saved position(s).")

    # ── CSV import ────────────────────────────────────────────────────────────

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import TOS Position CSV", "",
            "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        parsed, warnings = _parse_tos_csv(Path(path))
        if not parsed:
            msg = "No option positions found in that file."
            if warnings:
                msg += "\n\n" + "\n".join(warnings[:5])
            QMessageBox.warning(self, "Import", msg)
            return
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        self._table.blockSignals(False)
        for row in parsed:
            self._add_row(row)
        note = f"Imported {len(parsed)} position(s)."
        if warnings:
            note += f"  ({len(warnings)} row(s) skipped)"
        self._status_label.setText(note)


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
# Single leg: "SOLD -2 KTOS 100 (Weeklys) 22 MAY 26 50 PUT @.71 CBOE"
_TRD_SINGLE_RE = re.compile(
    r'(?:SOLD|BOT)\s+\S+\s+([A-Z]{1,10})\s+100'
    r'.*?(\d{1,2}\s+[A-Z]{3}\s+\d{2,4})'
    r'\s+([\d.]+)\s+(PUT|CALL)',
    re.IGNORECASE,
)


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


def _build_premium_lookup(rows: list[list[str]]) -> dict[tuple, dict]:
    """Return {(SYM, exp_MM/DD/YYYY, strike, TYPE_UPPER): {"price": str, "opened": str}}
    from Account Trade History SELL TO OPEN option rows.

    "price"  — cumulative net credit across all legs in the roll chain.
    "opened" — traced back through every calendar roll to the original put sell date,
               so Weeks Held reflects the full holding period across all rolls.

    Account Trade History is listed newest-first.  Each calendar trade has a SELL
    TO OPEN row followed immediately by a BUY TO CLOSE continuation row (empty
    Exec Time).  The TO CLOSE exp tells us what the new position was rolled *from*,
    building a provenance chain we walk backwards to find the initial open date.
    """
    lookup:      dict[tuple, dict]  = {}   # key → {price, opened} for net-price lookups
    all_opens:   dict[tuple, str]   = {}   # key → oldest open date (overwritten newest→oldest)
    all_prices:  dict[tuple, float] = {}   # key → net credit for that specific leg (never overwritten)
    rolled_from: dict[tuple, tuple] = {}   # new_key → old_key (roll provenance)

    in_section        = False
    col: dict[str, int] = {}
    last_cal_key: tuple | None = None      # pending TO CLOSE leg from a calendar

    for row in rows:
        if not row:
            continue
        cells = [c.strip().strip('"') for c in row]
        first = cells[0]

        if first == "Account Trade History":
            in_section    = True
            col           = {}
            last_cal_key  = None
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
        except (KeyError, IndexError):
            last_cal_key = None
            continue

        if typ not in ("PUT", "CALL"):
            last_cal_key = None
            continue

        exp_fmt = _parse_exp(exp)
        key     = (sym, exp_fmt, strike, typ)

        if side == "SELL" and pos_effect == "TO OPEN":
            try:
                float(net_price)
            except ValueError:
                last_cal_key = None
                continue

            # Parse open date from exec time ("5/19/26 12:06:54" → "05/19/2026")
            opened    = ""
            date_part = exec_time.split()[0] if exec_time else ""
            parts     = date_part.split("/")
            if len(parts) == 3:
                try:
                    m, d, y = int(parts[0]), int(parts[1]), int(parts[2])
                    if y < 100:
                        y += 2000
                    opened = f"{m:02d}/{d:02d}/{y}"
                except ValueError:
                    pass

            if key not in lookup:                    # first = most recent
                lookup[key] = {"price": net_price, "opened": opened}
            if key not in all_prices:               # first = most recent leg credit
                all_prices[key] = float(net_price)
            if opened:
                all_opens[key] = opened              # overwrite → last = oldest

            last_cal_key = key if spread == "CALENDAR" else None

        elif (side == "BUY" and pos_effect == "TO CLOSE"
              and last_cal_key is not None and not exec_time):
            # Continuation leg of a calendar: records which expiration was closed.
            # last_cal_key was opened by rolling *from* this key.
            rolled_from[last_cal_key] = key
            last_cal_key = None

        else:
            last_cal_key = None

    # Walk each position's roll chain to sum cumulative premium and find initial open date.
    for key in lookup:
        cur, seen = key, set()
        total_prem     = 0.0
        initial_opened = ""
        while True:
            if cur in all_prices:
                total_prem += all_prices[cur]
            if cur in all_opens:
                initial_opened = all_opens[cur]
            if cur not in rolled_from or cur in seen:
                break
            seen.add(cur)
            cur = rolled_from[cur]
        lookup[key]["price"] = str(round(total_prem, 4))
        if initial_opened:
            lookup[key]["opened"] = initial_opened

    return lookup


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
                key = (sym.upper(), _parse_exp(exp.strip()), strike.strip(), typ.upper())
                lookup[key] = round(lookup.get(key, 0.0) + total, 2)
        else:
            m = _TRD_SINGLE_RE.search(desc)
            if m:
                sym, exp, strike, typ = m.groups()
                key = (sym.upper(), _parse_exp(exp.strip()), strike.strip(), typ.upper())
                lookup[key] = round(lookup.get(key, 0.0) + total, 2)

    return lookup


def _parse_tos_csv(path: Path) -> tuple[list[dict], list[str]]:
    """Parse a TOS Account Statement CSV.

    Reads the 'Options' section for current open positions and matches
    fees from the Cash Balance TRD rows.
    """
    results:  list[dict] = []
    warnings: list[str]  = []

    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as e:
        return [], [str(e)]

    rows = list(csv.reader(io.StringIO(text)))

    # Build lookups from the full row list before scanning Options section
    fee_lookup     = _build_fee_lookup(rows)
    premium_lookup = _build_premium_lookup(rows)

    # Now find and parse the Options section
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

        # First row after "Options" is the column header
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
            warnings.append(f"Skipped row (missing column {e}): {cells[:5]}")
            continue

        if not sym or sym in ("SYMBOL",):
            continue

        exp_fmt     = _parse_exp(exp)
        contract_key = (sym, exp_fmt, strike.strip(), typ.upper())
        fees         = f"{fee_lookup[contract_key]:.2f}" if contract_key in fee_lookup else ""
        # Prefer Account Trade History Net Price — it reflects the actual credit
        # received for the roll rather than the individual leg price shown in
        # the Options section (which overstates premium for calendar spreads).
        opened = ""
        if contract_key in premium_lookup:
            entry  = premium_lookup[contract_key]
            price  = entry["price"]
            opened = entry["opened"]

        results.append({
            "symbol":     sym,
            "type":       typ,
            "expiration": exp_fmt,
            "strike":     strike,
            "qty":        qty,
            "premium":    price,
            "fees":       fees,
            "opened":     opened,
        })

    if not results and not warnings:
        warnings.append(
            "Could not find an 'Options' section. "
            "Make sure you exported an Account Statement from TOS."
        )

    return results, warnings
