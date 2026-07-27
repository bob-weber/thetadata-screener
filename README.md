# LSO Tools

Two PyQt6 desktop apps for running a wheel strategy (selling cash-secured puts,
then covered calls if assigned):

- **LSO Stock Screener** — scans for candidates, prices their option chains, and
  grades each contract for wheel suitability.
- **Portfolio Tracker** — imports Thinkorswim account statements and tracks
  realized P&L, wheel cycles and weekly return on allocated capital.

Market data comes from the **Schwab Market Data API** over HTTPS. There is no
local terminal or daemon to run.

---

## Requirements

| | |
|---|---|
| **Python 3.12+** | 3.14 is what it's developed against |
| **Schwab developer account** | A registered app with **Market Data Production** access, from [developer.schwab.com](https://developer.schwab.com) |

The Schwab app only needs market data. It has no account permissions, so the
token cannot see positions, place orders, or move money. Portfolio data comes
from statement CSVs you export yourself.

## Setup

```bash
git clone <your-remote> lso-tools
cd lso-tools

python3 -m venv gui-env
gui-env/bin/pip install PyQt6 schwab-py yfinance pandas numpy matplotlib requests
```

Then create your credentials file:

```bash
cp schwab_creds.txt.example schwab_creds.txt
# fill in app_key and app_secret from your Schwab app
```

`schwab_creds.txt` is gitignored and never committed. The callback URL defaults
to `https://127.0.0.1`, which must match the one registered on your Schwab app.

### One-time login

```bash
gui-env/bin/python -m core.schwab_client login
```

This opens a browser; you approve, and Schwab redirects to a `https://127.0.0.1/...`
address that will fail to load — that's expected. Copy the **full URL from the
address bar** (it carries the `?code=...`) and paste it back at the prompt.

That writes `schwab_token.json` (gitignored). The access token refreshes itself;
the refresh token lasts about **7 days**, so expect to re-run this weekly. The
app says so plainly when it has lapsed.

## Running

```bash
gui-env/bin/python run_screener.py      # LSO Stock Screener
gui-env/bin/python run_portfolio.py     # Portfolio Tracker
```

Desktop launchers for both live in [`desktop/`](desktop/) — see the README there.
They hard-code absolute paths, so edit them if the repo moves.

---

## The screener

Three tabs, run left to right. Each writes a cache the next one reads.

### 1. Stock Scanner

Pick a **ticker source**:

- **Universe / Watchlist** — screens everything. Pass 1 filters the universe to a
  price range with batched quotes; Pass 2 pulls 45 days of history for the
  survivors and keeps those under your RSI and BB% thresholds.
- **My Stocks** — scans only the tickers in the My Stocks box (`my_positions.txt`).
  The price range and the RSI/BB% thresholds are *measured but not applied*, so
  every name comes back with its indicators whatever they say. This is for
  looking at what you already own, not for finding new candidates.

The universe is built from the SEC EDGAR listing, validated against Schwab
pricing, and saved to `universe.json`. Refresh it with **Update Universe** when
listings change — not routinely. A `watchlist.txt` file, if present, overrides it.

### 2. Options Scanner

Fetches live option chains for the stock scan's candidates — one Schwab call per
symbol, run concurrently behind a self-tuning rate limiter — and keeps strikes
whose **premium %** falls in your band. Premium % is measured against the capital
the trade ties up: premium ÷ strike for a put, ÷ share price for a covered call.

The **Reject List** here (`reject_list.txt`) drops symbols before any chain is
fetched, whichever ticker source produced them.

### 3. LSO Analysis

Grades each contract A–F for wheel suitability, combining company fundamentals
from yfinance with the contract's own numbers. **Every factor, its bands and the
reasoning behind it is documented in [`docs/grading.md`](docs/grading.md).**

The Notes column is the full derivation of each grade, in the order applied.
Right-click any column header — or any cell — for an explanation of that column.

**Export for Claude** copies the whole table as markdown; clicking a ticker opens
a price / Bollinger / RSI chart.

---

## The portfolio tracker

Import a Thinkorswim **Account Statement** CSV (Positions tab → Import). It reads
the `Cash Balance` section for every option and stock trade, matches fees, and
reconstructs wheel cycles: put sold → rolled → expired, or assigned → covered
calls → shares sold.

Statements can overlap freely. Imports merge by multiplicity, so re-importing the
same file adds nothing while a statement covering new days tops up only what's
missing — and partial fills of one order are kept as the separate fills they are.

Tabs cover open positions and cycles, cumulative realized/unrealized P&L, and
weekly return on allocated capital. The **Help** menu explains how each figure is
derived.

One quirk worth knowing: a long-range statement export omits the `RAD` rows that
mark expiration and assignment, so settled cycles are inferred from their
expiration date instead. An assignment always attaches stock, which is what
distinguishes the two cases.

---

## Files

Everything is read and written relative to the repo root, which is why the
desktop launchers set `Path=`.

| File | What it is |
|---|---|
| `schwab_creds.txt` | Your Schwab app key/secret — gitignored |
| `schwab_token.json` | Cached OAuth token — gitignored, refresh ~7 days |
| `universe.json` | Saved scan universe; `universe_dropped.json` records rejects |
| `watchlist.txt` | Optional manual symbol list, overrides the universe |
| `my_positions.txt` | The My Stocks list (Stock Scanner) |
| `reject_list.txt` | Never-scan list (Options Scanner) |
| `*_cache.json` | Scan results and per-symbol history — regenerated, gitignored |
| `gains_history_<account>.json` | Accumulated statement history, per account |
| `my_option_events_<account>.json` | Wheel-cycle cash events, per account |

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Schwab login expired" | Re-run `python -m core.schwab_client login` — the 7-day refresh token lapsed |
| Options Scanner says to run the Stock Scanner first | It reads `tech_candidates_cache.json`; run a stock scan |
| A scan slows mid-run | Schwab rate-limited it; the limiter backs off and recovers on its own, and the log says so |
| `No module named PyQt6` | Run with `gui-env/bin/python`, not the system Python |
| IV/HV or the indicators are blank | The symbol needs ~20 daily closes; a very new listing won't have them |
| A launcher starts the app with no data | `Path=` is missing from the `.desktop` file — every data file is relative |

## For contributors

[`CLAUDE.md`](CLAUDE.md) describes the architecture: the screener pipeline, the
core library, and how the GUI tabs pass data. [`docs/grading.md`](docs/grading.md)
is the grading model. Keep both current alongside the code — the grading doc in
particular is the only place the scoring rationale is written down.
