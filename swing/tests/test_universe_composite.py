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
