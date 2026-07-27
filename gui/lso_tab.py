import json
from datetime import date
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
    QPushButton, QProgressBar, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QSplitter,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QGuiApplication

from .claude_dialog import ClaudeAnalysisDialog
from .chart_window import ChartWindow

from . import column_help
from .workers import LsoWorker, OPTIONS_RESULTS_CACHE

_COLS    = ["grade", "symbol", "stock_price", "strike", "premium", "otm_pct",
            "iv", "cushion_sigma", "iv_hv", "rsi", "bb_pct",
            "spread_pct", "open_interest", "capital",
            "sector", "beta", "mkt_cap_b",
            "earnings_date", "earnings_in_period", "flags", "notes"]
_HEADERS = ["Grade", "Symbol", "Stock", "Strike", "Premium %", "OTM%",
            "IV%", "Cushion σ", "IV/HV", "RSI", "BB%",
            "Spread %", "OI", "Capital",
            "Sector", "Beta", "Mkt Cap ($B)",
            "Earnings Date", "In Period?", "Flags", "Notes"]

# Per-column help, shown when hovering a column header. Where a column feeds the
# grade, the bands it scores on are spelled out — that scoring is the whole point
# of this table, and it's otherwise only visible in the Notes column.
_HELP = {
    "grade":       "Wheel suitability for this specific contract.\n\n"
                   "    A ≥ 85    B ≥ 70    C ≥ 55    D ≥ 40    F below\n\n"
                   "Starts at 70 for the symbol's fundamentals (sector, beta, "
                   "market cap, dividend, earnings), then every contract factor "
                   "below adjusts it. Notes lists each adjustment that fired.",
    "symbol":      "Underlying ticker. Click to open a price / BB / RSI chart.",
    "stock_price": "Underlying price from the stock scan.",
    "strike":      "Strike price. For a cash-secured put, strike × 100 is the "
                   "cash you lock up per contract.",
    "premium":     "Premium as a percentage of the capital the trade ties up:\n"
                   "    put  — premium ÷ strike (the cash you secure)\n"
                   "    call — premium ÷ share price (the shares you hold)\n\n"
                   "This is the figure the 1% rule is stated in, and what the "
                   "Options Scanner's Premium % band filters on.",
    "otm_pct":     "How far out of the money the strike sits.\n\n"
                   "Scores −100 if in the money, −30 under 2%, −10 under 4%, "
                   "+3 to 8%, +5 to 12%, −5 to 16%, −30 beyond — a very wide "
                   "strike still paying 1% means the market has priced in real "
                   "downside.",
    "iv":          "Implied volatility of the near-the-money contract, annualized.\n\n"
                   "Scores −15 under 25% (too thin to roll or sell calls "
                   "against), 0 from 25–80%, −8 above 80%.",
    "cushion_sigma":
                   "OTM distance in expected moves: OTM% ÷ the expected move to "
                   "expiration. The primary risk gate.\n\n"
                   "Scores −30 below 1σ (1.5σ for gappy names — earnings in the "
                   "period, or Energy / Basic Materials), 0 from 1–1.5σ, +5 above.",
    "iv_hv":       "Implied volatility ÷ the stock's own 20-day realized "
                   "volatility, both annualized.\n\n"
                   "Above 1 the option prices more movement than the stock has "
                   "been making — the gap a premium seller is paid for.\n\n"
                   "Scores +3 at 1.30 or above (premium rich), 0 from 0.90–1.30 "
                   "(in line), −3 below 0.90 (selling the move too cheap).",
    "rsi":         "Wilder's RSI of the underlying, from the stock scan.\n\n"
                   "Scores −5 below 20 (capitulation, not a dip), +5 from 20–40 "
                   "(the pullback the screen looks for), 0 from 40–60, −3 from "
                   "60–70, −5 at 70 and above.",
    "bb_pct":      "Where the underlying sits in its Bollinger Bands: 0% = lower "
                   "band, 100% = upper.\n\n"
                   "Scores −5 below 0 (a breakdown, not a dip), +5 from 0–33, "
                   "0 from 33–67, −3 from 67–100, −5 above 100.",
    "spread_pct":  "Bid-ask spread as a percentage of the mid — the toll on "
                   "every roll, since each one is a buy-to-close plus a "
                   "sell-to-open.\n\n"
                   "Scores +3 under 10% (tight), 0 from 10–25%, −5 from 25–50% "
                   "(a round trip gives back real premium), −15 above 50% — at "
                   "that point getting out can cost more than the time value "
                   "you sold.",
    "open_interest":
                   "Open contracts at this strike and expiration.\n\n"
                   "Not scored, but near zero means the quote is theoretical "
                   "and the spread above is unlikely to be achievable.",
    "capital":     "Cash secured per contract: strike × 100.",
    "sector":      "Sector from yfinance. Adjusts the base score — staples, "
                   "utilities and healthcare score up; biotech, crypto-adjacent "
                   "and speculative names score down.",
    "beta":        "Beta vs. the market. Lower is steadier and scores higher; "
                   "high-beta names swing through your strike more easily.",
    "mkt_cap_b":   "Market capitalisation in billions. Larger caps score higher "
                   "for option liquidity and survivability.",
    "earnings_date":     "Next earnings date from yfinance.",
    "earnings_in_period":
                   "Whether earnings fall between now and expiration.\n\n"
                   "If they do the contract takes a −40 hit and needs the wider "
                   "1.5σ cushion — an earnings gap is exactly the move a short "
                   "put can't absorb.",
    "flags":       "Short risk labels raised by the scoring above. Anything here "
                   "is worth reading the Notes for.",
    "notes":       "Every scoring adjustment that fired for this contract, in "
                   "the order applied — the full explanation of the grade.",
}

