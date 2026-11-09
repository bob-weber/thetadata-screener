"""Regression tests for the portfolio tracker (Positions, P&L Chart, Weekly RoR).

Run:  gui-env/bin/python -m unittest discover tests

Everything runs on a made-up TOS statement written to a temporary directory,
which is also the working directory for the duration — the account files the
app reads and writes are resolved relative to it, so the real ones are never
touched. Each case here is a bug that shipped once.
"""
import csv
import io
import os
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox   # noqa: E402

from gui import gains_tab as g                                         # noqa: E402
from gui import positions_tab as p                                     # noqa: E402
from gui.sharebook import ShareBook                                    # noqa: E402

ACCT = "11111111SCHW (TEST)"
OPENING = 10_000.00
DEPOSIT = 5_000.00          # arrives with no row of its own, as TOS exports do

# (date, type, description, misc fee, commission, amount or None, cash moved in
#  from outside just before this row). Balances are computed from these.
ROWS = [
    ("1/5/26",  "TRD", "SOLD -2 XYZ 100 (Weeklys) 9 JAN 26 12.5 PUT @.50 CBOE",              -0.02, -1.30, 100.00, 0),
    ("1/8/26",  "TRD", "SOLD -2 DIAGONAL XYZ 100 (Weeklys) 16 JAN 26/9 JAN 26 13/12.5 PUT @.20 CBOE", -0.04, 0, 40.00, 0),
    ("1/12/26", "TRD", "BOT +10 TUV @20.00",                                                  0, 0, -200.00, 0),
    ("1/16/26", "EXP", "BOT 200.0 XYZ UPON XYZ CORP",                                         0, 0, -2600.00, DEPOSIT),
    # Identical fills, different fees: only the second pays the commission.
    ("1/20/26", "TRD", "SOLD -1 XYZ 100 (Weeklys) 30 JAN 26 14 CALL @.30 CBOE",               -0.01, 0, 30.00, 0),
    ("1/20/26", "TRD", "SOLD -1 XYZ 100 (Weeklys) 30 JAN 26 14 CALL @.30 CBOE",               -0.01, -0.65, 30.00, 0),
    # A roll filled at $0.00: blank AMOUNT, fees still paid.
    ("1/22/26", "TRD", "SOLD -1 CALENDAR XYZ 100 (Weeklys) 6 FEB 26/30 JAN 26 14 CALL @.00 CBOE", -0.03, 0, None, 0),
    # A vertical used as a roll: buys back the 14 call, sells the 14.5.
    ("2/2/26",  "TRD", "BOT +1 VERTICAL XYZ 100 (Weeklys) 6 FEB 26 14/14.5 CALL @.10 CBOE",   -0.02, 0, -10.00, 0),
    ("2/10/26", "TRD", "BOT +100 ABC @50.00",                                                 0, 0, -5000.00, 0),
    ("2/11/26", "TRD", "SOLD -1 ABC 100 (Weeklys) 20 MAR 26 52 CALL @1.00 CBOE",              -0.01, 0, 100.00, 0),
    ("2/15/26", "TRD", "BOT +1 LMN 100 19 JAN 29 20 CALL @3.00 CBOE",                         -0.01, -0.65, -300.00, 0),
    # Called away: the share count is signed on the statement.
    ("3/20/26", "EXP", "SOLD -100.0 ABC UPON ABC INC",                                        -0.26, 0, 5200.00, 0),
]
# Holdings on the statement date: XYZ assigned; QRS moved in from another
# account (never bought here); TUV moved out (bought here, no longer listed);
# ABC called away.
EQUITIES = [("XYZ", "XYZ CORP", "+200", "13.00"), ("QRS", "QRS INC", "+50", "10.00")]


