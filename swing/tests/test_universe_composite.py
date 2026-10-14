"""멀티기간 수익률·백분위 점수·종가 캐시(2026-10-14) — 오프라인."""
import tempfile
import unittest

from swing import config, store, universe as U, universe_mock as M


class TestReturns(unittest.TestCase):
    def test_period_returns(self):
        from swing import tradedays
        dates = tradedays.trading_days("2025-01-01", "2026-10-13")
        closes = [100.0 + i for i in range(len(dates))]
        r = U.period_returns(dates, closes, "2026-10-13")
        last = closes[-1]
        self.assertAlmostEqual(r["ret1m"], round((last / closes[-22] - 1) * 100, 1))
        self.assertAlmostEqual(r["ret12m"], round((last / closes[-253] - 1) * 100, 1))
        prev_year_last = [c for d, c in zip(dates, closes) if d < "2026-01-01"][-1]
        self.assertAlmostEqual(r["retYtd"], round((last / prev_year_last - 1) * 100, 1))
        short = U.period_returns(dates[-30:], closes[-30:], "2026-10-13")
        self.assertIsNotNone(short["ret1m"])
        self.assertIsNone(short["ret3m"])
        self.assertIsNone(short["retYtd"])
        self.assertEqual(U.period_returns([], [], "2026-10-13")["ret1m"], None)
        # as-of: date 이후 행은 무시
        r2 = U.period_returns(dates, closes, "2026-06-30")
        self.assertLess(r2["ret1m"], r["ret1m"] + 100)

    def test_percentile(self):
        xs = [1, 2, 3, 4, 5]
        self.assertEqual(U.percentile(xs, 5), 90.0)
        self.assertEqual(U.percentile(xs, 1), 10.0)
        self.assertEqual(U.percentile(xs, 3), 50.0)
        self.assertEqual(U.percentile([7], 7), 50.0)
        self.assertIsNone(U.percentile(xs, None))
        self.assertIsNone(U.percentile([None, None], 1))


