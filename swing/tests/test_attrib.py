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


DK = "decision/2026-09-01/000001/entry"


def setup_dk():
    """판단 2026-09-01(종가 9,900) → 09-02 10,000 에 10주 체결."""
    L = E.new_ledger(1_000_000, start="2026-09-01")
    E.place_entry(L, code="000001", name="A", date="2026-09-01", valid_date="2026-09-02",
                  entry=10000, stop=9000, target=12000, weight=0.1, decision_key=DK)
    L["orders"][0]["sector"] = "전기·전자"
    E.settle_day(L, "2026-09-02", {"000001": bar(10000, 10200, 9900, 10100)})
    return L


def closer(m):
    return lambda code, d: m.get((code, d))


CL = {("000001", "2026-09-01"): 9900, ("000001", "2026-09-04"): 10400}


class TestSplit(unittest.TestCase):
    """선정·트레이딩 분해(2026-11-06) — 선정 + 진입 + 청산·비용 = 손익."""

    def closed(self):
        L = setup_dk()
        E.settle_day(L, "2026-09-03", {"000001": bar(10100, 10400, 10000, 10300)})
        E.close_position(L, L["positions"][0], "2026-09-04", 10500, "pm_sell")      # 장중 10,500 청산, 그날 종가 10,400
        E.settle_day(L, "2026-09-04", {"000001": bar(10300, 10600, 10200, 10400)})
        return L

    def test_closed_round(self):
        L = self.closed()
        e = attrib.compute(L, [], equity_now=eq(L), close=closer(CL))["all"]["byCode"][0]
        tax = round(10500 * 10 * config.SELL_TAX)
        self.assertEqual(e["split"]["sel"], 10 * (10400 - 9900))                  # 판단일 종가 → 청산일 종가
        self.assertEqual(e["split"]["ent"], 10 * (9900 - 10000))                  # 판단일 종가보다 비싸게 삼
        self.assertEqual(e["split"]["ext"], 10 * (10500 - 10400))                 # 청산가 − 그날 종가
        self.assertEqual(e["split"]["cost"], -tax)                                # 매도세는 비용으로 따로
        self.assertEqual(sum(e["split"][k] for k in ("sel", "ent", "ext", "cost")), e["pnl"])

    def test_missed_fill_moves_to_entry(self):
        L = self.closed()
        L["orderLog"][0]["partial"] = {"planned": 15, "filled": 10, "remain": 5}
        e = attrib.compute(L, [], equity_now=eq(L), close=closer(CL))["all"]["byCode"][0]
        miss = 5 * (10400 - 9900)
        self.assertEqual((e["split"]["sel"], e["split"]["ent"], e["split"]["miss"]), (5000 + miss, -1000 - miss, miss))
        L["orders"].append({**L["orderLog"][0], "id": "oX", "status": "open"})     # 재주문 진행 중이면 미체결 계산 보류
        e = attrib.compute(L, [], equity_now=eq(L), close=closer(CL))["all"]["byCode"][0]
        self.assertEqual(e["split"]["miss"], 0)

    def test_short_costs_split(self):
        L = E.new_ledger(1_000_000, start="2026-09-01")
        dk = "decision/2026-09-01/000002/entry-short"
        E.place_entry(L, code="000002", name="B", date="2026-09-01", valid_date="2026-09-02", side="short",
                      entry=10000, stop=11000, target=9000, weight=0.1, decision_key=dk)
        E.settle_day(L, "2026-09-02", {"000002": bar(10000, 10100, 9800, 9900)})
        E.settle_day(L, "2026-09-03", {"000002": bar(9900, 9950, 9600, 9700)})
        t = E.close_position(L, L["positions"][0], "2026-09-04", 9500, "pm_sell")
        E.settle_day(L, "2026-09-04", {"000002": bar(9700, 9700, 9400, 9600)})
        cl = closer({("000002", "2026-09-01"): 10100, ("000002", "2026-09-04"): 9600})
        e = attrib.compute(L, [], equity_now=eq(L), close=cl)["all"]["byCode"][0]
        q = t["qty"]
        self.assertGreater(t["tax"] + t["borrowFee"], 0)
        self.assertEqual(e["split"]["sel"], -q * (9600 - 10100))                  # 숏: 판단일 종가보다 내려 +
        self.assertEqual(e["split"]["ent"], -q * (10100 - 10000))                 # 판단일 종가보다 싸게 공매도 = −
        self.assertEqual(e["split"]["ext"], -q * (9500 - 9600))                   # 종가보다 싸게 환매 = +
        self.assertEqual(e["split"]["cost"], -round(t["tax"] + t["borrowFee"]))
        self.assertEqual(sum(e["split"][k] for k in ("sel", "ent", "ext", "cost")), e["pnl"])

    def test_missing_close_leaves_unsplit(self):
        L = self.closed()
        a = attrib.compute(L, [], equity_now=eq(L), close=closer({}))["all"]
        self.assertIsNone(a["byCode"][0]["split"])
        self.assertEqual((a["split"]["nNone"], a["split"]["none"]), (1, a["byCode"][0]["pnl"]))

    def test_held_and_day_settled(self):
        L = setup_dk()
        E.settle_day(L, "2026-09-03", {"000001": bar(10100, 10400, 10000, 10300)})
        p = L["positions"][0]
        ps = [{**p, "price": p["lastClose"]}]
        r = attrib.compute(L, ps, equity_now=eq(L), close=closer(CL))
        e = r["all"]["byCode"][0]
        self.assertEqual((e["split"]["sel"], e["split"]["ent"], e["split"]["ext"]), (4000, -1000, 0))
        d = r["day"]["byCode"][0]                                                  # 보유 중인 날 = 전부 선정(보유 판단)
        self.assertEqual((d["split"]["sel"], d["split"]["ent"], d["split"]["ext"]), (2000, 0, 0))

    def test_day_live_with_add(self):
        L = setup_dk()
        E.settle_day(L, "2026-09-03", {"000001": bar(10100, 10400, 10000, 10300)})
        p = L["positions"][0]
        row = {**p, "qty": 12, "entry": (10000 * 10 + 10450 * 2) / 12, "qtyLedger": 10, "entryLedger": 10000, "price": 10600,
               "liveAdd": {"qty": 2, "price": 10450}, "liveAdj": 2 * (10600 - 10450)}
        d = attrib.compute(L, [row], equity_now=eq(L) + 3300, live_day="2026-09-04", close=closer(CL))["day"]["byCode"][0]
        self.assertEqual(d["pnl"], 3300)
        self.assertEqual((d["split"]["sel"], d["split"]["ent"], d["split"]["ext"]), (12 * 300, 2 * (10300 - 10450), 0))


if __name__ == "__main__":
    unittest.main()
