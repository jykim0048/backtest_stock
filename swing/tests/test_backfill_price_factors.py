"""swing/tools/backfill_price_factors.py — 예전 판단에 3D 요인 소급(2026-11-12)."""
import tempfile
import unittest

from swing import store as S
from swing.tools import backfill_price_factors as B

REPORTS = {c: f"{c} 리포트 본문" for c in ("market", "sentiment", "news", "fundamentals", "flow")}
CHART = {"dates": ["2026-11-03", "2026-11-04", "2026-11-05"], "c": [169000.0, 170800.0, 184900.0]}


def fake_llm(system, user, max_tokens, schema):
    assert "+8.26%" in user and "market 리포트 본문" in user
    return ({"market": [{"title": "185,000원 돌파 시도", "direction": "up", "strength": "4", "evidence": "종가 184,900"}],
             "sentiment": [], "news": [{"title": "실적 우려", "direction": "down", "strength": "1", "evidence": "x"}],
             "fundamentals": [], "flow": [{"title": "외국인 순매수", "direction": "up", "strength": "3", "evidence": "y"}]},
            "gemini:test")


class TestBackfill(unittest.TestCase):
    def setUp(self):
        self.st = S.FileStore(tempfile.mkdtemp())
        self.st.put("decision/2026-11-05/035420/entry", {"code": "035420", "date": "2026-11-05", "reports": REPORTS, "chart": CHART})
        self.st.put("decision/2026-11-12/003490/entry", {"code": "003490", "date": "2026-11-12", "price_factors": {"items": []}})
        self.st.put("ledger", {"positions": [{"code": "035420", "name": "NAVER", "decisionKey": "decision/2026-11-05/035420/entry"}],
                               "orders": [{"code": "003490", "name": "대한항공", "decisionKey": "decision/2026-11-12/003490/entry"}]})

    def test_chart_basis(self):
        b = B.chart_basis(CHART, "2026-11-05")
        self.assertEqual((b["date"], b["ratePct"], b["kind"]), ("2026-11-05", 8.26, "day"))
        self.assertEqual(B.chart_basis(CHART, "2026-11-06")["kind"], "last")
        self.assertIsNone(B.chart_basis({"dates": ["2026-11-05"], "c": [1.0]}, "2026-11-05"))

    def test_targets_prefer_latest_review(self):
        self.assertEqual([k for k, _ in B.targets(self.st)], ["decision/2026-11-05/035420/entry", "decision/2026-11-12/003490/entry"])
        self.st.put("decision/2026-11-10/035420/review", {"code": "035420", "date": "2026-11-10"})
        self.st.put("decision/2026-11-04/035420/review", {"code": "035420", "date": "2026-11-04"})   # 앞 회차 — 제외
        self.assertEqual(B.targets(self.st)[0][0], "decision/2026-11-10/035420/review")

    def test_preview_writes_nothing(self):
        r = B.run(self.st, llm_fn=lambda *a: self.fail("미리보기에서 LLM 호출"), log=lambda m: None)
        self.assertEqual((r["filled"], r["skipped"]), (0, 1))
        self.assertIsNone(self.st.get("decision/2026-11-05/035420/entry").get("price_factors"))
        B.run(self.st, llm_fn=fake_llm, use_llm=True, log=lambda m: None)
        self.assertIsNone(self.st.get("decision/2026-11-05/035420/entry").get("price_factors"))

    def test_apply(self):
        r = B.run(self.st, llm_fn=fake_llm, apply=True, log=lambda m: None)
        self.assertEqual(r["filled"], 1)
        pf = self.st.get("decision/2026-11-05/035420/entry")["price_factors"]
        self.assertEqual(pf["basis"]["ratePct"], 8.26)
        self.assertAlmostEqual(sum(x["pp"] for x in pf["items"]), 8.3, places=1)
        self.assertEqual(pf["backfill"]["model"], "gemini:test")
        self.assertEqual({x["cat"] for x in pf["items"]}, {"market", "news", "flow"})
        r2 = B.run(self.st, llm_fn=fake_llm, apply=True, log=lambda m: None)            # 두 번째는 건너뜀
        self.assertEqual((r2["filled"], r2["skipped"]), (0, 2))

    def test_llm_failure_keeps_doc(self):
        def bad(*a):
            raise RuntimeError("quota")
        r = B.run(self.st, llm_fn=bad, apply=True, log=lambda m: None)
        self.assertEqual(r["failed"], ["decision/2026-11-05/035420/entry"])
        self.assertIsNone(self.st.get("decision/2026-11-05/035420/entry").get("price_factors"))


if __name__ == "__main__":
    unittest.main()
