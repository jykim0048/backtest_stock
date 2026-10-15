"""하방 촉매 Short 후보 소스(2026-10-15) — 오프라인. 10/13 아카이브 형식을 축약한 픽스처 + 두산퓨얼셀 EB 공시(누락 사례) 재현."""
import tempfile
import unittest

from swing import agent_iface as A
from swing import catalysts as C
from swing import config, daily, prices, store
from swing.tests.test_daily import FixedAgent, b, wb


def arch(with_rule=True, scoring=True):
    """15회차 중 3회차만 흉내: 삼성전자 하방 뉴스 3회, 제일엠앤에스 유상증자 공시(촉매 선정), 두산퓨얼셀 EB 공시(LLM 미선정·disclosures 에만),
    에이피알 하방 뉴스 1회(급락), 상방 1건."""
    news = lambda n, s: {"stock": n, "market": "KOSPI", "direction": "bearish", "summary": s, "kind": "news", "url": "u"}
    d_eb = {"corp": "두산퓨얼셀", "stockCode": "336260", "market": "KOSPI", "title": "주요사항보고서(교환사채권발행결정)", "url": "d1"}
    d_ri = {"corp": "제일엠앤에스", "stockCode": "412540", "market": "KOSDAQ", "title": "주요사항보고서(유상증자결정)", "url": "d2"}
    if with_rule:
        d_eb.update(dirRule="bearish", ruleKind="dilution")
        d_ri.update(dirRule="bearish", ruleKind="dilution")
    r1 = {"asof": "2026-10-13 09:10 KST", "catalysts": [news("삼성전자", "CXMT 우려"),
                                                        {"stock": "제일엠앤에스", "market": "KOSDAQ", "direction": "bearish", "kind": "disclosure",
                                                         "summary": "유상증자 결정 공시", "title": d_ri["title"], "dirRule": "bearish" if with_rule else None,
                                                         "ruleKind": "dilution" if with_rule else None, "code": "412540"},
                                                        {"stock": "예선테크", "market": "KOSDAQ", "direction": "bullish", "kind": "news", "summary": "급등"}],
          "disclosures": [d_eb, d_ri]}
    r2 = {"asof": "2026-10-13 11:10 KST", "catalysts": [news("삼성전자", "CXMT 우려 2"), news("에이피알", "목표가 하향")], "disclosures": [d_eb]}
    r3 = {"asof": "2026-10-13 15:07 KST", "final": True, "catalysts": [news("삼성전자", "6%대 급락")], "disclosures": []}
    rounds = [r1, r2, r3]
    if scoring:
        rounds.append({"asof": "2026-10-13 16:00 KST", "scoring": True,
                       "catalysts": [{"stock": "삼성전자", "market": "KOSPI", "score": 1, "changePct": -6.1},
                                     {"stock": "에이피알", "market": "KOSPI", "score": 1, "changePct": -24.5},
                                     {"stock": "제일엠앤에스", "market": "KOSDAQ", "score": 2, "changePct": -3.0}]})
    return {"date": "2026-10-13", "rounds": rounds}


class TestCollect(unittest.TestCase):
    def test_rule_direction(self):
        self.assertEqual(C.rule_direction("주요사항보고서(교환사채권발행결정)"), ("bearish", "dilution"))
        self.assertEqual(C.rule_direction("[기재정정]유상증자결정"), ("bearish", "dilution"))
        self.assertEqual(C.rule_direction("관리종목지정"), ("bearish", "distress"))
        self.assertEqual(C.rule_direction("단일판매ㆍ공급계약체결"), (None, None))
        self.assertEqual(C.rule_direction("유상증자결정 철회"), (None, None))

    def test_name_to_code(self):
        self.assertEqual(C.name_to_code("두산퓨얼셀"), "336260")
        self.assertEqual(C.name_to_code("삼성전자우"), "005930")
        self.assertIsNone(C.name_to_code("없는회사XYZ"))

    def test_collect_and_strength(self):
        e = C.collect_bearish(arch())
        self.assertIn("336260", e)                                   # 두산퓨얼셀: LLM 미선정 공시도 disclosures 규칙으로 잡힘
        eb = e["336260"]
        self.assertEqual((eb["kind"], eb["ruleKind"], eb["strength"], eb["discl"]), ("disclosure", "dilution", 4, 2))
        self.assertEqual(e["005930"]["strength"], 3)                 # 하방 뉴스 3회차
        self.assertEqual(e["005930"]["changePct"], -6.1)
        self.assertEqual(e["412540"]["strength"], 4)                 # 유상증자 공시
        self.assertEqual(e["278470"]["strength"], 2)                 # 뉴스 1회차
        self.assertNotIn("n:예선테크", e)                              # 상방은 제외
        # 구 아카이브(dirRule 없음)도 제목 규칙으로 동일
        e2 = C.collect_bearish(arch(with_rule=False))
        self.assertEqual(e2["336260"]["strength"], 4)
        self.assertEqual(e2["412540"]["ruleKind"], "dilution")
        # main 스코어러 bearScore 가 있으면 우선
        a3 = arch()
        a3["rounds"][-1]["catalysts"][0]["bearScore"] = 5
        self.assertEqual(C.collect_bearish(a3)["005930"]["strength"], 5)

    def test_short_candidates_filters(self):
        e = C.collect_bearish(arch())
        cands, skipped = C.short_candidates(e, exclude={"412540"}, decided={}, min_strength=4, max_drop=20, sector_of={"336260": "전기·가스"})
        self.assertEqual([c["code"] for c in cands], ["336260"])
        self.assertEqual(cands[0]["source"], "catalyst")
        self.assertEqual(cands[0]["sector"], "전기·가스")
        self.assertEqual(cands[0]["signal"], "하방촉매")
        self.assertIn("교환사채", cands[0]["reason"])
        self.assertEqual(skipped["412540"], "보유·대기·다른 소스")
        self.assertIn("강도", skipped["005930"])
        # 강도 2 까지 허용하면 삼성전자 포함, 에이피알은 급락 추격 금지
        cands, skipped = C.short_candidates(e, min_strength=2, max_drop=20)
        self.assertEqual([c["code"] for c in cands], ["336260", "412540", "005930"])
        self.assertIn("급락", skipped["278470"])
        # 쿨다운·주식선물 한정
        cands, skipped = C.short_candidates(e, decided={"336260": "2026-10-10"}, min_strength=4, fut={"412540"})
        self.assertEqual([c["code"] for c in cands], ["412540"])
        self.assertIn("쿨다운", skipped["336260"])

    def test_dart_direct(self):
        rows = [{"corp_cls": "Y", "corp_name": "두산퓨얼셀", "stock_code": "336260", "report_nm": "주요사항보고서(신주인수권부사채권발행결정)", "rcept_no": "1"},
                {"corp_cls": "Y", "corp_name": "삼성전자", "stock_code": "005930", "report_nm": "기업설명회(IR)개최", "rcept_no": "2"},
                {"corp_cls": "E", "corp_name": "비상장", "stock_code": "", "report_nm": "유상증자결정", "rcept_no": "3"}]
        out = C.dart_today_bearish("2026-10-13", api_key="k", fetch=lambda p: {"status": "000", "total_page": 1, "list": rows})
        self.assertEqual([(x["corp"], x["ruleKind"]) for x in out], [("두산퓨얼셀", "dilution")])
        self.assertEqual(C.dart_today_bearish("2026-10-13", api_key=None), [])
        # 아카이브에 없던 장 마감 후 공시가 직접 조회로 후보에 들어간다(direct 표시)
        e = C.collect_bearish({"date": "2026-10-13", "rounds": []}, out)
        self.assertTrue(e["336260"]["direct"])
        self.assertEqual(e["336260"]["strength"], 4)


