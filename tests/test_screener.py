"""Regression tests for the screener (Stock Scanner, Options Scanner, LSO grading).

Run:  gui-env/bin/python -m unittest discover tests

No network: Schwab, yfinance and EDGAR are replaced with stubs, and everything
runs from a temporary working directory so the real caches are never touched.
Each case is a bug a full review found.
"""
import json
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, timedelta
from unittest import mock

import pandas as pd

from core import lso_analyzer as la
from core import litigation_review as lr
from core import schwab_client
from core import screener as sc


class _InTempDir(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="lso-screener-test-")
        os.chdir(self.tmp)

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)


class Calendar(unittest.TestCase):
    def test_new_year_on_a_saturday_is_not_observed_on_friday(self):
        # NYSE stays open Dec 31 when Jan 1 is a Saturday.
        self.assertEqual(sc._last_session_before(date(2028, 1, 3)), date(2027, 12, 31))
        self.assertEqual(sc._last_trading_day(date(2028, 1, 1)), date(2027, 12, 31))

    def test_new_year_on_a_sunday_is_observed_monday(self):
        self.assertIn(date(2023, 1, 2), sc._us_market_holidays(2023))


class OptionsMath(unittest.TestCase):
    def test_otm_direction_follows_the_option_side(self):
        self.assertEqual(sc.otm_pct(100.0, 92.0, "P"), 8.0)     # put below: OTM
        self.assertEqual(sc.otm_pct(100.0, 108.0, "C"), 8.0)    # call above: OTM
        self.assertEqual(sc.otm_pct(100.0, 108.0, "P"), -8.0)   # put above: ITM


def _candles(n, start=date(2026, 1, 2), base=50.0):
    """n daily candles ending well before today, gently oscillating."""
    out, d = [], start
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        ts = int(datetime(d.year, d.month, d.day, 16).timestamp() * 1000)
        out.append({"datetime": ts, "close": base + (i % 7) - 3})
        d += timedelta(days=1)
    return {"candles": out}


class TechnicalPass(_InTempDir):
    CONFIG = {"rsi_threshold": 60, "bb_pct_threshold": 60}

    def _run(self, history, stop_after=None):
        calls = {"n": 0}

        def fake_history(client, sym, start=None, end=None):
            calls["n"] += 1
            if history[sym] is None:
                raise ValueError("boom")                 # a non-rate-limit failure
            return history[sym]

        stop = (lambda: calls["n"] >= stop_after) if stop_after else None
        priced = [{"symbol": s, "price": 50.0} for s in history]
        with mock.patch.object(schwab_client, "get_client", return_value=object()), \
             mock.patch.object(schwab_client, "price_history_daily", side_effect=fake_history), \
             mock.patch.object(sc.time, "sleep"):
            return sc.run_technical_filter(self.CONFIG, priced, stop_flag=stop,
                                           history_store_file="store.json",
                                           candidates_cache_file="cands.json")

    def test_every_price_screened_symbol_comes_back(self):
        rows = self._run({"GOOD": _candles(150), "NEW": _candles(10), "FAIL": None})
        by = {r["symbol"]: r for r in rows}
        self.assertEqual(set(by), {"GOOD", "NEW", "FAIL"})
        self.assertIsNotNone(by["GOOD"]["rsi"])
        self.assertEqual((by["NEW"]["rsi"], by["NEW"]["passes"], by["NEW"]["note"]),
                         (None, False, "not enough history"))
        self.assertEqual((by["FAIL"]["passes"], by["FAIL"]["note"]),
                         (False, "history fetch failed"))

    def test_stopping_keeps_the_histories_already_fetched(self):
        syms = {f"S{i}": _candles(150) for i in range(40)}
        rows = self._run(syms, stop_after=3)
        self.assertEqual(rows, [])
        with open("store.json") as f:
            stored = json.load(f)
        self.assertGreaterEqual(len(stored), 1)


