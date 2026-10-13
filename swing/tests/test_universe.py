"""유니버스(swing/universe.py) 오프라인 테스트 — mock DB증권·KIS 허브. 네트워크 없음."""
import json
import unittest

from swing import config, store, universe as U, universe_mock as M

DATE = "2026-10-12"


def wb():
    return {"asof": "2026-10-12 16:13 KST",
            "sectorFlow": {"rows": [{"name": "운송·창고", "signal": "동반강세", "chgPct": 2.29, "frgn": 295, "orgn": 414},
                                    {"name": "통신", "signal": "동반약세", "chgPct": -3.81, "frgn": 20, "orgn": -109},
                                    {"name": "보험", "signal": "혼조", "chgPct": 0.42},
                                    {"name": "제조", "signal": "동반약세", "chgPct": -4.19},
                                    {"name": "IT 서비스", "signal": "수급유입", "chgPct": -1.39}]}}


def mocks():
    secs = [s for s in M.load_sectors() if s["name"] in ("운송·창고", "통신", "보험", "제조", "IT 서비스", "화학")]
    return M.MockDbQuote(sectors=secs), M.MockHubFlow()


class TestPure(unittest.TestCase):
    def test_excluded(self):
        self.assertTrue(U.is_excluded("005935", "삼성전자우"))
        self.assertTrue(U.is_excluded("395400", "SK리츠"))
        self.assertTrue(U.is_excluded("069500", "KODEX 200"))
        self.assertFalse(U.is_excluded("005930", "삼성전자"))

    def test_direction(self):
        self.assertEqual(U.direction_of("동반강세", "운송·창고"), "long")
        self.assertEqual(U.direction_of("수급이탈", "통신"), "short")
        self.assertIsNone(U.direction_of("혼조", "보험"))
        self.assertIsNone(U.direction_of("동반약세", "제조"))       # 대분류 제외

    def test_weekly_metrics_units_and_window(self):
        dbq = M.MockDbQuote()
        rows = dbq.daily_investor("011200", "2026-09-01", DATE)
        m = U.weekly_metrics(rows, DATE, 205625, tv_div=1e8, flow_div=1e5)
        self.assertEqual(m["days"], len(rows))
        self.assertGreater(m["tv1w"], 0)
        self.assertIsNotNone(m["tvMom"])
        self.assertIsNotNone(m["flowBp"])
        # 원→억: 행 합과 일치
        last5 = sorted(rows, key=lambda r: r["Date"])[-5:]
        self.assertAlmostEqual(m["tv1w"], round(sum(float(r["AcmlTrPbmn"]) for r in last5) / 1e8), delta=1)
        self.assertIsNone(U.weekly_metrics(rows[:3], DATE, 100, tv_div=1e8, flow_div=1e5))   # 5일 미만

    def test_score_direction_symmetry(self):
        m = {"tvMom": 0.5, "flowBp": 25, "frgn1w": 100, "orgn1w": 50, "shortMom": -1.0, "loanBp": -20, "ret1w": 3.0, "shortRlim1w": 2}
        sl, hi, why = U.score_stock(m, "long")
        self.assertEqual(sl, 5)
        self.assertFalse(hi)
        ss, _, _ = U.score_stock(m, "short")
        self.assertEqual(ss, 2 - 1 - 1 - 1 + 0)      # 거래대금 +2, 수급 −1(역방향은 −1 고정), 공매도 −1, 대차 −1, 가격 불일치 0
        m2 = {"tvMom": 0.5, "flowBp": -25, "frgn1w": -100, "orgn1w": -50, "shortMom": 1.0, "loanBp": 20, "ret1w": -3.0, "shortRlim1w": 6}
        ss2, hi2, _ = U.score_stock(m2, "short")
        self.assertEqual(ss2, 5)                     # 상한 5 (6→5 클램프)
        self.assertTrue(hi2)
        self.assertEqual(U.score_stock({}, "long")[0], 0)

    def test_short_loan_metrics(self):
        f = M.MockHubFlow().one("017670")
        r = U.short_loan_metrics(f, 171187)
        self.assertIsNotNone(r["shortRlim1w"])
        self.assertIsNotNone(r["shortMom"])
        self.assertIsNotNone(r["loanChg"])
        self.assertEqual(U.short_loan_metrics(None, 100)["shortRlim1w"], None)


