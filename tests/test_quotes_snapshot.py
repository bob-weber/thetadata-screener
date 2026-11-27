"""Tests for quotes_snapshot.py's roll targets for short puts.

Run:  gui-env/bin/python -m unittest discover tests

No network: the Schwab client is a stub that honours the chain call's strike
and date window, so the next-weekly fallback is tested as Schwab would answer
it. Importing quotes_snapshot does not change the working directory (only
main() does), so this file leaves the other tests' temp directories alone.
"""
import unittest
from datetime import date, timedelta

import quotes_snapshot as qs

FRI = date(2026, 11, 27)        # a Friday; 24 Dec 2026 is the Christmas Thursday
WED_NEXT = date(2026, 12, 2)


def _row(strike=50.0, price=None, expiry=FRI, bid=None, ask=None, mark=None,
         typ="P", account="LSO", underlying="X"):
    intrinsic = None
    if price is not None:
        diff = strike - price if typ == "P" else price - strike
        intrinsic = round(max(0.0, diff), 4)
    return {"account": account, "symbol": f"{underlying} {expiry} {typ}{strike:g}",
            "underlying": underlying, "type": typ, "strike": strike,
            "expiry": expiry.isoformat(), "qty": -1, "bid": bid, "ask": ask,
            "mark": mark, "underlying_last": price, "intrinsic": intrinsic}


def _itm(pct, strike=50.0):
    """Underlying price that leaves a put at `strike` `pct` ITM (price-denominated)."""
    return strike / (1 + pct)


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeClient:
    """get_option_chain over a fixed list of (expiry, strike, bid, ask, mark)."""

    class Options:
        class ContractType:
            PUT = "PUT"

    def __init__(self, listings=(), error=None):
        self.listings = list(listings)
        self.error = error
        self.calls = []

    def get_option_chain(self, symbol, *, contract_type, strike, from_date, to_date):
        self.calls.append((symbol, strike, from_date, to_date))
        if self.error:
            raise self.error
        exp_map: dict = {}
        for exp, k, bid, ask, mark in self.listings:
            if from_date <= exp <= to_date:
                key = f"{exp.isoformat()}:{(exp - FRI).days}"
                exp_map.setdefault(key, {})[f"{k:.1f}"] = [
                    {"symbol": f"{symbol} {exp} P{k:g}", "bid": bid, "ask": ask, "mark": mark}]
        return _Resp({"putExpDateMap": exp_map})


class ThisFriday(unittest.TestCase):
    def test_weekdays_and_weekend(self):
        self.assertEqual(qs._this_friday(date(2026, 11, 23)), FRI)     # Monday
        self.assertEqual(qs._this_friday(FRI), FRI)
        self.assertEqual(qs._this_friday(date(2026, 11, 28)), FRI + timedelta(7))  # Sat
        self.assertEqual(qs._this_friday(date(2026, 11, 29)), FRI + timedelta(7))  # Sun


