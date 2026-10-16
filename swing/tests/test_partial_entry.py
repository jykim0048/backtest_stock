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


if __name__ == "__main__":
    unittest.main()
