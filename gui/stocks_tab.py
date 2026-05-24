import json
from pathlib import Path

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QLabel, QFileDialog, QAbstractItemView, QMessageBox,
)

from .positions_tab import _parse_tos_equities

STOCKS_FILE = Path("my_stock_positions.json")

_E       = ["symbol", "shares", "cost_basis", "current_price"]
_C       = ["total_cost", "current_value", "gain_loss", "ror_pct"]
_ALL     = _E + _C
_HEADERS = ["Symbol", "Shares", "Cost Basis", "Current Price",
            "Total Cost", "Current Value", "Gain/Loss $", "RoR %"]

_SYM_COL    = 0
_SHARES_COL = 1
_COST_COL   = 2
_PRICE_COL  = 3
_TCOST_COL  = 4
_CVAL_COL   = 5
_GL_COL     = 6
_ROR_COL    = 7

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


class ActiveStocksTab(QWidget):
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
        self._refresh_btn = QPushButton("Refresh Prices")
        del_btn           = QPushButton("Delete Selected")
        import_btn.clicked.connect(self._import_csv)
        self._refresh_btn.clicked.connect(self._refresh_prices)
        del_btn.clicked.connect(self._delete_selected)
        toolbar.addWidget(import_btn)
        toolbar.addWidget(self._refresh_btn)
        toolbar.addWidget(del_btn)
        toolbar.addStretch()
        self._status_label = QLabel("")
        toolbar.addWidget(self._status_label)
        root.addLayout(toolbar)

        hint = QLabel("Long positions only.  Cost Basis and Current Price are per share.")
        hint.setStyleSheet("color: grey; font-size: 11px;")
        root.addWidget(hint)

        box = QGroupBox("Long Stock Positions")
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

        summary_box = QGroupBox("Portfolio Summary")
        sl = QHBoxLayout(summary_box)
        self._lbl_cost = QLabel("Total Cost: —")
        self._lbl_val  = QLabel("Current Value: —")
        self._lbl_gl   = QLabel("Gain/Loss: —")
        self._lbl_ror  = QLabel("Overall RoR: —")
        for lbl in (self._lbl_cost, self._lbl_val, self._lbl_gl, self._lbl_ror):
            lbl.setStyleSheet("font-weight: bold; font-size: 13px;")
            sl.addWidget(lbl)
        root.addWidget(summary_box)

    # ── row helpers ───────────────────────────────────────────────────────────

    def _make_item(self, text: str, editable: bool = True) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        if not editable:
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def _cell_float(self, row: int, col: int) -> float | None:
        it = self._table.item(row, col)
        if not it:
            return None
        try:
            return float(it.text().replace("$", "").replace(",", "").replace("%", ""))
        except ValueError:
            return None

    def _add_row(self, data: dict | None = None):
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        row = self._table.rowCount()
        self._table.insertRow(row)
        d = data or {}
        for ci, col in enumerate(_E):
            self._table.setItem(row, ci, self._make_item(str(d.get(col, ""))))
        for ci in range(len(_E), len(_ALL)):
            self._table.setItem(row, ci, self._make_item("", editable=False))
        self._table.blockSignals(False)
        self._recompute_row(row)
        self._table.setSortingEnabled(True)
        self._update_summary()
        self._save()

    def _recompute_row(self, row: int):
        shares = self._cell_float(row, _SHARES_COL)
        cost   = self._cell_float(row, _COST_COL)
        price  = self._cell_float(row, _PRICE_COL)

        self._table.blockSignals(True)
        if shares is not None and cost is not None and shares > 0 and cost > 0:
            total_cost = shares * cost
            self._table.item(row, _TCOST_COL).setText(f"{total_cost:,.2f}")
            if price is not None:
                current_val = shares * price
                gain_loss   = current_val - total_cost
                ror         = gain_loss / total_cost * 100
                color       = _GREEN if gain_loss >= 0 else _RED
                sign        = "+" if gain_loss >= 0 else ""
                self._table.item(row, _CVAL_COL).setText(f"{current_val:,.2f}")
                gl_item  = self._table.item(row, _GL_COL)
                ror_item = self._table.item(row, _ROR_COL)
                gl_item.setText(f"{sign}{gain_loss:,.2f}")
                gl_item.setForeground(color)
                ror_item.setText(f"{sign}{ror:.2f}%")
                ror_item.setForeground(color)
            else:
                for ci in (_CVAL_COL, _GL_COL, _ROR_COL):
                    self._table.item(row, ci).setText("")
        else:
            for ci in (_TCOST_COL, _CVAL_COL, _GL_COL, _ROR_COL):
                self._table.item(row, ci).setText("")
        self._table.blockSignals(False)

    def _update_summary(self):
        total_cost = 0.0
        total_val  = 0.0
        has_prices = False

        for row in range(self._table.rowCount()):
            tc = self._cell_float(row, _TCOST_COL)
            cv = self._cell_float(row, _CVAL_COL)
            if tc is not None:
                total_cost += tc
            if cv is not None:
                total_val  += cv
                has_prices  = True

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
            self._lbl_ror.setText(f"Overall RoR: {sign}{ror:.2f}%")
            self._lbl_ror.setStyleSheet(style)
        else:
            self._lbl_val.setText("Current Value: —")
            self._lbl_gl.setText("Gain/Loss: —")
            self._lbl_ror.setText("Overall RoR: —")
            for lbl in (self._lbl_val, self._lbl_gl, self._lbl_ror):
                lbl.setStyleSheet("font-weight: bold; font-size: 13px;")

    def _on_item_changed(self, item: QTableWidgetItem):
        if item.column() in (_SHARES_COL, _COST_COL, _PRICE_COL):
            self._recompute_row(item.row())
            self._update_summary()
        self._save()

    def _delete_selected(self):
        rows = sorted({i.row() for i in self._table.selectedItems()}, reverse=True)
        if not rows:
            return
        self._table.blockSignals(True)
        for r in rows:
            self._table.removeRow(r)
        self._table.blockSignals(False)
        self._update_summary()
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
        STOCKS_FILE.write_text(json.dumps(rows, indent=2))

    def _load_saved(self):
        if not STOCKS_FILE.exists():
            return
        try:
            rows = json.loads(STOCKS_FILE.read_text())
        except Exception:
            return
        for row in rows:
            self._add_row(row)
        if rows:
            self._status_label.setText(f"Loaded {len(rows)} saved position(s).")

    # ── CSV import ────────────────────────────────────────────────────────────

    def _import_csv(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import TOS Account Statement", "",
            "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return
        parsed, warnings = _parse_tos_equities(Path(path))
        if not parsed:
            msg = "No long equity positions found in that file."
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
            note += f"  ({len(warnings)} skipped)"
        self._status_label.setText(note)

    # ── price refresh ─────────────────────────────────────────────────────────

    def _refresh_prices(self):
        symbols = []
        for row in range(self._table.rowCount()):
            it = self._table.item(row, _SYM_COL)
            if it and it.text().strip():
                symbols.append(it.text().strip().upper())
        if not symbols:
            self._status_label.setText("No positions to refresh.")
            return
        self._refresh_btn.setEnabled(False)
        self._status_label.setText(f"Fetching prices for {len(symbols)} symbol(s)…")
        self._fetcher = _PriceFetcher(symbols)
        self._fetcher.price_ready.connect(self._on_price_ready)
        self._fetcher.log_msg.connect(self._status_label.setText)
        self._fetcher.done.connect(self._on_refresh_done)
        self._fetcher.start()

    def _on_price_ready(self, symbol: str, price: float):
        for row in range(self._table.rowCount()):
            it = self._table.item(row, _SYM_COL)
            if it and it.text().strip().upper() == symbol:
                self._table.blockSignals(True)
                self._table.item(row, _PRICE_COL).setText(f"{price:.2f}")
                self._table.blockSignals(False)
                self._recompute_row(row)
                break
        self._update_summary()

    def _on_refresh_done(self):
        self._refresh_btn.setEnabled(True)
        self._status_label.setText("Prices refreshed.")
        self._save()