def _statement() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f"Account Statement for {ACCT} since 1/1/26 through 3/31/26"])
    w.writerow([])
    w.writerow(["Cash Balance"])
    w.writerow(["DATE", "TIME", "TYPE", "REF #", "DESCRIPTION", "Misc Fees",
                "Commissions & Fees", "AMOUNT", "BALANCE"])
    bal = OPENING
    for i, (d, typ, desc, misc, comm, amt, ext) in enumerate(ROWS):
        bal += ext + (amt or 0) + misc + comm
        w.writerow([d, "10:00:00", typ, f'="{1000 + i}"', desc,
                    f"{misc:.2f}" if misc else "", f"{comm:.2f}" if comm else "",
                    f"{amt:.2f}" if amt is not None else "", f"{bal:,.2f}"])
    w.writerow(["", "", "", "", "TOTAL", "", "", "", f"{bal:,.2f}"])
    w.writerow([])
    w.writerow(["Equities"])
    w.writerow(["Symbol", "Description", "Qty", "Trade Price"])
    for row in EQUITIES:
        w.writerow(row)
    w.writerow(["", "OVERALL TOTALS", "", ""])
    w.writerow([])
    return out.getvalue()


def _fees(rows=ROWS) -> float:
    return round(sum(misc + comm for _, _, _, misc, comm, _, _ in rows), 2)


class _InTempDir(unittest.TestCase):
    """Run each test from a fresh directory holding the made-up statement."""

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self._cwd = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="lso-portfolio-test-")
        os.chdir(self.tmp)
        self.stmt = Path(self.tmp) / "statement.csv"
        self.stmt.write_text(_statement())

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self):
        return list(csv.reader(io.StringIO(self.stmt.read_text())))

    def _positions(self):
        """Positions rows as the app builds them, by symbol and status."""
        rows = self._rows()
        stks = p._parse_equities(rows)
        holdings = {"asof": p._parse_statement_end(rows).strftime("%m/%d/%Y"),
                    "equities": {h["symbol"]: {"shares": float(h["shares"]),
                                               "price": float(h["cost_basis"])}
                                 for h in stks}}
        events = p._collect_cash_events(rows)
        return p._reconstruct_wheels(events, p._build_rad_outcomes(rows), holdings)

    def _import_positions(self):
        """Import through the Positions tab, as the button does."""
        p.PortfolioTab._refresh_market_prices = lambda self: None   # no network
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (str(self.stmt), ""))
        QMessageBox.warning = staticmethod(lambda *a, **k: None)
        tab = p.PortfolioTab()
        tab.import_csv()
        return tab


