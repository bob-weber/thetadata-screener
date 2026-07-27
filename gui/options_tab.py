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

from . import column_help
from .workers import OptionsWorker

REJECT_FILE      = Path("reject_list.txt")
CANDIDATES_CACHE = Path("tech_candidates_cache.json")
OPTIONS_CACHE    = Path("options_results_cache.json")

_COLS    = ["symbol", "expiration", "dte", "strike", "otm_pct", "premium",
            "delta", "iv", "cushion_sigma", "iv_pctile", "rsi", "bb_pct"]
_HEADERS = ["Symbol", "Expiration", "DTE", "Strike", "OTM%", "Premium",
            "Delta", "IV%", "Cushion σ", "IV %ile", "RSI", "BB%"]
_FLOAT_COLS = {"strike", "otm_pct", "premium", "delta",
               "iv", "cushion_sigma", "iv_pctile", "rsi", "bb_pct"}
_INT_COLS   = {"dte"}

# Per-column help, shown when hovering a column header.
_HELP = {
    "symbol":     "Underlying ticker.",
    "expiration": "Contract expiration date. The scan targets the date set in "
                  "Parameters, snapping back to the nearest listed expiration on "
                  "or before it.",
    "dte":        "Days to expiration, counted from today.",
    "strike":     "Strike price.\n\nFor a cash-secured put, strike × 100 is the "
                  "cash locked up per contract — and the price you pay per share "
                  "if you're assigned.",
    "otm_pct":    "How far out of the money the strike sits:\n"
                  "    (stock − strike) ÷ stock\n\n"
                  "Negative means the strike is already in the money.",
    "premium":    "Premium per share, in dollars — the bid when selling, the ask "
                  "when buying. Multiply by 100 for the cash per contract.\n\n"
                  "The Premium % box in Parameters filters on this as a fraction "
                  "of the capital it ties up, not on the dollar figure.",
    "delta":      "Option delta, from Schwab's chain.\n\nFor a short put its "
                  "absolute value approximates the chance of finishing in the "
                  "money: −0.20 ≈ 20%. It's also how much the contract price "
                  "moves per $1 move in the stock.",
    "iv":         "Implied volatility of the near-the-money contract, annualized.\n\n"
                  "Under 25% is a grinder — thin premium to enter, roll and sell "
                  "calls against. Over 80% pays richly but gaps hard.",
    "cushion_sigma":
                  "OTM distance measured in expected moves:\n"
                  "    OTM% ÷ the underlying's expected move to expiration\n\n"
                  "The primary risk gate. Below 1σ the premium isn't paying for "
                  "the risk — 1.5σ for gappy names (earnings in the period, or "
                  "Energy / Basic Materials).",
    "iv_pctile":  "Where today's IV sits within this symbol's own trailing year "
                  "of readings.\n\nHigh means premium is rich versus its own "
                  "history and likely to compress in your favour; low means "
                  "you're selling cheap volatility.",
    "rsi":        "Wilder's RSI of the underlying, carried from the stock scan.\n\n"
                  "Below 30 is oversold, above 70 overbought.",
    "bb_pct":     "Where the underlying sits inside its Bollinger Bands, carried "
                  "from the stock scan: 0% = lower band, 100% = upper band.\n\n"
                  "Below 0 means price has broken under the lower band.",
}


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

    # ── reject-list file helpers ──────────────────────────────────────────────

    def _load_reject(self):
        if REJECT_FILE.exists():
            self._reject_edit.setPlainText(REJECT_FILE.read_text())

    def _save_reject(self):
        REJECT_FILE.write_text(self._reject_edit.toPlainText())

    def _flush_saves(self):
        """Write out any edits still waiting on a debounce timer (e.g. on quit)."""
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

        # ── Reject list (applied to the stock-scan candidates) ────────────────
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

        prem_row = QWidget()
        ph2 = QHBoxLayout(prem_row)
        ph2.setContentsMargins(0, 0, 0, 0)
        self._premium_min = QLineEdit("0.9")
        self._premium_max = QLineEdit("2.0")
        ph2.addWidget(self._premium_min)
        ph2.addWidget(QLabel("–"))
        ph2.addWidget(self._premium_max)
        ph2.addWidget(QLabel("%"))
        pf.addRow("Premium %:", prem_row)

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
        column_help.install(self._table, _COLS, _HELP, self)
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
            "premium_pct_min":  float(self._premium_min.text()) / 100.0,
            "premium_pct_max":  float(self._premium_max.text()) / 100.0,
            "weeklies_only":    self._weeklies_only.isChecked(),
        }

    def _run(self):
        try:
            config = self._get_config()
        except ValueError as e:
            self._log.append(f"Invalid parameter: {e}")
            return

        self._run_btn.setEnabled(False)
        self._stop_btn.setEnabled(True)
        self._log.clear()
        self._table.setRowCount(0)
        self._results_box.setTitle("Results — 0 contracts")
        self._opts_bar.setValue(0)
        self._opts_label.setText("—")

        self._candidates_label.setText("Loading stock scan results…")

        self._worker = OptionsWorker(config, reject=self._get_reject())
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
        self._candidates_label.setText("Run the Stock Scanner first.")

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
