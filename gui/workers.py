import json
from datetime import date
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

OPTIONS_RESULTS_CACHE = "options_results_cache.json"
_OPT_CACHE_KEYS = ["date", "expiration_date", "right", "side", "yield_min", "yield_max"]


class UniverseWorker(QThread):
    """Refresh the scan universe — SEC EDGAR list validated against Schwab pricing."""
    log_msg  = pyqtSignal(str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal(dict)
    error    = pyqtSignal(str)

    def run(self):
        from core.screener import build_universe, ScreenerError
        try:
            data = build_universe(
                on_log=self.log_msg.emit,
                on_progress=lambda c, t: self.progress.emit(c, t),
            )
            self.finished.emit(data)
        except ScreenerError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(f"Unexpected error: {e}")


class StockScanWorker(QThread):
    """Combined stock scan — Pass 1 (price screen) then Pass 2 (technical filter).

    Driven by the Stock Scanner's single 'Run Scan' button. Pass 2 only ever sees
    the price-qualified symbols, so out-of-range stocks are never fetched. The
    price-qualified list is handed straight to Pass 2 (no cache round-trip).
    Separate progress/found signals let each table fill during its own pass.
    """
    log_msg        = pyqtSignal(str)
    price_progress = pyqtSignal(int, int)
    tech_progress  = pyqtSignal(int, int)
    price_found    = pyqtSignal(list)
    tech_found     = pyqtSignal(list)
    price_done     = pyqtSignal(list)   # price-qualified rows, when Pass 1 finishes
    finished       = pyqtSignal(list)   # final technical candidates
    error          = pyqtSignal(str)

    def __init__(self, config: dict, watchlist_file: str | None = None):
        super().__init__()
        self._config    = config
        self._watchlist = watchlist_file
        self._stop      = False

    def stop(self):
        self._stop = True

    def run(self):
        from core.screener import run_price_screen, run_technical_filter, ScreenerError
        try:
            price_qualified = run_price_screen(
                self._config,
                on_log=self.log_msg.emit,
                on_progress=lambda c, t: self.price_progress.emit(c, t),
                on_found=lambda rows: self.price_found.emit(rows),
                stop_flag=lambda: self._stop,
                watchlist_file=self._watchlist,
                use_cache=False,
            )
            self.price_done.emit(price_qualified)
            if self._stop:
                return
            if not price_qualified:
                self.log_msg.emit("No symbols in price range — skipping technical scan.")
                self.finished.emit([])
                return

            results = run_technical_filter(
                self._config,
                price_qualified,
                on_log=self.log_msg.emit,
                on_progress=lambda c, t: self.tech_progress.emit(c, t),
                on_found=lambda rows: self.tech_found.emit(rows),
                stop_flag=lambda: self._stop,
                use_cache=True,   # reuse per-symbol history store; live price always re-applied
            )
            self.finished.emit(results)
        except ScreenerError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(f"Unexpected error: {e}")


class OptionsWorker(QThread):
    log_msg           = pyqtSignal(str)
    opts_progress     = pyqtSignal(int, int)
    candidates_loaded = pyqtSignal(int)
    finished          = pyqtSignal(list)
    error             = pyqtSignal(str)

    def __init__(self, config: dict, positions: list[str] | None = None,
                 reject: set[str] | None = None):
        super().__init__()
        self._config    = config
        self._positions = positions  # None → use stock scan cache; list → fetch prices live
        self._reject    = {s.upper() for s in reject} if reject else set()
        self._stop      = False

    def stop(self):
        self._stop = True

    def _cache_key(self) -> dict:
        from core.screener import _current_trade_date
        c = self._config
        key = {
            "date":            date.today().isoformat(),
            "trade_date":      _current_trade_date().isoformat(),
            "expiration_date": c.get("expiration_date"),
            "right":           c.get("right", "P"),
            "side":            c.get("side", "sell"),
            "yield_min":       c.get("yield_min"),
            "yield_max":       c.get("yield_max"),
        }
        if self._positions is not None:
            key["ticker_source"] = "positions"
            key["positions"]     = sorted(self._positions)
        else:
            key["ticker_source"] = "scanner"
        return key

    def _load_scanner_candidates(self) -> list[dict] | None:
        cand_path = Path("tech_candidates_cache.json")
        if not cand_path.exists():
            self.error.emit("No stock scan results found — run the Stock Scanner first.")
            return None
        try:
            cached = json.loads(cand_path.read_text())
        except Exception as e:
            self.error.emit(f"Could not read stock scan cache: {e}")
            return None
        candidates = cached.get("candidates", [])
        if not candidates:
            self.error.emit("Stock scan returned no candidates — run the Stock Scanner first.")
            return None
        candidates = self._apply_reject(candidates, "candidate")
        if not candidates:
            self.error.emit("All stock-scan candidates are on the reject list.")
            return None
        scan_date = cached.get("date", "unknown date")
        self.log_msg.emit(f"Loaded {len(candidates)} candidates from stock scan ({scan_date}).")
        self.candidates_loaded.emit(len(candidates))
        return candidates

    def _apply_reject(self, candidates: list[dict], noun: str) -> list[dict]:
        """Drop candidates whose symbol is on the reject list; log how many."""
        if not self._reject:
            return candidates
        kept    = [c for c in candidates if c["symbol"].upper() not in self._reject]
        removed = len(candidates) - len(kept)
        if removed:
            self.log_msg.emit(f"Reject list: excluded {removed} {noun}(s).")
        return kept

    def _fetch_position_candidates(self) -> list[dict] | None:
        from core.screener import fetch_stock_prices, ScreenerError
        if self._reject:
            kept    = [p for p in self._positions if p.upper() not in self._reject]
            removed = len(self._positions) - len(kept)
            if removed:
                self.log_msg.emit(f"Reject list: excluded {removed} position(s).")
            self._positions = kept
            if not self._positions:
                self.error.emit("All positions are on the reject list.")
                return None
        self.log_msg.emit(f"Fetching current prices for {len(self._positions)} position(s)…")
        try:
            candidates = fetch_stock_prices(
                self._positions,
                on_log=self.log_msg.emit,
                on_progress=lambda c, t: self.opts_progress.emit(c, t),
                stop_flag=lambda: self._stop,
            )
        except ScreenerError as e:
            self.error.emit(str(e))
            return None
        except Exception as e:
            self.error.emit(f"Unexpected error fetching prices: {e}")
            return None
        if not candidates:
            self.error.emit("Could not fetch a current price for any of the specified tickers.")
            return None
        self.candidates_loaded.emit(len(candidates))
        # Reset progress bar for the upcoming options scan
        self.opts_progress.emit(0, len(candidates))
        return candidates

    def run(self):
        from core.screener import run_options_filter, ScreenerError

        # ── Load or fetch candidates ──────────────────────────────────────
        if self._positions is not None:
            candidates = self._fetch_position_candidates()
        else:
            candidates = self._load_scanner_candidates()

        if candidates is None:
            return

        # ── Run live scan (always; cache is only for startup pre-load) ───────
        key      = self._cache_key()
        opt_path = Path(OPTIONS_RESULTS_CACHE)
        try:
            results = run_options_filter(
                candidates,
                self._config,
                on_log=self.log_msg.emit,
                on_progress=lambda c, t: self.opts_progress.emit(c, t),
                stop_flag=lambda: self._stop,
            )
            opt_path.write_text(json.dumps({**key, "results": results}))
            self.finished.emit(results)
        except ScreenerError as e:
            self.error.emit(str(e))
        except Exception as e:
            self.error.emit(f"Unexpected error: {e}")


class LsoWorker(QThread):
    log_msg    = pyqtSignal(str)
    progress   = pyqtSignal(int, int)
    finished   = pyqtSignal(list)
    error      = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        from datetime import datetime
        from core.lso_analyzer import analyze_symbols, apply_contract_adjustments

        opt_path = Path(OPTIONS_RESULTS_CACHE)
        if not opt_path.exists():
            self.error.emit("No options results found — run the Options Scanner first.")
            return
        try:
            cached = json.loads(opt_path.read_text())
        except Exception as e:
            self.error.emit(f"Could not read options cache: {e}")
            return

        results   = cached.get("results", [])
        exp_str   = cached.get("expiration_date", "")
        scan_date = cached.get("date", "unknown date")
        if not results:
            self.error.emit("Options scan returned no results — run the Options Scanner first.")
            return

        try:
            expiration = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except Exception:
            self.error.emit(f"Could not parse expiration date: {exp_str!r}")
            return

        symbols = sorted({r["symbol"] for r in results})
        self.log_msg.emit(
            f"Analyzing {len(symbols)} unique symbols for LSO suitability "
            f"(exp {exp_str}, scan from {scan_date}) …"
        )

        price_lookup: dict[str, float] = {}
        try:
            cand_data = json.loads(Path("tech_candidates_cache.json").read_text())
            for c in cand_data.get("candidates", []):
                price_lookup[c["symbol"]] = c["price"]
        except Exception:
            pass

        analysis = analyze_symbols(
            symbols,
            expiration,
            on_log=self.log_msg.emit,
            on_progress=lambda c, t: self.progress.emit(c, t),
            stop_flag=lambda: self._stop,
        )

        sym_analysis = {r["symbol"]: r for r in analysis}

        merged = []
        for contract in results:
            sym        = contract["symbol"]
            strike     = contract.get("strike")
            stock_price = price_lookup.get(sym)
            if sym not in sym_analysis:
                continue
            if strike is not None and stock_price is not None:
                otm_pct = round((stock_price - strike) / stock_price * 100, 2)
            else:
                otm_pct = contract.get("otm_pct")
            adjusted = apply_contract_adjustments(
                sym_analysis[sym], otm_pct,
                iv=contract.get("iv"),
                cushion_sigma=contract.get("cushion_sigma"),
                iv_pctile=contract.get("iv_pctile"),
            )
            merged.append({
                **adjusted,
                "stock_price": stock_price,
                "strike":      strike,
                "premium":     contract.get("yield_pct"),
                "otm_pct":     otm_pct,
                "capital":     round(strike * 100) if strike is not None else None,
            })

        self.finished.emit(merged)