class ConcurrentFetch(unittest.TestCase):
    def test_failed_keys_are_reported(self):
        def call(k):
            if k == "bad":
                raise ValueError("nope")
            return k
        with mock.patch.object(sc.time, "sleep"):
            results, lim = sc._concurrent_fetch(["a", "bad", "b"], call, workers=2, start_rate=1000)
        self.assertEqual(set(results), {"a", "b"})
        self.assertEqual(lim.failed, ["bad"])

    def test_circuit_breaker_trips_on_a_block_that_starts_mid_scan(self):
        class Blocked(Exception):
            response = type("R", (), {"status_code": 403})()
        clock = {"t": 0.0}

        def call(k):
            if k == "k0":
                return k                                    # one early success
            clock["t"] += 40.0                              # then a sustained block
            raise Blocked()
        with mock.patch.object(sc.time, "monotonic", side_effect=lambda: clock["t"]), \
             mock.patch.object(sc.time, "sleep"):
            with self.assertRaises(sc.ScreenerError):
                sc._concurrent_fetch([f"k{i}" for i in range(6)], call, workers=1, start_rate=1000)


class UniverseValidation(unittest.TestCase):
    def test_expired_token_does_not_save_an_unvalidated_universe(self):
        with mock.patch.object(schwab_client, "quotes", side_effect=RuntimeError("401")), \
             mock.patch.object(schwab_client, "is_auth_error", return_value=True):
            with self.assertRaises(sc.ScreenerError):
                sc._resolve_priceable(object(), ["AAPL", "XYZW"])


class LsoGrading(_InTempDir):
    def setUp(self):
        super().setUp()
        with open("durable-tags.json", "w") as f:
            json.dump({"BIO": ["biotech-binary"], "PLAIN": []}, f)
        la._DURABLE_TAGS_CACHE = None if hasattr(la, "_DURABLE_TAGS_CACHE") else None

    def _failed_lookup(self, sym):
        with mock.patch.object(la.yf, "Ticker", side_effect=RuntimeError("rate limited")):
            return la.analyze_symbol(sym, date.today() + timedelta(days=7))

    def test_data_error_keeps_the_hard_gate(self):
        r = self._failed_lookup("BIO")
        self.assertTrue(r["reject"])
        good = la.apply_contract_adjustments(r, 10.0, cushion_sigma=2.0, rsi=30, spread_pct=2)
        self.assertEqual((good["grade"], good["risk_tier"]), ("F", "Reject"))

    def test_data_error_stays_ungraded_whatever_the_chain(self):
        r = self._failed_lookup("PLAIN")
        good = la.apply_contract_adjustments(r, 10.0, cushion_sigma=2.0, rsi=30, spread_pct=2)
        self.assertEqual(good["grade"], "?")


class Litigation(unittest.TestCase):
    def test_confirmed_entry_checked_this_quarter_is_not_refetched(self):
        today = date(2026, 12, 1)
        entry = {"severity": "overhang", "confirmed": True, "asof": "2026-08-01",
                 "review_by": "2026-11-15", "auto_asof": "2026-11-20"}
        self.assertFalse(lr._needs_review(entry, today))
        self.assertTrue(la._litigation_state(entry, today)["stale"])   # still flagged

    def test_auto_raise_keeps_the_entry_confirmed(self):
        store = {"X": {"severity": "overhang", "confirmed": True, "asof": "2026-01-01",
                       "review_by": "2026-04-11"}}
        raised = {"severity": "existential", "asof": "2026-05-01", "review_by": "2026-08-09",
                  "note": "restatement", "auto": True}
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "lit.json")
            with open(path, "w") as f:
                json.dump(store, f)
            with mock.patch.object(lr, "_fetch_all", return_value={"X": raised}), \
                 mock.patch.object(lr, "load_cik_map", return_value={"X": 1}):
                lr.refresh(["X"], litigation_file=path)
            with open(path) as f:
                self.assertTrue(json.load(f)["X"]["confirmed"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
