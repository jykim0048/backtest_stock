"""미도달 주문 재시도(2026-10-16, 방식 A) — 오프라인."""
import tempfile
import unittest

from swing import config, daily, prices, store
from swing.tests.test_daily import FixedAgent, b, wb


class TestRetry(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())
        self.ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600)})

    def _run(self, date, px, buy=()):
        return daily.run_day(date, self.st, self.ag, px, wb(date, buy=buy), log=lambda *a: None)

    def test_retry_then_fill(self):
        px = prices.DictPrices({
            "2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
            "2026-09-22": {"000010": b(10200, 10300, 10150, 10200)},   # 저가 10150 > 진입 10000 → 미도달, 종가 +2% 안
            "2026-09-23": {"000010": b(10100, 10200, 9950, 10000)},    # 재주문 체결
        })
        r1 = self._run("2026-09-21", px, buy=[("000010", "화학")])
        self.assertEqual([c["ordered"] for c in r1["candidates"]], [True])
        r2 = self._run("2026-09-22", px)                                # 후보 없음(매트릭스 pend 제외) → 재시도만
        self.assertEqual(len(r2["retries"]), 1)
        self.assertTrue(r2["retries"][0]["ok"])
        self.assertEqual(r2["retries"][0]["retry"], 1)
        L = self.st.get("ledger")
        o = [x for x in L["orders"] if x["status"] == "open"][0]
        self.assertEqual((o["entry"], o["stop"], o["target"], o["retry"], o["validDate"]), (10000, 9500, 10600, 1, "2026-09-23"))
        self.assertEqual(o["decisionKey"], r1["candidates"][0]["decisionKey"])   # 재분석 없음 — 같은 판단
        self.assertEqual(len(self.ag.calls), 1)                          # 에이전트 호출 추가 없음
        r3 = self._run("2026-09-23", px)
        L = self.st.get("ledger")
        self.assertEqual([p["code"] for p in L["positions"]], ["000010"])
        self.assertEqual(r3["retries"], [])

    def test_band_guard_stops(self):
        px = prices.DictPrices({
            "2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
            "2026-09-22": {"000010": b(10500, 10700, 10400, 10600)},   # 종가 +6% → 가드
        })
        self._run("2026-09-21", px, buy=[("000010", "화학")])
        r2 = self._run("2026-09-22", px)
        self.assertEqual(len(r2["retries"]), 1)
        self.assertFalse(r2["retries"][0]["ok"])
        self.assertIn("밖", r2["retries"][0]["why"])
        self.assertEqual([x for x in self.st.get("ledger")["orders"] if x["status"] == "open"], [])

    def test_stop_guard_and_max(self):
        old = config.ENTRY_RETRY_MAX
        config.ENTRY_RETRY_MAX = 1
        try:
            px = prices.DictPrices({
                "2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
                "2026-09-22": {"000010": b(10100, 10150, 10050, 10100)},   # 미도달 → 재시도 1
                "2026-09-23": {"000010": b(10100, 10150, 10050, 10100)},   # 미도달 → 상한
            })
            self._run("2026-09-21", px, buy=[("000010", "화학")])
            r2 = self._run("2026-09-22", px)
            self.assertTrue(r2["retries"][0]["ok"])
            r3 = self._run("2026-09-23", px)
            self.assertFalse(r3["retries"][0]["ok"])
            self.assertIn("상한", r3["retries"][0]["why"])
        finally:
            config.ENTRY_RETRY_MAX = old
        # 손절 침범 가드
        st2 = store.FileStore(tempfile.mkdtemp())
        px = prices.DictPrices({
            "2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
            "2026-09-22": {"000010": b(9700, 9800, 9400, 9450)},       # 저가 9400 < 진입 → 체결됐을 것. 미도달 아님
            "2026-09-24": {"000010": b(10100, 10150, 10050, 10100)},
        })
        r1 = daily.run_day("2026-09-21", st2, self.ag, px, wb("2026-09-21", buy=[("000010", "화학")]), log=lambda *a: None)
        r2 = daily.run_day("2026-09-22", st2, self.ag, px, wb("2026-09-22"), log=lambda *a: None)
        self.assertEqual(r2["retries"], [])                             # 체결(그 뒤 손절)이라 재시도 대상 아님


if __name__ == "__main__":
    unittest.main()
