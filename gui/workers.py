import json
from datetime import date
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

OPTIONS_RESULTS_CACHE = "options_results_cache.json"
_OPT_CACHE_KEYS = ["date", "expiration_date", "right", "side", "yield_min", "yield_max"]


class StockWorker(QThread):
    log_msg        = pyqtSignal(str)
    pass1_progress = pyqtSignal(int, int)
    pass2_progress = pyqtSignal(int, int)
    finished       = pyqtSignal(list)
    error          = pyqtSignal(str)

    def __init__(self, config: dict, watchlist_file: str | None = None):
        super().__init__()
        self._config    = config
        self._watchlist = watchlist_file
        self._stop      = False

    def stop(self):
        self._stop = True

    def run(self):
        from core.screener import run_stock_filter, ScreenerError
        try:
            results = run_stock_filter(
                self._config,
                on_log=self.log_msg.emit,
                on_pass1_progress=lambda c, t: self.pass1_progress.emit(c, t),
                on_pass2_progress=lambda c, t: self.pass2_progress.emit(c, t),
                stop_flag=lambda: self._stop,
                watchlist_file=self._watchlist,
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

    def __init__(self, config: dict):
        super().__init__()
        self._config = config
        self._stop   = False

    def stop(self):
        self._stop = True

    def _cache_key(self) -> dict:
        c = self._config
        return {
            "date":            date.today().isoformat(),
            "expiration_date": c.get("expiration_date"),
            "right":           c.get("right", "P"),
            "side":            c.get("side", "sell"),
            "yield_min":       c.get("yield_min"),
            "yield_max":       c.get("yield_max"),
        }

    def run(self):
        from core.screener import run_options_filter, ScreenerError

        # ── Load stock candidates ─────────────────────────────────────────
        cand_path = Path("tech_candidates_cache.json")
        if not cand_path.exists():
            self.error.emit("No stock scan results found — run the Stock Scanner first.")
            return
        try:
            cached = json.loads(cand_path.read_text())
        except Exception as e:
            self.error.emit(f"Could not read stock scan cache: {e}")
            return
        if cached.get("date") != date.today().isoformat():
            self.error.emit("Stock scan results are from a previous day — run the Stock Scanner first.")
            return
        candidates = cached.get("candidates", [])
        if not candidates:
            self.error.emit("Stock scan returned no candidates — run the Stock Scanner first.")
            return
        self.candidates_loaded.emit(len(candidates))

        # ── Options results cache ─────────────────────────────────────────
        key        = self._cache_key()
        opt_path   = Path(OPTIONS_RESULTS_CACHE)
        if opt_path.exists():
            try:
                opt_cached = json.loads(opt_path.read_text())
                if all(opt_cached.get(k) == v for k, v in key.items()):
                    self.log_msg.emit(
                        f"Options results loaded from cache "
                        f"({len(opt_cached['results'])} contracts)."
                    )
                    self.finished.emit(opt_cached["results"])
                    return
            except Exception:
                pass

        # ── Run live scan ─────────────────────────────────────────────────
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


class WheelWorker(QThread):
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
        from core.wheel_analyzer import analyze_symbols, apply_contract_adjustments

        opt_path = Path(OPTIONS_RESULTS_CACHE)
        if not opt_path.exists():
            self.error.emit("No options results found — run the Options Scanner first.")
            return
        try:
            cached = json.loads(opt_path.read_text())
        except Exception as e:
            self.error.emit(f"Could not read options cache: {e}")
            return
        if cached.get("date") != date.today().isoformat():
            self.error.emit("Options results are from a previous day — run the Options Scanner first.")
            return

        results  = cached.get("results", [])
        exp_str  = cached.get("expiration_date", "")
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
            f"Analyzing {len(symbols)} unique symbols for wheel suitability "
            f"(exp {exp_str}) …"
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
            sym     = contract["symbol"]
            strike  = contract.get("strike")
            otm_pct = contract.get("otm_pct")
            if sym not in sym_analysis:
                continue
            adjusted = apply_contract_adjustments(sym_analysis[sym], otm_pct)
            merged.append({
                **adjusted,
                "stock_price": price_lookup.get(sym),
                "strike":      strike,
                "premium":     contract.get("yield_pct"),
                "otm_pct":     otm_pct,
                "capital":     round(strike * 100) if strike is not None else None,
            })

        self.finished.emit(merged)
