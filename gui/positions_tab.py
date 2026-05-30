import calendar
import csv
import io
import json
import re
from datetime import date
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QFileDialog,
    QAbstractItemView, QMessageBox,
)

POSITIONS_FILE = Path("my_option_positions.json")
STOCKS_FILE    = Path("my_stock_positions.json")

# ── Options column layout ──────────────────────────────────────────────────────
_OE     = ["symbol", "type", "expiration", "strike", "qty", "premium", "fees", "opened"]
_OC     = ["5pct_below", "10pct_below", "weekly_ror", "weeks_held"]
_O_HDRS = ["Symbol", "P/C", "Expiration", "Strike", "Qty",
           "Premium", "Fees", "Opened", "5% Below", "10% Below",
           "Weekly ROR", "Weeks Held"]

_O_STRIKE_COL  = _OE.index("strike")
_O_EXP_COL     = _OE.index("expiration")
_O_PREMIUM_COL = _OE.index("premium")
_O_OPENED_COL  = _OE.index("opened")
_O_5PCT_COL    = len(_OE)
_O_10PCT_COL   = len(_OE) + 1
_O_ROR_COL     = len(_OE) + 2
_O_WEEKS_COL   = len(_OE) + 3

# ── Stocks column layout ───────────────────────────────────────────────────────
_SE     = ["symbol", "shares", "cost_basis", "current_price", "opened"]
_SC     = ["total_cost", "current_value", "gain_loss", "ror_pct"]
_S_HDRS = ["Symbol", "Shares", "Cost Basis", "Current Price", "Opened",
           "Total Cost", "Current Value", "Gain/Loss $", "RoR %"]

_S_SYM_COL    = 0
_S_SHARES_COL = 1
_S_COST_COL   = 2
_S_PRICE_COL  = 3
_S_OPENED_COL = 4
_S_TCOST_COL  = 5
_S_CVAL_COL   = 6
_S_GL_COL     = 7
_S_ROR_COL    = 8

_GREEN = QColor("#2e7d32")
_RED   = QColor("#c62828")


class _PriceFetcher(QThread):
    price_ready = pyqtSignal(str, float)
    log_msg     = pyqtSignal(str)
    done        = pyqtSignal()

    def __init__(self, symbols: list[str]):
        super().__init__()
        self._symbols = symbols

    def run(self):
        import yfinance as yf
        for sym in self._symbols:
            try:
                hist = yf.Ticker(sym).history(period="2d")
                if not hist.empty:
                    self.price_ready.emit(sym, float(hist["Close"].iloc[-1]))
                else:
                    self.log_msg.emit(f"{sym}: no price data")
            except Exception as e:
                self.log_msg.emit(f"{sym}: {e}")
        self.done.emit()


