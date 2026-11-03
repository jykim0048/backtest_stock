"""진입 우선순위(2026-11-03) — PM 등급 → 높은 비중 순으로 현금 배분, 조건 안 맞는 상위 종목은 다음 순위를 막지 않는다. 오프라인."""
import tempfile
import unittest

from swing import daily, engine as E, prices, store
from swing.tests.test_daily import FixedAgent, b, wb
from swing.tests.test_vwap_exec import D0, D1, path
from swing.vwap import config as VC
from swing.vwap.executor import Executor

ORIG = (VC.TRANCHES, VC.MIN_GAP_MIN)


def setUpModule():
    VC.TRANCHES, VC.MIN_GAP_MIN = 3, 20


def tearDownModule():
    VC.TRANCHES, VC.MIN_GAP_MIN = ORIG


def order(oid, code, rating, weight, qty=300, entry=10000.0, side="long"):
    return {"id": oid, "code": code, "name": code, "side": side, "type": "entry", "created": D0, "validDate": D1,
            "entry": entry, "stop": entry * (1.05 if side == "short" else 0.95), "target": None, "weight": weight, "qty": qty,
            "status": "open", "source": "matrix", "rating": rating}


def book(cash, *orders):
    L = E.new_ledger(500_000_000, start=D0)
    L["asof"], L["cash"] = D0, cash
    L["orders"].extend(orders)
    return L


def batches(*streams):
    by = {}
    for s in streams:
        for t in s:
            by.setdefault(t.ts, []).append(t)
    return sorted(by.items())


def play(ex, *streams):
    for t_, batch in batches(*streams):
        ex.on_batch(t_, batch)


def qty(ex, oid):
    return sum(f["qty"] for f in ex.doc["orders"][oid]["fills"])


class TestKey(unittest.TestCase):
    def test_rating_beats_weight_then_weight_breaks_ties(self):
        xs = [order("a", "1", "Overweight", 0.07), order("b", "2", "Buy", 0.03), order("c", "3", "Overweight", 0.05),
              order("d", "4", None, 0.09), order("e", "5", "Buy", 0.05)]
        self.assertEqual([x["id"] for x in sorted(xs, key=E.entry_priority)], ["e", "b", "a", "c", "d"])

    def test_short_uses_sell_first(self):
        xs = [order("a", "1", "Underweight", 0.07, side="short"), order("b", "2", "Sell", 0.03, side="short")]
        self.assertEqual([x["id"] for x in sorted(xs, key=E.entry_priority)], ["b", "a"])


class TestLiveTrader(unittest.TestCase):
    def ex(self, L):
        return Executor(store.FileStore(tempfile.mkdtemp()), D1, L, log=lambda *_: None)

    def test_scarce_cash_goes_to_higher_rating_regardless_of_code(self):
        ex = self.ex(book(300_000, order("o1", "000010", "Overweight", 0.09), order("o2", "000090", "Buy", 0.03)))
        play(ex, path("000010", [("09:00", 10000)]), path("000090", [("09:00", 10000)]))
        self.assertEqual((qty(ex, "o2"), qty(ex, "o1")), (30, 0))              # 코드가 뒤여도 Buy 먼저(첫 분할 30주 = 30만 원)

    def test_same_rating_higher_weight_first(self):
        ex = self.ex(book(300_000, order("o1", "000010", "Buy", 0.03), order("o2", "000090", "Buy", 0.08)))
        play(ex, path("000010", [("09:00", 10000)]), path("000090", [("09:00", 10000)]))
        self.assertEqual((qty(ex, "o2"), qty(ex, "o1")), (30, 0))

    def test_next_rank_trades_while_top_rank_waits_for_price(self):
        L = book(10_000_000, order("o1", "000010", "Buy", 0.09), order("o2", "000090", "Overweight", 0.03))
        ex = self.ex(L)
        top = path("000010", [("09:00", 10000), ("09:05", 10300), ("09:10", 10350)])     # 상한 위 → 분할 조건 미달
        nxt = path("000090", [("09:00", 10000), ("09:05", 9990), ("09:10", 9980)])
        play(ex, top, nxt)
        self.assertEqual(qty(ex, "o1"), 30)                                    # 시가 첫 분할만
        self.assertGreater(qty(ex, "o2"), 30)                                  # 다음 순위는 VWAP 분할이 진행

    def test_vwap_slice_cash_goes_to_top_rank_when_both_qualify(self):
        ex = self.ex(book(900_000, order("o1", "000090", "Buy", 0.09), order("o2", "000010", "Overweight", 0.03)))
        rows = [("09:00", 10000), ("09:05", 9990), ("09:10", 9980), ("09:15", 9970)]
        play(ex, path("000010", rows), path("000090", rows))
        self.assertGreater(qty(ex, "o1"), 30)                                  # 시가 뒤 남은 30만 원은 코드가 뒤인 Buy 가 쓴다
        self.assertEqual(qty(ex, "o2"), 30)


class TestSettle(unittest.TestCase):
    def test_cash_short_settlement_fills_higher_rank_first(self):
        L = book(3_000_000, order("o1", "000010", "Overweight", 0.09), order("o2", "000090", "Buy", 0.03))
        ev = E.settle_day(L, D1, {"000010": b(10000, 10100, 9900, 10000), "000090": b(10000, 10100, 9900, 10000)})
        self.assertEqual([o["code"] for o in ev["filled"]], ["000090"])
        self.assertEqual([o["code"] for o in ev["skipped"]], ["000010"])


class TestOrderCarriesRating(unittest.TestCase):
    def test_rating_on_order_and_retry(self):
        st = store.FileStore(tempfile.mkdtemp())
        ag = FixedAgent({"000010": ("Overweight", "Buy", 10000, 9500, 10600)})
        px = prices.DictPrices({"2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
                                "2026-09-22": {"000010": b(10200, 10300, 10150, 10200)}})
        daily.run_day("2026-09-21", st, ag, px, wb("2026-09-21", buy=[("000010", "화학")]), log=lambda *a: None)
        self.assertEqual(st.get("ledger")["orders"][0]["rating"], "Overweight")
        daily.run_day("2026-09-22", st, ag, px, wb("2026-09-22"), log=lambda *a: None)
        o = st.get("ledger")["orders"][0]
        self.assertEqual((o["retry"], o["rating"]), (1, "Overweight"))         # 미도달 재주문도 같은 등급 유지


if __name__ == "__main__":
    unittest.main()
