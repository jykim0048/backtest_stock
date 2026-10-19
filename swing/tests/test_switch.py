"""스위칭(2026-10-19, PLAN_LIVE_TRADER §2-1) — 시가 일괄 현금 확보 청산 대신 Live Trader 가 현금 한도 매수 +
수익률 낮은 보유 종목 VWAP 분할 재원 매도. 엔진은 재원 매도를 진입 전에 반영하고 시가 청산은 기본 끔."""
import tempfile
import unittest

from swing import config, engine as E, store
from swing.tests.test_daily import b
from swing.tests.test_vwap_exec import D1, ledger_with, path, run, ts
from swing.vwap import config as VC
from swing.vwap.executor import Executor

ORIG = (VC.TRANCHES, VC.MIN_GAP_MIN)


def setUpModule():
    VC.TRANCHES, VC.MIN_GAP_MIN = 3, 20


def tearDownModule():
    VC.TRANCHES, VC.MIN_GAP_MIN = ORIG


def book(cash, positions=(), order=True):
    L = ledger_with(order={} if order else None)
    L["cash"] = cash
    for i, (code, entry, qty) in enumerate(positions, 1):
        L["positions"].append({"id": f"p{i}", "code": code, "name": code, "side": "long", "qty": qty, "entry": float(entry),
                               "stop": entry * 0.5, "target": None, "fillDate": "2026-10-16", "holdDay": 2,
                               "lastClose": float(entry), "sellPending": None})
    return L


def merge(*streams):
    return sorted([t for s in streams for t in s], key=lambda t: t.ts)


def fills(r):
    return [(f["ts"][11:16], f["qty"], f["price"], f["mode"]) for f in r["fills"]]


class TestCashLimitedBuy(unittest.TestCase):
    def test_open_slice_limited_by_cash(self):
        ex = Executor(store.FileStore(tempfile.mkdtemp()), D1, book(50_000), log=lambda *_: None)
        run(ex, path("000010", [("09:00", 10000)]))
        r = ex.doc["orders"]["o1"]
        self.assertEqual(fills(r), [("09:00", 5, 10000, "open")])          # 30주 계획 → 현금 5만 원어치 5주
        self.assertEqual(r["plan"][:2], [5, 115])                          # 못 산 25주는 다음 분할로
        self.assertAlmostEqual(ex.cash(), 0)

    def test_no_switch_when_cash_enough(self):
        ex = Executor(store.FileStore(tempfile.mkdtemp()), D1, book(10_000_000, [("000020", 5000, 100)]), log=lambda *_: None)
        run(ex, merge(path("000010", [("09:00", 10000), ("09:10", 9990)]), path("000020", [("09:00", 5000), ("09:10", 5200)])))
        self.assertNotIn("s:p1", ex.doc["exits"])


