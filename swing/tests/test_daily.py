import sqlite3
import tempfile
import unittest

from swing import agent_iface as A
from swing import daily, prices, store


def wb(date, buy=(), down=(), sec_sig=None):
    rows = [{"name": "화학", "signal": "동반강세"}, {"name": "유통", "signal": sec_sig or "혼조"}]
    return {"asof": f"{date} 16:13 KST",
            "sectorFlow": {"rows": rows},
            "sectorScreen": {"matrix": {
                "동반강세": [{"code": c, "name": c, "sector": s} for c, s in buy],
                "수급유입": [], "수급이탈": [],
                "동반약세": [{"code": c, "name": c, "sector": "유통"} for c in down]}}}


def b(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


class FixedAgent:
    """code → (rating, action, entry, stop, target) / review → rating."""
    name = "fixed"

    def __init__(self, entry=None, review="Hold"):
        self.entry, self.review, self.calls = entry or {}, review, []

    def decide(self, req):
        self.calls.append((req.code, req.purpose))
        d = A.Decision(code=req.code, date=req.date, purpose=req.purpose, agent=self.name)
        if req.purpose == "review":
            d.rating = self.review
            return d
        if req.code == "999999":
            raise RuntimeError("LLM 실패")
        r = self.entry.get(req.code)
        if r:
            d.rating, d.action, d.entry, d.stop, d.target = r
            d.weight = 0.1
        return d


class TestDaily(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())

    def test_flow_entry_review_sell(self):
        ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600),
                         "000020": ("Hold", "Hold", None, None, None)}, review="Sell")
        px = prices.DictPrices({
            "2026-09-21": {"000010": b(10000, 10100, 9900, 10050), "000020": b(5000, 5000, 5000, 5000)},
            "2026-09-22": {"000010": b(10050, 10100, 9950, 10000)},
            "2026-09-23": {"000010": b(10000, 10100, 9900, 10000)},
            "2026-09-28": {"000010": b(9900, 10000, 9800, 9900)}})
        # D1: 후보 2 → 주문 1(000010), Hold 1
        r1 = daily.run_day("2026-09-21", self.st, ag, px,
                           wb("2026-09-21", buy=[("000010", "화학"), ("000020", "화학")]))
        self.assertEqual([c["ordered"] for c in r1["candidates"]], [True, False])
        self.assertEqual(r1["candidates"][1]["why"], "PM Hold")
        L = self.st.get("ledger")
        self.assertEqual(L["orders"][0]["validDate"], "2026-09-22")
        self.assertEqual(L["orders"][0]["qty"], 5000)          # 10% × 5억 / 1만
        # D2: 체결, 섹터 신호 없음 → 검토 없음, 보유 종목은 후보에서 제외
        r2 = daily.run_day("2026-09-22", self.st, ag, px,
                           wb("2026-09-22", buy=[("000010", "화학")]))
        self.assertEqual(r2["candidates"], [])
        self.assertEqual(len(self.st.get("ledger")["positions"]), 1)
        # D3: 종목이 동반약세 칸 → 검토 → PM Sell → 매도 예약
        r3 = daily.run_day("2026-09-23", self.st, ag, px, wb("2026-09-23", down=["000010"]))
        self.assertTrue(r3["reviews"][0]["sell"])
        self.assertTrue(self.st.get("ledger")["positions"][0]["sellPending"])
        self.assertTrue(self.st.get("decision/2026-09-23/000010/review"))
        # 9/24·25 추석 휴장 → 다음 영업일 9/28 시가 매도
        self.assertEqual(daily.run_day("2026-09-24", self.st, ag, px, None)["skip"], "휴장일")
        daily.run_day("2026-09-28", self.st, ag, px, wb("2026-09-28"))
        t = self.st.get("ledger")["trades"][0]
        self.assertEqual((t["reason"], t["exitPrice"]), ("pm_sell", 9900))

    def test_stale_wb_no_decisions_but_settles(self):
        ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, None)})
        px = prices.DictPrices({"2026-09-21": {"000010": b(1, 1, 1, 10000)},
                                "2026-09-22": {"000010": b(10000, 10000, 9900, 10000)}})
        daily.run_day("2026-09-21", self.st, ag, px, wb("2026-09-21", buy=[("000010", "화학")]))
        r = daily.run_day("2026-09-22", self.st, ag, px, wb("2026-09-21", buy=[("000030", "화학")]))
        self.assertIn("신규·매도 검토 생략", r["notes"][0])
        self.assertEqual(r["candidates"], [])
        self.assertEqual(len(self.st.get("ledger")["positions"]), 1)   # 정산은 진행

    def test_agent_error_isolated_and_idempotent(self):
        ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, None)})
        px = prices.DictPrices({"2026-09-21": {"000010": b(1, 1, 1, 10000)}})
        r = daily.run_day("2026-09-21", self.st, ag, px,
                          wb("2026-09-21", buy=[("999999", "화학"), ("000010", "화학")]))
        self.assertIn("판단 실패", r["candidates"][0]["why"])
        self.assertTrue(r["candidates"][1]["ordered"])
        again = daily.run_day("2026-09-21", self.st, ag, px, wb("2026-09-21"))
        self.assertIn("이미 처리됨", again["skip"])

    def test_mock_agent_runs(self):
        px = prices.MockPrices()
        r = daily.run_day("2026-09-21", self.st, A.MockAgent(), px,
                          wb("2026-09-21", buy=[("000010", "화학"), ("000017", "화학")]))
        self.assertEqual([c["ordered"] for c in r["candidates"]], [True, False])


class TestSqlStore(unittest.TestCase):
    def test_roundtrip_sqlite(self):
        path = tempfile.mktemp(suffix=".db")
        s = store.SqlStore(lambda: sqlite3.connect(path), "?")
        s.put("ledger", {"a": 1, "한글": "값"})
        s.put("ledger", {"a": 2})
        s.put("decision/2026-09-21/000010/entry", {"x": 1})
        self.assertEqual(s.get("ledger"), {"a": 2})
        self.assertIsNone(s.get("none"))
        self.assertEqual(s.keys("decision/"), ["decision/2026-09-21/000010/entry"])

    def test_filestore_keys(self):
        s = store.FileStore(tempfile.mkdtemp())
        s.put("run/2026-09-21", {})
        s.put("ledger", {})
        self.assertEqual(s.keys("run/"), ["run/2026-09-21"])


if __name__ == "__main__":
    unittest.main()