class StatementParsing(_InTempDir):
    def setUp(self):
        super().setUp()
        self.parsed = g.parse_statement(self.stmt)

    def test_reads_every_trade(self):
        # d51b832 chained the trade branches onto the balance capture and
        # read none at all.
        self.assertEqual(len(self.parsed["opt_trades"]), 8)
        self.assertEqual(len(self.parsed["stk_trades"]), 4)

    def test_fees_come_from_their_own_columns(self):
        trades = self.parsed["opt_trades"] + self.parsed["stk_trades"]
        self.assertAlmostEqual(sum(t["fees"] for t in trades), _fees(), places=2)

    def test_zero_dollar_fill_kept_for_its_fees(self):
        zero = [t for t in self.parsed["opt_trades"] if "@.00" in t["desc"]]
        self.assertEqual(len(zero), 1)
        self.assertEqual(zero[0]["amount"], 0.0)
        self.assertAlmostEqual(zero[0]["fees"], -0.03)

    def test_called_away_sale_is_parsed_despite_its_sign(self):
        sold = [t for t in self.parsed["stk_trades"] if t["action"] == "SOLD"]
        self.assertEqual([(t["symbol"], t["shares"]) for t in sold], [("ABC", 100.0)])

    def test_opening_balance_and_daily_balances(self):
        self.assertEqual(self.parsed["opening"], {"date": "2026-01-05", "balance": OPENING})
        last = self.parsed["balances"][-1]
        expected = OPENING + DEPOSIT + sum((a or 0) + m + c for _, _, _, m, c, a, _ in ROWS)
        self.assertEqual(last["date"], "2026-03-20")
        self.assertAlmostEqual(last["balance"], expected, places=2)

    def test_reimport_adds_nothing_and_keeps_each_fills_fees(self):
        data = g._blank_history()
        g.merge_statement(data, self.parsed)
        added = g.merge_statement(data, g.parse_statement(self.stmt))
        self.assertEqual(added["opt_trades"] + added["stk_trades"], 0)
        twins = sorted(t["fees"] for t in data["opt_trades"] if "30 JAN 26 14 CALL @.30" in t["desc"])
        self.assertEqual(twins, [-0.66, -0.01])

    def test_reimport_repairs_twins_stored_with_the_first_fills_fees(self):
        data = g._blank_history()
        g.merge_statement(data, self.parsed)
        for t in data["opt_trades"]:
            if "30 JAN 26 14 CALL @.30" in t["desc"]:
                t["fees"] = -0.01                      # how the old merge stored them
        g.merge_statement(data, g.parse_statement(self.stmt))
        twins = sorted(t["fees"] for t in data["opt_trades"] if "30 JAN 26 14 CALL @.30" in t["desc"])
        self.assertEqual(twins, [-0.66, -0.01])

    def test_vertical_reads_as_a_roll_to_the_new_strike(self):
        leg = g._parse_opt_leg("BOT +1 VERTICAL XYZ 100 (Weeklys) 6 FEB 26 14/14.5 CALL @.10 CBOE")
        self.assertEqual((leg["action"], leg["strike"], leg["close_strike"], leg["is_roll"]),
                         ("SOLD", 14.5, 14.0, True))


class PnlChart(_InTempDir):
    def setUp(self):
        super().setUp()
        self._import_positions()                  # saves holdings for transfers
        self.data = g._blank_history()
        g.merge_statement(self.data, g.parse_statement(self.stmt))
        self.view = g.prepared(self.data, ACCT)
        self.series = g.reconstruct(self.view, with_unrealized=False)

    def test_realized_includes_fees_and_the_called_away_sale(self):
        options = sum((a or 0) + m + c for _, _, d, m, c, a, _ in ROWS if " 100 " in d)
        stock = 5200.00 - 0.26 - 5000.00            # ABC: sold − fee − cost
        self.assertAlmostEqual(self.series["realized"][-1], options + stock, places=2)
        self.assertAlmostEqual(self.series["fees"][-1], -_fees(), places=2)

    def test_capital_is_opening_plus_unlisted_deposit_plus_transfers_at_cost(self):
        moved = 50 * 10.00 - 10 * 20.00             # QRS in, TUV out
        self.assertAlmostEqual(self.series["deposits"][-1], OPENING + DEPOSIT + moved, places=2)

    def test_transfers_move_shares_without_realizing(self):
        by_day = g._stock_realized_by_date(self.view)
        self.assertEqual({str(d): round(v, 2) for d, v in by_day.items()},
                         {"2026-03-20": 199.74, "2026-01-12": 0.0})

    def test_weekly_relative_range_counts_back_from_the_last_trade(self):
        wk = g._weekly_allocated_ror(self.view, 7, None, None)
        self.assertLessEqual(wk["weeks"][-1].date(), date(2026, 3, 20))

    def test_range_figures_count_from_the_start_of_the_range(self):
        tab = g.GainsTab()
        tab._range_sel, tab._range_from, tab._range_to = "custom", "2026-02-01", "2026-03-31"
        win = tab._filtered_series(self.series)
        i = self.series["dates"].index(win["dates"][0])
        for k in ("realized", "total", "fees"):
            self.assertAlmostEqual(win[k][-1], self.series[k][-1] - self.series[k][i - 1], places=2)
        self.assertEqual(win["deposits"][-1], self.series["deposits"][-1])