class TestSectorResolve(unittest.TestCase):
    def test_resolve_db_sectors(self):
        dbq = M.MockDbQuote()
        sm = M.mock_sector_map()
        res = U.resolve_db_sectors(dbq, sm, codes=[str(1005 + i) for i in range(len(dbq.sectors))] + ["1999"], log=lambda *a: None)
        names = {r["name"] for r in res}
        self.assertEqual(names, {s["name"] for s in dbq.sectors})
        self.assertTrue(all(r["overlap"] >= 0.3 for r in res))
        # USTOCKCOND 가 비면 겹침 해석으로 대체되고 캐시됨
        import tempfile
        st = store.FileStore(tempfile.mkdtemp())
        dbq.industry_codes = lambda mkt="K": []
        inds, how = U.sector_codes(dbq, st, DATE, sector_map=sm, log=lambda *a: None)
        self.assertEqual(how, "overlap")
        self.assertEqual({i["name"] for i in inds}, names)
        inds2, how2 = U.sector_codes(dbq, st, DATE, sector_map=sm)
        self.assertEqual(how2, "cache")
        self.assertEqual(inds2, inds)
        # build_universe 도 이 경로로 업종을 찾는다
        doc = U.build_universe(DATE, wb(), dbq=dbq, hub=M.MockHubFlow(), store=st, log=lambda *a: None)
        self.assertEqual(doc["diag"]["sectorSource"], "cache")
        self.assertGreater(doc["summary"]["sectors"], 0)


class TestBuild(unittest.TestCase):
    def test_build_doc(self):
        dbq, hub = mocks()
        ledger = {"positions": [{"code": "011200", "side": "long"}], "orders": [{"code": "017670", "side": "short"}]}
        doc = U.build_universe(DATE, wb(), dbq=dbq, hub=hub, ledger=ledger, decided={"086280": "2026-10-10"}, log=lambda *a: None)
        names = [s["name"] for s in doc["sectors"]]
        self.assertNotIn("화학", names)                           # sectorFlow 에 없는 업종은 제외
        self.assertIn("화학", doc["diag"]["unmatched"])
        by = {s["name"]: s for s in doc["sectors"]}
        self.assertEqual(by["운송·창고"]["dir"], "long")
        self.assertEqual(by["통신"]["dir"], "short")
        self.assertIsNone(by["보험"]["dir"])
        self.assertTrue(by["제조"]["big"])
        self.assertEqual(len(by["제조"]["top"]), 3)               # 대분류·혼조는 관찰 3개, 지표 없음
        self.assertIsNone(by["제조"]["top"][0]["score"])
        # 방향 업종: Top ≤ 10, 점수 내림차순, 플래그
        top = by["운송·창고"]["top"]
        self.assertLessEqual(len(top), config.UNI_TOPN)
        self.assertEqual([x["score"] for x in top], sorted([x["score"] for x in top], reverse=True))
        hmm = next(x for x in top if x["code"] == "011200")
        self.assertTrue(hmm["held"])
        self.assertIn("held", hmm["flags"])
        self.assertTrue(any(x["hasStockFuture"] for x in top))
        glv = next((x for x in top if x["code"] == "086280"), None)
        if glv:
            self.assertEqual(glv["done"], "2026-10-10")
        for x in top:
            self.assertEqual(x["side"], "long")
            self.assertIn("tv1w", x)
            self.assertIn("shortRlim1w", x)
            self.assertGreaterEqual(x["tv20d"], config.UNI_MIN_TV_EOK)
        # 정렬: long → short → 관찰
        dirs = [s["dir"] for s in doc["sectors"]]
        self.assertEqual(dirs, sorted(dirs, key=lambda d: {"long": 0, "short": 1, None: 2}[d]))
        # 요약·진단
        sm = doc["summary"]
        self.assertEqual(sm["sectors"], 5)
        self.assertEqual(sm["signaled"], 3)
        self.assertEqual(sm["big"], 1)
        self.assertGreater(sm["cut"], 0)
        self.assertIn("components", doc["diag"]["rawSample"])
        self.assertIn("investor", doc["diag"]["rawSample"])
        self.assertEqual(doc["diag"]["calls"]["db"], dbq.calls)
        json.dumps(doc)                                           # 직렬화 가능

    def test_run_universe_store_and_skip(self):
        import tempfile
        dbq, hub = mocks()
        with tempfile.TemporaryDirectory() as d:
            st = store.FileStore(d)
            r = U.run_universe(DATE, st, wb(), dbq=dbq, hub=hub, log=lambda *a: None)
            self.assertTrue(r.get("ok"))
            self.assertIsNotNone(st.get(f"universe/{DATE}"))
            self.assertEqual(U.run_universe(DATE, st, wb(), dbq=dbq, hub=hub)["skip"], "이미 산출됨")
            self.assertEqual(U.run_universe("2026-10-11", st, wb(), dbq=dbq, hub=hub)["skip"], "휴장일")
            calls = dbq.calls
            U.run_universe(DATE, st, wb(), dbq=dbq, hub=hub, force=True, log=lambda *a: None)
            self.assertGreater(dbq.calls, calls)

    def test_component_failure_is_isolated(self):
        dbq, hub = mocks()
        orig = dbq.industry_components

        def boom(code, sort="42"):
            if code == "1005":
                raise RuntimeError("IGW00001")
            return orig(code, sort)
        dbq.industry_components = boom
        doc = U.build_universe(DATE, wb(), dbq=dbq, hub=hub, log=lambda *a: None)
        bad = next(s for s in doc["sectors"] if s.get("error"))
        self.assertEqual(bad["top"], [])
        self.assertTrue(any("USTOCKCONDLIST" in e for e in doc["diag"]["errors"]))
        self.assertGreater(sum(len(s["top"]) for s in doc["sectors"]), 0)


