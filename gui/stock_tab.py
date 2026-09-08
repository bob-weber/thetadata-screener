import json
import re
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLineEdit, QPushButton, QProgressBar, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QFileDialog, QSplitter,
    QRadioButton, QButtonGroup, QApplication,
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor

from . import column_help
from .collapsible import CollapsibleGroupBox
from .workers import StockScanWorker, UniverseWorker

PRICE_CACHE      = "price_screen_cache.json"
CANDIDATES_CACHE = "tech_candidates_cache.json"
MY_STOCKS_FILE   = Path("my_positions.txt")

# Ticker Source modes
SRC_UNIVERSE = 0
SRC_IMPORT   = 1
SRC_SPECIFY  = 2


def _parse_ticker_list(raw: str) -> list[str]:
    """Tickers from free text — commas, spaces and newlines all separate.

    One parser for both the typed list and the imported file, so a file with
    comma-separated tickers and a typed list with newlines both work. Order is
    kept and duplicates dropped.
    """
    out, seen = [], set()
    for tok in re.split(r"[,\s]+", raw.strip()):
        t = tok.strip().upper()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out

_PRICE_COLS    = ["symbol", "price", "rsi", "bb_pct"]
_PRICE_HEADERS = ["Symbol", "Price", "RSI", "BB%"]

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
              "Below 30 is oversold, above 70 overbought. A Universe scan keeps "
              "only symbols under the RSI threshold; an Import List or Specify "
              "Stocks scan reports it for every ticker without filtering.",
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


_FAIL_COLOR = QColor("#8a8a8a")   # greyed: measured, but outside the thresholds


