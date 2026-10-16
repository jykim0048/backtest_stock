"""체결일 손절 → 다음 날 종가 재판별 → 매도면 3일째 시가 청산(2026-10-16). run_day 통합, 오프라인."""
import tempfile
import unittest

from swing import daily, prices, store
from swing.tests.test_daily import ReviewAgent, b, wb


class TestFillStopReview(unittest.TestCase):
    def _px(self):
        return prices.DictPrices({
            "2026-09-14": {"000010": b(10000, 10100, 9900, 10050)},
            "2026-09-15": {"000010": b(10000, 10100, 9400, 9600)},    # D: 체결(10000) + 저가 9400 ≤ 손절 9500
            "2026-09-16": {"000010": b(9300, 9450, 9100, 9200)},      # D+1: 갭·장중 손절 유예, 종가로 재판별
            "2026-09-17": {"000010": b(9150, 9300, 9100, 9250)},      # D+2: 시가 청산(매도 판정 시)
        })

    def _run(self, st, ag, date, buy=()):
        return daily.run_day(date, st, ag, self._px(), wb(date, buy=buy), log=lambda *a: None)

    def test_sell_on_review_then_open_exit(self):
        st = store.FileStore(tempfile.mkdtemp())
        ag = ReviewAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600)}, reviews=[("Sell", None, None, None)])
        self._run(st, ag, "2026-09-14", buy=[("000010", "화학")])
        r1 = self._run(st, ag, "2026-09-15")
        self.assertEqual(r1["reviews"], [])                         # D 에는 재판별 없음
        L = st.get("ledger")
        self.assertEqual((L["trades"], L["positions"][0]["fillStop"]["date"]), ([], "2026-09-15"))
        r2 = self._run(st, ag, "2026-09-16")
        self.assertEqual(L["trades"], [])
        rv = r2["reviews"][0]
        self.assertEqual((rv["kinds"][0], rv["action"]), ("fill_stop", "sell"))
        self.assertIn("체결일(09-15) 손절가 9,500", rv["trigger"])
        self.assertIn("09-16 종가 9,200", rv["trigger"])
        self.assertEqual(st.get("ledger")["trades"], [])            # D+1 에는 청산 없음
        self._run(st, ag, "2026-09-17")
        t = st.get("ledger")["trades"][0]
        self.assertEqual((t["reason"], t["exitPrice"], t["exitDate"]), ("stop", 9150, "2026-09-17"))

    def test_hold_with_new_stop(self):
        st = store.FileStore(tempfile.mkdtemp())
        ag = ReviewAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600)}, reviews=[("Hold", 10400, 8900, None)])
        self._run(st, ag, "2026-09-14", buy=[("000010", "화학")])
        self._run(st, ag, "2026-09-15")
        r2 = self._run(st, ag, "2026-09-16")
        self.assertEqual(r2["reviews"][0]["action"], "hold_updated")
        p = st.get("ledger")["positions"][0]
        self.assertEqual((p["stop"], p["target"], p.get("extended")), (8900, 10400, None))
        self.assertTrue(p["fillStop"]["reviewed"])
        self._run(st, ag, "2026-09-17")                             # 새 손절 8,900 위 — 계속 보유
        self.assertEqual(len(st.get("ledger")["positions"]), 1)


if __name__ == "__main__":
    unittest.main()
