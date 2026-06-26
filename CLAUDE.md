# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the apps

The virtualenv is `gui-env` (PyQt6 + all deps).

```bash
# Screener app (market data via Schwab — needs a cached token, see below)
gui-env/bin/python run_screener.py

# Portfolio tracker
gui-env/bin/python run_portfolio.py

# One-time Schwab login (creates schwab_token.json; refresh ~weekly)
gui-env/bin/python -m core.schwab_client login
```

There are no test suites or linting configs in this repo.

## Architecture

### Screener data flow (pipeline)

```
Stock Scanner  →  tech_candidates_cache.json
                         ↓
Options Scanner  →  options_results_cache.json
                         ↓
LSO Analysis  (reads both caches, fetches yfinance metadata)
```

The GUI tabs pass data forward via PyQt6 signals (`scan_finished`, `scan_finished` → `refresh_options_status`, etc.) and read/write the JSON caches directly. Cache files match `*_cache.json` and are gitignored; they are regenerated on each run.

### Core library (`core/`)

- **`screener.py`** — all screener logic; the screener GUI calls this exclusively. All market data comes from Schwab via `schwab_client`.
  - `run_price_screen()` — Pass 1: batched Schwab `quotes()` (~250/call) filtered to the price range. Writes `price_screen_cache.json`.
  - `run_technical_filter()` — Pass 2: 45-day history via Schwab `price_history_daily()`, then RSI/BB% filter. Writes `tech_history_cache.json` and `tech_candidates_cache.json`.
  - `run_options_filter()` — real-time option chains via Schwab (`_fetch_schwab_chain`, one call per symbol) with a yield range filter.
  - `run_screener()` — convenience wrapper that chains the passes above.
  - Symbol universe: persisted to `universe.json`, built from the SEC EDGAR list (NYSE/Nasdaq common stocks, ETFs/funds filtered by name) validated against Schwab pricing via `build_universe()`; rejects go to `universe_dropped.json`. Precedence: `watchlist.txt` → `universe.json` → bootstrap from EDGAR. Refreshed on demand by the Stock Scanner's "Update Universe" button.

- **`schwab_client.py`** — read-only Schwab Market Data client (wraps schwab-py). `get_client()` (cached token + manual-login fallback), `quotes()`, `option_chain()`, `price_history_daily()`. Credentials in gitignored `schwab_creds.txt`; token cached in gitignored `schwab_token.json` (access ~30 min, refresh ~7 days).

- **`lso_analyzer.py`** — scores and grades symbols for LSO (wheel-strategy) suitability using yfinance metadata.
  - `analyze_symbol()` — fetches sector, beta, market cap, dividend, earnings date from `yf.Ticker.info` and `ticker.calendar`. Starts at base score 70; applies adjustments from `_SECTOR_SCORES`, `_INDUSTRY_EXTRA`, `_score_beta()`, `_score_market_cap()`.
  - `apply_contract_adjustments()` — re-scores per-contract based on OTM% (handles ITM, near-ATM, comfortable, conservative bands).
  - Grading: A ≥ 85, B ≥ 70, C ≥ 55, D ≥ 40, F < 40.

### Screener GUI (`gui/`)

Built with PyQt6. `run_screener.py` just creates the `QApplication` (no local terminal — market data is fetched from Schwab over HTTPS).

- **`main_window.py`** — `QMainWindow` with a `QTabWidget` holding three tabs. Wires inter-tab signals.
- **`workers.py`** — `QThread` subclasses that run blocking I/O off the main thread:
  - `UniverseWorker` → `core.screener.build_universe` (the "Update Universe" button)
  - `PriceScreenWorker` → `core.screener.run_price_screen`
  - `TechnicalWorker` → `core.screener.run_technical_filter`
  - `OptionsWorker` → `core.screener.run_options_filter`
  - `LsoWorker` → `core.lso_analyzer.analyze_symbols` + `apply_contract_adjustments`, merges with options results
- **`stock_tab.py`** — Stock Scanner UI; "Update Universe" button refreshes the universe; emits `scan_finished` when done.
- **`options_tab.py`** — Options Scanner UI; scans from stock-scan candidates.
- **`lso_tab.py`** — LSO Analysis UI; reads `options_results_cache.json` and calls `LsoWorker`.

### Portfolio GUI (`gui/`)

`run_portfolio.py` launches a standalone window; it needs no market-data API (prices via yfinance).

- **`portfolio_window.py`** — `QMainWindow` wrapping `PortfolioTab`.
- **`positions_tab.py`** — Position tracker; persists to `my_option_positions.json` and `my_stock_positions.json`. Imports TOS Account Statement CSVs (parses the `Options` section and matches fees from `Cash Balance TRD` rows). Refreshes prices via yfinance. Computes 5%/10% below strike automatically.

### Market data (Schwab)

All screener market data comes from the Schwab Market Data API (read-only), via `core/schwab_client.py` wrapping schwab-py. OAuth 2.0; the access token (cached in gitignored `schwab_token.json`) auto-refreshes, and the ~7-day refresh token requires re-running `python -m core.schwab_client login`. App key/secret live in gitignored `schwab_creds.txt` (template: `schwab_creds.txt.example`). The app is registered for **Market Data Production only**, so the token cannot touch accounts. yfinance still supplies LSO/portfolio metadata.
