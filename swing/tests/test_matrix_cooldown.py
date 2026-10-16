"""매트릭스 후보 쿨다운(2026-10-16) — 유니버스·하방 촉매와 같은 규칙(최근 UNI_COOLDOWN_DAYS 영업일 판단 → 제외). 오프라인."""
import tempfile
import unittest

from swing import config, daily, prices, store
from swing.tests.test_daily import FixedAgent, b, wb


class TestMatrixCooldown(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())

    def _px(self):
        bar = b(10000, 10100, 9900, 10000)
        return prices.DictPrices({d: {"000010": bar, "000020": bar} for d in
                                  ("2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18")})

    def _run(self, ag, date, buy, down=()):
        return daily.run_day(date, self.st, ag, self._px(), wb(date, buy=buy, down=down), log=lambda *a: None)

    def test_hold_cooled_then_returns(self):
        self.assertTrue(config.MATRIX_COOLDOWN)
        ag = FixedAgent()                                             # 전부 Hold(미주문)
        buy = [("000010", "화학")]
        r1 = self._run(ag, "2026-09-14", buy, down=["000020"])
        self.assertEqual([c["code"] for c in r1["candidates"]], ["000010"])
        self.assertEqual([c["code"] for c in r1["shortCandidates"]], ["000020"])
        r2 = self._run(ag, "2026-09-15", buy, down=["000020"])       # Long·Short 모두 쿨다운
        self.assertEqual((r2["candidates"], r2["shortCandidates"]), ([], []))
        self.assertEqual(r2["matrixCooldown"], ["000010", "000020"])
        self.assertTrue(any("쿨다운 제외 2" in n for n in r2["notes"]))
        r3 = self._run(ag, "2026-09-16", buy, down=["000020"])       # 판단일 포함 3영업일째까지
        self.assertEqual(r3["matrixCooldown"], ["000010", "000020"])
        r4 = self._run(ag, "2026-09-17", buy, down=["000020"])       # 4영업일째 다시 후보
        self.assertEqual([c["code"] for c in r4["candidates"]], ["000010"])
        self.assertEqual(r4["matrixCooldown"], [])

    def test_off(self):
        old = config.MATRIX_COOLDOWN
        config.MATRIX_COOLDOWN = False
        try:
            ag = FixedAgent()
            self._run(ag, "2026-09-14", [("000010", "화학")])
            r2 = self._run(ag, "2026-09-15", [("000010", "화학")])
            self.assertEqual([c["code"] for c in r2["candidates"]], ["000010"])   # 끄면 종전처럼 매일 재분석
        finally:
            config.MATRIX_COOLDOWN = old


if __name__ == "__main__":
    unittest.main()