class NeedsTarget(unittest.TestCase):
    def weeks(self, pct, expiry=FRI, **kw):
        return qs._needs_target(_row(price=_itm(pct), expiry=expiry, **kw), FRI)

    def test_bands_for_an_expiring_line(self):
        self.assertEqual(self.weeks(0.07), 1)
        self.assertEqual(self.weeks(0.12), 2)
        self.assertEqual(self.weeks(0.17), 3)
        self.assertEqual(self.weeks(0.25), 4)

    def test_pct_itm_is_price_denominated(self):
        # 50 strike, 47.5 price: 5.26% of price (needs a roll), 5.0% of strike.
        self.assertEqual(qs._needs_target(_row(price=47.5), FRI), 1)
        # 47.7: 4.8% of price -> under 5%, so a line expiring next week holds.
        self.assertIsNone(qs._needs_target(_row(price=47.7, expiry=FRI + timedelta(7)), FRI))

    def test_at_band_holds(self):
        self.assertIsNone(self.weeks(0.07, expiry=FRI + timedelta(weeks=1)))
        self.assertIsNone(self.weeks(0.12, expiry=FRI + timedelta(weeks=2)))
        self.assertEqual(self.weeks(0.12, expiry=FRI + timedelta(weeks=1)), 2)

    def test_maxed_line_gets_no_target(self):
        self.assertIsNone(self.weeks(0.30, expiry=FRI + timedelta(weeks=4)))
        self.assertIsNone(self.weeks(0.30, expiry=date(2026, 12, 24)))   # holiday Thursday

    def test_mid_week_expiry_in_the_band_week_is_at_band(self):
        self.assertIsNone(self.weeks(0.07, expiry=WED_NEXT))
        self.assertEqual(self.weeks(0.12, expiry=WED_NEXT), 2)

    def test_under_five_percent_waits_for_expiration_day(self):
        self.assertIsNone(self.weeks(0.03, expiry=FRI + timedelta(7)))
        self.assertEqual(self.weeks(0.03), 1)

    def test_expiring_near_otm(self):
        self.assertEqual(qs._needs_target(_row(price=50 * 1.02), FRI), 1)    # 2% OTM
        self.assertIsNone(qs._needs_target(_row(price=50 * 1.04), FRI))      # 4% OTM

    def test_calls_and_missing_price_skipped(self):
        self.assertIsNone(qs._needs_target(_row(price=40.0, typ="C"), FRI))
        self.assertIsNone(qs._needs_target(_row(price=None), FRI))


