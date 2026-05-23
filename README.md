# ThetaData Options Screener

A PyQt6 desktop application for screening put-selling (wheel strategy) candidates using end-of-day options data from [ThetaData](https://thetadata.net).

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **ThetaData account** | Free or paid plan at [thetadata.net](https://thetadata.net) |
| **Java 17+** | OpenJDK 21 recommended; required to run the ThetaData terminal |
| **Python 3.11+** | Used for the GUI and all screener logic |
| **Git** | To clone this repo |

---

## 1. Create a ThetaData account

1. Sign up at [thetadata.net](https://thetadata.net).
2. Note your **email address** and **password** — you will need them in step 4.

---

## 2. Clone the repository

```bash
git clone https://github.com/bob-weber/thetadata-screener.git
cd thetadata-screener
```

---

## 3. Download the ThetaData terminal

The terminal is a Java JAR that proxies all data requests. It is **not** included in this repo.

1. Log in to your ThetaData account and download `ThetaTerminalv3.jar`.
2. Place it in the root of the cloned repo (next to `run_gui.py`):

```
thetadata-screener/
├── ThetaTerminalv3.jar   ← here
├── run_gui.py
└── ...
```

---

## 4. Create your credentials file

Create a file named `creds.txt` in the repo root. It must contain exactly two lines — no extra spaces or blank lines:

```
your@email.com
yourpassword
```

> `creds.txt` is gitignored and will never be committed.

---

## 5. Set up the Python virtual environment

The GUI uses a dedicated virtualenv (`gui-env`) with PyQt6 and all required packages.

```bash
python3 -m venv gui-env
gui-env/bin/pip install --upgrade pip
gui-env/bin/pip install PyQt6 thetadata pandas numpy yfinance requests toml
```

> If you are on Windows, replace `gui-env/bin/pip` with `gui-env\Scripts\pip`.

---

## 6. Run the application

```bash
gui-env/bin/python run_gui.py
```

This automatically:
- Starts the ThetaData terminal in the background (`start_terminal.sh`)
- Opens the GUI window
- Stops the terminal cleanly when you close the window

> On first launch the terminal may take 10–20 seconds to authenticate and connect. Watch `thetadata.log` in the repo root if you need to debug connection issues.

**Windows / manual terminal launch:** If the shell scripts don't work on your platform, start the terminal manually before running the GUI:

```bash
java -jar ThetaTerminalv3.jar
```

---

## 7. Using the application

The GUI has four tabs that feed into each other in order:

### Stock Scanner
Fetches the full NYSE/Nasdaq common-stock universe from SEC EDGAR, applies a price filter, then a 45-day technical filter (RSI + Bollinger Band%). Run this first to build a candidate list.

### Options Scanner
Scans EOD option chains for the candidates produced by the Stock Scanner. Apply yield range, DTE, and strike filters to narrow down contracts worth selling puts on.

You can also switch the **Ticker Source** to **My Positions** and enter tickers directly — useful for checking current prices and available contracts on stocks you already hold.

### Wheel Analysis
Scores and grades each contract from the Options Scanner for wheel-strategy suitability. Grades are A–F based on sector, beta, market cap, OTM%, and dividend profile.

### Positions
Track your open option positions. Import directly from a **ThinkorSwim Account Statement CSV** (the app parses the `Options` section and matches commissions from `Cash Balance TRD` rows). The tab computes 5% and 10% below strike automatically for each position.

---

## 8. Configuration

`config.toml` in the repo root controls terminal connection settings (host, port, streaming). The defaults work out of the box for local use. You should not need to edit this file unless you are running the terminal on a different machine.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `thetadata.log` shows auth errors | Check `creds.txt` — must be exactly two lines, email then password, no trailing spaces |
| GUI hangs on startup | The terminal takes time to connect; wait ~20 s or tail `thetadata.log` |
| `No module named PyQt6` | Make sure you are running with `gui-env/bin/python`, not the system Python |
| No results from Options Scanner | Run the Stock Scanner tab first, or switch to "My Positions" source |
| Port 25503 already in use | Run `./stop_terminal.sh` to kill a stale terminal process |
