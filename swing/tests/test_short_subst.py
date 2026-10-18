"""Short 주식 대용 담보(2026-10-18, SHORT_COLLATERAL=subst) — 엔진 오프라인."""
import unittest

from swing import config
from swing import engine as E


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


class TestShortSubst(unittest.TestCase):
    def setUp(self):
        self._cfg = (config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN)
        config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN = "subst", 0.7, 1.0

    def tearDown(self):
        config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN = self._cfg

    def _long_full(self, cap=1_000_000):
        """현금 거의 전부를 Long 으로(평가 100만 중 Long 95만, 현금 5만)."""
        L = E.new_ledger(cap, start="2026-09-01")
        E.place_entry(L, code="000010", name="L", date="2026-09-01", valid_date="2026-09-02",
                      entry=10000, stop=9000, target=12000, weight=0.95)
        E.settle_day(L, "2026-09-02", {"000010": bar(10000, 10100, 9950, 10000)})
        self.assertEqual(L["positions"][0]["qty"], 95)
        return L

    def test_short_fills_on_substitute_when_cash_short(self):
        L = self._long_full()
        cash0 = L["cash"]                                            # 50,000
        E.place_entry(L, code="000001", name="S", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=10500, target=9400, weight=0.3, side="short")
        E.settle_day(L, "2026-09-03", {"000010": bar(10000, 10000, 10000, 10000),
                                       "000001": bar(9900, 10100, 9850, 10000)})
        s = next(p for p in L["positions"] if p["side"] == "short")
        self.assertEqual((s["qty"], s["collateral"]), (30, "subst"))  # 현금 5만으로는 불가 → 대용(95만 × 70%)으로 체결
        tax = 300_000 * config.SELL_TAX
        fee = 300_000 * config.BORROW_RATE / 365
        self.assertAlmostEqual(L["cash"], cash0 - tax - fee)          # 매도세·대차료만 현금 유출
        c = E.collateral(L)
        self.assertAlmostEqual(c["req"], 300_000)
        self.assertAlmostEqual(c["pledged"], 0)                      # 대용가 66.5만 ≥ 요구 30만
        eq = L["equity"][-1]["equity"]
        self.assertAlmostEqual(eq, round(L["cash"] + 950_000 + 0))     # 숏 평가 = (진입 − 현재) × 수량 = 0

    def test_cover_realizes_gain_only(self):
        L = self._long_full()
        E.place_entry(L, code="000001", name="S", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=10500, target=9400, weight=0.3, side="short")
        E.settle_day(L, "2026-09-03", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(9900, 10100, 9850, 10000)})
        cash1 = L["cash"]
        E.schedule_sell(L, "000001", "test")
        E.settle_day(L, "2026-09-04", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(9500, 9600, 9400, 9500)})
        t = L["trades"][-1]
        self.assertEqual(t["side"], "short")
        self.assertAlmostEqual(L["cash"], cash1 + 30 * 500)           # 환매 = 차익만 현금(담보 반환 없음), 시가 환매일 대차료 없음

    def test_partial_when_room_short(self):
        L = self._long_full()                                        # 여력 = 5만 + 66.5만 = 71.5만
        E.place_entry(L, code="000001", name="S", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=10500, target=9400, weight=1.0, side="short")   # 100주 = 100만 요구
        E.settle_day(L, "2026-09-03", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(9900, 10100, 9850, 10000)})
        s = next(p for p in L["positions"] if p["side"] == "short")
        self.assertEqual(s["qty"], 71)                               # floor(71.5만 / (1만 × 1.002))
        o = next(x for x in L["orderLog"] if x["code"] == "000001")
        self.assertEqual(o["partial"]["planned"], 100)

    def test_long_buy_cannot_use_pledged_cash(self):
        L = E.new_ledger(1_000_000, start="2026-09-01")
        E.place_entry(L, code="000001", name="S", date="2026-09-01", valid_date="2026-09-02",
                      entry=10000, stop=10500, target=9400, weight=0.5, side="short")      # Long 없음 → 현금이 담보
        E.settle_day(L, "2026-09-02", {"000001": bar(9900, 10100, 9850, 10000)})
        c = E.collateral(L)
        self.assertAlmostEqual(c["pledged"], 500_000)
        E.place_entry(L, code="000010", name="L", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=9000, target=12000, weight=0.9)                    # 90만 매수 시도
        E.settle_day(L, "2026-09-03", {"000001": bar(10000, 10000, 10000, 10000), "000010": bar(10000, 10100, 9950, 10000)})
        o = next(x for x in L["orderLog"] if x["code"] == "000010")
        self.assertIn(o["status"], ("filled", "skipped"))
        if o["status"] == "filled":                                  # 부분 진입이면 묶인 현금(대용 반영)을 넘지 않음
            self.assertLessEqual(o["qty"] * 10000, 1_000_000 - 500_000 + 1 + o["qty"] * 10000 * 0.7)
        self.assertGreaterEqual(E.collateral(L)["room"], 0)

    def test_margin_call_schedules_cover(self):
        L = self._long_full()
        E.place_entry(L, code="000001", name="S", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=99999, target=9400, weight=0.6, side="short")
        E.settle_day(L, "2026-09-03", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(9900, 10100, 9850, 10000)})
        # Long 급락 + Short 급등 → 대용가 < 요구 담보
        ev = E.settle_day(L, "2026-09-04", {"000010": bar(9100, 9100, 9050, 9100), "000001": bar(12000, 13000, 12000, 13000)})
        s = next(p for p in L["positions"] if p["side"] == "short")
        self.assertEqual((s["sellPending"] or {}).get("kind"), "margin")
        self.assertTrue(ev.get("marginCall"))

    def test_cash_mode_unchanged(self):
        config.SHORT_COLLATERAL = "cash"
        L = E.new_ledger(1_000_000, start="2026-09-01")
        E.place_entry(L, code="000001", name="S", date="2026-09-01", valid_date="2026-09-02",
                      entry=10000, stop=10500, target=9400, weight=0.1, side="short")
        E.settle_day(L, "2026-09-02", {"000001": bar(9900, 10100, 9850, 10000)})
        self.assertEqual(L["positions"][0]["collateral"], "cash")
        self.assertLess(L["cash"], 1_000_000 - 100_000 + 1)


if __name__ == "__main__":
    unittest.main()