class PortfolioTab(QWidget):
    def __init__(self):
        super().__init__()
        self._fetcher = None
        self._setup_ui()
        self._load_saved()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        toolbar = QHBoxLayout()
        import_btn        = QPushButton("Import TOS CSV…")
        add_opt_btn       = QPushButton("Add Option")
        del_opt_btn       = QPushButton("Delete Option")
        self._refresh_btn = QPushButton("Refresh Prices")
        del_stk_btn       = QPushButton("Delete Stock")
        import_btn.clicked.connect(self._import_csv)
        add_opt_btn.clicked.connect(lambda: self._add_option_row())
        del_opt_btn.clicked.connect(self._delete_selected_options)
        self._refresh_btn.clicked.connect(self._refresh_prices)
        del_stk_btn.clicked.connect(self._delete_selected_stocks)
        for w in (import_btn, add_opt_btn, del_opt_btn):
            toolbar.addWidget(w)
        toolbar.addSpacing(12)
        for w in (self._refresh_btn, del_stk_btn):
            toolbar.addWidget(w)
        toolbar.addStretch()
        self._status_label = QLabel("")
        toolbar.addWidget(self._status_label)
        root.addLayout(toolbar)

        hint = QLabel(
            "Options: Strike / 5%–10% below are in dollars; Qty negative = short.  "
            "Stocks: Cost Basis and Current Price are per share."
        )
        hint.setStyleSheet("color: grey; font-size: 11px;")
        root.addWidget(hint)

        opt_box = QGroupBox("Option Positions")
        ol = QVBoxLayout(opt_box)
        self._opt_table = QTableWidget(0, len(_O_HDRS))
        self._opt_table.setHorizontalHeaderLabels(_O_HDRS)
        self._opt_table.setSortingEnabled(True)
        self._opt_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._opt_table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked |
                                        QTableWidget.EditTrigger.SelectedClicked)
        self._opt_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._opt_table.verticalHeader().setVisible(False)
        self._opt_table.itemChanged.connect(self._on_opt_item_changed)
        ol.addWidget(self._opt_table)
        root.addWidget(opt_box, 2)

        stk_box = QGroupBox("Long Stock Positions")
        sl = QVBoxLayout(stk_box)
        self._stk_table = QTableWidget(0, len(_S_HDRS))
        self._stk_table.setHorizontalHeaderLabels(_S_HDRS)
        self._stk_table.setSortingEnabled(True)
        self._stk_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._stk_table.setEditTriggers(QTableWidget.EditTrigger.DoubleClicked |
                                        QTableWidget.EditTrigger.SelectedClicked)
        self._stk_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._stk_table.verticalHeader().setVisible(False)
        self._stk_table.itemChanged.connect(self._on_stk_item_changed)
        sl.addWidget(self._stk_table)
        root.addWidget(stk_box, 1)

        sum_box = QGroupBox("Portfolio Summary")
        sml = QHBoxLayout(sum_box)
        self._lbl_cost    = QLabel("Total Cost: —")
        self._lbl_val     = QLabel("Current Value: —")
        self._lbl_gl      = QLabel("Gain/Loss: —")
        self._lbl_ror     = QLabel("Total RoR: —")
        self._lbl_ann_ror = QLabel("Annualized RoR: —")
        for lbl in (self._lbl_cost, self._lbl_val, self._lbl_gl,
                    self._lbl_ror, self._lbl_ann_ror):
            lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
            sml.addWidget(lbl)
        root.addWidget(sum_box)

    # ── shared helpers ────────────────────────────────────────────────────────

    def _make_item(self, text: str, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    # ── Options rows ──────────────────────────────────────────────────────────

    def _add_option_row(self, data: dict | None = None):
        self._opt_table.setSortingEnabled(False)
        self._opt_table.blockSignals(True)
        row = self._opt_table.rowCount()
        self._opt_table.insertRow(row)
        d = data or {}
        for ci, col in enumerate(_OE):
            self._opt_table.setItem(row, ci, self._make_item(str(d.get(col, ""))))
        self._opt_table.setItem(row, _O_5PCT_COL,  self._make_item("", editable=False))
        self._opt_table.setItem(row, _O_10PCT_COL, self._make_item("", editable=False))
        self._opt_table.setItem(row, _O_ROR_COL,   self._make_item("", editable=False))
        self._opt_table.setItem(row, _O_WEEKS_COL, self._make_item("", editable=False))
        self._opt_table.blockSignals(False)
        self._recompute_option_row(row)
        self._opt_table.setSortingEnabled(True)
        self._save_options()

    def _recompute_option_row(self, row: int):
        def _float(col):
            it = self._opt_table.item(row, col)
            if not it:
                return None
            try:
                return float(it.text().replace("$", "").replace(",", "").replace("%", ""))
            except ValueError:
                return None

        strike  = _float(_O_STRIKE_COL)
        premium = _float(_O_PREMIUM_COL)

        it = self._opt_table.item(row, _O_EXP_COL)
        exp_text = it.text().strip() if it else ""
        it = self._opt_table.item(row, _O_OPENED_COL)
        opened_text = it.text().strip() if it else ""

        self._opt_table.blockSignals(True)

        if strike is not None:
            self._opt_table.item(row, _O_5PCT_COL).setText(f"{strike * 0.95:.2f}")
            self._opt_table.item(row, _O_10PCT_COL).setText(f"{strike * 0.90:.2f}")
        else:
            self._opt_table.item(row, _O_5PCT_COL).setText("")
            self._opt_table.item(row, _O_10PCT_COL).setText("")

        # Weekly ROR uses total days deployed (initial open → expiry), not just DTE
        # remaining, so near-expiry rolls don't show an artificially inflated rate.
        ror_text = ""
        if strike and strike > 0 and premium is not None and exp_text and opened_text:
            try:
                em, ed, ey = (int(p) for p in exp_text.split("/"))
                dte = (date(ey, em, ed) - date.today()).days
                om, od, oy = (int(p) for p in opened_text.split("/"))
                days_held  = (date.today() - date(oy, om, od)).days
                total_days = days_held + dte
                if total_days > 0:
                    ror_text = f"{(premium / strike) * (7 / total_days) * 100:.2f}%"
            except (ValueError, ZeroDivisionError):
                pass
        self._opt_table.item(row, _O_ROR_COL).setText(ror_text)

        weeks_text = ""
        if opened_text:
            try:
                m, d, y = (int(p) for p in opened_text.split("/"))
                days_held = (date.today() - date(y, m, d)).days
                if days_held >= 0:
                    weeks_text = f"{days_held / 7:.1f}"
            except ValueError:
                pass
        self._opt_table.item(row, _O_WEEKS_COL).setText(weeks_text)

        self._opt_table.blockSignals(False)

    def _on_opt_item_changed(self, item: QTableWidgetItem):
        if item.column() in (_O_STRIKE_COL, _O_EXP_COL, _O_PREMIUM_COL, _O_OPENED_COL):
            self._recompute_option_row(item.row())
        self._save_options()

    def _delete_selected_options(self):
        rows = sorted({i.row() for i in self._opt_table.selectedItems()}, reverse=True)
        if not rows:
            return
        self._opt_table.blockSignals(True)
        for r in rows:
            self._opt_table.removeRow(r)
        self._opt_table.blockSignals(False)
        self._save_options()
        self._status_label.setText(f"Deleted {len(rows)} option row(s).")

    # ── Stocks rows ───────────────────────────────────────────────────────────

    def _add_stock_row(self, data: dict | None = None):
        self._stk_table.setSortingEnabled(False)
        self._stk_table.blockSignals(True)
        row = self._stk_table.rowCount()
        self._stk_table.insertRow(row)
        d = data or {}
        for ci, col in enumerate(_SE):
            self._stk_table.setItem(row, ci, self._make_item(str(d.get(col, ""))))
        for ci in range(len(_SE), len(_SE) + len(_SC)):
            self._stk_table.setItem(row, ci, self._make_item("", editable=False))
        self._stk_table.blockSignals(False)
        self._recompute_stock_row(row)
        self._stk_table.setSortingEnabled(True)
        self._update_summary()
        self._save_stocks()

    def _cell_float_stk(self, row: int, col: int) -> float | None:
        it = self._stk_table.item(row, col)
        if not it:
            return None
        try:
            return float(it.text().replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return None

    def _recompute_stock_row(self, row: int):
        shares = self._cell_float_stk(row, _S_SHARES_COL)
        cost   = self._cell_float_stk(row, _S_COST_COL)
        price  = self._cell_float_stk(row, _S_PRICE_COL)

        self._stk_table.blockSignals(True)
        if shares is not None and cost is not None and shares > 0 and cost > 0:
            total_cost = shares * cost
            self._stk_table.item(row, _S_TCOST_COL).setText(f"{total_cost:,.2f}")
            if price is not None:
                current_val = shares * price
                gain_loss   = current_val - total_cost
                ror         = gain_loss / total_cost * 100
                color       = _GREEN if gain_loss >= 0 else _RED
                sign        = "+" if gain_loss >= 0 else ""
                self._stk_table.item(row, _S_CVAL_COL).setText(f"{current_val:,.2f}")
                gl_item  = self._stk_table.item(row, _S_GL_COL)
                ror_item = self._stk_table.item(row, _S_ROR_COL)
                gl_item.setText(f"{sign}{gain_loss:,.2f}")
                gl_item.setForeground(color)
                ror_item.setText(f"{sign}{ror:.2f}%")
                ror_item.setForeground(color)
            else:
                for ci in (_S_CVAL_COL, _S_GL_COL, _S_ROR_COL):
                    self._stk_table.item(row, ci).setText("")
        else:
            for ci in (_S_TCOST_COL, _S_CVAL_COL, _S_GL_COL, _S_ROR_COL):
                self._stk_table.item(row, ci).setText("")
        self._stk_table.blockSignals(False)

    def _update_summary(self):
        total_cost = 0.0
        total_val  = 0.0
        has_prices = False

        # For annualized RoR: cost-weighted average across positions with an opened date
        ann_weight = 0.0
        ann_sum    = 0.0

        for row in range(self._stk_table.rowCount()):
            tc = self._cell_float_stk(row, _S_TCOST_COL)
            cv = self._cell_float_stk(row, _S_CVAL_COL)
            if tc is not None:
                total_cost += tc
            if cv is not None:
                total_val  += cv
                has_prices  = True

            if tc and tc > 0 and cv is not None:
                it = self._stk_table.item(row, _S_OPENED_COL)
                opened_text = it.text().strip() if it else ""
                if opened_text:
                    try:
                        m, d, y = (int(p) for p in opened_text.split("/"))
                        days_held = (date.today() - date(y, m, d)).days
                        if days_held > 0:
                            ann_ror = ((cv / tc) ** (365 / days_held) - 1) * 100
                            ann_sum    += ann_ror * tc
                            ann_weight += tc
                    except (ValueError, ZeroDivisionError):
                        pass

        self._lbl_cost.setText(f"Total Cost: ${total_cost:,.2f}")
        self._lbl_cost.setStyleSheet("font-weight: bold; font-size: 13px;")

        if has_prices and total_cost > 0:
            gain  = total_val - total_cost
            ror   = gain / total_cost * 100
            sign  = "+" if gain >= 0 else ""
            color = "#2e7d32" if gain >= 0 else "#c62828"
            style = f"font-weight: bold; font-size: 13px; color: {color};"
            self._lbl_val.setText(f"Current Value: ${total_val:,.2f}")
            self._lbl_val.setStyleSheet("font-weight: bold; font-size: 13px;")
            self._lbl_gl.setText(f"Gain/Loss: {sign}${gain:,.2f}")
            self._lbl_gl.setStyleSheet(style)
            self._lbl_ror.setText(f"Total RoR: {sign}{ror:.2f}%")
            self._lbl_ror.setStyleSheet(style)
        else:
            self._lbl_val.setText("Current Value: —")
            self._lbl_gl.setText("Gain/Loss: —")
            self._lbl_ror.setText("Total RoR: —")
            for lbl in (self._lbl_val, self._lbl_gl, self._lbl_ror):
                lbl.setStyleSheet("font-weight: bold; font-size: 13px;")

        if ann_weight > 0:
            ann = ann_sum / ann_weight
            sign  = "+" if ann >= 0 else ""
            color = "#2e7d32" if ann >= 0 else "#c62828"
            self._lbl_ann_ror.setText(f"Annualized RoR: {sign}{ann:.2f}%")
            self._lbl_ann_ror.setStyleSheet(
                f"font-weight: bold; font-size: 13px; color: {color};"
            )
        else:
            self._lbl_ann_ror.setText("Annualized RoR: —")
            self._lbl_ann_ror.setStyleSheet("font-weight: bold; font-size: 13px;")

    def _on_stk_item_changed(self, item: QTableWidgetItem):
        if item.column() in (_S_SHARES_COL, _S_COST_COL, _S_PRICE_COL):
            self._recompute_stock_row(item.row())
        if item.column() in (_S_SHARES_COL, _S_COST_COL, _S_PRICE_COL, _S_OPENED_COL):
            self._update_summary()
        self._save_stocks()

    def _delete_selected_stocks(self):
        rows = sorted({i.row() for i in self._stk_table.selectedItems()}, reverse=True)
        if not rows:
            return
        self._stk_table.blockSignals(True)
        for r in rows:
            self._stk_table.removeRow(r)
        self._stk_table.blockSignals(False)
        self._update_summary()
        self._save_stocks()
        self._status_label.setText(f"Deleted {len(rows)} stock row(s).")

    # ── persistence ───────────────────────────────────────────────────────────

    def _opt_row_to_dict(self, row: int) -> dict:
        return {
            col: (self._opt_table.item(row, ci).text()
                  if self._opt_table.item(row, ci) else "")
            for ci, col in enumerate(_OE)
        }

    def _stk_row_to_dict(self, row: int) -> dict:
        return {
            col: (self._stk_table.item(row, ci).text()
                  if self._stk_table.item(row, ci) else "")
            for ci, col in enumerate(_SE)
        }

    def _save_options(self):
        rows = [self._opt_row_to_dict(r) for r in range(self._opt_table.rowCount())]
        POSITIONS_FILE.write_text(json.dumps(rows, indent=2))

    def _save_stocks(self):
        rows = [self._stk_row_to_dict(r) for r in range(self._stk_table.rowCount())]
        STOCKS_FILE.write_text(json.dumps(rows, indent=2))

    def _load_saved(self):
        if POSITIONS_FILE.exists():
            try:
                for row in json.loads(POSITIONS_FILE.read_text()):
                    self._add_option_row(row)
            except Exception:
                pass

        if STOCKS_FILE.exists():
            try:
                for row in json.loads(STOCKS_FILE.read_text()):
                    self._add_stock_row(row)
            except Exception:
                pass

    # ── CSV import ────────────────────────────────────────────────────────────

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import TOS Account Statement", "",
            "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return

        opts, opt_warns, stks, stk_warns = _parse_tos_csv(Path(path))

        if not opts and not stks:
            all_warns = opt_warns + stk_warns
            msg = "No option or equity positions found."
            if all_warns:
                msg += "\n\n" + "\n".join(all_warns[:5])
            QMessageBox.warning(self, "Import", msg)
            return

        if opts:
            self._opt_table.blockSignals(True)
            self._opt_table.setRowCount(0)
            self._opt_table.blockSignals(False)
            for row in opts:
                self._add_option_row(row)

        if stks:
            self._stk_table.blockSignals(True)
            self._stk_table.setRowCount(0)
            self._stk_table.blockSignals(False)
            for row in stks:
                self._add_stock_row(row)

        parts = []
        if opts:
            parts.append(f"{len(opts)} option(s)")
        if stks:
            parts.append(f"{len(stks)} stock(s)")
        note = "Imported " + " and ".join(parts) + "."
        all_warns = opt_warns + stk_warns
        if all_warns:
            note += f"  ({len(all_warns)} row(s) skipped)"
        self._status_label.setText(note)

    # ── price refresh ─────────────────────────────────────────────────────────

    def _refresh_prices(self):
        symbols = []
        for row in range(self._stk_table.rowCount()):
            it = self._stk_table.item(row, _S_SYM_COL)
            if it and it.text().strip():
                symbols.append(it.text().strip().upper())
        if not symbols:
            self._status_label.setText("No stock positions to refresh.")
            return
        self._refresh_btn.setEnabled(False)
        self._status_label.setText(f"Fetching prices for {len(symbols)} symbol(s)…")
        self._fetcher = _PriceFetcher(symbols)
        self._fetcher.price_ready.connect(self._on_price_ready)
        self._fetcher.log_msg.connect(self._status_label.setText)
        self._fetcher.done.connect(self._on_refresh_done)
        self._fetcher.start()

    def _on_price_ready(self, symbol: str, price: float):
        for row in range(self._stk_table.rowCount()):
            it = self._stk_table.item(row, _S_SYM_COL)
            if it and it.text().strip().upper() == symbol:
                self._stk_table.blockSignals(True)
                self._stk_table.item(row, _S_PRICE_COL).setText(f"{price:.2f}")
                self._stk_table.blockSignals(False)
                self._recompute_stock_row(row)
                break
        self._update_summary()

    def _on_refresh_done(self):
        self._refresh_btn.setEnabled(True)
        self._status_label.setText("Prices refreshed.")
        self._save_stocks()


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
        key     = (sym, exp_fmt, _norm_strike(strike), typ)

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
