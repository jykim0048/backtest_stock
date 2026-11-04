"""예탁원 CA 일정 → 뉴스·공시 분석가 입력(2026-11-04) — 피처 계산·05 덧붙이기·프롬프트 주입."""
import json
import os
import tempfile
import unittest

from swing.agent import ca as CA
from swing.tests import test_agent_pipeline as TP

DATE = "2026-11-04"                                  # 수요일


def payload():
    return {"status": "success", "asof": "2026-11-04 16:40", "errors": {}, "events": [
        {"type": "paidin", "label": "유상증자", "code": "000100", "recordDate": "2026-11-10",
         "milestones": [{"kind": "ex", "label": "권리락", "date": "2026-11-09"},
                        {"kind": "record", "label": "기준일", "date": "2026-11-10"},
                        {"kind": "listing", "label": "신주 상장", "date": "2026-12-30"}],
         "fields": {"issuePrice": 40000, "newSharesPct": 20.0, "discountPct": 25}},
        {"type": "revsplit", "label": "액면교체", "code": "000100", "recordDate": "2026-11-12",
         "milestones": [{"kind": "halt", "label": "매매정지", "date": "2026-11-11", "end": "2026-11-18"},
                        {"kind": "listing", "label": "재상장", "date": "2026-11-19"}],
         "fields": {"direction": "split"}},
        {"type": "dividend", "label": "배당", "code": "000100", "recordDate": "2025-12-31",
         "milestones": [{"kind": "ex", "label": "배당락(추정)", "date": "2025-12-30"}], "fields": {}},
    ]}


class TestFeatures(unittest.TestCase):
    def test_d_n_window_gap(self):
        f = CA.features(payload(), DATE, 45000, hold_days=15)
        self.assertEqual(f["status"], "ok")
        self.assertEqual(len(f["items"]), 2)               # 1년 전 배당은 버림
        pi = [x for x in f["items"] if x["type"] == "paidin"][0]
        ex = pi["milestones"][0]
        self.assertEqual(ex["d_n"], 3)                     # 11-05·06·09 → 3영업일
        self.assertEqual(pi["stage"], "권리락 D+3")
        self.assertTrue(pi["inHoldWindow"])
        self.assertEqual(pi["gapPct"], 12.5)
        self.assertEqual(pi["gapRef"], "발행가")
        self.assertEqual(pi["sizePct"], 20.0)
        rs = [x for x in f["items"] if x["type"] == "revsplit"][0]
        self.assertEqual(rs["haltInWindow"], {"from": "2026-11-11", "to": "2026-11-18"})
        self.assertTrue(f["haltInWindow"])

    def test_outside_window(self):
        f = CA.features(payload(), DATE, 45000, hold_days=2)
        rs = [x for x in f["items"] if x["type"] == "revsplit"][0]
        self.assertFalse(rs["inHoldWindow"])               # 정지 시작 D+5 > 보유 2일
        self.assertIsNone(rs["haltInWindow"])

    def test_empty(self):
        self.assertEqual(CA.features({"events": []}, DATE, None)["status"], "empty")


class TestAttach(unittest.TestCase):
    def _dir(self):
        d = tempfile.mkdtemp()
        for n, v in (("05_disclosures.json", {"disclosures": []}), ("01_price.json", {"latest_ohlcv": {"Close": 45000}})):
            with open(os.path.join(d, n), "w", encoding="utf-8") as f:
                json.dump(v, f)
        return d

    def test_ok_and_failure(self):
        req = TP.req()
        req.date = DATE
        d = self._dir()
        self.assertEqual(CA.attach(d, {"code": "000100"}, req, fetch_fn=lambda c: payload()), "ok")
        with open(os.path.join(d, "05_disclosures.json"), encoding="utf-8") as f:
            doc = json.load(f)
        self.assertEqual(doc["disclosures"], [])
        self.assertEqual(doc["corporate_actions"]["close"], 45000)

        def boom(c):
            raise RuntimeError("hub down")
        self.assertEqual(CA.attach(d, {"code": "000100"}, req, fetch_fn=boom), "unavailable")
        with open(os.path.join(d, "05_disclosures.json"), encoding="utf-8") as f:
            self.assertIn("hub down", json.load(f)["corporate_actions"]["error"])
        self.assertEqual(CA.attach(tempfile.mkdtemp(), {"code": "000100"}, req), "skipped")

    def test_appendix_markdown(self):
        req = TP.req()
        req.date = DATE
        d = self._dir()
        CA.attach(d, {"code": "000100"}, req, fetch_fn=lambda c: payload())
        md = CA.to_markdown(d)
        self.assertIn("| 유상증자 | 권리락 D+3 | 예 |", md)
        self.assertIn("매매정지 겹침", md)
        self.assertIn("발행가 +12.5%", md)
        self.assertEqual(CA.to_markdown(tempfile.mkdtemp()), "")


class TestPrompt(unittest.TestCase):
    def _news_user(self, status):
        llm = TP.FakeLLM()
        TP.agent(llm=llm, ca_fn=lambda out, stock, req: status).decide(TP.req())
        users = [u for r, u in llm.calls if r == "03_news_analyst.md"]
        self.assertEqual(len(users), 1)
        others = [u for r, u in llm.calls if r in ("01_market_analyst.md", "05_flow_analyst.md")]
        self.assertTrue(all("CA 일정" not in u for u in others))   # 뉴스 분석가에게만
        return users[0]

    def test_guide_only_for_news(self):
        self.assertIn("CA 유형별 해석 기준", self._news_user("ok"))
        self.assertIn("수집 실패", self._news_user("unavailable"))
        self.assertIn("CA 일정 없음", self._news_user("empty"))
        self.assertNotIn("CA 일정", self._news_user("skipped"))


if __name__ == "__main__":
    unittest.main()