class Positions(_InTempDir):
    def setUp(self):
        super().setUp()
        self.rows = self._positions()

    def _one(self, sym, status=None):
        hits = [r for r in self.rows if r["symbol"] == sym and r["status"] != "Blended"
                and (status is None or r["status"] == status)]
        self.assertEqual(len(hits), 1, [(r["symbol"], r["status"]) for r in hits])
        return hits[0]

    def test_diagonal_roll_then_assignment_carries_all_put_premium(self):
        r = self._one("XYZ")
        self.assertEqual(r["opened"], "01/05/2026")            # the 12.5 put, not the roll
        self.assertEqual(r["assn_price"], "13.00")
        put_net = (100.00 - 1.32) + (40.00 - 0.04)
        self.assertEqual(r["cost_basis"], f"{13 - put_net / 200:.2f}")

    def test_recovery_subtracts_call_premium_and_expired_calls_read_as_such(self):
        r = self._one("XYZ")
        call_net = (30 - 0.01) + (30 - 0.66) + (0 - 0.03) + (-10 - 0.02)
        basis = 13 - ((100.00 - 1.32) + (40.00 - 0.04)) / 200
        self.assertEqual(r["recovery"], f"{basis - call_net / 200:.2f}")
        self.assertEqual(r["expiration"], "uncovered")
        self.assertIn("Call expired", r["lifecycle"])
        self.assertEqual((r["qty"], r["unit"]), ("200", "shares"))

    def test_long_call_is_its_own_row(self):
        r = self._one("LMN")
        self.assertEqual((r["status"], r["strike"], r["qty"], r["unit"]), ("Open", "20", "1", "contract"))
        self.assertEqual(r["cost_basis"], "3.01")              # (300 + 0.66) / 100
        self.assertEqual(r["recovery"], "23.01")

    def test_transferred_in_and_out(self):
        self.assertIn("Transferred in", self._one("QRS")["lifecycle"])
        moved = self._one("TUV")
        self.assertEqual((moved["status"], moved["cost_basis"]), ("Transferred", "+0.00"))

    def test_called_away_outright_purchase(self):
        r = self._one("ABC")
        self.assertEqual(r["status"], "Called away")
        self.assertEqual(r["cost_basis"], f"{100 - 0.01 + 5200 - 0.26 - 5000:+,.2f}")

    def test_import_keeps_each_fills_fees_and_reimport_adds_nothing(self):
        tab = self._import_positions()
        n = len(tab._events)
        twins = sorted(e["fees"] for e in tab._events
                       if e["kind"] == "opt" and e["otype"] == "CALL" and e["exp"] == "01/30/2026"
                       and not e.get("roll"))
        self.assertEqual(twins, [0.01, 0.66])
        tab.import_csv()
        self.assertEqual(len(tab._events), n)

    def test_long_call_exercise_becomes_the_shares(self):
        ev = [{"sym": "LMN", "date": "02/15/2026", "kind": "opt", "otype": "CALL",
               "amount": -300.0, "fees": 0.66, "strike": "20", "exp": "01/19/2029",
               "qty": 1, "action": "BOT", "roll": False},
              {"sym": "LMN", "date": "03/01/2026", "kind": "stock", "action": "BOT",
               "shares": 100.0, "amount": -2000.0, "fees": 0.0, "assigned": True}]
        hold = {"asof": "03/31/2026", "equities": {"LMN": {"shares": 100, "price": 20}}}
        rows = p._reconstruct_wheels(ev, {}, hold)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["lifecycle"], "Long call → Exercised → Holding")
        self.assertEqual((r["assn_price"], r["cost_basis"]), ("20.00", "23.01"))

    def test_partial_diagonal_leaves_the_rest_at_the_old_strike(self):
        ev = [{"sym": "X", "date": "06/01/2026", "kind": "opt", "otype": "PUT",
               "amount": 400.0, "fees": 0.0, "strike": "130", "exp": "12/18/2026",
               "qty": 4, "action": "SOLD", "roll": False},
              {"sym": "X", "date": "06/05/2026", "kind": "opt", "otype": "PUT",
               "amount": 50.0, "fees": 0.0, "strike": "140", "exp": "12/31/2027",
               "qty": 2, "action": "SOLD", "roll": True, "close_strike": "130",
               "close_exp": "12/18/2026"}]
        live = {r["strike"]: r["qty"] for r in p._reconstruct_wheels(ev, {})
                if r["status"] == "Open"}
        self.assertEqual(live, {"130": "2", "140": "2"})

    def test_sort_key_orders_dates_across_a_year_end(self):
        a = p._row_sort_key({"status": "Closed", "symbol": "X", "opened": "12/15/2026"})
        b = p._row_sort_key({"status": "Closed", "symbol": "X", "opened": "01/05/2027"})
        self.assertLess(a, b)

    def test_grouped_sort_keeps_a_tickers_rows_together(self):
        live = [r for r in self.rows if r["status"] in ("Open", "Holding", "Blended")]
        for field in ("expiration", "capital", "premium", "symbol"):
            for desc in (False, True):
                syms = [r["symbol"] for grp in p._grouped_order(live, field, desc, {}) for r in grp]
                runs = [s for i, s in enumerate(syms) if i == 0 or syms[i - 1] != s]
                self.assertEqual(len(runs), len(set(runs)), (field, desc, syms))


