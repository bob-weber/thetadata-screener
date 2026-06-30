"""Standalone price / Bollinger Bands / RSI chart for a single symbol.

Opened from the LSO Analysis tab by clicking a ticker. Fetches history from
Schwab off the UI thread and draws an embedded matplotlib chart: price with
Bollinger Bands on top, RSI(14) below. A timeframe selector switches between
intraday (1D/1W) and daily (1M–1Y) candles.
"""
import matplotlib
matplotlib.use("QtAgg")  # noqa: E402  — pick the PyQt6 (QtAgg) backend before pyplot
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from matplotlib.dates import AutoDateLocator, ConciseDateFormatter

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox,
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt

from core import screener

_BB_PERIOD  = 20
_BB_STD     = 2.0
_RSI_PERIOD = 14

# label → (lookback days, intraday interval in minutes or None for daily)
_TIMEFRAMES = [
    ("1D", 1,   5),
    ("1W", 7,   30),
    ("1M", 31,  None),
    ("3M", 93,  None),
    ("6M", 186, None),
    ("1Y", 372, None),
]
_DEFAULT_TF = "6M"


class _HistoryWorker(QThread):
    """Fetch history + compute indicators off the UI thread."""
    loaded = pyqtSignal(object)
    error  = pyqtSignal(object)

    def __init__(self, symbol: str, days: int, minutes, req_id: int):
        super().__init__()
        self._symbol  = symbol
        self._days    = days
        self._minutes = minutes
        self._req_id  = req_id

    def run(self):
        try:
            df    = screener.fetch_history_df(self._symbol, days=self._days,
                                              intraday_minutes=self._minutes)
            close = df["close"]
            sma, upper, lower = screener.calc_bb_bands(close, _BB_PERIOD, _BB_STD)
            rsi   = screener.calc_rsi_series(close, _RSI_PERIOD)
            self.loaded.emit({
                "req_id": self._req_id, "df": df,
                "sma": sma, "upper": upper, "lower": lower, "rsi": rsi,
            })
        except Exception as e:  # ScreenerError or anything unexpected
            self.error.emit({"req_id": self._req_id, "msg": str(e)})


class ChartWindow(QDialog):
    """Non-modal chart window for one symbol, with a timeframe selector."""

    def __init__(self, symbol: str, parent=None):
        super().__init__(parent)
        self._symbol  = symbol.upper()
        self._req_id  = 0           # bumped per fetch; stale results are ignored
        self._workers = []          # keep refs so running threads aren't GC'd
        self.setWindowTitle(f"{self._symbol} — Price / BB / RSI")
        self.setModal(False)
        self.resize(900, 660)

        layout = QVBoxLayout(self)

        # ── Controls ──────────────────────────────────────────────────────
        ctrl = QHBoxLayout()
        ctrl.addWidget(QLabel("Timeframe:"))
        self._tf_combo = QComboBox()
        for label, _, _ in _TIMEFRAMES:
            self._tf_combo.addItem(label)
        self._tf_combo.setCurrentText(_DEFAULT_TF)
        self._tf_combo.currentTextChanged.connect(lambda _=None: self._load())
        ctrl.addWidget(self._tf_combo)
        ctrl.addStretch(1)
        self._status = QLabel("")
        ctrl.addWidget(self._status)
        layout.addLayout(ctrl)

        # ── Chart ─────────────────────────────────────────────────────────
        self._figure  = Figure(figsize=(9, 6.2), layout="constrained")
        self._canvas  = FigureCanvas(self._figure)
        self._toolbar = NavigationToolbar(self._canvas, self)
        layout.addWidget(self._toolbar)
        layout.addWidget(self._canvas, 1)

        self._load()

    # ── data loading ────────────────────────────────────────────────────────

    def _selected_timeframe(self):
        label = self._tf_combo.currentText()
        for lbl, days, minutes in _TIMEFRAMES:
            if lbl == label:
                return lbl, days, minutes
        return _TIMEFRAMES[0]

    def _load(self):
        label, days, minutes = self._selected_timeframe()
        self._req_id += 1
        self._status.setText(f"Loading {self._symbol} {label} …")
        self._tf_combo.setEnabled(False)

        worker = _HistoryWorker(self._symbol, days, minutes, self._req_id)
        worker.loaded.connect(self._on_loaded)
        worker.error.connect(self._on_error)
        worker.finished.connect(lambda w=worker: self._workers.remove(w)
                                if w in self._workers else None)
        self._workers.append(worker)
        worker.start()

    def _on_error(self, payload: dict):
        if payload["req_id"] != self._req_id:
            return                                  # superseded by a newer request
        self._tf_combo.setEnabled(True)
        self._status.setText(f"Could not load {self._symbol}: {payload['msg']}")

    def _on_loaded(self, data: dict):
        if data["req_id"] != self._req_id:
            return                                  # superseded by a newer request
        self._tf_combo.setEnabled(True)
        self._status.setText("")
        self._draw(data)

    # ── drawing ─────────────────────────────────────────────────────────────

    def _draw(self, data: dict):
        df    = data["df"]
        close = df["close"]
        idx   = df.index

        self._figure.clear()
        ax_price = self._figure.add_subplot(2, 1, 1)
        ax_rsi   = self._figure.add_subplot(2, 1, 2, sharex=ax_price)

        # ── Price + Bollinger Bands ───────────────────────────────────────
        ax_price.plot(idx, close, color="#1f77b4", linewidth=1.3, label="Close")
        ax_price.plot(idx, data["sma"], color="#888888", linewidth=1.0,
                      linestyle="--", label=f"SMA{_BB_PERIOD}")
        ax_price.plot(idx, data["upper"], color="#c0392b", linewidth=0.8, alpha=0.7)
        ax_price.plot(idx, data["lower"], color="#c0392b", linewidth=0.8, alpha=0.7)
        ax_price.fill_between(idx, data["lower"], data["upper"],
                              color="#c0392b", alpha=0.06,
                              label=f"BB({_BB_PERIOD}, {_BB_STD:g}σ)")
        last = float(close.iloc[-1])
        ax_price.set_title(f"{self._symbol}   last ${last:,.2f}   "
                           f"({self._tf_combo.currentText()})")
        ax_price.set_ylabel("Price ($)")
        ax_price.grid(True, alpha=0.25)
        ax_price.legend(loc="upper left", fontsize=8, framealpha=0.6)

        # ── RSI ───────────────────────────────────────────────────────────
        ax_rsi.plot(idx, data["rsi"], color="#6a1b9a", linewidth=1.1)
        ax_rsi.axhline(70, color="#c0392b", linewidth=0.8, linestyle="--", alpha=0.7)
        ax_rsi.axhline(30, color="#1a7a1a", linewidth=0.8, linestyle="--", alpha=0.7)
        ax_rsi.axhline(50, color="#aaaaaa", linewidth=0.6, alpha=0.5)
        ax_rsi.set_ylim(0, 100)
        ax_rsi.set_yticks([0, 30, 50, 70, 100])
        ax_rsi.set_ylabel(f"RSI({_RSI_PERIOD})")
        ax_rsi.grid(True, alpha=0.25)

        # Concise date/time labels that adapt to intraday vs daily ranges.
        locator = AutoDateLocator()
        ax_rsi.xaxis.set_major_locator(locator)
        ax_rsi.xaxis.set_major_formatter(ConciseDateFormatter(locator))

        self._canvas.draw_idle()
