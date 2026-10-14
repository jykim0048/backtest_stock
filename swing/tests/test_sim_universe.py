"""유니버스 선정 방식 A/B/C 시뮬(swing/tools/sim_universe.py) — 오프라인 mock."""
import tempfile
import unittest

from swing import store, universe_mock as M
from swing.tools import sim_universe as S

FROM, TO = "2026-09-09", "2026-09-26"


def day_doc():
    """선정 규칙 단위 테스트용 미니 유니버스."""
    def st(code, sector, score, tv):
        return {"code": code, "name": code, "sector": sector, "signal": "동반강세", "score": score, "tv1w": tv, "rank": 1}
    secs = [{"name": "금속", "signal": "동반강세", "dir": "long", "top": [st("A1", "금속", 5, 900), st("A2", "금속", 5, 800), st("A3", "금속", 4, 700)]},
            {"name": "건설", "signal": "동반강세", "dir": "long", "top": [st("B1", "건설", 4, 600), st("B2", "건설", 3, 500)]},
            {"name": "화학", "signal": "동반강세", "dir": "long", "top": [st("C1", "화학", 2, 400), st("C2", "화학", 1, 300)]},
            {"name": "통신", "signal": "동반약세", "dir": "short", "top": [st("S1", "통신", 3, 100)]}]
    for s in secs:
        for i, x in enumerate(s["top"]):
            x["rank"] = i + 1
    from swing import universe as U
    return {"sectors": secs, "rankLong": U.integrated_rank(secs, "long", 200), "rankShort": U.integrated_rank(secs, "short", 200)}


class TestSelect(unittest.TestCase):
    def setUp(self):
        self.p = S.default_params(capLong=4, capShort=4)
        self.d = day_doc()

    def test_B_integrated(self):
        self.assertEqual([x["code"] for x in S.select_B(self.d, "long", self.p, set(), set())], ["A1", "A2", "A3", "B1"])

    def test_C_sector_cap(self):
        self.assertEqual([x["code"] for x in S.select_C(self.d, "long", self.p, set(), set())], ["A1", "A2", "B1", "B2"])

    def test_A_round_robin(self):
        # 1바퀴: 금속1(5)·건설1(4)·화학1(2) → 2바퀴: 금속2
        self.assertEqual([x["code"] for x in S.select_A(self.d, "long", self.p, set(), set())], ["A1", "B1", "C1", "A2"])
        # 점수 미달(C2=1)·보유·쿨다운 제외
        out = S.select_A(self.d, "long", self.p, {"A1"}, {"B1"})
        self.assertEqual([x["code"] for x in out], ["A2", "B2", "C1", "A3"])
        self.assertEqual([x["code"] for x in S.select_A(self.d, "short", self.p, set(), set())], ["S1"])


class TestAsof(unittest.TestCase):
    def test_asof_flow_date_formats(self):
        f = {"shorts": [{"date": "20260915", "volRlim": 1}, {"date": "2026-09-14", "volRlim": 2}, {"date": "20260913", "volRlim": 3}],
             "loans": [{"date": "20260915", "rmndAmt": 1}]}
        r = S._asof_flow(f, "2026-09-15")
        self.assertEqual([x["volRlim"] for x in r["shorts"]], [2, 3])
        self.assertEqual(r["loans"], [])


class TestRun(unittest.TestCase):
    def test_mock_run_structure(self):
        st = store.FileStore(tempfile.mkdtemp())
        dbq, hub = M.MockDbQuote(), M.MockHubFlow(d0=TO, n=90)
        doc = S.run(S.default_params(), FROM, TO, dbq=dbq, hub=hub, sig=S.SignalSource(st, None),
                    prices_fetch=M.mock_chart_prices(dbq), store=st, log=lambda *a: None, key="sim/universe_abc/t")
        self.assertEqual(set(doc["methods"]), {"A", "B", "C"})
        self.assertGreater(doc["coverage"]["daysWithUniverse"], 5)          # wb_history 9/9~9/26
        self.assertEqual(set(doc["signalSource"].values()) - {None}, {"history"})
        for m, r in doc["methods"].items():
            mt = r["metrics"]
            self.assertEqual(mt["days"], len(r["series"]))
            self.assertGreater(mt["trades"], 0)
            self.assertTrue(all(s["long"] + s["short"] == s["open"] for s in r["series"]))
            self.assertTrue(all(s["selected"] <= 20 for s in r["series"]))
            self.assertIn("avgHHI", mt)
            self.assertTrue(all(t["side"] in ("long", "short") and t["reason"] in ("expiry", "stop", "stop_gap") for t in r["trades"]))
            # 방향별 보유 ≤ 상한 × 보유일
            self.assertTrue(all(s["long"] <= 10 * 5 and s["short"] <= 10 * 5 for s in r["series"]))
        # C 는 하루 선정에서 업종당 ≤ 2
        self.assertIsNotNone(st.get("sim/universe_abc/t"))
        self.assertIn("A", S.summary_text(doc))

    def test_sector_cap_c_in_run(self):
        from collections import Counter
        d = day_doc()
        p = S.default_params(capLong=10, capShort=10)
        sel = S.select_C(d, "long", p, set(), set())
        self.assertTrue(all(v <= 2 for v in Counter(x["sector"] for x in sel).values()))


if __name__ == "__main__":
    unittest.main()
