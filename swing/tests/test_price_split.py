"""판단 팝업 3D 시장·업종·종목 고유 분리 + 규칙 요인(swing/price_split.py, 2026-11-13) — 오프라인."""
import datetime
import unittest

from swing import price_split as ps

TODAY = "2026-11-13"
IB = {
    "date": TODAY,
    "indices": {"kospi": {"rate": -1.0}, "kosdaq": {"rate": 0.5}},
    "sectorHeat": [{"name": "화학", "changePct": -3.0}],
    "sectorsDown": [{"stocks": [{"code": "051910", "news": ["LG화학 증설 연기"]}]}],
    "earningsEvents": {"releasedToday": [
        {"date": "2026-11-12", "code": "093370", "period": "202609", "fs": "연결", "op": 100.0, "opYoY": -20.0,
         "consensus": {"op": 130.0}, "surprise": {"opGap": -23.1, "npGap": -30.0}, "time": "16:10", "late": True},
        {"date": TODAY, "code": "111111", "surprise": {"opGap": 30.0}, "time": "15:40"},     # 오늘 마감 뒤 → 제외
        {"date": TODAY, "code": "222222", "surprise": {}, "tag": "턴어", "time": "10:00"},
        {"date": TODAY, "code": "333333", "surprise": {"opGap": 2.0}, "time": "10:00"},     # 부합 → 제외
    ]},
}
CODE_SECTOR = {"map": {"093370": "화학", "051910": "화학"}}
SECTOR_MAP = {"sectors": {"chem": {"name": "화학", "stocks": [
    {"code": "051910", "name": "LG화학", "cap": 300}, {"code": "093370", "name": "후성", "cap": 10},
    {"code": "011170", "name": "롯데케미칼", "cap": 100}, {"code": "009830", "name": "한화솔루션", "cap": 90},
    {"code": "011780", "name": "금호석유", "cap": 50}]}}}
PF = {"basis": {"date": "2026-11-12", "ratePct": 12.0, "close": 10070.0},
      "items": [{"cat": "news", "title": "반도체 소재 수요", "dir": 1, "strength": 3, "evidence": "e"},
                {"cat": "flow", "title": "외국인 순매도", "dir": -1, "strength": 2, "evidence": "e"},
                {"cat": "etc", "title": "x", "dir": 1, "strength": 0, "pp": 1.0}]}
TODAY_PX = {"date": TODAY, "price": 9260.0, "prevClose": 10070.0, "ratePct": -8.04, "kind": "live"}
PX = {"051910": {"rate": -4.0}, "011170": {"rate": -2.0}, "009830": {"rate": 1.0}}


def ctx():
    return ps.build_context(IB, CODE_SECTOR, SECTOR_MAP)


class TestDecompose(unittest.TestCase):
    def test_market_sector_idio_and_rules(self):
        out = ps.decompose("093370", False, -8.04, ctx(), PX, PF, TODAY_PX)
        it = out["items"]
        self.assertAlmostEqual(sum(x["pp"] for x in it), -8.0, places=6)            # 합계 = 등락률(0.1 단위)
        self.assertEqual(it[0]["cat"], "index")
        self.assertEqual(it[0]["pp"], -1.0)
        sec = [x for x in it if x["cat"] == "sector"]
        self.assertEqual([x["title"].split()[0] for x in sec], ["LG화학", "롯데케미칼", "한화솔루션", "그"])   # 자기 자신 제외 상위 3
        self.assertAlmostEqual(sum(x["pp"] for x in sec), -2.0, places=1)            # 업종 −3 − 지수 −1
        self.assertEqual(sec[0]["evidence"], "뉴스: LG화학 증설 연기")
        self.assertEqual(out["sector"], "화학")
        self.assertEqual(out["layers"]["idio"], -5.04)
        rules = {x["ruleName"]: x for x in it if x.get("rule")}
        self.assertEqual(set(rules), {"가격 되돌림", "실적 발표"})                    # 전일 +12% → 되돌림, 컨센 −23% → 실적
        rv, ea = rules["가격 되돌림"], rules["실적 발표"]
        self.assertEqual((rv["dir"], rv["strength"], rv["cat"], rv["title"]), (-1, 2, "etc", ""))
        self.assertIn("전일 +12.0% 급등 되돌림", rv["fullTitle"])
        self.assertEqual((ea["dir"], ea["strength"]), (-1, 5))
        self.assertEqual(ea["fullTitle"], "실적 컨센서스 하회 −23.1%")
        self.assertIn("26년 3Q 연결 잠정 영업이익 100억(YoY −20%) · 컨센서스 130억 · 순이익 컨센比 −30.0% — 11/12 16:10 공시(장 마감 뒤 → 오늘 반영).",
                      ea["evidence"])
        self.assertFalse(any(x["cat"] == "etc" and x["title"] == "x" for x in it))   # 저장된 etc 는 다시 계산

    def test_kosdaq_no_sector(self):
        out = ps.decompose("093370", True, 3.0, ctx(), {}, {"basis": {}, "items": PF["items"][:2]}, {"date": TODAY})
        self.assertEqual([x["cat"] for x in out["items"]][:1], ["index"])
        self.assertFalse(any(x["cat"] == "sector" for x in out["items"]))
        self.assertEqual(out["layers"]["idio"], 2.5)

    def test_unexplained_idio_goes_to_etc(self):
        pf = {"basis": {}, "items": [{"cat": "news", "title": "호재", "dir": 1, "strength": 3, "evidence": "e"}]}
        out = ps.decompose("999999", False, -4.0, ctx(), {}, pf, {"date": TODAY})
        etc = [x for x in out["items"] if x["cat"] == "etc"]
        self.assertEqual(etc[0]["title"], "설명되지 않은 종목 고유 변동")


