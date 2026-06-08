import json
from pathlib import Path

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLineEdit, QPushButton, QProgressBar, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QFileDialog, QSplitter,
)
from PyQt6.QtCore import Qt, pyqtSignal

from .workers import StockWorker

CANDIDATES_CACHE = "tech_candidates_cache.json"

_COLS    = ["symbol", "price", "rsi", "bb_pct"]
_HEADERS = ["Symbol", "Price", "RSI", "BB%"]
_NUMERIC = {"price", "rsi", "bb_pct"}


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


class StockScannerTab(QWidget):
    scan_finished = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._worker       = None
        self._results_box  = None
        self._table        = None
        self._setup_ui()
        self._load_cached_results()

    def _load_cached_results(self):
        path = Path(CANDIDATES_CACHE)
        if not path.exists():
            return
        try:
            cached = json.loads(path.read_text())
        except Exception:
            return
        candidates = cached.get("candidates", [])
        if not candidates:
            return
        ts = cached.get("scanned_at") or cached.get("date", "unknown")
        self._populate_table(candidates)
        self._results_box.setTitle(f"Results — {len(candidates)} candidates  |  last scan: {ts}")

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── Parameters ────────────────────────────────────────────────────────
        params_box = QGroupBox("Parameters")
        pf = QFormLayout(params_box)
        pf.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        price_row = QWidget()
        ph = QHBoxLayout(price_row)
        ph.setContentsMargins(0, 0, 0, 0)
        self._price_min = QLineEdit("10.0")
        self._price_max = QLineEdit("200.0")
        ph.addWidget(self._price_min)
        ph.addWidget(QLabel("–"))
        ph.addWidget(self._price_max)
        pf.addRow("Price range ($):", price_row)

        self._rsi_threshold    = QLineEdit("40.0")
        self._bb_pct_threshold = QLineEdit("33.0")
        self._rsi_period       = QLineEdit("14")
        self._bb_period        = QLineEdit("20")
        self._throttle         = QLineEdit("0.1")
        pf.addRow("RSI threshold (<):", self._rsi_threshold)
        pf.addRow("BB% threshold (<):", self._bb_pct_threshold)
        pf.addRow("RSI period:",        self._rsi_period)
        pf.addRow("BB period:",         self._bb_period)
        pf.addRow("Throttle (s):",      self._throttle)

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
        pf.addRow("Watchlist file:", wl_row)

        root.addWidget(params_box)

        # ── Buttons ───────────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._run_btn  = QPushButton("Run Stock Scanner")
        self._stop_btn = QPushButton("Stop")
        self._run_btn.setFixedHeight(32)
        self._stop_btn.setFixedHeight(32)
        self._stop_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._run)
        self._stop_btn.clicked.connect(self._stop)
        btn_row.addStretch()
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._stop_btn)
        root.addLayout(btn_row)

        # ── Progress ──────────────────────────────────────────────────────────
        prog_box = QGroupBox("Progress")
        pl = QFormLayout(prog_box)
        self._pass1_bar   = QProgressBar()
        self._pass1_label = QLabel("—")
        self._pass2_bar   = QProgressBar()
        self._pass2_label = QLabel("—")
        self._pass1_bar.setRange(0, 100)
        self._pass2_bar.setRange(0, 100)
        pl.addRow("Pass 1 — price screen:", self._pass1_bar)
        pl.addRow("",                        self._pass1_label)
        pl.addRow("Pass 2 — history fetch:", self._pass2_bar)
        pl.addRow("",                        self._pass2_label)
        root.addWidget(prog_box)

        # ── Splitter: log + results ───────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        log_box = QGroupBox("Log")
        ll = QVBoxLayout(log_box)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setFont(self._log.font())
        ll.addWidget(self._log)
        splitter.addWidget(log_box)

        self._results_box = QGroupBox("Results — 0 candidates")
        rl = QVBoxLayout(self._results_box)
        self._table = QTableWidget(0, len(_HEADERS))
        self._table.setHorizontalHeaderLabels(_HEADERS)
        self._table.setSortingEnabled(True)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self._table.verticalHeader().setVisible(False)
        rl.addWidget(self._table)
        splitter.addWidget(self._results_box)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        root.addWidget(splitter, 1)

    # ── slots ─────────────────────────────────────────────────────────────────

    def _browse_watchlist(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select watchlist", "", "Text files (*.txt);;All files (*)"
        )
        if path:
            self._watchlist_edit.setText(path)

    def _get_config(self) -> dict:
        return {
            "price_min":        float(self._price_min.text()),
            "price_max":        float(self._price_max.text()),
            "rsi_threshold":    float(self._rsi_threshold.text()),
            "bb_pct_threshold": float(self._bb_pct_threshold.text()),
            "rsi_period":       int(self._rsi_period.text()),
            "bb_period":        int(self._bb_period.text()),
            "stock_throttle":   float(self._throttle.text()),
        }

    def _run(self):
        try:
            config = self._get_config()
        except ValueError as e:
            self._log.append(f"Invalid parameter: {e}")
            return

        watchlist = self._watchlist_edit.text().strip() or None
        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._log.clear()
        self._table.setRowCount(0)
        self._results_box.setTitle("Results — 0 candidates")
        self._pass1_bar.setValue(0)
        self._pass2_bar.setValue(0)
        self._pass1_label.setText("—")
        self._pass2_label.setText("—")

        self._worker = StockWorker(config, watchlist)
        self._worker.log_msg.connect(self._log.append)
        self._worker.pass1_progress.connect(self._on_pass1_progress)
        self._worker.pass2_progress.connect(self._on_pass2_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.stop()
        self._stop_btn.setEnabled(False)

    def _on_pass1_progress(self, current: int, total: int):
        self._pass1_label.setText(f"{current:,} / {total:,}")
        if total > 0:
            self._pass1_bar.setValue(int(current * 100 / total))

    def _on_pass2_progress(self, current: int, total: int):
        self._pass2_label.setText(f"{current:,} / {total:,}")
        if total > 0:
            self._pass2_bar.setValue(int(current * 100 / total))

    def _on_finished(self, results: list):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._pass1_bar.setValue(100)
        self._pass2_bar.setValue(100)
        self._log.append(f"Done — {len(results)} candidate(s) found.")
        ts = self._read_scan_timestamp()
        self._populate_table(results, title=f"Results — {len(results)} candidate(s)  |  last scan: {ts}")
        self.scan_finished.emit()

    def _read_scan_timestamp(self) -> str:
        try:
            cached = json.loads(Path(CANDIDATES_CACHE).read_text())
            return cached.get("scanned_at") or cached.get("date", "unknown")
        except Exception:
            return "unknown"

    def _on_error(self, msg: str):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._log.append(f"ERROR: {msg}")

    def _populate_table(self, results: list, title: str | None = None):
        self._results_box.setTitle(title or f"Results — {len(results)} candidate(s)")
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(results))
        for r, row in enumerate(results):
            for c, key in enumerate(_COLS):
                self._table.setItem(r, c, _make_item(row.get(key, "")))
        self._table.setSortingEnabled(True)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