_SYMBOL_COL = _COLS.index("symbol")
_LINK_COLOR = "#58a6ff"  # light blue link, legible on the dark table background

_GRADE_COLORS = {
    "A": ("#1a7a1a", "#e6ffe6"),  # dark green text, light green bg
    "B": ("#2a6a2a", "#f0fff0"),
    "C": ("#7a6a00", "#fffde6"),  # amber
    "D": ("#8a3300", "#fff0e6"),  # orange
    "F": ("#8a0000", "#ffe6e6"),  # red
    "?": ("#555555", "#f5f5f5"),
}


class _SortItem(QTableWidgetItem):
    """Table item that sorts by a stored numeric key."""
    def __init__(self, display: str, sort_key):
        super().__init__(display)
        self._key = sort_key

    def __lt__(self, other):
        if isinstance(other, _SortItem):
            try:
                return self._key < other._key
            except TypeError:
                pass
        return super().__lt__(other)


def _grade_sort_key(grade: str) -> int:
    return {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4}.get(grade, 5)


class LsoAnalysisTab(QWidget):
    def __init__(self):
        super().__init__()
        self._worker      = None
        self._results_box = None
        self._table       = None
        self._results     = []
        self._chart_windows = []   # keep refs so non-modal charts aren't GC'd
        self._setup_ui()
        self.refresh_options_status()

    def refresh_options_status(self):
        """Update the status label from the last options scan cache, if any."""
        path = Path(OPTIONS_RESULTS_CACHE)
        if not path.exists():
            self._status_label.setText("Run the Options Scanner first.")
            return
        try:
            cached = json.loads(path.read_text())
        except Exception:
            self._status_label.setText("Run the Options Scanner first.")
            return
        results   = cached.get("results", [])
        scan_date = cached.get("date", "unknown date")
        if results:
            self._status_label.setText(
                f"{len(results)} contract(s) from Options Scanner (run {scan_date})"
            )
        else:
            self._status_label.setText("Run the Options Scanner first.")

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── Controls ──────────────────────────────────────────────────────
        ctrl_row = QHBoxLayout()
        self._status_label = QLabel("Run the Options Scanner first.")
        self._run_btn    = QPushButton("Analyze for LSO")
        self._stop_btn   = QPushButton("Stop")
        self._export_btn = QPushButton("Export for Claude")
        self._run_btn.setFixedHeight(32)
        self._stop_btn.setFixedHeight(32)
        self._export_btn.setFixedHeight(32)
        self._stop_btn.setEnabled(False)
        self._export_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._run)
        self._stop_btn.clicked.connect(self._stop)
        self._export_btn.clicked.connect(self._export)
        ctrl_row.addWidget(self._status_label, 1)
        ctrl_row.addWidget(self._run_btn)
        ctrl_row.addWidget(self._stop_btn)
        ctrl_row.addWidget(self._export_btn)
        root.addLayout(ctrl_row)

        # ── Progress ──────────────────────────────────────────────────────
        prog_box = QGroupBox("Progress")
        pl = QVBoxLayout(prog_box)
        self._progress_bar   = QProgressBar()
        self._progress_label = QLabel("—")
        self._progress_bar.setRange(0, 100)
        pl.addWidget(self._progress_bar)
        pl.addWidget(self._progress_label)
        root.addWidget(prog_box)

        # ── Splitter: log + results ───────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        log_box = QGroupBox("Log")
        ll = QVBoxLayout(log_box)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        ll.addWidget(self._log)
        splitter.addWidget(log_box)

        self._results_box = QGroupBox("Results — 0 symbols analyzed")
        rl = QVBoxLayout(self._results_box)

        legend = QLabel(
            "<b>Grade:</b> "
            "<span style='color:#1a7a1a'>A = strong</span> &nbsp; "
            "<span style='color:#2a6a2a'>B = good</span> &nbsp; "
            "<span style='color:#7a6a00'>C = acceptable</span> &nbsp; "
            "<span style='color:#8a3300'>D = risky</span> &nbsp; "
            "<span style='color:#8a0000'>F = avoid</span>"
        )
        rl.addWidget(legend)

        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        column_help.install(self._table, _COLS, _HELP, self)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setWordWrap(True)
        self._table.itemDoubleClicked.connect(self._on_double_click)
        self._table.cellClicked.connect(self._on_cell_clicked)
        rl.addWidget(self._table)
        splitter.addWidget(self._results_box)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 3)
        root.addWidget(splitter, 1)

    # ── slots ─────────────────────────────────────────────────────────────

    def _run(self):
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._log.clear()
        self._table.setRowCount(0)
        self._results_box.setTitle("Results — 0 symbols analyzed")
        self._progress_bar.setValue(0)
        self._progress_label.setText("—")

        self._worker = LsoWorker()
        self._worker.log_msg.connect(self._log.append)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.stop()
        self._stop_btn.setEnabled(False)

    def _on_progress(self, current: int, total: int):
        self._progress_label.setText(f"{current} / {total}")
        if total > 0:
            self._progress_bar.setValue(int(current * 100 / total))

    def _on_finished(self, results: list):
        self._results = results
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._export_btn.setEnabled(bool(results))
        self._progress_bar.setValue(100)
        self._status_label.setText(f"{len(results)} contracts analyzed.")
        self._log.append(f"Done — {len(results)} contracts analyzed.")
        self._populate_table(results)

    def _on_error(self, msg: str):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._log.append(f"ERROR: {msg}")
        self._status_label.setText("Error — see log.")

    def _on_cell_clicked(self, row: int, col: int):
        """Single-click on the (link-styled) Symbol cell opens its chart."""
        if col != _SYMBOL_COL:
            return
        item = self._table.item(row, col)
        symbol = item.text().strip() if item else ""
        if not symbol:
            return
        win = ChartWindow(symbol, self)
        win.finished.connect(lambda _=None, w=win: self._chart_windows.remove(w)
                             if w in self._chart_windows else None)
        self._chart_windows.append(win)
        win.show()

    def _on_double_click(self, item: QTableWidgetItem):
        grade_item = self._table.item(item.row(), _COLS.index("grade"))
        if grade_item is None:
            return
        row_data = grade_item.data(Qt.ItemDataRole.UserRole)
        if not row_data:
            return
        symbol = row_data.get("symbol", "")
        if not symbol:
            return
        dlg = ClaudeAnalysisDialog(symbol, row_data, self)
        dlg.exec()

    def _export(self):
        if not self._results:
            return

        try:
            meta = json.loads(Path(OPTIONS_RESULTS_CACHE).read_text())
        except Exception:
            meta = {}

        lines = [
            f"# LSO Analysis — {date.today()}",
            f"",
            f"- Scan date: {meta.get('date', 'unknown')}",
            f"- Expiration: {meta.get('expiration_date', 'unknown')}",
            f"- Right: {meta.get('right', 'P')}  Side: {meta.get('side', 'sell')}",
            f"- Premium %: {meta.get('premium_pct_min', 0)*100:.1f}% – {meta.get('premium_pct_max', 0)*100:.1f}%",
            f"- Contracts: {len(self._results)}",
            f"",
            f"| Grade | Symbol | Stock | Strike | Premium % | OTM% | IV% | Cushion σ | IV/HV | RSI | BB% | Spread % | OI | Capital | Sector | Beta | Mkt Cap ($B) | Earnings | In Period | Flags | Notes |",
            f"|-------|--------|-------|--------|-----------|------|-----|-----------|---------|-----|-----|----------|-----|---------|--------|------|--------------|----------|-----------|-------|-------|",
        ]

        sorted_results = sorted(
            self._results,
            key=lambda r: (_grade_sort_key(r.get("grade", "?")), r.get("symbol", ""))
        )
        for r in sorted_results:
            def _v(key, prefix="", suffix="", fmt=""):
                val = r.get(key)
                if val is None:
                    return ""
                formatted = format(val, fmt) if fmt else str(val)
                return f"{prefix}{formatted}{suffix}"

            cap = r.get("capital")
            cap_str = f"${int(cap):,}" if cap is not None else ""
            cells = [
                r.get("grade", "?"),
                r.get("symbol", ""),
                _v("stock_price", "$", "", ".2f"),
                _v("strike",      "$", "", ".2f"),
                _v("premium",     "",  "%", ".2f"),
                _v("otm_pct",     "",  "%", ".1f"),
                _v("iv",            "",  "%", ".0f"),
                _v("cushion_sigma", "",  "σ", ".2f"),
                _v("iv_hv",         "",  "",  ".2f"),
                _v("rsi",           "",  "",  ".1f"),
                _v("bb_pct",        "",  "",  ".1f"),
                _v("spread_pct",    "",  "%", ".0f"),
                _v("open_interest", "",  "",  ""),
                cap_str,
                r.get("sector", ""),
                _v("beta",        "",  "", ".2f"),
                _v("mkt_cap_b",   "",  "", ".1f"),
                r.get("earnings_date", ""),
                "YES" if r.get("earnings_in_period") else "no",
                r.get("flags", ""),
                r.get("notes", ""),
            ]
            lines.append("| " + " | ".join(cells) + " |")

        QGuiApplication.clipboard().setText("\n".join(lines))
        self._status_label.setText(f"Copied {len(self._results)} contracts to clipboard.")

    def _populate_table(self, results: list):
        results = sorted(results, key=lambda r: (_grade_sort_key(r.get("grade", "?")), r.get("symbol", "")))
        self._results_box.setTitle(f"Results — {len(results)} contracts analyzed")
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(results))

        for r, row in enumerate(results):
            grade = row.get("grade", "?")
            fg, bg = _GRADE_COLORS.get(grade, ("#000", "#fff"))

            for c, key in enumerate(_COLS):
                val = row.get(key)

                if key == "grade":
                    item = _SortItem(grade, _grade_sort_key(grade))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setForeground(QColor(fg))
                    item.setBackground(QColor(bg))
                    item.setData(Qt.ItemDataRole.UserRole, row)
                elif key == "symbol":
                    item = QTableWidgetItem(str(val) if val is not None else "")
                    item.setForeground(QColor(_LINK_COLOR))
                    font = item.font()
                    font.setUnderline(True)
                    item.setFont(font)
                    item.setToolTip("Click to open price / BB / RSI chart")
                elif key == "stock_price" and val is not None:
                    item = _SortItem(f"${float(val):.2f}", float(val))
                elif key == "strike" and val is not None:
                    item = _SortItem(f"${float(val):.2f}", float(val))
                elif key == "premium" and val is not None:
                    item = _SortItem(f"{float(val):.2f}%", float(val))
                elif key == "otm_pct" and val is not None:
                    item = _SortItem(f"{float(val):.1f}%", float(val))
                elif key == "iv":
                    item = _SortItem(f"{float(val):.0f}%" if val is not None else "—",
                                     float(val) if val is not None else -1.0)
                elif key == "cushion_sigma":
                    item = _SortItem(f"{float(val):.2f}σ" if val is not None else "—",
                                     float(val) if val is not None else -99.0)
                elif key == "iv_hv":
                    item = _SortItem(f"{float(val):.2f}" if val is not None else "—",
                                     float(val) if val is not None else -1.0)
                elif key == "capital" and val is not None:
                    item = _SortItem(f"${int(val):,}", float(val))
                elif key == "beta" and val is not None:
                    item = _SortItem(f"{float(val):.2f}", float(val))
                elif key == "mkt_cap_b" and val is not None:
                    item = _SortItem(f"{float(val):.2f}", float(val))
                elif key == "earnings_in_period":
                    item = QTableWidgetItem("YES" if val else "no")
                    if val:
                        item.setForeground(QColor("#cc0000"))
                        item.setBackground(QColor("#ffe6e6"))
                else:
                    item = QTableWidgetItem(str(val) if val is not None else "")

                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)

        self._table.setSortingEnabled(True)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setStretchLastSection(True)
        notes_col = _COLS.index("notes")
        self._table.setColumnWidth(notes_col, 300)
