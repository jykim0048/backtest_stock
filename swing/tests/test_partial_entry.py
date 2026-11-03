"""현금 부족 시 부분 진입(최소 50%) + 잔여분 추가 주문(2026-10-16, A+B). 오프라인."""
import tempfile
import unittest

from swing import config, daily, engine as E, prices, store
from swing.tests.test_daily import FixedAgent, b, wb


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


class TestEnginePartial(unittest.TestCase):
    def _ledger(self, cash):
        L = E.new_ledger(1_000_000, start="2026-09-01")
        E.place_entry(L, code="000001", name="A", date="2026-09-01", valid_date="2026-09-02",
                      entry=10000, stop=9500, target=10600, weight=1.0, qty=100)
        L["cash"] = cash
        return L

    def test_partial_fill_when_at_least_half(self):
        L = self._ledger(600_000)
        ev = E.settle_day(L, "2026-09-02", {"000001": bar(10000, 10100, 9900, 10050)})
        p = L["positions"][0]
        self.assertEqual(p["qty"], 60)
        o = ev["filled"][0]
        self.assertEqual(o["partial"], {"planned": 100, "filled": 60, "remain": 40, "positionId": p["id"]})
        self.assertIn("부분 진입", o["note"])
        self.assertEqual(len(ev["partials"]), 1)
        self.assertAlmostEqual(L["cash"], 0)

    def test_skip_when_below_half(self):
        L = self._ledger(400_000)
        ev = E.settle_day(L, "2026-09-02", {"000001": bar(10000, 10100, 9900, 10050)})
        self.assertEqual(L["positions"], [])
        self.assertIn("최소 50%", ev["skipped"][0]["note"])

    def test_off(self):
        old = config.PARTIAL_ENTRY
        config.PARTIAL_ENTRY = False
        try:
            L = self._ledger(600_000)
            ev = E.settle_day(L, "2026-09-02", {"000001": bar(10000, 10100, 9900, 10050)})
            self.assertEqual(L["positions"], [])
            self.assertEqual(ev["skipped"][0]["status"], "skipped")
        finally:
            config.PARTIAL_ENTRY = old

    def test_topup_merges_with_average_price(self):
        L = self._ledger(600_000)
        ev = E.settle_day(L, "2026-09-02", {"000001": bar(10000, 10100, 9900, 10050)})
        pid = L["positions"][0]["id"]
        L["cash"] += 500_000
        E.place_entry(L, code="000001", name="A", date="2026-09-02", valid_date="2026-09-03", entry=10000, stop=9500,
                      target=10600, weight=1.0, qty=40, extra={"topupOf": pid, "topup": 1})
        E.settle_day(L, "2026-09-03", {"000001": bar(10050, 10100, 9950, 10000)})
        p = L["positions"][0]
        self.assertEqual((p["qty"], p["entry"], len(p["topups"])), (100, 10000, 1))


class TestDailyTopup(unittest.TestCase):
    def test_partial_then_topup_next_day(self):
        st = store.FileStore(tempfile.mkdtemp())
        ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600)})
        px = prices.DictPrices({
            "2026-09-14": {"000010": b(10000, 10100, 9900, 10050)},
            "2026-09-15": {"000010": b(10000, 10100, 9900, 10050)},
            "2026-09-16": {"000010": b(10050, 10100, 9950, 10000)},
        })
        daily.run_day("2026-09-14", st, ag, px, wb("2026-09-14", buy=[("000010", "화학")]), log=lambda *a: None)
        L = st.get("ledger")
        o = L["orders"][0]
        L["cash"] = o["qty"] * o["entry"] * 0.6                     # 계획의 60% 만 현금
        st.put("ledger", L)
        r2 = daily.run_day("2026-09-15", st, ag, px, wb("2026-09-15"), log=lambda *a: None)
        self.assertEqual(len(r2["topups"]), 1)
        self.assertTrue(r2["topups"][0]["ok"])
        L = st.get("ledger")
        planned = o["qty"]
        self.assertEqual(L["positions"][0]["qty"], int(planned * 0.6))
        L["cash"] += planned * 10000                                # 다음 날 현금이 풀렸다고 가정
        st.put("ledger", L)
        daily.run_day("2026-09-16", st, ag, px, wb("2026-09-16"), log=lambda *a: None)
        p = st.get("ledger")["positions"][0]
        self.assertEqual(p["qty"], planned)
        self.assertEqual(len(p["topups"]), 1)


class TestPmWeightFill(unittest.TestCase):
    """PM 비중 채우기(2026-11-03) — 횟수 제한·종가 밴드 없이, 최근 PM 판단이 매수·보유 쪽이면 계획 수량까지 매일 추가 주문."""
    K = "decision/2026-10-19/000010/entry"

    def ledger(self, **pos):
        L = E.new_ledger(100_000_000, start="2026-10-19")
        L["orderLog"].append({"id": "o1", "code": "000010", "name": "A", "side": "long", "type": "entry", "created": "2026-10-19", "validDate": "2026-10-20",
                              "entry": 10000.0, "stop": 9500.0, "target": 11000.0, "weight": 0.1, "qty": 100, "status": "filled", "decisionKey": self.K,
                              "rating": "Overweight", "source": "matrix", "partial": {"planned": 1000, "filled": 100, "remain": 900, "positionId": "p1"}})
        L["positions"].append(dict({"id": "p1", "code": "000010", "name": "A", "side": "long", "qty": 300, "entry": 10200.0, "stop": 9500.0,
                                    "target": 11000.0, "fillDate": "2026-10-20", "holdDay": 4, "decisionKey": self.K, "rating": "Overweight",
                                    "topups": [{"qty": 100}, {"qty": 100}], "sellPending": None}, **pos))
        return L

    def topups(self, L, close=10600.0):
        return daily._topups(L, {"000010": bar(close, close, close, close)}, "2026-10-23", "2026-10-26")

    def test_no_count_limit_and_no_price_band(self):
        L = self.ledger()
        r = self.topups(L, close=10600.0)                    # 진입가 +6% — 종전 ±3% 밴드면 막혔다
        self.assertTrue(r[0]["ok"])
        o = L["orders"][-1]
        self.assertEqual((o["qty"], o["entry"], o["topupOf"], o["topup"]), (700, 10000.0, "p1", 3))

    def test_hold_review_keeps_filling(self):
        r = self.topups(self.ledger(lastRating="Hold"))
        self.assertTrue(r[0]["ok"])

    def test_underweight_review_stops(self):
        r = self.topups(self.ledger(lastRating="Underweight"))
        self.assertFalse(r[0]["ok"])

    def test_stops_after_any_exit_in_round(self):
        L = self.ledger()
        L["trades"].append({"code": "000010", "decisionKey": self.K, "reason": "switch"})
        self.assertFalse(self.topups(L)[0]["ok"])

    def test_stops_near_max_hold(self):
        r = self.topups(self.ledger(holdDay=config.MAX_HOLD_DAYS - config.TOPUP_STOP_BEFORE))
        self.assertFalse(r[0]["ok"])

    def test_close_below_stop_stops(self):
        self.assertFalse(self.topups(self.ledger(), close=9400.0)[0]["ok"])

    def test_filled_position_has_no_row(self):
        self.assertEqual(self.topups(self.ledger(qty=950)), [])     # 남은 50주 < 계획의 10%


if __name__ == "__main__":
    unittest.main()