class RollTarget(unittest.TestCase):
    def test_deep_itm_close_cost_is_at_least_intrinsic(self):
        # Old 9.80/10.20 mid 10.00 under 10.30 intrinsic: closing costs >= 10.30.
        row = _row(price=39.70, bid=9.80, ask=10.20, mark=10.00)
        c = FakeClient([(FRI + timedelta(weeks=4), 50.0, 10.60, 11.00, 10.80)])
        t = qs._roll_target(c, row, 4, FRI)
        self.assertFalse(t["old_quote_unreliable"])
        self.assertEqual(t["credit_mid"], 0.80)        # what ToS's mid shows
        self.assertEqual(t["credit_est"], 0.30)        # 10.60 bid - 10.30 intrinsic
        self.assertEqual(t["min_credit"], 0.6)
        self.assertLess(t["credit_est"], t["min_credit"])

    def test_placeholder_quote_on_expiring_otm_put(self):
        # FLY 27 Nov 2026: 19P expiring, 0.00/1.00 placeholder; filled at 0.70.
        row = _row(strike=19.0, price=19.32, bid=0.0, ask=1.0, mark=0.5, underlying="FLY")
        c = FakeClient([(FRI + timedelta(7), 19.0, 0.70, 1.00, 0.85)])
        t = qs._roll_target(c, row, 1, FRI)
        self.assertTrue(t["old_quote_unreliable"])
        self.assertEqual(t["credit_mid"], 0.35)
        self.assertEqual(t["credit_natural"], -0.30)
        self.assertEqual(t["credit_est"], 0.68)        # new bid - 0.02
        self.assertEqual(t["weeks_added"], 1)

    def test_sound_otm_quote_uses_old_mid(self):
        row = _row(price=51.0, bid=0.20, ask=0.24, mark=0.22)
        c = FakeClient([(FRI + timedelta(7), 50.0, 0.90, 1.00, 0.95)])
        self.assertEqual(qs._roll_target(c, row, 1, FRI)["credit_est"], 0.68)

    def test_wide_spread_marks_old_quote_unreliable(self):
        self.assertTrue(qs._unreliable(0.10, 0.90, 0.50))
        self.assertTrue(qs._unreliable(0.0, 0.05, 0.03))
        self.assertTrue(qs._unreliable(0.20, None, 0.25))
        self.assertFalse(qs._unreliable(0.40, 0.50, 0.45))

    def test_holiday_thursday_counts_as_its_friday(self):
        row = _row(price=_itm(0.25), bid=12, ask=12.4, mark=12.2)
        c = FakeClient([(date(2026, 12, 24), 50.0, 12.5, 12.9, 12.7)])
        t = qs._roll_target(c, row, 4, FRI)
        self.assertEqual(t["expiry"], "2026-12-24")
        self.assertEqual(t["weeks_added"], 4)
        self.assertFalse(t["past_cap"])

    def test_mid_week_old_expiry_counts_whole_weeks(self):
        row = _row(price=_itm(0.12), expiry=WED_NEXT, bid=6, ask=6.2, mark=6.1)
        c = FakeClient([(FRI + timedelta(weeks=2), 50.0, 6.5, 6.7, 6.6)])
        t = qs._roll_target(c, row, 2, FRI)
        self.assertEqual(t["weeks_added"], 1)
        self.assertEqual(t["min_credit"], 0.15)

    def test_strike_missing_at_band_falls_to_next_weekly(self):
        row = _row(price=_itm(0.12), bid=6, ask=6.2, mark=6.1)
        c = FakeClient([(FRI + timedelta(weeks=2), 55.0, 9, 9.2, 9.1),    # other strike
                        (FRI + timedelta(weeks=3), 50.0, 6.8, 7.0, 6.9)])
        t = qs._roll_target(c, row, 2, FRI)
        self.assertEqual(t["expiry"], (FRI + timedelta(weeks=3)).isoformat())
        self.assertEqual(t["weeks_added"], 3)
        self.assertFalse(t["past_cap"])

    def test_fallback_past_the_cap_is_flagged(self):
        row = _row(price=_itm(0.25), bid=12, ask=12.4, mark=12.2)
        c = FakeClient([(FRI + timedelta(weeks=5), 50.0, 13, 13.4, 13.2)])
        t = qs._roll_target(c, row, 4, FRI)
        self.assertTrue(t["past_cap"])
        self.assertEqual(t["weeks_added"], 5)

    def test_no_search_beyond_the_next_weekly(self):
        # A monthly 3 weeks past the cap is not a roll target.
        row = _row(price=_itm(0.25), bid=12, ask=12.4, mark=12.2)
        c = FakeClient([(date(2027, 1, 15), 50.0, 13, 13.4, 13.2)])
        self.assertIn("error", qs._roll_target(c, row, 4, FRI))
        _, _, frm, to = c.calls[0]
        self.assertEqual((frm, to), (date(2026, 12, 24), date(2027, 1, 1)))

    def test_chain_failure_is_recorded_auth_failure_raises(self):
        row = _row(price=_itm(0.07), bid=3, ask=3.2, mark=3.1)
        t = qs._roll_target(FakeClient(error=RuntimeError("503 boom")), row, 1, FRI)
        self.assertIn("503 boom", t["error"])
        with self.assertRaises(RuntimeError):
            qs._roll_target(FakeClient(error=RuntimeError("invalid_grant")), row, 1, FRI)


class AddRollTargets(unittest.TestCase):
    def test_line_held_in_several_accounts_costs_one_chain_call(self):
        price = _itm(0.07)
        rows = [_row(price=price, bid=3.3, ask=3.5, mark=3.4, account=a)
                for a in ("IRA", "LSO", "Michelle LSO")]
        rows.append(_row(strike=40.0, price=price, account="LSO"))      # OTM, no target
        c = FakeClient([(FRI + timedelta(7), 50.0, 3.6, 3.8, 3.7)])
        qs.add_roll_targets(c, rows, FRI)
        self.assertEqual(len(c.calls), 1)
        self.assertEqual(rows[0]["roll_target"], rows[2]["roll_target"])
        self.assertEqual(rows[0]["roll_target"]["credit_est"], 0.2)     # 3.60 - 3.40
        self.assertNotIn("roll_target", rows[3])


if __name__ == "__main__":
    unittest.main()
