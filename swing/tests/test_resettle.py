"""하루 재정산 도구(swing/tools/resettle_day.py) — 저장 판단 재사용·같은 날 쿨다운 제외·미리보기 무쓰기 오프라인 테스트."""
import tempfile
import unittest

from swing import agent_iface as A
from swing import config, daily, prices, store
from swing.tests.test_daily import FixedAgent, b, wb
from swing.tools import resettle_day as R


class NoLLM:
    name = "nollm"

    def decide(self, req):
        raise AssertionError("재정산은 LLM(에이전트)을 부르면 안 된다")


class TestResettle(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())
        self.px = prices.DictPrices({"2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
                                     "2026-09-22": {"000010": b(10050, 10100, 9950, 10000), "000030": b(5000, 5050, 4950, 5000)}})
        ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600)})
        daily.run_day("2026-09-21", self.st, ag, self.px, wb("2026-09-21", buy=[("000010", "화학")]), log=lambda *_: None)
        self.st.put("backup/pre", self.st.get("ledger"))
        # 사고 재현: 원래 런 = 시세 없는 장중 기록(asof 없음)으로 주문이 미도달 취소된 원장 + 그날 판단 저장
        bad = self.st.get("ledger")
        bad["asof"] = "2026-09-22"
        bad["orders"] = []
        self.st.put("ledger", bad)
        self.st.put("intraday/2026-09-22", {"date": "2026-09-22", "asof": None, "complete": False,
                                            "watched": {"orders": ["o1"], "positions": []}, "orders": {}, "exits": {}})
        d = A.Decision(code="000030", date="2026-09-22", purpose="entry", rating="Buy", action="Buy",
                       entry=5000, stop=4700, target=5500, weight=0.05)
        self.st.put("decision/2026-09-22/000030/entry", d.to_dict())
        self.wb2 = wb("2026-09-22", buy=[("000030", "화학")])

    def run_(self):
        orig = R.StoredAgent.decide
        return R.resettle("2026-09-22", self.st, "backup/pre", prices=self.px, wb=self.wb2, uni=None, cat=None,
                          log=lambda *_: None), orig

    def test_preview_fills_by_daily_rule_and_reuses_decision(self):
        old = config.MATRIX_COOLDOWN
        config.MATRIX_COOLDOWN = True
        try:
            (ov, run, ag), _ = self.run_()
        finally:
            config.MATRIX_COOLDOWN = old
        L = ov.get("ledger")
        self.assertEqual([(p["code"], p["entry"]) for p in L["positions"]], [("000010", 10000)])   # 시세 없는 기록 무시 → 일봉 체결
        self.assertEqual([o["code"] for o in L["orders"]], ["000030"])     # 같은 날 저장 판단은 쿨다운에서 빠지고 재사용
        self.assertEqual(ag.missing, [])
        self.assertEqual(self.st.get("ledger")["positions"], [])           # 미리보기는 운영 저장소에 안 씀

    def test_missing_decision_no_order_no_llm(self):
        import os
        os.remove(self.st._path("decision/2026-09-22/000030/entry"))
        (ov, run, ag), _ = self.run_()
        self.assertEqual(ov.get("ledger")["orders"], [])
        self.assertEqual(ag.missing, ["decision/2026-09-22/000030/entry"])

    def test_cooldown_patch_restored(self):
        from swing import universe
        before = universe.recent_decisions
        self.run_()
        self.assertIs(universe.recent_decisions, before)


if __name__ == "__main__":
    unittest.main()
