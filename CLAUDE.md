# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

The virtualenv is `gui-env` (PyQt6 + all deps).

```bash
# GUI application (starts the ThetaData terminal automatically)
gui-env/bin/python run_gui.py

# Start / stop the ThetaData terminal manually
./start_terminal.sh
./stop_terminal.sh
```

There are no test suites or linting configs in this repo.

## Architecture

### Data flow (pipeline)

```
Stock Scanner  →  tech_candidates_cache.json
                         ↓
Options Scanner  →  options_results_cache.json
                         ↓
Wheel Analysis  (reads both caches, fetches yfinance metadata)
```

The GUI tabs pass data forward via PyQt6 signals (`scan_finished`, `scan_finished` → `refresh_options_status`, etc.) and read/write the JSON caches directly. Cache files match `*_cache.json` and are gitignored; they are regenerated on each run.

### Core library (`core/`)

- **`screener.py`** — all screener logic; the GUI calls this exclusively.
  - `run_stock_filter()` — two-pass: price screen (Pass 1) then 45-day history + RSI/BB% filter (Pass 2). Writes `price_screen_cache.json`, `tech_history_cache.json`, and `tech_candidates_cache.json`.
  - `run_options_filter()` — fetches EOD option chains via the v3 REST API (`http://127.0.0.1:25503`) and applies yield range filter.
  - `run_screener()` — convenience wrapper that chains the two above.
  - `fetch_stock_prices()` — used by the Positions tab to get current prices for held tickers.
  - Symbol universe: SEC EDGAR list (NYSE/Nasdaq common stocks only, ETFs/funds filtered by name) cross-referenced with ThetaData's symbol list. Falls back to `watchlist.txt` if that file is present.

- **`wheel_analyzer.py`** — scores and grades symbols for wheel-strategy suitability using yfinance metadata.
  - `analyze_symbol()` — fetches sector, beta, market cap, dividend, earnings date from `yf.Ticker.info` and `ticker.calendar`. Starts at base score 70; applies adjustments from `_SECTOR_SCORES`, `_INDUSTRY_EXTRA`, `_score_beta()`, `_score_market_cap()`.
  - `apply_contract_adjustments()` — re-scores per-contract based on OTM% (handles ITM, near-ATM, comfortable, conservative bands).
  - Grading: A ≥ 85, B ≥ 70, C ≥ 55, D ≥ 40, F < 40.

### GUI (`gui/`)

Built with PyQt6. `run_gui.py` invokes `start_terminal.sh` before creating the `QApplication`, and hooks `stop_terminal.sh` to `aboutToQuit`.

- **`main_window.py`** — `QMainWindow` with a `QTabWidget` holding four tabs. Wires inter-tab signals.
- **`workers.py`** — `QThread` subclasses that run blocking I/O off the main thread:
  - `StockWorker` → `core.screener.run_stock_filter`
  - `OptionsWorker` → `core.screener.run_options_filter` (sources candidates from stock scan cache or live price fetch for positions)
  - `WheelWorker` → `core.wheel_analyzer.analyze_symbols` + `apply_contract_adjustments`, merges with options results
- **`stock_tab.py`** — Stock Scanner UI; emits `scan_finished` when done.
- **`options_tab.py`** — Options Scanner UI; can scan from stock-scan candidates or from the Positions tab tickers.
- **`wheel_tab.py`** — Wheel Analysis UI; reads `options_results_cache.json` and calls `WheelWorker`.
- **`positions_tab.py`** — Position tracker; persists to `my_option_positions.json`. Imports TOS Account Statement CSVs (parses the `Options` section and matches fees from `Cash Balance TRD` rows). Computes 5%/10% below strike automatically.

### ThetaData terminal

The Java terminal (`ThetaTerminalv3.jar`) must be running before any data fetch. It listens on port `25503` (v3 REST) and authenticates using credentials in `creds.txt` (gitignored). `config.toml` controls terminal settings (host, ports, FPSS streaming).
