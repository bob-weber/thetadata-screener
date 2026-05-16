import os
from datetime import date

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton,
    QTextEdit, QLabel, QInputDialog, QLineEdit,
)
from PyQt6.QtCore import QThread, pyqtSignal


def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    path = os.path.expanduser("~/.anthropic_key")
    if os.path.exists(path):
        return open(path).read().strip()
    return ""


def _save_api_key(key: str) -> None:
    path = os.path.expanduser("~/.anthropic_key")
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)


class _ClaudeWorker(QThread):
    chunk    = pyqtSignal(str)
    finished = pyqtSignal()
    error    = pyqtSignal(str)

    def __init__(self, symbol: str, row: dict, api_key: str):
        super().__init__()
        self._symbol  = symbol
        self._row     = row
        self._api_key = api_key

    def run(self):
        try:
            import yfinance as yf
            import anthropic

            hist = yf.Ticker(self._symbol).history(period="30d")
            if not hist.empty:
                price_lines = [
                    f"  {dt.strftime('%Y-%m-%d')}: ${row['Close']:.2f}"
                    for dt, row in hist.iterrows()
                ]
                price_history = "\n".join(price_lines[-20:])
            else:
                price_history = "  (not available)"

            r = self._row
            if r.get("earnings_date"):
                in_period = " — WITHIN OPTION PERIOD" if r.get("earnings_in_period") else ""
                earnings_info = f"{r['earnings_date']}{in_period}"
            else:
                earnings_info = "not available"

            prompt = f"""You are helping a retail options trader evaluate {self._symbol} for a wheel strategy (selling cash-secured puts, then covered calls if assigned).

Scan data as of {date.today()}:
- Stock price: ${r.get('stock_price', 'N/A')}
- Put strike under consideration: ${r.get('strike', 'N/A')} ({r.get('otm_pct', 'N/A')}% OTM)
- Wheel suitability grade: {r.get('grade', '?')} (score {r.get('score', '?')}/100)
- Sector: {r.get('sector', 'Unknown')}
- Beta: {r.get('beta', 'N/A')}
- Market cap: ${r.get('mkt_cap_b', 'N/A')}B
- Risk flags: {r.get('flags', '—')}
- Analysis notes: {r.get('notes', '')}
- Earnings date: {earnings_info}

Last 30 days of closing prices:
{price_history}

Give a concise, practical analysis in four sections:

**Company** — what the business does and its competitive position.

**Recent trend** — interpret the price action above. Is the weakness a buying opportunity or a sign of deteriorating fundamentals?

**Put selling risks** — specific risks for this trade: earnings gap, IV environment, sector headwinds, anything that could cause a bad assignment.

**Verdict** — is this a good wheel candidate at this strike? What price level or event should the trader watch before entering?

Be direct and specific. 2–3 sentences per section."""

            client = anthropic.Anthropic(api_key=self._api_key)
            with client.messages.stream(
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                for text in stream.text_stream:
                    self.chunk.emit(text)

            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


class ClaudeAnalysisDialog(QDialog):
    def __init__(self, symbol: str, row: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Claude Analysis — {symbol}")
        self.resize(720, 520)
        self._symbol = symbol
        self._row    = row
        self._worker = None
        self._buf    = ""
        self._setup_ui()
        self._start()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        self._status = QLabel(f"Analyzing {self._symbol}…")
        layout.addWidget(self._status)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        layout.addWidget(self._text, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        btn_row.addWidget(close)
        layout.addLayout(btn_row)

    def _start(self):
        api_key = _get_api_key()
        if not api_key:
            key, ok = QInputDialog.getText(
                self, "Anthropic API Key",
                "Enter your Anthropic API key:",
                QLineEdit.EchoMode.Password,
            )
            if not ok or not key.strip():
                self._status.setText("No API key — analysis cancelled.")
                return
            _save_api_key(key.strip())
            api_key = key.strip()

        self._worker = _ClaudeWorker(self._symbol, self._row, api_key)
        self._worker.chunk.connect(self._on_chunk)
        self._worker.finished.connect(self._on_finished)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _on_chunk(self, text: str):
        self._buf += text
        cursor = self._text.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(text)
        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()

    def _on_finished(self):
        self._status.setText(f"Analysis complete — {self._symbol}")
        self._text.setMarkdown(self._buf)

    def _on_error(self, msg: str):
        self._status.setText("Error")
        self._text.setPlainText(f"Error: {msg}")
