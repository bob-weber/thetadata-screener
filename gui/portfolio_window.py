from PyQt6.QtWidgets import (
    QMainWindow, QTabWidget, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QDateEdit, QMessageBox,
    QDialog, QTextBrowser, QDialogButtonBox,
)
from PyQt6.QtCore import QDate

from .positions_tab import PortfolioTab
from .gains_tab     import GainsTab, WeeklyRorTab, _RANGE_OPTIONS


_HELP_HTML = """
<h2>Portfolio Tracker — Calculations &amp; Methodology</h2>

<p><b>Data source.</b> Everything is derived from imported <i>TOS Account
Statement</i> CSVs (Import TOS CSV…). The parsed events — deposits/withdrawals,
option legs, stock trades, and daily balances — are merged (de-duplicated) and
saved to <code>gains_history.json</code>. Re-importing the same statement is
safe; only new rows are added.</p>

<h3>Global Range (top bar)</h3>
<p>One control drives every tab: <b>1 Week, 3 Weeks, 3 Months, 1 Year, All Time,
Custom</b>. Relative ranges count back from the most recent data point (not
today). Custom uses the From/To dates. On the Positions tab, active
(Open/Holding) positions are always shown regardless of range.</p>

<h3>Positions tab</h3>
<p>One row per wheel cycle (Put → Assigned → Covered Call → …), reconstructed
from the statement. P&amp;L and RoR include all premiums plus the stock outcome,
net of fees.</p>

<h3>P&amp;L Chart tab</h3>
<ul>
<li><b>Realized P&amp;L</b> = option premium cash flows + stock gains/losses
(average-cost basis), recognized on the trade/sale date (cash basis).</li>
<li><b>Total P&amp;L</b> = realized + unrealized. Unrealized marks open share
holdings to market using yfinance closes.</li>
<li><b>Net Deposits</b> is the blue base; gain/loss bars stack on top (green/red).
Untick <i>Show deposits</i> to plot P&amp;L from a zero baseline so it autoscales.</li>
<li><b>RoR %</b> in the titles = P&amp;L ÷ net deposits.</li>
<li>The date axis is the union of all event dates (balance, trade, deposit), so a
trading day plots even if its end-of-day balance row hasn't posted yet.</li>
</ul>

<h3>Weekly RoR tab</h3>
<p>Return on the capital actually <b>deployed in option trades</b> — not total
account capital. Each bar is one week; the bar height is that week's RoR %, and
the number on top is the capital allocated that week.</p>
<p><b>Realized (numerator), per week:</b></p>
<ul>
<li><b>Option premium</b> is <i>accrued</i> — each leg's premium is spread evenly
across the business days it is open (trade date → to-open expiration; for rolls,
the further/to-open expiration). A week's premium is the sum of those daily
slices. So a put rolled out two weeks spreads its premium across those days, and
premium can accrue into future weeks until expiration.</li>
<li><b>Stock gain/loss</b> from selling assigned shares (average-cost basis) is
booked in full on the <b>day of sale</b> (not accrued).</li>
</ul>
<p><b>Allocated capital (denominator), per week:</b> the collateral of open
<b>short puts</b> (strike × 100 × contracts) averaged over the days a position is
open. Long stock positions and covered-call collateral are <i>not</i> counted.</p>
<p><b>Weekly RoR</b> = weekly realized ÷ weekly allocated. Each bar is labelled
with that week's realized gain/loss ($) over the capital allocated.</p>
<p><b>Bar colours:</b> past weeks are realized (green = gain, red = loss); the
<span style="color:#f0ad4e"><b>current week</b></span> is amber (the boundary);
weeks after it are <span style="color:#caa800"><b>projected</b></span> (yellow) —
premium still accruing on positions held now, so allocation tapers off into the
future as those positions expire.</p>
<p>Reconciliation: summed across all weeks, realized equals the P&amp;L Chart's
realized total — accrual only changes <i>which week</i> premium lands in.</p>

<h3>Assumptions &amp; limitations</h3>
<ul>
<li>Accrual uses <b>business days</b> (Mon–Fri); market holidays are ignored.</li>
<li>Weekly allocated counts <b>short-put collateral only</b>. A week with a stock
sale but no open puts has $0 allocated, so its RoR shows 0 % even though the
realized dollars still count in the totals.</li>
<li>Unrealized P&amp;L depends on yfinance returning quotes for open symbols; if it
returns nothing, Total P&amp;L will equal Realized.</li>
</ul>
"""


class PortfolioWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Portfolio Tracker")
        self.resize(1100, 780)

        self._positions_tab = PortfolioTab()
        self._gains_tab     = GainsTab()
        self._weekly_tab    = WeeklyRorTab()

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
        self._positions_tab.csv_imported.connect(self._weekly_tab.reload)
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
        tabs.addTab(positions_tab,    "Positions")
        tabs.addTab(gains_tab,        "P&L Chart")
        tabs.addTab(self._weekly_tab, "Weekly RoR")

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)
        layout.addLayout(bar)
        layout.addWidget(tabs, 1)
        self.setCentralWidget(root)

        help_menu = self.menuBar().addMenu("&Help")
        act = help_menu.addAction("Calculations && Methodology")
        act.triggered.connect(self._show_help)

        self._emit_range()   # apply the default ("All Time") to both tabs

    def _show_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Calculations & Methodology")
        dlg.resize(720, 640)
        lay = QVBoxLayout(dlg)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(_HELP_HTML)
        lay.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dlg.reject)
        buttons.accepted.connect(dlg.accept)
        lay.addWidget(buttons)
        dlg.exec()

    def _emit_range(self):
        sel = self._range_combo.currentData()
        self._custom_box.setVisible(sel == "custom")
        lo = self._date_from.date().toString("yyyy-MM-dd")
        hi = self._date_to.date().toString("yyyy-MM-dd")
        self._positions_tab.apply_range(sel, lo, hi)
        self._gains_tab.apply_range(sel, lo, hi)
        self._weekly_tab.apply_range(sel, lo, hi)

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
        self._weekly_tab.reload()
