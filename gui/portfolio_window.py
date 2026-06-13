from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QMessageBox,
)

from .positions_tab import PortfolioTab, _DATE_FILTERS
from .gains_tab     import GainsTab


class PortfolioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Portfolio Tracker")
        self.resize(1100, 780)

        positions_tab = PortfolioTab()
        gains_tab     = GainsTab()

        # ── Shared top bar ────────────────────────────────────────────────────
        import_btn   = QPushButton("Import TOS CSV…")
        clear_btn    = QPushButton("Clear All")
        date_combo   = QComboBox()
        date_combo.addItems(_DATE_FILTERS)
        account_lbl  = QLabel("Account: —")
        account_lbl.setStyleSheet("font-weight: bold;")
        status_lbl   = QLabel("")

        import_btn.clicked.connect(positions_tab.import_csv)
        clear_btn.clicked.connect(lambda: self._clear_all(positions_tab, gains_tab))
        date_combo.currentTextChanged.connect(positions_tab.set_date_filter)

        positions_tab.account_changed.connect(
            lambda acct: account_lbl.setText(f"Account: {acct}" if acct else "Account: —"))
        positions_tab.status_changed.connect(status_lbl.setText)
        positions_tab.csv_imported.connect(gains_tab.process_csv)

        bar = QHBoxLayout()
        bar.addWidget(import_btn)
        bar.addWidget(clear_btn)
        bar.addSpacing(12)
        bar.addWidget(QLabel("Show:"))
        bar.addWidget(date_combo)
        bar.addSpacing(16)
        bar.addWidget(account_lbl)
        bar.addStretch()
        bar.addWidget(status_lbl)

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

    def _clear_all(self, positions_tab: PortfolioTab, gains_tab: GainsTab):
        reply = QMessageBox.question(
            self, "Clear All",
            "Clear all positions and P&L history?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        positions_tab.clear_all()
        gains_tab.clear_history()