class TestComposite(unittest.TestCase):
    def _sectors(self):
        def st(code, **kw):
            x = {"code": code, "score": 3, "tv20d": 50, "ret1m": 5, "ret3m": 10, "ret6m": 20, "ret12m": 30, "flowBp": 10, "shortMom": -0.5, "loanBp": -5, "tvMom": 0.2}
            x.update(kw)
            return x
        return [{"name": "A", "dir": "long", "top": [st("L1", ret1m=20, ret3m=40, ret6m=60, ret12m=80, flowBp=50, tv20d=500),
                                                     st("L2"), st("L3", ret1m=-10, ret3m=-20, ret6m=-30, ret12m=-40, flowBp=-20, tv20d=5)]},
                {"name": "B", "dir": "short", "top": [st("S1", ret1m=-30, ret3m=-40, ret6m=-50, ret12m=-60, flowBp=-40, shortMom=1.0, loanBp=10),
                                                      st("S2", ret1m=30, ret3m=40, ret6m=50, ret12m=60, flowBp=40)]},
                {"name": "C", "dir": None, "top": [st("X", score=None)]}]

    def test_liq_pool_all_vs_side(self):
        """유동성 백분위 풀 — all(Long·Short 합산, 기본) vs side(방향별). Short 풀이 대형주뿐일 때 side 는 과도하게 엄격."""
        def secs():
            def st(code, tv):
                return {"code": code, "score": 3, "tv20d": tv, "ret1m": 1, "ret3m": 1, "flowBp": 1, "tvMom": 0.1}
            return [{"name": "A", "dir": "long", "top": [st(f"L{i}", tv) for i, tv in enumerate((25, 30, 40, 60, 80, 120, 200, 400))]},
                    {"name": "B", "dir": "short", "top": [st("S1", 200), st("S2", 900), st("S3", 1500), st("S4", 3000)]}]
        side = {x["code"]: x for s in U.annotate_composite(secs(), liq_gate=30, liq_pool="side") for x in s["top"]}
        alls = {x["code"]: x for s in U.annotate_composite(secs(), liq_gate=30, liq_pool="all") for x in s["top"]}
        self.assertTrue(side["S1"]["liqGate"])                    # 방향별: Short 4종목 중 최하 → 200억인데 게이트
        self.assertFalse(alls["S1"]["liqGate"])                   # 합산: 12종목 중 중간 → 통과
        self.assertEqual(alls["S1"]["liqScore"], alls["L6"]["liqScore"])   # 같은 금액이면 방향 무관 같은 점수
        self.assertTrue(alls["L0"]["liqGate"])

    def test_observe_sectors_scored_without_entering_pool(self):
        """관찰 업종(대분류·혼조, dir None) 종목은 같은 방향 풀 기준으로 채점만 — 풀 종목 점수는 그대로."""
        base = {x["code"]: x for s in U.annotate_composite(self._sectors()) for x in s["top"]}
        secs = self._sectors()
        secs.append({"name": "제조", "dir": None, "big": True, "top": [
            {"code": "B1", "score": 2, "side": "long", "observe": True, "tv20d": 9999, "ret1m": 99, "ret3m": 99, "ret6m": 99,
             "ret12m": 99, "flowBp": 999, "shortMom": -9, "loanBp": -99, "tvMom": 9}]})
        got = {x["code"]: x for s in U.annotate_composite(secs) for x in s["top"]}
        for c in ("L1", "L2", "L3", "S1", "S2"):
            self.assertEqual((got[c]["pxScore"], got[c]["flowScore"], got[c]["liqScore"], got[c]["totalScore"]),
                             (base[c]["pxScore"], base[c]["flowScore"], base[c]["liqScore"], base[c]["totalScore"]))
        self.assertGreaterEqual(got["B1"]["pxScore"], 90)                 # 풀 최상위보다 높으면 ~100 상당
        self.assertIsNotNone(got["B1"]["totalScore"])

    def test_liq_modes(self):
        """유동성 반영 방식(2026-10-14 시뮬 비교): gate 공란 / weight 가중합 / soft 비례 감점."""
        g = {x["code"]: x for s in U.annotate_composite(self._sectors(), liq_mode="gate", liq_gate=30) for x in s["top"]}
        w = {x["code"]: x for s in U.annotate_composite(self._sectors(), liq_mode="weight", w_px=0.5, w_flow=0.4, w_liq=0.1,
                                                         liq_gate=30) for x in s["top"]}
        s_ = {x["code"]: x for s in U.annotate_composite(self._sectors(), liq_mode="soft", liq_gate=30) for x in s["top"]}
        L3g, L3w, L3s = g["L3"], w["L3"], s_["L3"]
        self.assertIsNone(L3g["totalScore"])
        self.assertTrue(L3g["liqGate"])
        self.assertAlmostEqual(L3w["totalScore"], round(0.5 * L3w["pxScore"] + 0.4 * L3w["flowScore"] + 0.1 * L3w["liqScore"], 1))
        self.assertFalse(L3w["liqGate"])
        base = 0.55 * L3s["pxScore"] + 0.45 * L3s["flowScore"]
        self.assertAlmostEqual(L3s["totalScore"], round(base * L3s["liqScore"] / 30, 1))
        self.assertEqual(L3s["liqPenalty"], round(L3s["liqScore"] / 30, 3))
        self.assertEqual(g["L1"]["totalScore"], s_["L1"]["totalScore"])     # 유동성 충분하면 gate·soft 같음

    def test_scores_and_gate(self):
        secs = U.annotate_composite(self._sectors(), w_px=0.55, w_flow=0.45, liq_gate=30)
        L = {x["code"]: x for x in secs[0]["top"]}
        self.assertGreater(L["L1"]["pxScore"], L["L2"]["pxScore"])
        self.assertGreater(L["L2"]["pxScore"], L["L3"]["pxScore"])
        self.assertGreater(L["L1"]["flowScore"], L["L3"]["flowScore"])
        self.assertAlmostEqual(L["L1"]["totalScore"], round(0.55 * L["L1"]["pxScore"] + 0.45 * L["L1"]["flowScore"], 1))
        self.assertTrue(L["L3"]["liqGate"])                      # tv20d 5 → 유동성 백분위 최하 → 게이트
        self.assertIsNone(L["L3"]["totalScore"])
        self.assertIsNotNone(L["L1"]["totalScore"])
        # Short 은 하락·순매도가 높은 점수
        S = {x["code"]: x for x in secs[1]["top"]}
        self.assertGreater(S["S1"]["pxScore"], S["S2"]["pxScore"])
        self.assertGreater(S["S1"]["flowScore"], S["S2"]["flowScore"])
        self.assertNotIn("pxScore", secs[2]["top"][0])            # 방향 없는 업종은 미산출

    def test_build_universe_has_composite(self):
        dbq, hub = M.MockDbQuote(), M.MockHubFlow()
        wb = {"asof": "2026-10-12 16:13 KST", "sectorFlow": {"rows": [{"name": "운송·창고", "signal": "동반강세"}, {"name": "통신", "signal": "동반약세"}]}}
        st = store.FileStore(tempfile.mkdtemp())
        doc = U.build_universe("2026-10-12", wb, dbq=dbq, hub=hub, store=st, log=lambda *a: None)
        top = doc["sectors"][0]["top"]
        self.assertTrue(top)
        for x in top:
            for k in ("ret1m", "ret3m", "ret12m", "pxScore", "liqScore", "flowScore"):
                self.assertIn(k, x)
            self.assertIsNotNone(x["ret12m"])
        self.assertTrue(any(x.get("totalScore") is not None for x in top))
        self.assertIn("totalScore", doc["rankLong"][0])
        self.assertGreater(doc["diag"]["calls"]["px"], 0)
        self.assertTrue(st.keys("uni_cache/px/"))
        # 둘째 날: 캐시 증분 — 조회는 있지만 범위가 작다(호출 수는 종목당 1)
        pxc = U.PriceCache(st, dbq)
        d, c = pxc.closes(top[0]["code"], "2026-10-13")
        self.assertEqual(pxc.calls, 1)
        self.assertLessEqual(len(d), config.UNI_PX_KEEP)
        d2, _ = pxc.closes(top[0]["code"], "2026-10-13")
        self.assertEqual(pxc.calls, 1)                            # 메모리 재사용
        self.assertEqual(d, d2)


if __name__ == "__main__":
    unittest.main()
