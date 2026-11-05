"""성과 기여도(swing/attrib.py, 2026-11-05) — 정산 뒤 당일·누적 합계가 평가액 변화와 맞는지.
python -m unittest discover -s swing/tests -t ."""
import unittest

from swing import attrib, config, engine as E


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def setup():
    L = E.new_ledger(1_000_000, start="2026-09-01")
    E.place_entry(L, code="000001", name="A", date="2026-09-01", valid_date="2026-09-02",
                  entry=10000, stop=9000, target=12000, weight=0.1)
    L["orders"][0]["sector"] = "전기·전자"
    E.settle_day(L, "2026-09-02", {"000001": bar(10000, 10200, 9900, 10100)})
    return L


def eq(L):
    return L["equity"][-1]["equity"]


class TestAttrib(unittest.TestCase):
    def test_engine_records_prev_close(self):
        L = setup()
        p = L["positions"][0]
        self.assertEqual((p.get("prevClose"), p["lastClose"]), (None, 10100))   # 진입일은 전일 종가 없음
        E.settle_day(L, "2026-09-03", {"000001": bar(10100, 10400, 10000, 10300)})
        self.assertEqual((p["prevClose"], p["lastClose"]), (10100, 10300))
        t = E.close_position(L, p, "2026-09-04", 10500, "pm_sell")
        self.assertEqual(t["prevClose"], 10300)

    def test_day_settled_matches_equity_change(self):
        L = setup()
        r = attrib.compute(L, [], equity_now=eq(L))
        self.assertTrue(r["day"]["available"])                                # 진입일 = 체결가 기준
        self.assertEqual(r["day"]["byCode"][0]["pnl"], 1000)                   # (10100 − 10000) × 10
        E.settle_day(L, "2026-09-03", {"000001": bar(10100, 10400, 10000, 10300)})
        r = attrib.compute(L, [], equity_now=eq(L))
        d = r["day"]
        self.assertTrue(d["available"])
        self.assertEqual((d["total"], d["sum"], d["residual"]), (2000, 2000, 0))
        self.assertEqual(d["bySector"][0]["sector"], "전기·전자")
        self.assertAlmostEqual(d["byCode"][0]["contribPct"], 2000 / L["equity"][-2]["equity"] * 100, places=3)
        # 청산일 — (청산가 − 전일 종가) × 수량 − 매도세
        E.close_position(L, L["positions"][0], "2026-09-04", 10500, "pm_sell")
        E.settle_day(L, "2026-09-04", {"000001": bar(10300, 10600, 10200, 10500)})
        d = attrib.compute(L, [], equity_now=eq(L))["day"]
        self.assertTrue(d["available"])
        self.assertEqual(d["residual"], 0, d)
        self.assertEqual(d["byCode"][0]["realized"], round(2000 - 10500 * 10 * config.SELL_TAX))
        # 누적 — 실현손익 합 = 평가액 − 초기 자본
        a = attrib.compute(L, [], equity_now=eq(L))["all"]
        self.assertEqual((a["residual"], a["byCode"][0]["held"]), (0, False))

    def test_missing_prev_close_marks_unavailable(self):
        L = setup()
        E.settle_day(L, "2026-09-03", {"000001": bar(10100, 10400, 10000, 10300)})
        L["positions"][0].pop("prevClose")                                     # 기록 추가 전 정산분
        d = attrib.compute(L, [], equity_now=eq(L))["day"]
        self.assertFalse(d["available"])
        self.assertIn("note", d)

    def test_day_live_uses_ledger_qty_and_adjustment(self):
        L = setup()
        ps = [{"id": "p1", "code": "000001", "name": "A", "side": "long", "sector": "전기·전자", "qty": 6, "qtyLedger": 10,
               "entry": 10000, "entryLedger": 10000, "lastClose": 10100, "price": 10300, "liveAdj": -4 * 100,   # 4주 10200 청산
               "liveExit": {"qty": 4}, "liveRealized": 800},
              {"id": "live:o9", "code": "000002", "name": "B", "side": "short", "sector": "화학", "qty": 5, "entry": 20000,
               "price": 19800, "liveRealized": 0}]
        r = attrib.compute(L, ps, equity_now=eq(L) + 1600 + 1000, live_day="2026-09-03")
        d = r["day"]
        self.assertEqual([(e["code"], e["pnl"]) for e in d["byCode"]], [("000001", 1600), ("000002", 1000)])
        self.assertEqual(d["residual"], 0)
        self.assertEqual({g["sector"] for g in d["bySector"]}, {"전기·전자", "화학"})


if __name__ == "__main__":
    unittest.main()
