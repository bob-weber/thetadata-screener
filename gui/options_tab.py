import json
from pathlib import Path

from PyQt6.QtCore import Qt, QDate, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLineEdit, QPushButton, QProgressBar, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QSplitter, QDateEdit,
    QRadioButton, QButtonGroup, QApplication, QCheckBox,
)

from PyQt6.QtCore import pyqtSignal as _pyqtSignal

from .workers import OptionsWorker

POSITIONS_FILE   = Path("my_positions.txt")
REJECT_FILE      = Path("reject_list.txt")
CANDIDATES_CACHE = Path("tech_candidates_cache.json")
OPTIONS_CACHE    = Path("options_results_cache.json")

_COLS    = ["symbol", "expiration", "dte", "strike", "otm_pct", "premium", "yield_pct",
            "delta", "iv", "sigma_pct", "cushion_sigma", "iv_pctile"]
_HEADERS = ["Symbol", "Expiration", "DTE", "Strike", "OTM%", "Premium", "Yield%",
            "Delta", "IV%", "σ Move%", "Cushion σ", "IV %ile"]
_FLOAT_COLS = {"strike", "otm_pct", "premium", "yield_pct", "delta",
               "iv", "sigma_pct", "cushion_sigma", "iv_pctile"}
_INT_COLS   = {"dte"}


class _NumericItem(QTableWidgetItem):
    def __lt__(self, other):
        try:
            return float(self.text()) < float(other.text())
        except ValueError:
            return super().__lt__(other)