class TestDbQuoteClient(unittest.TestCase):
    """DbQuote.post — 헤더·본문·연속조회·오류(가짜 post)."""

    class R:
        def __init__(self, data, headers=None, status=200):
            self.data, self.headers, self.status_code = data, headers or {}, status

        def json(self):
            return self.data

    def test_rows_of_shapes(self):
        R = U.DbQuote._rows_of
        self.assertEqual(R({"Out": [{"a": 1}]}), [{"a": 1}])
        self.assertEqual(R({"Out": {"list": [{"a": 1}, {"a": 2}]}}), [{"a": 1}, {"a": 2}])
        self.assertEqual(R({"Out": {"a": 1}}), [{"a": 1}])
        self.assertEqual(R({"Out2": [{"b": 1}]}), [{"b": 1}])
        self.assertEqual(R({}), [])
        self.assertEqual(U._pick_code_name({"BstpCode": "1005", "BstpNm": "음식료·담배", "x": "1"}), ("1005", "음식료·담배"))
        self.assertEqual(U._pick_code_name({"Iscd": "1021", "KorIsnm": "운송·창고"}), ("1021", "운송·창고"))

    def test_post_cont_and_errors(self):
        calls = []

        def post(url, headers=None, json=None, timeout=None):
            calls.append((url, headers, json))
            if headers["cont_yn"] == "N":
                return self.R({"rsp_cd": "00000", "Out": [{"Iscd": "1"}]}, {"cont_yn": "Y", "cont_key": "K1"})
            return self.R({"rsp_cd": "00000", "Out": [{"Iscd": "2"}]}, {"cont_yn": "N", "cont_key": ""})

        class Tok:
            rest_base = "https://x:8443"

            def _token(self):
                return "T0K"
        q = U.DbQuote(token_source=Tok(), post=post, sleep=lambda s: None, now=lambda: 0.0)
        rows = q.post(U.DbQuote.P_COMP, {"a": 1})
        self.assertEqual([r["Iscd"] for r in rows], ["1", "2"])
        self.assertEqual(calls[0][0], "https://x:8443" + U.DbQuote.P_COMP)
        self.assertEqual(calls[0][1]["authorization"], "Bearer T0K")
        self.assertEqual(calls[1][1]["cont_key"], "K1")
        self.assertEqual(calls[0][2], {"In": {"a": 1}})
        self.assertEqual(q.calls, 2)

        def bad(url, headers=None, json=None, timeout=None):
            return self.R({"rsp_cd": "IGW00201", "rsp_msg": "유량"})
        q2 = U.DbQuote(token_source=Tok(), post=bad, sleep=lambda s: None, now=lambda: 0.0, log=lambda *a: None)
        with self.assertRaises(RuntimeError):
            q2.post(U.DbQuote.P_COMP, {"a": 1})
        self.assertEqual(q2.industry_codes(), [])            # 업종 목록은 변형 입력을 모두 시도한 뒤 빈 목록(겹침 해석으로 대체)


if __name__ == "__main__":
    unittest.main()
