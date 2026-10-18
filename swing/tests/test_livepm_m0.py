"""Live PM M0 — 진입 근거(thesis) 저장·소급 + livepm 노브(PLAN_PM §5·§11)."""
import tempfile
import unittest

from swing import agent_iface as A
from swing import daily, engine as E, prices, store
from swing.tests.test_daily import b, wb
from swing.tools import backfill_thesis

PM_MD = "**Rating**: Buy\n\n**Executive Summary**: HBM 수주 기대, 외국인 순매수 전환.\n\n**Time Horizon**: 1~2주\n"
TR_MD = "**Action**: Buy\n\n**Reasoning**: 수급 개선. 9,500 이탈 시 논거 무효.\n\n**Entry Price**: 10000\n"


class ThesisAgent:
    name = "thesis"

    def decide(self, req):
        d = A.Decision(code=req.code, date=req.date, purpose=req.purpose, agent=self.name,
                       rating="Buy", action="Buy", entry=10000, stop=9500, target=10600, weight=0.1,
                       summary="HBM 수주 기대, 외국인 순매수 전환.", reports={"pm": PM_MD, "trader": TR_MD})
        if req.purpose == "review":
            d.rating = "Hold"
        return d


class TestThesisFields(unittest.TestCase):
    def test_fields(self):
        d = ThesisAgent().decide(A.DecisionRequest(code="1", name="A", date="2026-09-21", purpose="entry"))
        f = A.thesis_fields(d)
        self.assertEqual(f["thesis"], "HBM 수주 기대, 외국인 순매수 전환.")
        self.assertIn("9,500 이탈", f["invalidation"])
        self.assertEqual(f["horizon"], "1~2주")

    def test_empty_decision_gives_no_keys(self):
        self.assertEqual(A.thesis_fields(A.Decision(code="1", date="d", purpose="entry")), {})


class TestThesisFlow(unittest.TestCase):
    def test_order_and_position_carry_thesis(self):
        st = store.FileStore(tempfile.mkdtemp())
        px = prices.DictPrices({"2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
                                "2026-09-22": {"000010": b(10050, 10100, 9950, 10000)}})
        daily.run_day("2026-09-21", st, ThesisAgent(), px, wb("2026-09-21", buy=[("000010", "화학")]))
        o = st.get("ledger")["orders"][0]
        self.assertEqual(o["horizon"], "1~2주")
        daily.run_day("2026-09-22", st, ThesisAgent(), px, wb("2026-09-22"))
        p = st.get("ledger")["positions"][0]
        self.assertEqual(p["thesis"], "HBM 수주 기대, 외국인 순매수 전환.")
        self.assertIn("9,500", p["invalidation"])

    def test_fill_without_thesis_has_no_keys(self):
        L = E.new_ledger(1_000_000, start="2026-09-01")
        E.place_entry(L, code="1", name="A", date="2026-09-01", valid_date="2026-09-02",
                      entry=10000, stop=9500, target=None, weight=0.1)
        E.settle_day(L, "2026-09-02", {"1": b(10000, 10100, 9900, 10000)})
        self.assertNotIn("thesis", L["positions"][0])


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())
        L = E.new_ledger(1_000_000, start="2026-09-01")
        for code in ("1", "2", "3"):
            E.place_entry(L, code=code, name=code, date="2026-09-01", valid_date="2026-09-02", entry=10000,
                          stop=9500, target=None, weight=0.1, decision_key=f"decision/2026-09-01/{code}/entry")
        E.settle_day(L, "2026-09-02", {c: b(10000, 10100, 9900, 10000) for c in ("1", "2", "3")})
        L["positions"][2]["thesis"] = "기존"
        self.st.put("ledger", L)
        d = ThesisAgent().decide(A.DecisionRequest(code="1", name="1", date="2026-09-01", purpose="entry"))
        self.st.put("decision/2026-09-01/1/entry", d.to_dict())     # 2 는 판단 원문 없음

    def test_preview_does_not_write(self):
        r = backfill_thesis.backfill(self.st, apply=False, log=lambda *_: None)
        self.assertEqual((r["filled"], r["missing"], r["skipped"]), (1, ["2"], 1))
        self.assertNotIn("thesis", self.st.get("ledger")["positions"][0])

    def test_apply_writes_and_keeps_existing(self):
        backfill_thesis.backfill(self.st, apply=True, log=lambda *_: None)
        ps = self.st.get("ledger")["positions"]
        self.assertEqual(ps[0]["horizon"], "1~2주")
        self.assertNotIn("thesis", ps[1])
        self.assertEqual(ps[2]["thesis"], "기존")


class TestLivePmConfig(unittest.TestCase):
    def test_defaults_match_plan(self):
        from swing.livepm import config as C
        self.assertNotIn("os", dir(C))                                # 환경변수로 덮어쓰지 않음(코드 상수, 2026-10-19)
        self.assertEqual((C.STOP_NEAR, C.DEBOUNCE_MIN, C.MAX_CALLS), (0.70, 15, 40))
        self.assertEqual(C.SCHEDULE, ("09:10", "11:00", "13:30", "14:50"))
        self.assertTrue(C.LLM_CHAIN.startswith("anthropic:"))


if __name__ == "__main__":
    unittest.main()