class TestRules(unittest.TestCase):
    def test_prev_move_needs_adjacent_day(self):
        self.assertEqual(ps.prev_move(PF["basis"], TODAY_PX)["rate"], 12.0)
        self.assertIsNone(ps.prev_move(dict(PF["basis"], close=9000.0), TODAY_PX))      # 종가가 다름 → 사이에 거래일
        self.assertIsNone(ps.prev_move(dict(PF["basis"], date=TODAY), TODAY_PX))

    def test_reversal_only_when_opposite(self):
        pm = {"date": "2026-11-12", "close": 10070.0, "rate": 12.0}
        self.assertIsNone(ps.reversal_factor(pm, 2.0))
        self.assertIsNone(ps.reversal_factor(dict(pm, rate=8.0), -2.0))
        self.assertEqual(ps.reversal_factor(dict(pm, rate=-24.0), 3.0)["strength"], 5)

    def test_earn_rules(self):
        c = ctx()
        self.assertIsNone(ps.earn_factor(c["earn"]["111111"], TODAY, PF))              # 오늘 15:30 뒤
        self.assertEqual(ps.earn_factor(c["earn"]["222222"], TODAY, PF)["strength"], 2)  # 태그만
        self.assertIsNone(ps.earn_factor(c["earn"]["333333"], TODAY, PF))              # 부합
        dup = {"basis": {"date": TODAY}, "items": [{"title": "3Q 영업이익 컨센서스 하회"}]}
        self.assertIsNone(ps.earn_factor(c["earn"]["093370"], TODAY, dup))             # 애널리스트가 이미 실적 요인


class TestForDecision(unittest.TestCase):
    NOW = datetime.datetime(2026, 11, 13, 10, 30)

    def test_held_position_uses_today(self):
        split, t = ps.for_decision("093370", False, PF, TODAY_PX, ctx=ctx(), prices=PX, now=self.NOW)
        self.assertIs(t, TODAY_PX)
        self.assertEqual(split["ratePct"], -8.04)

    def test_unheld_fills_today_from_main_prices(self):
        px = dict(PX, **{"093370": {"price": 9260.0, "prevClose": 10070.0, "rate": -8.04}})
        split, t = ps.for_decision("093370", False, PF, None, ctx=ctx(), prices=px, now=self.NOW)
        self.assertEqual((t["kind"], t["source"], t["ratePct"], t["ts"]), ("live", "main", -8.04, "10:30"))
        self.assertIsNotNone(split)
        late = datetime.datetime(2026, 11, 13, 21, 0)
        self.assertEqual(ps.for_decision("093370", False, PF, None, ctx=ctx(), prices=px, now=late), (None, None))

    def test_briefing_date_must_match(self):
        old = dict(TODAY_PX, date="2026-11-12")
        self.assertEqual(ps.for_decision("093370", False, PF, old, ctx=ctx(), prices=PX, now=self.NOW), (None, old))

    def test_no_context(self):
        c = ps.build_context(None, None, None)
        self.assertEqual(ps.for_decision("093370", False, PF, TODAY_PX, ctx=c, prices=PX, now=self.NOW), (None, TODAY_PX))


if __name__ == "__main__":
    unittest.main()
