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
  - `run_technical_filter()` — Pass 2: `HISTORY_DAYS` (180) of daily history via Schwab `price_history_daily()`, then RSI/BB% filter. The window is sized for RSI, not for the bands: BB% and HV are windowed and settle in 20 bars, but Wilder's RSI is recursive and needs several multiples of its period to converge. Each store entry records the lookback it was fetched with, so raising `HISTORY_DAYS` re-fetches short entries rather than reusing them. Writes `tech_history_cache.json` and `tech_candidates_cache.json`. Each candidate row carries `bb_upper`/`bb_lower` as well as `bb_pct`, so the options pass can place a strike on the same band the stock's BB% is read from.
  - `run_options_filter()` — real-time option chains via Schwab (`_fetch_schwab_chain`, one call per symbol) filtered on premium % (`premium_pct` = premium ÷ stock price, the 1%-rule figure), then optionally on `strike_bb_pct` and on `oi_min`.
  - `oi_min` — minimum open interest per strike (missing OI counts as zero). Needed because `premium_pct` isn't monotonic across a put chain: the bid can't go below the $0.01 tick while the strike keeps shrinking, so a 2¢ bid on a $3 strike reads as 0.67% and clears a floor the $9 strike misses. Open interest is what separates those from a real quote.
  - Strike-level Bollinger test: `bb_pct_at()` places any value on a band, so `strike_bb_pct` is where the *strike* sits (0% = lower band) while `bb_pct` stays where the *stock* sits. The two read in opposite directions — below 0 is a breakdown for a price but cushion for a strike — so they are separate fields, and the LSO grade still scores the stock's. Enabled by `strike_bb_filter` with threshold `strike_bb_pct`: a put keeps strikes at or below it, a call at or above. Candidates cached before `bb_upper`/`bb_lower` existed leave the column blank and stand the filter down for that symbol rather than rejecting it (logged).
  - `run_screener()` — convenience wrapper that chains the passes above.
  - Ticker source: `config["symbols"]` (the Stock Scanner's "Import List" and "Specify Stocks" modes) scans exactly those tickers instead of the universe — the price range and the RSI/BB% thresholds never reject, so every one of your symbols comes back with its indicators. Each row carries `passes` (would it have met the RSI/BB% thresholds), which greys the row out in the Stock Scanner and excludes it from the Options Scanner; a symbol with no computable indicator fails. A universe scan drops its failures outright, so all its rows pass. The price cache key drops the price range for a symbol scan, but thresholds stay in the full key for both — they shape the result either way.
  - Symbol universe: persisted to `universe.json`, built from the SEC EDGAR list (NYSE/Nasdaq common stocks, ETFs/funds filtered by name) validated against Schwab pricing via `build_universe()`; rejects go to `universe_dropped.json`. Precedence: `watchlist.txt` → `universe.json` → bootstrap from EDGAR. Refreshed on demand by the Stock Scanner's "Update Universe" button.

- **`schwab_client.py`** — read-only Schwab Market Data client (wraps schwab-py). `get_client()` (cached token + manual-login fallback), `quotes()`, `option_chain()`, `price_history_daily()`. Credentials in gitignored `schwab_creds.txt`; token cached in gitignored `schwab_token.json` (access ~30 min, refresh ~7 days).

- **`lso_analyzer.py`** — scores and grades symbols for LSO (wheel-strategy) suitability using yfinance metadata.
  - `analyze_symbol()` — fetches sector, beta, market cap, dividend, earnings date from `yf.Ticker.info` and `ticker.calendar`. Starts at base score 70; applies adjustments from `_SECTOR_SCORES`, `_INDUSTRY_EXTRA`, `_score_beta()`, `_score_market_cap()`.
  - `apply_contract_adjustments()` — re-scores per contract: OTM% band, σ-cushion gate, IV level, IV/HV, RSI, BB% and bid-ask spread.
  - Grading: A ≥ 85, B ≥ 70, C ≥ 55, D ≥ 40, F < 40 (score clamped 0–100).
  - Hard gates (`_HARD_GATE_TAGS`): a `recent-ipo` or `biotech-binary` tag forces score 0 / grade F / tier `Reject`; `apply_contract_adjustments()` returns early so a good chain can't undo it.
  - Risk tier + max allocation (`_risk_tier()`): tier follows the score (Low ≥ 85, Medium ≥ 70, High below), then `crypto-linked` (`_TIER_CAP_TAGS`) or unprofitability caps it at High — sizing inputs, never gates.
  - `_profitability()` leads on TTM operating income (`_ttm_operating_income()`, quarterly income statement) and falls back to trailing EPS / net income. Operating income is what catches IREN, whose EPS is flattered by a one-off booked below the operating line.
  - `_external_funding()` (quarterly balance sheet) flags a loss-making company funding itself by issuing paper: share count +20% YoY **sustained** across ≥3 of 4 quarters (excludes one-step M&A issuance like SNPS), or net new debt ≥ 15% of market cap (measured against cap, not prior debt, so a small base like RBRK's doesn't explode). Combined with the hand-assigned `core-revenue-declining` tag it forms the IREN pattern → max allocation "15% or pass". Adds two yfinance fetches per symbol, each individually guarded.
  - `load_durable_tags()` / `tags_for()` — hand-maintained risk tags from `durable-tags.json` (ticker → list of tags), shown in the LSO table's Tags column and never scored. Re-read when the file's mtime changes, so tags added mid-session appear on the next run. A ticker mapped to `[]` is researched-with-no-tag; one absent from the file renders as UNTAGGED and is listed in the status line and log.
  - **Every factor, its band and its rationale is documented in [`docs/grading.md`](docs/grading.md)** — update it alongside any scoring change.

### Screener GUI (`gui/`)

Built with PyQt6. `run_screener.py` just creates the `QApplication` (no local terminal — market data is fetched from Schwab over HTTPS).

- **`main_window.py`** — `QMainWindow` with a `QTabWidget` holding three tabs. Wires inter-tab signals.
- **`workers.py`** — `QThread` subclasses that run blocking I/O off the main thread:
  - `UniverseWorker` → `core.screener.build_universe` (the "Update Universe" button)
  - `PriceScreenWorker` → `core.screener.run_price_screen`
  - `TechnicalWorker` → `core.screener.run_technical_filter`
  - `OptionsWorker` → `core.screener.run_options_filter`
  - `LsoWorker` → `core.lso_analyzer.analyze_symbols` + `apply_contract_adjustments`, merges with options results
- **`stock_tab.py`** — Stock Scanner UI; a Ticker Source radio picks one of three: **Universe** (the whole universe, price range and RSI/BB% thresholds enforced), **Import List** (a text file of tickers, re-read at scan time), or **Specify Stocks** (a one-line comma/space-separated list, persisted to `my_positions.txt` one-per-line, debounced). The two explicit-list modes report every ticker with its indicators rather than filtering, greying out (`_style_row`) those outside the thresholds. `_parse_ticker_list()` splits on commas, spaces and newlines for both. "Update Universe" button refreshes the universe; emits `scan_finished` when done. Ticker Source, Parameters, Progress and Log are `CollapsibleGroupBox`es; the Import List / Specify Stocks boxes stay plain, since the radio already shows and hides them.
- **`collapsible.py`** — `CollapsibleGroupBox`, a `QGroupBox` whose title bar collapses its contents (content goes in `box.content()`, not the box). Uses Qt's checkable-group-box click handling with the indicator zero-sized, so the ▾/▸ title *is* the button; collapsed it goes flat and pins its height so a stretchy layout can't leave an empty strip. A box inside a `QSplitter` needs the pane resized on `expanded_changed` — a splitter keeps a pane's size even when its contents are hidden.
- **`options_tab.py`** — Options Scanner UI; scans from stock-scan candidates, minus the rows marked `passes: false` and minus the reject list (`reject_list.txt`, edited here). The Strike BB% parameter gates strikes on the underlying's band (the Stock Scanner's BB% threshold gates which symbols get a chain at all); its "at or below / at or above" label follows the Put/Call radio. Min open interest defaults to 1. Reject List, Parameters, Progress and Log are `CollapsibleGroupBox`es (all open on launch, state not persisted) so the results table can take the window. "Export for Claude…" writes the scanned contracts to a CSV (the table's columns plus `premium_pct`, `sigma_pct` and `hv`, which have no column), the same as the LSO tab's export.
- **`lso_tab.py`** — LSO Analysis UI; reads `options_results_cache.json` and calls `LsoWorker`.

### Portfolio GUI (`gui/`)

`run_portfolio.py` launches a standalone window; it needs no market-data API (prices via yfinance).

- **`portfolio_window.py`** — `QMainWindow` wrapping `PortfolioTab`.
- **`positions_tab.py`** — Position tracker; persists to `my_option_positions.json` and `my_stock_positions.json`. Imports TOS Account Statement CSVs (parses the `Options` section and matches fees from `Cash Balance TRD` rows). Refreshes prices via yfinance. Computes 5%/10% below strike automatically.

### Market data (Schwab)

All screener market data comes from the Schwab Market Data API (read-only), via `core/schwab_client.py` wrapping schwab-py. OAuth 2.0; the access token (cached in gitignored `schwab_token.json`) auto-refreshes, and the ~7-day refresh token requires re-running `python -m core.schwab_client login`. App key/secret live in gitignored `schwab_creds.txt` (template: `schwab_creds.txt.example`). The app is registered for **Market Data Production only**, so the token cannot touch accounts. yfinance still supplies LSO/portfolio metadata.