def _style_row(table: QTableWidget, r: int, row: dict, cols: list[str]):
    """Grey a row that was measured but missed the RSI/BB% thresholds.

    Every scan produces these now: the technical pass measures the whole
    price-screened list and rejects nothing, so the left-hand table shows why a
    symbol missed the cut instead of it simply vanishing.
    """
    if row.get("passes", True):
        return
    why = ("Outside the RSI / BB% thresholds — excluded from the Options "
           "Scanner. Shown for reference.")
    if row.get("rsi") is None or row.get("bb_pct") is None:
        why = ("Not enough history to compute RSI / BB% — excluded from the "
               "Options Scanner.")
    for c in range(len(cols)):
        item = table.item(r, c)
        if item is not None:
            item.setForeground(_FAIL_COLOR)
            item.setToolTip(why)


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
        _style_row(table, r, row, cols)
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
        _style_row(table, r, row, cols)
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
        """Load the typed list from my_positions.txt into the one-line editor.

        The file stays one ticker per line — it predates this editor and is
        easier to edit by hand that way — so it is joined for display and split
        again on save.
        """
        if MY_STOCKS_FILE.exists():
            tickers = _parse_ticker_list(MY_STOCKS_FILE.read_text())
            self._specify_edit.setText(", ".join(tickers))

    def _save_my_stocks(self):
        MY_STOCKS_FILE.write_text(
            "\n".join(_parse_ticker_list(self._specify_edit.text())) + "\n")

    def _flush_saves(self):
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_my_stocks()

    # ── startup cache loading ──────────────────────────────────────────────────

    def _load_cached_results(self):
        # The candidates cache holds the same symbols as the price cache but with
        # RSI/BB% attached, so it is the better source for the left table. The
        # price cache is the fallback for a price scan with no technical pass yet.
        if not self._load_cache(CANDIDATES_CACHE, "candidates", self._price_table,
                                self._price_box, _PRICE_COLS, "symbols"):
            self._load_cache(PRICE_CACHE, "qualified", self._price_table,
                             self._price_box, _PRICE_COLS, "symbols")
        self._load_cache(CANDIDATES_CACHE, "candidates", self._cand_table,
                         self._cand_box, _COLS, "candidates",
                         only_passing=True)

    def _load_cache(self, path_str, key, table, box, cols, noun,
                    only_passing: bool = False) -> bool:
        """Fill ``table`` from a cache file. Returns whether anything was loaded."""
        path = Path(path_str)
        if not path.exists():
            return False
        try:
            cached = json.loads(path.read_text())
        except Exception:
            return False
        rows = cached.get(key, [])
        if not rows:
            return False
        ts    = cached.get("scanned_at") or cached.get("date", "unknown")
        shown = [r for r in rows if r.get("passes")] if only_passing else rows
        _fill_table(table, shown, cols)
        title = f"{box.property('_base')} — {len(shown)} {noun}"
        if not only_passing and any("passes" in r for r in rows):
            title += f", {sum(1 for r in rows if r.get('passes'))} pass"
        box.setTitle(f"{title}  |  last scan: {ts}")
        return True

    # ── UI construction ─────────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── Ticker source ─────────────────────────────────────────────────────
        source_box = self._source_box = CollapsibleGroupBox("Ticker Source")
        sh = QHBoxLayout(source_box.content())
        sh.setSpacing(16)
        self._universe_src_btn = QRadioButton("Universe")
        self._import_src_btn   = QRadioButton("Import List")
        self._specify_src_btn  = QRadioButton("Specify Stocks")
        self._universe_src_btn.setToolTip("Scan the whole universe, filtered by "
                                          "price range and RSI / BB%.")
        self._import_src_btn.setToolTip("Scan exactly the tickers in a text file.")
        self._specify_src_btn.setToolTip("Scan exactly the tickers you type below.")
        self._universe_src_btn.setChecked(True)
        self._source_group = QButtonGroup()
        self._source_group.addButton(self._universe_src_btn, SRC_UNIVERSE)
        self._source_group.addButton(self._import_src_btn,   SRC_IMPORT)
        self._source_group.addButton(self._specify_src_btn,  SRC_SPECIFY)
        sh.addWidget(self._universe_src_btn)
        sh.addWidget(self._import_src_btn)
        sh.addWidget(self._specify_src_btn)
        sh.addStretch()
        root.addWidget(source_box)

        # ── Import List (hidden until its radio is selected) ──────────────────
        self._import_box = QGroupBox("Import List — text file of tickers")
        il = QHBoxLayout(self._import_box)
        self._import_edit = QLineEdit()
        self._import_edit.setPlaceholderText("path to a .txt file of tickers")
        self._import_edit.textChanged.connect(self._refresh_import_count)
        import_browse = QPushButton("Browse…")
        import_browse.clicked.connect(self._browse_import_list)
        self._import_count = QLabel("—")
        self._import_count.setStyleSheet("color: grey;")
        il.addWidget(self._import_edit, 1)
        il.addWidget(import_browse)
        il.addWidget(self._import_count)
        self._import_box.setVisible(False)
        root.addWidget(self._import_box)

        # ── Specify Stocks (hidden until its radio is selected) ───────────────
        # One line, not a text area: a list of tickers is short, and a multi-line
        # box costs vertical space the tables want.
        self._specify_box = QGroupBox("Specify Stocks — separate with commas or spaces")
        sl = QHBoxLayout(self._specify_box)
        self._specify_edit = QLineEdit()
        self._specify_edit.setPlaceholderText("AAPL, MSFT, TSLA")
        self._specify_edit.textChanged.connect(self._save_timer.start)
        self._specify_edit.textChanged.connect(self._refresh_specify_count)
        self._specify_count = QLabel("—")
        self._specify_count.setStyleSheet("color: grey;")
        sl.addWidget(self._specify_edit, 1)
        sl.addWidget(self._specify_count)
        self._specify_box.setVisible(False)
        root.addWidget(self._specify_box)

        self._source_group.idToggled.connect(lambda *_: self._on_source_changed())

        # ── Parameters ────────────────────────────────────────────────────────
        params_box = self._params_box = CollapsibleGroupBox("Parameters")
        pf = QFormLayout(params_box.content())
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

        # A wider net than the old 40 / 33: these now pick which symbols get an
        # option chain fetched at all, and the Options Scanner's Strike BB%
        # makes the real call on where the strike sits.
        self._rsi_threshold    = QLineEdit("45.0")
        self._bb_pct_threshold = QLineEdit("60.0")
        self._rsi_period       = QLineEdit("14")
        self._bb_period        = QLineEdit("20")
        pf.addRow("RSI threshold (<):", self._rsi_threshold)
        pf.addRow("BB% threshold (<):", self._bb_pct_threshold)
        pf.addRow("RSI period:",        self._rsi_period)
        pf.addRow("BB period:",         self._bb_period)

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
        prog_box = self._prog_box = CollapsibleGroupBox("Progress")
        pl = QFormLayout(prog_box.content())
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
        self._splitter = splitter

        self._log_box = CollapsibleGroupBox("Log")
        ll = QVBoxLayout(self._log_box.content())
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        ll.addWidget(self._log)
        splitter.addWidget(self._log_box)
        # A splitter keeps a pane's size even when its contents are hidden, so
        # collapsing the log has to hand the space over explicitly.
        self._log_box.expanded_changed.connect(self._resize_for_log)

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

        self._on_source_changed()

    # ── slots ─────────────────────────────────────────────────────────────────

    def _resize_for_log(self, expanded: bool):
        """Give the result tables the log's space when the log is collapsed."""
        sizes = self._splitter.sizes()
        total = sum(sizes) or self._splitter.height()
        if not total:
            return
        if expanded:
            self._splitter.setSizes([total // 3, total - total // 3])
        else:
            header = self._log_box.sizeHint().height()
            self._splitter.setSizes([header, max(total - header, 0)])

    def _source_mode(self) -> int:
        return self._source_group.checkedId()

    def _on_source_changed(self):
        """Show the input the chosen source needs, grey out what it doesn't use.

        An explicit list (imported or typed) reports every ticker on it with its
        indicators rather than rejecting any, so the price range has nothing to
        do — but the RSI/BB% thresholds still mark which rows qualify.
        """
        mode = self._source_mode()
        self._import_box.setVisible(mode == SRC_IMPORT)
        self._specify_box.setVisible(mode == SRC_SPECIFY)
        # The price range has nothing to reject on a list you chose deliberately.
        # The RSI/BB% thresholds stay live: they no longer reject either, but
        # they decide which rows are greyed out and which reach the options scan.
        for w in (self._price_min, self._price_max):
            w.setEnabled(mode == SRC_UNIVERSE)

    def _browse_import_list(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ticker list", "", "Text files (*.txt);;All files (*)"
        )
        if path:
            self._import_edit.setText(path)

    def _read_import_list(self) -> list[str]:
        """Tickers from the imported file, re-read at scan time.

        Read on use rather than cached at browse time, so editing the file
        doesn't need the path picking again.
        """
        path = self._import_edit.text().strip()
        if not path:
            return []
        try:
            return _parse_ticker_list(Path(path).read_text())
        except OSError:
            return []

    def _refresh_import_count(self):
        path = self._import_edit.text().strip()
        if not path:
            self._import_count.setText("—")
            return
        if not Path(path).exists():
            self._import_count.setText("file not found")
            return
        self._import_count.setText(f"{len(self._read_import_list())} tickers")

    def _refresh_specify_count(self):
        n = len(_parse_ticker_list(self._specify_edit.text()))
        self._specify_count.setText(f"{n} ticker{'' if n == 1 else 's'}")

    def _get_symbols(self) -> list[str]:
        """The explicit ticker list for the current source ([] for Universe)."""
        if self._source_mode() == SRC_IMPORT:
            return self._read_import_list()
        if self._source_mode() == SRC_SPECIFY:
            return _parse_ticker_list(self._specify_edit.text())
        return []

    def _get_config(self) -> dict:
        config = {
            "price_min":        float(self._price_min.text()),
            "price_max":        float(self._price_max.text()),
            "rsi_threshold":    float(self._rsi_threshold.text()),
            "bb_pct_threshold": float(self._bb_pct_threshold.text()),
            "rsi_period":       int(self._rsi_period.text()),
            "bb_period":        int(self._bb_period.text()),
        }
        if self._source_mode() != SRC_UNIVERSE:
            config["symbols"] = self._get_symbols()
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
        mode = self._source_mode()
        if mode != SRC_UNIVERSE and not self._get_symbols():
            if mode == SRC_IMPORT:
                path = self._import_edit.text().strip()
                self._log.append(
                    f"No tickers read from {path!r} — pick a text file of tickers."
                    if path else "No file chosen — pick a text file of tickers.")
            else:
                self._log.append("No tickers specified — type at least one ticker.")
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

        self._worker = StockScanWorker(config)
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
                "Universe: none saved — click Update Universe "
                "(or scan an Import List / Specify Stocks list instead)")
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
        # Pass 2 streams every measured symbol; only the passes are candidates.
        # The left-hand table is refilled wholesale when the pass finishes rather
        # than updated per row — it is already showing these symbols from pass 1,
        # and it is user-sortable, so a row index captured here would not survive
        # the user clicking a header mid-scan.
        _append_rows(self._cand_table, [r for r in rows if r.get("passes")], _COLS)
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
        passed = [r for r in results if r.get("passes")]
        self._log.append(
            f"Technical scan done — {len(passed)} of {len(results)} symbol(s) "
            f"met the thresholds.")
        ts = self._read_scan_timestamp(CANDIDATES_CACHE)
        # Left: everything measured, failures greyed, so a miss is explainable.
        # Right: the candidates the Options Scanner will actually see.
        _fill_table(self._price_table, results, _PRICE_COLS)
        self._price_box.setTitle(
            f"Price-Screened — {len(results)} symbols, {len(passed)} pass"
            f"  |  last scan: {ts}")
        _fill_table(self._cand_table, passed, _COLS)
        self._cand_box.setTitle(
            f"Technical Candidates — {len(passed)} candidates  |  last scan: {ts}")
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