class TestSwitching(unittest.TestCase):
    def test_sell_lowest_return_above_vwap_then_buy(self):
        L = book(0, [("000020", 5000, 400), ("000030", 4000, 400)])          # 000020 +4%, 000030 +12.5%
        ex = Executor(store.FileStore(tempfile.mkdtemp()), D1, L, log=lambda *_: None)
        ticks = merge(path("000010", [("09:00", 10000), ("09:05", 9990), ("09:10", 9980), ("09:30", 9970)]),
                      path("000020", [("09:00", 5100), ("09:05", 5150), ("09:10", 5200), ("09:30", 5200)]),
                      path("000030", [("09:00", 4400), ("09:05", 4450), ("09:10", 4500), ("09:30", 4500)]))
        run(ex, ticks)
        self.assertNotIn("s:p2", ex.doc["exits"])                          # 수익률 높은 종목은 남김
        sw = ex.doc["exits"]["s:p1"]
        self.assertEqual({f["mode"] for f in sw["fills"]}, {"vwap"})
        self.assertTrue(all(f["price"] >= f["vwap"] for f in sw["fills"]))  # VWAP 이상에서만 판다
        self.assertNotIn("open", {f["mode"] for f in sw["fills"]})         # 시가에는 안 판다
        self.assertGreater(sum(f["qty"] for f in ex.doc["orders"]["o1"]["fills"]), 0)   # 판 돈으로 매수
        self.assertGreaterEqual(ex.cash(), 0)

    def test_force_sell_on_5pct_drop_from_open(self):
        L = book(0, [("000020", 5000, 400)])
        ex = Executor(store.FileStore(tempfile.mkdtemp()), D1, L, log=lambda *_: None)
        run(ex, merge(path("000010", [("09:00", 10000)]), path("000020", [("09:00", 5000), ("09:02", 4700)])))
        sw = ex.doc["exits"]["s:p1"]
        self.assertEqual(fills(sw)[0][3], "force")
        self.assertEqual(fills(sw)[0][2], 4700)

    def test_stop_sells_only_remaining_after_switch(self):
        L = book(0, [("000020", 5000, 400)])
        L["positions"][0]["stop"] = 4600.0
        ex = Executor(store.FileStore(tempfile.mkdtemp()), D1, L, log=lambda *_: None)
        run(ex, merge(path("000010", [("09:00", 10000), ("09:05", 10000), ("09:10", 10000)]),
                      path("000020", [("09:00", 5000), ("09:05", 5100), ("09:10", 5200), ("09:40", 4590, 4580)])))
        sold = sum(f["qty"] for f in ex.doc["exits"]["s:p1"]["fills"])
        self.assertGreater(sold, 0)
        self.assertEqual(ex.doc["exits"]["p1"]["planned"], 400 - sold)
        self.assertTrue(ex.doc["exits"]["s:p1"]["done"])


class TestEngineSwitch(unittest.TestCase):
    def test_switch_applied_before_entry_no_open_liquidation(self):
        L = book(0, [("000020", 5000, 400), ("000030", 5000, 400)])
        I = {"date": D1, "asof": ts("15:30"), "complete": True, "watched": {"orders": ["o1"], "positions": ["p1", "p2"]},
             "orders": {"o1": {"fills": [{"ts": ts("09:30"), "qty": 150, "price": 10000.0, "mode": "vwap"}]}},
             "exits": {"s:p1": {"trigger": "switch", "kind": "switch", "positionId": "p1",
                                "fills": [{"ts": ts("09:20"), "qty": 302, "price": 5000.0, "mode": "vwap"}]}}}
        ev = E.settle_day(L, D1, {"000010": b(10000, 10100, 9900, 10000), "000020": b(5000, 5100, 4900, 5000),
                                  "000030": b(4500, 4600, 4400, 4500)}, intraday=I)
        reasons = [t["reason"] for t in ev["exits"]]
        self.assertEqual(reasons, ["switch"])                              # 손실 종목(000030) 시가 청산 없음
        pos = {p["code"]: p["qty"] for p in L["positions"]}
        self.assertEqual(pos, {"000020": 98, "000030": 400, "000010": 150})
        self.assertGreaterEqual(L["cash"], 0)

    def test_daily_rule_without_trader_partial_no_liquidation(self):
        L = book(1_000_000, [("000030", 5000, 400)])                       # 현금 100만, 주문 300주 × 1만 = 300만
        ev = E.settle_day(L, D1, {"000010": b(10000, 10100, 9900, 10000), "000030": b(4500, 4600, 4400, 4500)})
        self.assertFalse(ev["exits"])                                     # 시가 청산 안 함(기본)
        self.assertEqual(L["orderLog"][-1]["status"], "skipped")           # 살 수 있는 100주 < 50% → 종전 규칙대로 스킵
        self.assertFalse(config.RAISE_CASH_AT_OPEN)

    def test_intraday_fills_over_cash_accepted_partially(self):
        L = book(500_000)
        I = {"date": D1, "asof": ts("15:30"), "complete": True, "watched": {"orders": ["o1"], "positions": []},
             "orders": {"o1": {"fills": [{"ts": ts("09:30"), "qty": 100, "price": 10000.0, "mode": "vwap"}]}}, "exits": {}}
        E.settle_day(L, D1, {"000010": b(10000, 10100, 9900, 10000)}, intraday=I)
        self.assertEqual(L["positions"][0]["qty"], 50)                     # 장중 체결분은 최소 비율 없이 살 수 있는 만큼


if __name__ == "__main__":
    unittest.main()
