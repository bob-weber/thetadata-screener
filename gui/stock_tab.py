import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLineEdit, QPushButton, QProgressBar, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QFileDialog, QSplitter,
    QRadioButton, QButtonGroup, QApplication,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal

from . import column_help
from .workers import StockScanWorker, UniverseWorker

PRICE_CACHE      = "price_screen_cache.json"
CANDIDATES_CACHE = "tech_candidates_cache.json"
MY_STOCKS_FILE   = Path("my_positions.txt")

_PRICE_COLS    = ["symbol", "price"]
_PRICE_HEADERS = ["Symbol", "Price"]

_COLS    = ["symbol", "price", "rsi", "bb_pct"]
_HEADERS = ["Symbol", "Price", "RSI", "BB%"]

# Per-column help, shown when hovering a column header.
_HELP = {
    "symbol": "Ticker symbol.",
    "price":  "Last trade price from Schwab.\n\n"
              "On a trading day this live quote is also used as today's closing "
              "bar when computing RSI and BB%, so both track the current price "
              "rather than yesterday's close.",
    "rsi":    "Wilder's RSI over the RSI-period bars (default 14).\n\n"
              "Below 30 is oversold, above 70 overbought. A universe scan keeps "
              "only symbols under the RSI threshold; a My Stocks scan reports it "
              "for every ticker without filtering.",
    "bb_pct": "Where price sits inside its Bollinger Bands: 0% = lower band, "
              "100% = upper band, over the BB period (default 20) at 2σ.\n\n"
              "Below 0 means price has broken under the lower band; above 100, "
              "over the upper one.",
}




class _NumericItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except ValueError:
            return super().__lt__(other)


def _make_item(val) -> QTableWidgetItem:
    key_is_numeric = isinstance(val, (int, float))
    if key_is_numeric:
        item = _NumericItem(f"{val:.2f}" if isinstance(val, float) else str(val))
    else:
        item = QTableWidgetItem(str(val) if val is not None else "")
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _new_table(headers: list[str], cols: list[str] | None = None) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    if cols:
        column_help.install(table, cols, _HELP)
    table.setSortingEnabled(True)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    table.verticalHeader().setVisible(False)
    return table


def _fill_table(table: QTableWidget, rows: list[dict], cols: list[str]):
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    for r, row in enumerate(rows):
        for c, key in enumerate(cols):
            table.setItem(r, c, _make_item(row.get(key, "")))
    table.setSortingEnabled(True)
    table.resizeColumnsToContents()


def _append_rows(table: QTableWidget, rows: list[dict], cols: list[str]):
    """Append rows to a table without disturbing those already shown."""
    table.setSortingEnabled(False)
    for row in rows:
        r = table.rowCount()
        table.insertRow(r)
        for c, key in enumerate(cols):
            table.setItem(r, c, _make_item(row.get(key, "")))
    table.setSortingEnabled(True)
    table.resizeColumnsToContents()