class ShortAgent(FixedAgent):
    def decide(self, req):
        self.calls.append((req.code, req.purpose, req.source, (req.catalyst or {}).get("strength")))
        d = A.Decision(code=req.code, date=req.date, purpose=req.purpose, agent=self.name, side=req.side)
        r = self.entry.get(req.code)
        if r:
            d.rating, d.action, d.entry, d.stop, d.target = r
            d.weight = 0.05
        return d


class TestRunDay(unittest.TestCase):
    def test_catalyst_short_in_run_day(self):
        st = store.FileStore(tempfile.mkdtemp())
        ag = ShortAgent({"336260": ("Sell", "Sell", 20000, 21500, 18000)})
        px = prices.DictPrices({"2026-10-13": {"336260": b(20500, 20800, 19900, 20000), "412540": b(1000, 1000, 1000, 1000)}})
        cat = C.CatalystRunner(st, fetch=lambda d: arch(), dart_fetch=lambda p: {"status": "000", "total_page": 1, "list": []},
                               sector_of={"336260": "전기·가스"})
        old = config.CAT_DART_DIRECT
        config.CAT_DART_DIRECT = False
        try:
            run = daily.run_day("2026-10-13", st, ag, px, wb("2026-10-13"), cat=cat)
        finally:
            config.CAT_DART_DIRECT = old
        sc = [c for c in run["shortCandidates"] if c["source"] == "catalyst"]
        self.assertEqual([c["code"] for c in sc], ["336260", "412540"])     # 강도 4 둘(제일엠앤에스는 Hold)
        self.assertTrue(sc[0]["ordered"])
        self.assertEqual(sc[0]["catalyst"]["strength"], 4)
        self.assertEqual(sc[0]["signal"], "하방촉매")
        self.assertIn(("336260", "entry", "catalyst", 4), ag.calls)
        self.assertTrue(any("하방 촉매 후보 Short 2" in n for n in run["notes"]))
        doc = st.get("catalyst/2026-10-13")
        self.assertEqual(doc["candidates"], ["336260", "412540"])
        self.assertIn("005930", doc["skipped"])
        # 원장 주문에 source=catalyst
        L = st.get("ledger")
        self.assertEqual([o["source"] for o in L["orders"]], ["catalyst"])
        # 쿨다운: 다음 날 같은 종목은 제외
        px2 = prices.DictPrices({"2026-10-14": {"336260": b(20000, 20100, 19500, 19600), "412540": b(1000, 1000, 1000, 1000)}})
        config.CAT_DART_DIRECT = False
        try:
            run2 = daily.run_day("2026-10-14", st, ag, px2, wb("2026-10-14"), cat=cat)
        finally:
            config.CAT_DART_DIRECT = old
        self.assertEqual([c["code"] for c in run2["shortCandidates"] if c["source"] == "catalyst"], [])
        self.assertIn("쿨다운", st.get("catalyst/2026-10-14")["skipped"].get("412540", ""))

    def test_disabled_or_none(self):
        st = store.FileStore(tempfile.mkdtemp())
        px = prices.DictPrices({"2026-10-13": {}})
        run = daily.run_day("2026-10-13", st, FixedAgent(), px, wb("2026-10-13"))
        self.assertEqual(run["shortCandidates"], [])
        self.assertEqual(run.get("catShortCapSkipped"), [])


if __name__ == "__main__":
    unittest.main()