def _put(d, q, exp, amt, strike="50", action="SOLD", roll=False, close_exp=None):
    e = {"sym": "Q", "date": d, "kind": "opt", "otype": "PUT", "amount": amt,
         "fees": 0.0, "strike": strike, "exp": exp, "qty": q, "action": action,
         "roll": roll}
    if close_exp:
        e["close_exp"] = close_exp
    return e


class PutQuantities(unittest.TestCase):
    """Contract counts the Positions tab must get right without a statement row."""

    def _open(self, events):
        return [(r["opened"], r["qty"], r["_capital"]) for r in p._reconstruct_wheels(events, {})
                if r["status"] == "Open"]

    def test_expired_position_is_not_reopened_by_a_later_sale(self):
        # IRA RIOT 18: August's lot expired (no row); October's sale is new.
        rows = p._reconstruct_wheels([_put("08/17/2026", 44, "08/21/2026", 836.0),
                                      _put("10/05/2026", 21, "12/31/2099", 400.0)], {})
        by_status = {r["status"]: (r["opened"], r["premium"]) for r in rows}
        self.assertEqual(by_status["Open"], ("10/05/2026", "400.00"))
        self.assertEqual(by_status["Expired"], ("08/17/2026", "836.00"))

    def test_partial_calendar_roll_keeps_the_contracts_left_behind(self):
        ev = [_put("12/01/2099", 10, "12/11/2099", 500.0),
              _put("12/03/2099", 5, "12/18/2099", 50.0, roll=True, close_exp="12/11/2099")]
        self.assertEqual(self._open(ev), [("12/01/2099", "10", 50000.0)])
        ev.append(_put("12/04/2099", 5, "12/11/2099", -100.0, action="BOT"))
        self.assertEqual(self._open(ev), [("12/01/2099", "5", 25000.0)])

    def test_contracts_at_two_expirations_all_count(self):
        ev = [_put("12/01/2099", 10, "12/11/2099", 500.0), _put("12/02/2099", 5, "12/18/2099", 300.0)]
        self.assertEqual(self._open(ev), [("12/01/2099", "15", 75000.0)])

    def test_second_assignment_at_a_strike_goes_to_its_own_put(self):
        stock = lambda d: {"sym": "Q", "date": d, "kind": "stock", "action": "BOT",
                           "shares": 100.0, "amount": -5000.0, "fees": 0.0, "assigned": True}
        rows = p._reconstruct_wheels([_put("01/05/2026", 1, "01/09/2026", 100.0), stock("01/10/2026"),
                                      _put("01/12/2026", 1, "01/23/2026", 120.0), stock("01/24/2026")], {})
        self.assertEqual([(r["status"], r["qty"], r["premium"]) for r in rows],
                         [("Holding", "200", "220.00")])

    def test_held_shares_show_the_calls_strike_not_the_puts(self):
        # LSO CHWY: assigned on the 22 put, covered by a call rolled to 20.
        stock = {"sym": "Q", "date": "01/10/2026", "kind": "stock", "action": "BOT",
                 "shares": 100.0, "amount": -2200.0, "fees": 0.0, "assigned": True}
        call = {**_put("01/12/2026", 1, "12/31/2099", 50.0, strike="20"), "otype": "CALL"}
        rows = p._reconstruct_wheels([_put("01/05/2026", 1, "01/09/2026", 100.0, strike="22"),
                                      stock, call], {})
        self.assertEqual([(r["status"], r["strike"], r["expiration"], r["assn_price"])
                          for r in rows], [("Holding", "20", "12/31/2099", "22.00")])