class StockScannerTab(QWidget):
    scan_finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._worker = None

        # Debounced write-back of the My Stocks editor, flushed on quit so an
        # edit made just before exiting isn't lost inside the timer window.
        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._save_my_stocks)
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._flush_saves)

        self._setup_ui()
        self._load_my_stocks()
        self._load_cached_results()
        self._refresh_universe_label()

    # ── my-stocks file helpers ─────────────────────────────────────────────────

    def _load_my_stocks(self):
        if MY_STOCKS_FILE.exists():
            self._my_stocks_edit.setPlainText(MY_STOCKS_FILE.read_text())

    def _save_my_stocks(self):
        MY_STOCKS_FILE.write_text(self._my_stocks_edit.toPlainText())

    def _flush_saves(self):
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_my_stocks()

    def _get_my_stocks(self) -> list[str]:
        raw = self._my_stocks_edit.toPlainText()
        return [t.strip().upper() for t in raw.splitlines() if t.strip()]

    # ── startup cache loading ──────────────────────────────────────────────────

    def _load_cached_results(self):
        self._load_cache(PRICE_CACHE, "qualified", self._price_table,
                         self._price_box, _PRICE_COLS, "symbols")
        self._load_cache(CANDIDATES_CACHE, "candidates", self._cand_table,
                         self._cand_box, _COLS, "candidates")

    def _load_cache(self, path_str, key, table, box, cols, noun):
        path = Path(path_str)
        if not path.exists():
            return
        try:
            cached = json.loads(path.read_text())
        except Exception:
            return
        rows = cached.get(key, [])
        ts   = cached.get("scanned_at") or cached.get("date", "unknown")
        _fill_table(table, rows, cols)
        box.setTitle(f"{box.property('_base')} — {len(rows)} {noun}  |  last scan: {ts}")

    # ── UI construction ─────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── Ticker source ─────────────────────────────────────────────────────
        source_box = QGroupBox("Ticker Source")
        sh = QHBoxLayout(source_box)
        sh.setSpacing(16)
        self._universe_src_btn = QRadioButton("Universe / Watchlist")
        self._mine_src_btn     = QRadioButton("My Stocks")
        self._universe_src_btn.setChecked(True)
        self._source_group = QButtonGroup()
        self._source_group.addButton(self._universe_src_btn, 0)
        self._source_group.addButton(self._mine_src_btn,     1)
        sh.addWidget(self._universe_src_btn)
        sh.addWidget(self._mine_src_btn)
        sh.addStretch()
        root.addWidget(source_box)

        # ── My Stocks editor (hidden until its radio is selected) ─────────────
        self._my_stocks_box = QGroupBox("My Stocks — one ticker per line")
        ml = QVBoxLayout(self._my_stocks_box)
        self._my_stocks_edit = QTextEdit()
        self._my_stocks_edit.setPlaceholderText("AAPL\nMSFT\nTSLA")
        self._my_stocks_edit.setFixedHeight(110)
        self._my_stocks_edit.textChanged.connect(self._save_timer.start)
        ml.addWidget(self._my_stocks_edit)
        self._my_stocks_box.setVisible(False)
        root.addWidget(self._my_stocks_box)

        self._universe_src_btn.toggled.connect(self._on_source_changed)

        # ── Parameters ────────────────────────────────────────────────────────
        params_box = QGroupBox("Parameters")
        pf = QFormLayout(params_box)
        pf.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        price_row = QWidget()
        ph = QHBoxLayout(price_row)
        ph.setContentsMargins(0, 0, 0, 0)
        self._price_min = QLineEdit("10.0")
        self._price_max = QLineEdit("400.0")
        ph.addWidget(self._price_min)
        ph.addWidget(QLabel("–"))
        ph.addWidget(self._price_max)
        pf.addRow("Price range ($):", price_row)

        self._rsi_threshold    = QLineEdit("40.0")
        self._bb_pct_threshold = QLineEdit("33.0")
        self._rsi_period       = QLineEdit("14")
        self._bb_period        = QLineEdit("20")
        pf.addRow("RSI threshold (<):", self._rsi_threshold)
        pf.addRow("BB% threshold (<):", self._bb_pct_threshold)
        pf.addRow("RSI period:",        self._rsi_period)
        pf.addRow("BB period:",         self._bb_period)

        wl_row = QWidget()
        wh = QHBoxLayout(wl_row)
        wh.setContentsMargins(0, 0, 0, 0)
        self._watchlist_edit = QLineEdit()
        self._watchlist_edit.setPlaceholderText("optional — leave blank to scan all symbols")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_watchlist)
        clear_btn  = QPushButton("Clear")
        clear_btn.clicked.connect(self._watchlist_edit.clear)
        wh.addWidget(self._watchlist_edit, 1)
        wh.addWidget(browse_btn)
        wh.addWidget(clear_btn)
        self._watchlist_row = wl_row
        pf.addRow("Watchlist file:", wl_row)

        root.addWidget(params_box)

        # ── Universe ──────────────────────────────────────────────────────────
        uni_row = QHBoxLayout()
        self._universe_label = QLabel("Universe: —")
        self._universe_btn = QPushButton("Update Universe")
        self._universe_btn.setToolTip(
            "Refresh the scan universe from SEC EDGAR, validated against Schwab "
            "pricing. Only needed when listings change.")
        self._universe_btn.clicked.connect(self._update_universe)
        uni_row.addWidget(self._universe_label, 1)
        uni_row.addWidget(self._universe_btn)
        root.addLayout(uni_row)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._scan_btn = QPushButton("Run Scan")
        self._scan_btn.setToolTip(
            "Price screen (Pass 1) then RSI/BB% technical filter (Pass 2) on the "
            "in-range symbols only.")
        self._stop_btn  = QPushButton("Stop")
        for b in (self._scan_btn, self._stop_btn):
            b.setFixedHeight(32)
        self._stop_btn.setEnabled(False)
        self._scan_btn.clicked.connect(self._run_scan)
        self._stop_btn.clicked.connect(self._stop)
        btn_row.addStretch()
        btn_row.addWidget(self._scan_btn)
        btn_row.addWidget(self._stop_btn)
        root.addLayout(btn_row)

        # ── Progress ──────────────────────────────────────────────────────────
        prog_box = QGroupBox("Progress")
        pl = QFormLayout(prog_box)
        self._price_bar   = QProgressBar()
        self._price_plabel = QLabel("—")
        self._tech_bar    = QProgressBar()
        self._tech_plabel = QLabel("—")
        self._price_bar.setRange(0, 100)
        self._tech_bar.setRange(0, 100)
        pl.addRow("Price scan:",     self._price_bar)
        pl.addRow("",                self._price_plabel)
        pl.addRow("Technical scan:", self._tech_bar)
        pl.addRow("",                self._tech_plabel)
        root.addWidget(prog_box)

        # ── Splitter: log + two result tables ───────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        log_box = QGroupBox("Log")
        ll = QVBoxLayout(log_box)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        ll.addWidget(self._log)
        splitter.addWidget(log_box)

        results_split = QSplitter(Qt.Orientation.Horizontal)

        self._price_box = QGroupBox("Price-Screened — 0 symbols")
        self._price_box.setProperty("_base", "Price-Screened")
        prl = QVBoxLayout(self._price_box)
        self._price_table = _new_table(_PRICE_HEADERS, _PRICE_COLS)
        prl.addWidget(self._price_table)
        results_split.addWidget(self._price_box)

        self._cand_box = QGroupBox("Technical Candidates — 0 candidates")
        self._cand_box.setProperty("_base", "Technical Candidates")
        cl = QVBoxLayout(self._cand_box)
        self._cand_table = _new_table(_HEADERS, _COLS)
        cl.addWidget(self._cand_table)
        results_split.addWidget(self._cand_box)

        results_split.setStretchFactor(0, 1)
        results_split.setStretchFactor(1, 2)
        splitter.addWidget(results_split)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

    # ── slots ─────────────────────────────────────────────────────────────────

    def _on_source_changed(self, universe_checked: bool):
        """Grey out what a my-stocks scan doesn't use, so the UI says what applies.

        A my-stocks scan reports every ticker on the list with its indicators, so
        the price range and the RSI/BB% thresholds have nothing to reject — but
        the periods still decide how those indicators are computed.
        """
        self._my_stocks_box.setVisible(not universe_checked)
        for w in (self._price_min, self._price_max, self._rsi_threshold,
                  self._bb_pct_threshold, self._watchlist_row):
            w.setEnabled(universe_checked)

    def _browse_watchlist(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select watchlist", "", "Text files (*.txt);;All files (*)"
        )
        if path:
            self._watchlist_edit.setText(path)

    def _get_config(self) -> dict:
        config = {
            "price_min":        float(self._price_min.text()),
            "price_max":        float(self._price_max.text()),
            "rsi_threshold":    float(self._rsi_threshold.text()),
            "bb_pct_threshold": float(self._bb_pct_threshold.text()),
            "rsi_period":       int(self._rsi_period.text()),
            "bb_period":        int(self._bb_period.text()),
        }
        if self._mine_src_btn.isChecked():
            config["symbols"] = self._get_my_stocks()
        return config

    def _begin_scan(self) -> dict | None:
        try:
            config = self._get_config()
        except ValueError as e:
            self._log.append(f"Invalid parameter: {e}")
            return None
        self._scan_btn.setEnabled(False)
        self._universe_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._log.clear()
        return config

    def _run_scan(self):
        if self._mine_src_btn.isChecked() and not self._get_my_stocks():
            self._log.append("No tickers in My Stocks — add at least one ticker.")
            return
        config = self._begin_scan()
        if config is None:
            return
        self._price_table.setRowCount(0)
        self._price_box.setTitle("Price-Screened — 0 symbols")
        self._price_bar.setValue(0)
        self._price_plabel.setText("—")
        self._cand_table.setRowCount(0)
        self._cand_box.setTitle("Technical Candidates — 0 candidates")
        self._tech_bar.setValue(0)
        self._tech_plabel.setText("—")

        watchlist = self._watchlist_edit.text().strip() or None
        self._worker = StockScanWorker(config, watchlist)
        self._worker.log_msg.connect(self._log.append)
        self._worker.price_progress.connect(self._on_price_progress)
        self._worker.tech_progress.connect(self._on_tech_progress)
        self._worker.price_found.connect(self._on_price_found)
        self._worker.tech_found.connect(self._on_tech_found)
        self._worker.price_done.connect(self._on_price_finished)
        self._worker.finished.connect(self._on_tech_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.stop()
        self._stop_btn.setEnabled(False)

    def _idle(self):
        self._scan_btn.setEnabled(True)
        self._universe_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)

    # ── universe ────────────────────────────────────────────────────────────────
    def _refresh_universe_label(self):
        from core.screener import load_universe, UNIVERSE_FILE
        symbols = load_universe()
        if symbols is None:
            self._universe_label.setText(
                "Universe: none saved — click Update Universe (or set a watchlist)")
            return
        try:
            updated = json.loads(Path(UNIVERSE_FILE).read_text()).get("updated", "?")
        except Exception:
            updated = "?"
        self._universe_label.setText(
            f"Universe: {len(symbols):,} tickers · updated {updated}")

    def _update_universe(self):
        self._scan_btn.setEnabled(False)
        self._universe_btn.setEnabled(False)
        self._stop_btn.setEnabled(False)
        self._log.clear()
        self._log.append("Updating universe from SEC EDGAR …")
        self._worker = UniverseWorker()
        self._worker.log_msg.connect(self._log.append)
        self._worker.progress.connect(self._on_universe_progress)
        self._worker.finished.connect(self._on_universe_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_universe_progress(self, current: int, total: int):
        self._universe_label.setText(f"Validating against Schwab … {current:,}/{total:,}")

    def _on_universe_finished(self, data: dict):
        self._idle()
        self._log.append(
            f"Universe updated — {data.get('count', 0):,} tickers ({data.get('source', '')}).")
        self._refresh_universe_label()

    def _on_price_progress(self, current: int, total: int):
        self._price_plabel.setText(f"{current:,} / {total:,}")
        if total > 0:
            self._price_bar.setValue(int(current * 100 / total))

    def _on_tech_progress(self, current: int, total: int):
        self._tech_plabel.setText(f"{current:,} / {total:,}")
        if total > 0:
            self._tech_bar.setValue(int(current * 100 / total))

    def _on_price_found(self, rows: list):
        _append_rows(self._price_table, rows, _PRICE_COLS)
        self._price_box.setTitle(
            f"Price-Screened — {self._price_table.rowCount()} symbols  |  scanning…")

    def _on_tech_found(self, rows: list):
        _append_rows(self._cand_table, rows, _COLS)
        self._cand_box.setTitle(
            f"Technical Candidates — {self._cand_table.rowCount()} candidates  |  scanning…")

    def _on_price_finished(self, results: list):
        # Pass 1 done; the technical pass continues, so don't go idle here.
        self._price_bar.setValue(100)
        ts = self._read_scan_timestamp(PRICE_CACHE)
        _fill_table(self._price_table, results, _PRICE_COLS)
        self._price_box.setTitle(f"Price-Screened — {len(results)} symbols  |  last scan: {ts}")

    def _on_tech_finished(self, results: list):
        self._idle()
        self._tech_bar.setValue(100)
        self._log.append(f"Technical scan done — {len(results)} candidate(s) found.")
        ts = self._read_scan_timestamp(CANDIDATES_CACHE)
        _fill_table(self._cand_table, results, _COLS)
        self._cand_box.setTitle(f"Technical Candidates — {len(results)} candidates  |  last scan: {ts}")
        self.scan_finished.emit()

    def _read_scan_timestamp(self, path_str: str) -> str:
        try:
            cached = json.loads(Path(path_str).read_text())
            return cached.get("scanned_at") or cached.get("date", "unknown")
        except Exception:
            return "unknown"

    def _on_error(self, msg: str):
        self._idle()
        self._log.append(f"ERROR: {msg}")
