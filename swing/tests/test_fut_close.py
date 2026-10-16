"""주식선물 미상장 Short 정리(2026-10-16) — 그날 종가 환매, 재주문 안 함. 오프라인."""
import tempfile
import unittest

from swing import config, daily, engine as E, prices, store
from swing.tests.test_daily import FixedAgent, b, wb


class FutUni:
    def __init__(self, fut):
        self.fut = fut

    def fut_set(self, date):
        return self.fut

    def build_or_load(self, date, wb_, ledger):
        raise RuntimeError("no universe in test")

    def attach_scores(self, cands, side, date, source="matrix"):
        return cands


class TestFutClose(unittest.TestCase):
    def test_non_futures_short_closed_at_close(self):
        st = store.FileStore(tempfile.mkdtemp())
        L = E.new_ledger(10_000_000, start="2026-09-11")
        for code in ("000001", "000002"):                            # 000001 = 선물 미상장, 000002 = 상장
            E.place_entry(L, code=code, name=code, date="2026-09-11", valid_date="2026-09-14",
                          entry=10000, stop=10500, target=9000, weight=0.1, side="short")
        st.put("ledger", L)
        px = prices.DictPrices({"2026-09-14": {"000001": b(10000, 10100, 9900, 9950), "000002": b(10000, 10100, 9900, 9950)}})
        r = daily.run_day("2026-09-14", st, FixedAgent(), px, wb("2026-09-14"), uni=FutUni({"000002"}), log=lambda *a: None)
        L = st.get("ledger")
        self.assertEqual([p["code"] for p in L["positions"]], ["000002"])
        t = L["trades"][0]
        self.assertEqual((t["code"], t["reason"], t["exitPrice"]), ("000001", "fut_none", 9950))
        self.assertEqual(r["futClosed"], ["000001"])
        self.assertTrue(any("종가 환매 1종목" in n for n in r["notes"]))

    def test_no_retry_for_non_futures_short(self):
        st = store.FileStore(tempfile.mkdtemp())
        L = E.new_ledger(10_000_000, start="2026-09-11")
        E.place_entry(L, code="000001", name="A", date="2026-09-11", valid_date="2026-09-14",
                      entry=10000, stop=10500, target=9000, weight=0.1, side="short")
        st.put("ledger", L)
        px = prices.DictPrices({"2026-09-14": {"000001": b(9800, 9900, 9700, 9850)}})   # 고가 9,900 < 진입 → 미도달
        r = daily.run_day("2026-09-14", st, FixedAgent(), px, wb("2026-09-14"), uni=FutUni({"000002"}), log=lambda *a: None)
        self.assertFalse(r["retries"][0]["ok"])
        self.assertIn("선물 미상장", r["retries"][0]["why"])

    def test_off(self):
        old = config.SHORT_FUT_CLOSE
        config.SHORT_FUT_CLOSE = False
        try:
            st = store.FileStore(tempfile.mkdtemp())
            L = E.new_ledger(10_000_000, start="2026-09-11")
            E.place_entry(L, code="000001", name="A", date="2026-09-11", valid_date="2026-09-14",
                          entry=10000, stop=10500, target=9000, weight=0.1, side="short")
            st.put("ledger", L)
            px = prices.DictPrices({"2026-09-14": {"000001": b(10000, 10100, 9900, 9950)}})
            daily.run_day("2026-09-14", st, FixedAgent(), px, wb("2026-09-14"), uni=FutUni({"000002"}), log=lambda *a: None)
            self.assertEqual(len(st.get("ledger")["positions"]), 1)
        finally:
            config.SHORT_FUT_CLOSE = old


if __name__ == "__main__":
    unittest.main()
