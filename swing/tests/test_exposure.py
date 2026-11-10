"""노출·현금 한도(PLAN_EXPOSURE.md, 2026-11-10) — swing/exposure.py 순수 함수. 오프라인."""
import unittest

from swing import exposure as X


def pos(code, side, sector, value, name=None):
    return {"code": code, "name": name or code, "side": side, "sector": sector, "value": value}


class TestExposure(unittest.TestCase):
    def test_limits_are_the_decided_rules(self):
        L = X.limits(["005930", "000660"])
        self.assertEqual((L["netMin"], L["netMax"], L["cashMax"]), (-0.10, 0.15, 0.10))
        self.assertEqual((L["sectorNetMax"], L["sectorNetRelease"]), (0.10, 0.08))
        self.assertEqual((L["weightMax"], L["weightMaxTop"], L["topCodes"]), (0.05, 0.10, ["005930", "000660"]))

    def test_weight_cap_top2_only(self):
        top = ["005930", "000660"]
        self.assertEqual(X.weight_cap("005930", top), 0.10)
        self.assertEqual(X.weight_cap("000660", top), 0.10)
        self.assertEqual(X.weight_cap("373220", top), 0.05)        # 시총 3위부터는 5%
        self.assertEqual(X.weight_cap("0220W0", top), 0.05)        # 영숫자 코드

    def test_top_cap_codes_from_universe_doc(self):
        doc = {"sectors": [{"name": "전기·전자", "stocks": [{"code": "005930", "cap": 9_000_000}, {"code": "000660", "cap": 4_000_000},
                                                          {"code": "373220", "cap": 1_000_000}]},
                           {"name": "제약", "stocks": [{"code": "196170", "cap": 5_000_000}]}],           # 코스닥 대형주
               "rankLong": [{"code": "005380", "cap": 800_000}]}
        kospi = {"005930", "000660", "373220", "005380"}
        self.assertEqual(X.top_cap_codes(doc, lambda c: c in kospi), ["005930", "000660"])   # 코스닥 196170 은 제외
        self.assertEqual(X.top_cap_codes(doc), ["005930", "196170"])                          # 시장 구분을 안 주면 전체 기준
        self.assertEqual(X.top_cap_codes(None), list(X.DEFAULT_TOP))                          # 문서 없음 → 기본값
        self.assertEqual(X.top_cap_codes({"sectors": [{"stocks": [{"code": "005930", "cap": 1}]}]}), list(X.DEFAULT_TOP))   # 1종목뿐 → 기본값

    def test_summarize_2026_11_10_book(self):
        # 2026-11-10 10:37 실측 장부를 단순화 — Long 58.6% · Short 20.8% · 현금 39.5%
        eq = 1000.0
        ps = [pos("A", "long", "IT 서비스", 143), pos("B", "long", "음식료·담배", 126), pos("C", "long", "유통", 98),
              pos("D", "long", "전기·전자", 0), pos("E", "short", "전기·전자", 112), pos("F", "long", "기타", 219),
              pos("G", "short", "화학", 53), pos("H", "short", "기타", 43)]
        s = X.summarize(ps, 395, eq)
        self.assertAlmostEqual(s["longPct"], 0.586)
        self.assertAlmostEqual(s["shortPct"], 0.208)
        self.assertAlmostEqual(s["netPct"], 0.378)
        self.assertAlmostEqual(s["netOver"], 0.228)                 # 상한 +15% 를 22.8%p 초과
        self.assertAlmostEqual(s["cashOver"], 0.295)                # 현금 한도 10% 를 29.5%p 초과
        over = {r["sector"]: r["brake"] for r in s["sectors"] if r["over"]}
        self.assertEqual(over, {"IT 서비스": "long", "음식료·담배": "long", "전기·전자": "short", "기타": "long"})   # 유통 +9.8% 는 한도 안
        self.assertEqual(s["sectors"][0]["sector"], "기타")         # |순비중| 큰 순

    def test_summarize_inside_band_and_below_min(self):
        s = X.summarize([pos("A", "long", "x", 500), pos("B", "short", "y", 450)], 50, 1000)
        self.assertEqual((s["netOver"], s["cashOver"]), (0.0, 0.0))
        s = X.summarize([pos("A", "long", "x", 300), pos("B", "short", "y", 450)], 100, 1000)
        self.assertAlmostEqual(s["netOver"], -0.05)                 # 하한 −10% 를 5%p 밑돎(음수)
        self.assertEqual(X.summarize([], 0, 0)["netPct"], 0.0)      # 빈 장부·평가액 0 도 안전

    def test_over_weight_uses_per_stock_cap(self):
        top = ["005930", "000660"]
        ps = [pos("005930", "long", "전기·전자", 90), pos("373220", "long", "전기·전자", 60), pos("035720", "short", "IT 서비스", 51),
              pos("000660", "long", "전기·전자", 101), pos("051910", "long", "화학", 50)]
        ow = X.summarize(ps, 0, 1000, top)["overWeight"]
        self.assertEqual([(r["code"], r["cap"]) for r in ow], [("373220", 0.05), ("000660", 0.10), ("035720", 0.05)])   # 초과 폭 큰 순, 5% 딱은 통과


if __name__ == "__main__":
    unittest.main()