def _call(d, q, exp, amt, strike, action="SOLD", close_strike=None, close_exp=None):
    e = {**_put(d, q, exp, amt, strike=strike, action=action,
                roll=bool(close_exp), close_exp=close_exp), "otype": "CALL"}
    if close_strike:
        e["close_strike"] = close_strike
    return e


def _open_call_legs(events) -> dict[tuple, int]:
    """Open calls per (strike, exp), worked out from the events alone — not
    through the cycle routing under test, so the two can disagree."""
    legs: dict[tuple, int] = {}
    for e in events:
        if e.get("otype") != "CALL":
            continue
        if e.get("roll") and e.get("close_exp"):
            old = (e.get("close_strike") or e["strike"], e["close_exp"])
            legs[old] = legs.get(old, 0) - e["qty"]
        leg = (e["strike"], e["exp"])
        legs[leg] = legs.get(leg, 0) + (e["qty"] if e["action"] == "SOLD" else -e["qty"])
    today = date.today()
    return {l: q for l, q in legs.items() if q > 0 and p._parse_mdy(l[1]) >= today}


class HeldSharesShowTheirCall(unittest.TestCase):
    """A Holding row's Strike and Expiration must name the same open call.

    Each column was right by its own definition while the row as a whole was
    wrong: Expiration moved to the covering call and Strike stayed on the
    assigned put, so LSO CHWY (22 put, call rolled to 20) read as a 22 call.
    A line-by-line review passed it. This checks the pair, not the columns.
    """

    SCENARIOS = {
        "one call": [_call("01/12/2026", 2, "12/17/2099", 60.0, "23")],
        "rolled to a new strike": [
            _call("01/12/2026", 2, "12/17/2099", 60.0, "23"),
            _call("01/20/2026", 2, "12/31/2099", 40.0, "20",
                  close_strike="23", close_exp="12/17/2099")],
        "two expirations": [_call("01/12/2026", 1, "12/31/2099", 30.0, "24"),
                            _call("01/12/2026", 1, "12/17/2099", 30.0, "23")],
        "two strikes, one expiration": [_call("01/12/2026", 1, "12/17/2099", 30.0, "21"),
                                        _call("01/12/2026", 1, "12/17/2099", 30.0, "20")],
        "bought back": [_call("01/12/2026", 2, "12/17/2099", 60.0, "23"),
                        _call("01/15/2026", 2, "12/17/2099", -20.0, "23", action="BOT")],
    }

    def test_strike_and_expiration_name_one_open_call(self):
        assigned = [_put("01/05/2026", 2, "01/09/2026", 100.0, strike="22"),
                    {"sym": "Q", "date": "01/10/2026", "kind": "stock", "action": "BOT",
                     "shares": 200.0, "amount": -4400.0, "fees": 0.0, "assigned": True}]
        for name, calls in self.SCENARIOS.items():
            with self.subTest(name):
                events = assigned + calls
                [row] = [r for r in p._reconstruct_wheels(events, {})
                         if r["status"] == "Holding"]
                legs = _open_call_legs(events)
                if not legs:
                    self.assertEqual(row["expiration"], "uncovered")
                    continue
                nearest = min(legs, key=lambda l: p._parse_mdy(l[1]))[1]
                strikes = sorted((k for k, x in legs if x == nearest), key=float)
                self.assertEqual((row["strike"], row["expiration"]),
                                 ("/".join(strikes), nearest))
                self.assertEqual(row["assn_price"], "22.00")