def _make_item(key: str, val) -> QTableWidgetItem:
    if key in _FLOAT_COLS and val is not None:
        item = _NumericItem(f"{float(val):.2f}")
    elif key in _INT_COLS and val is not None:
        item = _NumericItem(str(int(val)))
    else:
        item = QTableWidgetItem(str(val) if val is not None else "")
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class OptionsScannerTab(QWidget):
    scan_finished = _pyqtSignal()

    def __init__(self):
        super().__init__()
        self._worker      = None
        self._results_box = None
        self._table       = None

        self._save_timer = QTimer()
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._save_positions)

        self._reject_save_timer = QTimer()
        self._reject_save_timer.setSingleShot(True)
        self._reject_save_timer.setInterval(800)
        self._reject_save_timer.timeout.connect(self._save_reject)

        # Flush any pending debounced saves before the app exits, so edits made
        # right before quitting aren't lost to the 800ms timer window.
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._flush_saves)

        self._setup_ui()
        self._load_positions()
        self._load_reject()
        self.refresh_scanner_status()
        self._load_cached_results()

    @staticmethod
    def _next_friday(q: QDate) -> QDate:
        days = (5 - q.dayOfWeek()) % 7
        return q.addDays(days if days > 0 else 7)

    # ── scanner status ────────────────────────────────────────────────────────

    def refresh_scanner_status(self):
        """Update the candidates label from the last stock scan cache, if any."""
        if not self._scanner_btn.isChecked():
            return
        if not CANDIDATES_CACHE.exists():
            self._candidates_label.setText("Run the Stock Scanner first.")
            return
        try:
            cached = json.loads(CANDIDATES_CACHE.read_text())
        except Exception:
            self._candidates_label.setText("Run the Stock Scanner first.")
            return
        candidates = cached.get("candidates", [])
        ts = cached.get("scanned_at") or cached.get("date", "unknown")
        if candidates:
            self._candidates_label.setText(
                f"{len(candidates)} candidates from Stock Scanner  |  last scan: {ts}"
            )
        else:
            self._candidates_label.setText("Run the Stock Scanner first.")

    def _cache_trade_date(self) -> str:
        """EOD trading day the cached chains are from (falls back to run date)."""
        try:
            c = json.loads(OPTIONS_CACHE.read_text())
            return c.get("trade_date") or c.get("date", "unknown")
        except Exception:
            return "unknown"

    def _load_cached_results(self):
        if not OPTIONS_CACHE.exists():
            return
        try:
            cached = json.loads(OPTIONS_CACHE.read_text())
        except Exception:
            return
        results = cached.get("results", [])
        if not results:
            return
        trade_date = cached.get("trade_date") or cached.get("date", "unknown")
        self._populate_table(results)
        self._results_box.setTitle(f"Results — {len(results)} contract(s) (EOD {trade_date})")

    # ── positions file helpers ────────────────────────────────────────────────

    def _load_positions(self):
        if POSITIONS_FILE.exists():
            self._positions_edit.setPlainText(POSITIONS_FILE.read_text())

    def _save_positions(self):
        POSITIONS_FILE.write_text(self._positions_edit.toPlainText())

    def _get_positions(self) -> list[str]:
        raw = self._positions_edit.toPlainText()
        tickers = [t.strip().upper() for t in raw.splitlines()]
        return [t for t in tickers if t]

    # ── reject-list file helpers ──────────────────────────────────────────────

    def _load_reject(self):
        if REJECT_FILE.exists():
            self._reject_edit.setPlainText(REJECT_FILE.read_text())

    def _save_reject(self):
        REJECT_FILE.write_text(self._reject_edit.toPlainText())

    def _flush_saves(self):
        """Write out any edits still waiting on a debounce timer (e.g. on quit)."""
        if self._save_timer.isActive():
            self._save_timer.stop()
            self._save_positions()
        if self._reject_save_timer.isActive():
            self._reject_save_timer.stop()
            self._save_reject()

    def _get_reject(self) -> set[str]:
        raw = self._reject_edit.toPlainText()
        return {t.strip().upper() for t in raw.splitlines() if t.strip()}

    # ── UI construction ───────────────────────────────────────────────────────

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # ── Ticker source ─────────────────────────────────────────────────────
        source_box = QGroupBox("Ticker Source")
        sh = QHBoxLayout(source_box)
        sh.setSpacing(16)

        self._scanner_btn   = QRadioButton("Stock Scanner Results")
        self._positions_btn = QRadioButton("My Positions")
        self._scanner_btn.setChecked(True)
        self._source_group = QButtonGroup()
        self._source_group.addButton(self._scanner_btn,   0)
        self._source_group.addButton(self._positions_btn, 1)
        sh.addWidget(self._scanner_btn)
        sh.addWidget(self._positions_btn)
        sh.addStretch()
        root.addWidget(source_box)

        # ── My Positions editor (hidden until radio selected) ─────────────────
        self._positions_box = QGroupBox("My Positions — one ticker per line")
        pl = QVBoxLayout(self._positions_box)
        self._positions_edit = QTextEdit()
        self._positions_edit.setPlaceholderText("AAPL\nMSFT\nTSLA")
        self._positions_edit.setFixedHeight(110)
        self._positions_edit.textChanged.connect(self._save_timer.start)
        pl.addWidget(self._positions_edit)
        self._positions_box.setVisible(False)
        root.addWidget(self._positions_box)

        self._scanner_btn.toggled.connect(
            lambda checked: self._positions_box.setVisible(not checked)
        )

        # ── Reject list (always applied, both ticker sources) ─────────────────
        reject_box = QGroupBox("Reject List — never scan these (one ticker per line)")
        rj = QVBoxLayout(reject_box)
        self._reject_edit = QTextEdit()
        self._reject_edit.setPlaceholderText("TSLA\nGME\nMSTR")
        self._reject_edit.setFixedHeight(80)
        self._reject_edit.textChanged.connect(self._reject_save_timer.start)
        rj.addWidget(self._reject_edit)
        root.addWidget(reject_box)

        # ── Parameters ────────────────────────────────────────────────────────
        params_box = QGroupBox("Parameters")
        pf = QFormLayout(params_box)

        # Put / Call
        self._put_btn  = QRadioButton("Put")
        self._call_btn = QRadioButton("Call")
        self._put_btn.setChecked(True)
        self._right_group = QButtonGroup()
        self._right_group.addButton(self._put_btn,  0)
        self._right_group.addButton(self._call_btn, 1)
        pc_row = QWidget()
        ph = QHBoxLayout(pc_row)
        ph.setContentsMargins(0, 0, 0, 0)
        ph.addWidget(self._put_btn)
        ph.addWidget(self._call_btn)
        ph.addStretch()
        pf.addRow("Type:", pc_row)

        # Sell / Buy
        self._sell_btn = QRadioButton("Sell")
        self._buy_btn  = QRadioButton("Buy")
        self._sell_btn.setChecked(True)
        self._side_group = QButtonGroup()
        self._side_group.addButton(self._sell_btn, 0)
        self._side_group.addButton(self._buy_btn,  1)
        sb_row = QWidget()
        sbh = QHBoxLayout(sb_row)
        sbh.setContentsMargins(0, 0, 0, 0)
        sbh.addWidget(self._sell_btn)
        sbh.addWidget(self._buy_btn)
        sbh.addStretch()
        pf.addRow("Side:", sb_row)

        self._exp_date = QDateEdit()
        self._exp_date.setCalendarPopup(True)
        self._exp_date.setDate(self._next_friday(QDate.currentDate()))
        self._exp_date.setDisplayFormat("yyyy-MM-dd")
        pf.addRow("Expiration date:", self._exp_date)

        self._weeklies_only = QCheckBox("Only scan symbols with weekly options")
        self._weeklies_only.setChecked(True)
        pf.addRow("", self._weeklies_only)

        yield_row = QWidget()
        yh = QHBoxLayout(yield_row)
        yh.setContentsMargins(0, 0, 0, 0)
        self._yield_min = QLineEdit("0.9")
        self._yield_max = QLineEdit("2.0")
        yh.addWidget(self._yield_min)
        yh.addWidget(QLabel("–"))
        yh.addWidget(self._yield_max)
        yh.addWidget(QLabel("%"))
        pf.addRow("Yield range:", yield_row)

        root.addWidget(params_box)

        # ── Buttons + status ──────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self._candidates_label = QLabel("Run the Stock Scanner first.")
        self._run_btn  = QPushButton("Run Options Scanner")
        self._stop_btn = QPushButton("Stop")
        self._run_btn.setFixedHeight(32)
        self._stop_btn.setFixedHeight(32)
        self._stop_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._run)
        self._stop_btn.clicked.connect(self._stop)
        btn_row.addWidget(self._candidates_label, 1)
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._stop_btn)
        root.addLayout(btn_row)

        # ── Progress ──────────────────────────────────────────────────────────
        prog_box = QGroupBox("Progress")
        prog_form = QFormLayout(prog_box)
        self._opts_bar   = QProgressBar()
        self._opts_label = QLabel("—")
        self._opts_bar.setRange(0, 100)
        prog_form.addRow("Options scan:", self._opts_bar)
        prog_form.addRow("",              self._opts_label)
        root.addWidget(prog_box)

        # ── Splitter: log + results ───────────────────────────────────────────
        splitter = QSplitter(Qt.Orientation.Vertical)

        log_box = QGroupBox("Log")
        ll = QVBoxLayout(log_box)
        self._log = QTextEdit()
        self._log.setReadOnly(True)
        ll.addWidget(self._log)
        splitter.addWidget(log_box)

        self._results_box = QGroupBox("Results — 0 contracts")
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

    def _get_config(self) -> dict:
        return {
            "right":            "P" if self._put_btn.isChecked() else "C",
            "side":             "sell" if self._sell_btn.isChecked() else "buy",
            "expiration_date":  self._exp_date.date().toString("yyyy-MM-dd"),
            "yield_min":        float(self._yield_min.text()) / 100.0,
            "yield_max":        float(self._yield_max.text()) / 100.0,
            "weeklies_only":    self._weeklies_only.isChecked(),
        }

    def _run(self):
        try:
            config = self._get_config()
        except ValueError as e:
            self._log.append(f"Invalid parameter: {e}")
            return

        using_positions = self._positions_btn.isChecked()
        positions = None
        if using_positions:
            positions = self._get_positions()
            if not positions:
                self._log.append("No tickers in My Positions — add at least one ticker.")
                return

        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._log.clear()
        self._table.setRowCount(0)
        self._results_box.setTitle("Results — 0 contracts")
        self._opts_bar.setValue(0)
        self._opts_label.setText("—")

        if using_positions:
            self._candidates_label.setText(f"{len(positions)} position(s) queued")
        else:
            self._candidates_label.setText("Loading stock scan results…")

        self._worker = OptionsWorker(config, positions=positions, reject=self._get_reject())
        self._worker.log_msg.connect(self._log.append)
        self._worker.candidates_loaded.connect(self._on_candidates_loaded)
        self._worker.opts_progress.connect(self._on_opts_progress)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop(self):
        if self._worker:
            self._worker.stop()
        self._stop_btn.setEnabled(False)

    def _on_candidates_loaded(self, count: int):
        if self._positions_btn.isChecked():
            self._candidates_label.setText(f"{count} position(s) with prices fetched")
        else:
            self._candidates_label.setText(f"{count} candidates from Stock Scanner")

    def _on_opts_progress(self, current: int, total: int):
        self._opts_label.setText(f"{current:,} / {total:,}")
        if total > 0:
            self._opts_bar.setValue(int(current * 100 / total))

    def _on_finished(self, results: list):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._opts_bar.setValue(100)
        self._log.append(f"Done — {len(results)} contract(s) found.")
        self._populate_table(results)
        self._results_box.setTitle(
            f"Results — {len(results)} contract(s) (EOD {self._cache_trade_date()})")
        self.scan_finished.emit()

    def _on_error(self, msg: str):
        self._run_btn.setEnabled(True)
        self._stop_btn.setEnabled(False)
        self._log.append(f"ERROR: {msg}")
        if self._scanner_btn.isChecked():
            self._candidates_label.setText("Run the Stock Scanner first.")
        else:
            self._candidates_label.setText("Ready.")

    def _populate_table(self, results: list):
        self._results_box.setTitle(f"Results — {len(results)} contract(s)")
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(results))
        for r, row in enumerate(results):
            for c, key in enumerate(_COLS):
                self._table.setItem(r, c, _make_item(key, row.get(key)))
        self._table.setSortingEnabled(True)
        self._table.resizeColumnsToContents()
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
