from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGroupBox, QFormLayout,
    QLineEdit, QPushButton, QProgressBar, QTextEdit, QTableWidget,
    QTableWidgetItem, QHeaderView, QLabel, QSplitter, QDateEdit,
    QRadioButton, QButtonGroup,
)
from PyQt6.QtCore import Qt, QDate

from .workers import OptionsWorker

_COLS    = ["symbol", "expiration", "dte", "strike", "otm_pct", "premium", "yield_pct", "delta"]
_HEADERS = ["Symbol", "Expiration", "DTE", "Strike", "OTM%", "Premium", "Yield%", "Delta"]
_FLOAT_COLS = {"strike", "otm_pct", "premium", "yield_pct", "delta"}
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
    def __init__(self):
        super().__init__()
        self._worker      = None
        self._results_box = None
        self._table       = None
        self._setup_ui()

    @staticmethod
    def _next_friday(q: QDate) -> QDate:
        # Qt dayOfWeek: 1=Mon … 5=Fri … 7=Sun
        days = (5 - q.dayOfWeek()) % 7
        return q.addDays(days if days > 0 else 7)

    def _setup_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

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
        sh = QHBoxLayout(sb_row)
        sh.setContentsMargins(0, 0, 0, 0)
        sh.addWidget(self._sell_btn)
        sh.addWidget(self._buy_btn)
        sh.addStretch()
        pf.addRow("Side:", sb_row)

        self._exp_date = QDateEdit()
        self._exp_date.setCalendarPopup(True)
        self._exp_date.setDate(self._next_friday(QDate.currentDate()))
        self._exp_date.setDisplayFormat("yyyy-MM-dd")
        pf.addRow("Expiration date:", self._exp_date)

        yield_row = QWidget()
        yh = QHBoxLayout(yield_row)
        yh.setContentsMargins(0, 0, 0, 0)
        self._yield_min = QLineEdit("0.9")
        self._yield_max = QLineEdit("1.1")
        yh.addWidget(self._yield_min)
        yh.addWidget(QLabel("–"))
        yh.addWidget(self._yield_max)
        yh.addWidget(QLabel("%"))
        pf.addRow("Yield range:", yield_row)

        self._opts_throttle = QLineEdit("0.5")
        pf.addRow("Throttle (s):", self._opts_throttle)

        root.addWidget(params_box)

        # ── Buttons + candidates status ───────────────────────────────────────
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
        pl = QFormLayout(prog_box)
        self._opts_bar   = QProgressBar()
        self._opts_label = QLabel("—")
        self._opts_bar.setRange(0, 100)
        pl.addRow("Options scan:", self._opts_bar)
        pl.addRow("",              self._opts_label)
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
            "options_throttle": float(self._opts_throttle.text()),
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

        self._worker = OptionsWorker(config)
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
        self._log.append(f"Loaded {count} candidates from stock scan cache.")

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