class Storage(_InTempDir):
    def test_vertical_and_its_split_legs_count_once(self):
        rows = list(csv.reader(io.StringIO(
            "Cash Balance\nDATE,TIME,TYPE,REF #,DESCRIPTION,Misc Fees,Commissions & Fees,AMOUNT,BALANCE\n"
            "6/22/26,10:00:00,TRD,=\"1\",BOT +3 VERTICAL DVN 100 (Weeklys) 26 JUN 26 43/43.5 CALL @.26 CBOE,-0.09,,-78.00,100\n"
            "6/22/26,10:00:00,TRD,=\"2\",BOT +3 DVN 100 (Weeklys) 26 JUN 26 43 CALL @1.40 CBOE,-0.05,,-420.00,100\n"
            "6/22/26,10:00:00,TRD,=\"3\",SOLD -3 DVN 100 (Weeklys) 26 JUN 26 43.5 CALL @1.14 CBOE,-0.04,,342.00,100\n")))
        events = p._collect_cash_events(rows)
        self.assertEqual(p._drop_split_roll_duplicates(events), 2)
        self.assertEqual(len(events), 1)

    def test_unreadable_account_file_is_kept_before_anything_overwrites_it(self):
        from gui.account_store import events_path
        path = events_path(ACCT)
        path.write_text('{"events": [ truncated')
        QMessageBox.warning = staticmethod(lambda *a, **k: None)
        tab = p.PortfolioTab()
        tab._read_events(ACCT)
        kept = list(Path(self.tmp).glob(path.name + ".unreadable-*"))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0].read_text(), '{"events": [ truncated')

    def test_saves_replace_the_file_whole(self):
        from gui.account_store import write_json
        target = Path(self.tmp) / "x.json"
        write_json(target, {"a": 1})
        self.assertEqual(target.read_text().replace(" ", "").replace("\n", ""), '{"a":1}')
        self.assertEqual(list(Path(self.tmp).glob("x.json.tmp")), [])

    def test_legacy_file_is_set_aside_not_deleted(self):
        from gui import account_store as a
        a.events_path("default").write_text('{"events": []}')
        a._LEGACY_EVENTS.write_text('{"events": [1]}')
        a.migrate_legacy()
        self.assertFalse(a._LEGACY_EVENTS.exists())
        self.assertEqual(len(list(Path(self.tmp).glob("my_option_events.json.migrated-*"))), 1)


class ShareBookTests(unittest.TestCase):
    def test_average_cost_realization(self):
        b = ShareBook()
        b.buy("A", 100, -1000)
        b.buy("A", 100, -2000)
        realized, removed = b.sell("A", 100, 1800)
        self.assertEqual((realized, removed), (300.0, 1500.0))
        self.assertEqual(b.held(), [("A", 100.0, 1500.0)])

    def test_transfer_realizes_nothing_even_without_basis(self):
        b = ShareBook()
        self.assertEqual(b.sell("A", 10, 500, transfer=True), (0.0, 0.0))
        self.assertEqual(b.sell("A", 10, 500), (500, 0.0))


if __name__ == "__main__":
    unittest.main()
