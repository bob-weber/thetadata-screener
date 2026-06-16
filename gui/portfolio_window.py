from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QDateEdit, QMessageBox,
)
from PyQt6.QtCore import QDate

from .positions_tab import PortfolioTab
from .gains_tab     import GainsTab, _RANGE_OPTIONS


class PortfolioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Portfolio Tracker")
        self.resize(1100, 780)

        self._positions_tab = PortfolioTab()
        self._gains_tab     = GainsTab()

        # ── Shared top bar ────────────────────────────────────────────────────
        import_btn   = QPushButton("Import TOS CSV…")
        clear_btn    = QPushButton("Clear All")
        account_lbl  = QLabel("Account: —")
        account_lbl.setStyleSheet("font-weight: bold;")
        status_lbl   = QLabel("")

        # Global time/range control — drives both the Positions table and chart.
        self._range_combo = QComboBox()
        for label, val in _RANGE_OPTIONS:
            self._range_combo.addItem(label, val)
        self._range_combo.setCurrentText("All Time")

        self._custom_box = QWidget()
        cbl = QHBoxLayout(self._custom_box)
        cbl.setContentsMargins(0, 0, 0, 0)
        self._date_from = QDateEdit()
        self._date_to   = QDateEdit()
        for de in (self._date_from, self._date_to):
            de.setCalendarPopup(True)
            de.setDisplayFormat("yyyy-MM-dd")
        cbl.addWidget(QLabel("From:"))
        cbl.addWidget(self._date_from)
        cbl.addWidget(QLabel("To:"))
        cbl.addWidget(self._date_to)
        self._custom_box.setVisible(False)
        self._init_custom_dates()

        self._range_combo.currentIndexChanged.connect(self._emit_range)
        self._date_from.dateChanged.connect(self._emit_range)
        self._date_to.dateChanged.connect(self._emit_range)

        import_btn.clicked.connect(self._positions_tab.import_csv)
        clear_btn.clicked.connect(self._clear_all)

        self._positions_tab.account_changed.connect(
            lambda acct: account_lbl.setText(f"Account: {acct}" if acct else "Account: —"))
        self._positions_tab.status_changed.connect(status_lbl.setText)
        self._positions_tab.csv_imported.connect(self._gains_tab.process_csv)
        self._positions_tab.csv_imported.connect(lambda _p: self._on_data_changed())

        bar = QHBoxLayout()
        bar.addWidget(import_btn)
        bar.addWidget(clear_btn)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Range:"))
        bar.addWidget(self._range_combo)
        bar.addWidget(self._custom_box)
        bar.addSpacing(16)
        bar.addWidget(account_lbl)
        bar.addStretch()
        bar.addWidget(status_lbl)

        gains_tab     = self._gains_tab
        positions_tab = self._positions_tab

        tabs = QTabWidget()
        tabs.addTab(positions_tab, "Positions")
        tabs.addTab(gains_tab,     "P&L Chart")

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addLayout(bar)
        layout.addWidget(tabs, 1)
        self.setCentralWidget(root)

        self._emit_range()   # apply the default ("All Time") to both tabs

    def _emit_range(self):
        sel = self._range_combo.currentData()
        self._custom_box.setVisible(sel == "custom")
        lo = self._date_from.date().toString("yyyy-MM-dd")
        hi = self._date_to.date().toString("yyyy-MM-dd")
        self._positions_tab.apply_range(sel, lo, hi)
        self._gains_tab.apply_range(sel, lo, hi)

    def _init_custom_dates(self):
        """Default the From/To pickers to the full span of imported data."""
        lo, hi = self._gains_tab.data_date_bounds()
        for de, iso in ((self._date_from, lo), (self._date_to, hi)):
            de.blockSignals(True)
            if iso:
                de.setDate(QDate.fromString(iso, "yyyy-MM-dd"))
            de.blockSignals(False)

    def _on_data_changed(self):
        """After a CSV import: refresh custom-date bounds and re-apply the range."""
        if self._range_combo.currentData() != "custom":
            self._init_custom_dates()
        self._emit_range()

    def _clear_all(self):
        reply = QMessageBox.question(
            self, "Clear All",
            "Clear all positions and P&L history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._positions_tab.clear_all()
        self._gains_tab.clear_history()
