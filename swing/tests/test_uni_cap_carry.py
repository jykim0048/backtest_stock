"""유니버스 분석 상한 이연(2026-10-16) — Long 15 / Short 15, 한쪽 후보가 모자라면 남는 자리를 다른 쪽에. 오프라인."""
import tempfile
import unittest
from unittest import mock

from swing import config, daily, prices, store, universe
from swing.tests.test_daily import FixedAgent, b, wb


class FakeUni:
    def __init__(self):
        self.calls = 0

    def build_or_load(self, date, wb_, ledger):
        return {"date": date, "sectors": [], "knobs": {}}

    def attach_scores(self, cands, side, date, source="matrix"):
        return cands

    def fut_set(self, date):
        return None


def cands(prefix, n, side):
    return [{"code": f"{prefix}{i:04d}", "name": f"{prefix}{i}", "sector": "화학", "signal": "동반강세" if side == "long" else "동반약세",
             "score": 3, "reason": "", "source": "universe", "metrics": {}} for i in range(n)]


class TestCapCarry(unittest.TestCase):
    def _run(self, n_long, n_short):
        st = store.FileStore(tempfile.mkdtemp())
        L_, S_ = cands("10", n_long, "long"), cands("20", n_short, "short")

        def fake(doc, side, exclude=(), decided=None, fut_only=False, mode=None):
            return [dict(x) for x in (L_ if side == "long" else S_)]
        px = prices.DictPrices({"2026-09-14": {}})
        with mock.patch.object(universe, "universe_candidates", side_effect=fake):
            return daily.run_day("2026-09-14", st, FixedAgent(), px, wb("2026-09-14"), uni=FakeUni(), log=lambda *a: None)

    def test_defaults_15_15(self):
        self.assertEqual((config.UNI_NEW_PER_DAY, config.UNI_NEW_SHORT_PER_DAY), (15, 15))

    def test_short_shortage_carries_to_long(self):
        r = self._run(30, 6)                                          # Short 6 → 9 자리 Long 으로
        self.assertEqual(r["uniCaps"]["long"], 24)
        self.assertEqual(len([c for c in r["candidates"] if c["source"] == "universe"]), 24)
        self.assertEqual(len([c for c in r["shortCandidates"] if c["source"] == "universe"]), 6)
        self.assertEqual(len(r["uniCapSkipped"]), 6)
        self.assertTrue(any("상한 이연 Long 24/Short 15" in n for n in r["notes"]))

    def test_long_shortage_carries_to_short(self):
        r = self._run(4, 40)
        self.assertEqual(r["uniCaps"]["short"], 26)
        self.assertEqual(len([c for c in r["shortCandidates"] if c["source"] == "universe"]), 26)

    def test_both_full_no_carry_and_off(self):
        r = self._run(20, 20)
        self.assertEqual((r["uniCaps"]["long"], r["uniCaps"]["short"]), (15, 15))
        old = config.UNI_CAP_CARRY
        config.UNI_CAP_CARRY = False
        try:
            r2 = self._run(30, 6)
            self.assertEqual(r2["uniCaps"]["long"], 15)
        finally:
            config.UNI_CAP_CARRY = old


if __name__ == "__main__":
    unittest.main()
