"""순노출 한도 장중 적용(2026-11-17 사용자 결정, exposure.NET_ENFORCE=intraday) — 정산은 막지 않고 Live Trader 체결 직전·Live PM T8."""
import json
import tempfile
import unittest
from unittest import mock

from swing import daily, prices, store
from swing import engine as E
from swing import exposure as X
from swing.livepm import agent as LA
from swing.livepm import snapshot as SN
from swing.livepm.runner import DayRunner
from swing.tests.test_exposure_glide import WAgent, flat, wb
from swing.tests.test_livepm_core import entry, long_ledger
from swing.tests.test_vwap_exec import D1, ledger_with, path, run, ts
from swing.vwap import config as C
from swing.vwap.executor import Executor
from swing.tests.test_daily import b


class _Intraday(unittest.TestCase):
    """단계 한도를 D1(2026-10-19)에서 켜고, 최종 한도를 작게 잡아 몇 주로 한도에 닿게 한다(원장 exposureGlide 없음 → 최종 한도)."""
    NET_MAX, NET_MIN = 0.002, -0.002

    def setUp(self):
        for k, v in (("NET_ENFORCE", "intraday"), ("GLIDE_ON", True), ("GLIDE_START", "2026-10-01"),
                     ("NET_MAX", self.NET_MAX), ("NET_MIN", self.NET_MIN)):
            p = mock.patch.object(X, k, v)
            p.start()
            self.addCleanup(p.stop)
        self.keep = (C.TRANCHES, C.MIN_GAP_MIN)
        C.TRANCHES, C.MIN_GAP_MIN = 3, 20
        self.addCleanup(lambda: setattr(C, "TRANCHES", self.keep[0]) or setattr(C, "MIN_GAP_MIN", self.keep[1]))
        self.st = store.FileStore(tempfile.mkdtemp())


class TestLiveFunctions(_Intraday):
    def test_live_limits_modes(self):
        self.assertEqual(X.live_limits({}, D1)["netMax"], 0.002)
        self.assertIsNone(X.live_limits({}, "2026-09-30"))                       # 단계 한도 시작 전
        with mock.patch.object(X, "NET_ENFORCE", "order"):
            self.assertIsNone(X.live_limits({}, D1))                              # 종전 모드 — 장중 검사 안 함

    def test_live_gross_adds_fills_and_subtracts_exits(self):
        ps = [{"code": "A", "side": "long", "qty": 100, "lastClose": 1000}, {"code": "B", "side": "short", "qty": 50, "lastClose": 2000}]
        doc = {"orders": {"o": {"code": "C", "side": "short", "fills": [{"qty": 10, "price": 3000}]}},
               "exits": {"p": {"code": "A", "side": "long", "fills": [{"qty": 40, "price": 1100}]}}}
        g = X.live_gross(ps, doc, {"A": 1200}.get)
        self.assertEqual((g["long"], g["short"]), (60 * 1200, 50 * 2000 + 10 * 3000))   # 현재가 없으면 종가·체결 평균
        self.assertEqual(X.live_room({"netMin": -0.1, "netMax": 0.1}, 1_000_000, g, "long"), 100_000 - g["net"])

    def test_live_pending_reserves_room(self):
        # 진행 중 Long 청산 잔량은 Short 여유에서, Short 환매 잔량은 Long 여유에서 미리 뺀다(2026-11-19). 끝난 청산은 무시.
        doc = {"exits": {"a": {"code": "A", "side": "long", "planned": 100, "fills": [{"qty": 40, "price": 1000}]},
                         "b": {"code": "B", "side": "short", "planned": 10, "fills": [], "ref": 2000},
                         "c": {"code": "C", "side": "long", "planned": 50, "fills": [], "done": True}}}
        pend = X.live_pending(doc, {"A": 1100}.get)
        self.assertEqual((pend["long"], pend["short"]), (60 * 1100, 10 * 2000))   # 현재가 없으면 기준가
        lim, eq, g = {"netMin": -0.1, "netMax": 0.1}, 1_000_000, {"net": 0.0}
        self.assertEqual(X.live_room(lim, eq, g, "short", pend), 100_000 - 66_000)
        self.assertEqual(X.live_room(lim, eq, g, "long", pend), 100_000 - 20_000)
        self.assertEqual(X.live_room(lim, eq, g, "short"), 100_000)               # pending 없으면 종전

    def test_live_breach_buffer(self):
        lim, eq = {"netMin": -0.15, "netMax": 0.15}, 1_000_000
        self.assertIsNone(X.live_breach(lim, eq, {"net": 160_000}))                # +16% — 버퍼 2%p 안
        b_ = X.live_breach(lim, eq, {"net": 180_000})
        self.assertEqual((b_["side"], round(b_["excessKrw"])), ("long", 30_000))
        self.assertEqual(X.live_breach(lim, eq, {"net": -200_000})["side"], "short")


class TestTraderNetCap(_Intraday):
    def ex(self, L):
        return Executor(self.st, D1, L, log=lambda *_: None)

    def test_long_entry_waits_at_net_cap(self):
        # 평가액 5억 · 보유 Long 50만(0.1%) · 한도 +0.2%(100만) → 여유 50만 = 시가 30주 + VWAP 20주, 그 뒤는 대기
        ex = self.ex(ledger_with(order={}, position={}))
        run(ex, path("000010", [("09:00", 10000), ("09:10", 9990), ("09:30", 9990), ("09:50", 9980)]))
        r = ex.doc["orders"]["o1"]
        self.assertEqual([(f["mode"], f["qty"]) for f in r["fills"]], [("open", 30), ("vwap", 20)])
        self.assertFalse(r["done"])
        self.assertGreaterEqual(r["netWait"]["n"], 2)
        self.assertEqual(ex.doc["net"]["netMax"], 0.002)

    def test_opposite_position_gives_room(self):
        # Short 보유 50만이 Long 50만을 상쇄 → 순노출 0, 여유 100만 = 100주까지
        L = ledger_with(order={}, position={})
        L["positions"].append(dict(L["positions"][0], id="p2", code="000030", side="short", qty=50, entry=10000.0,
                                   stop=10500.0, lastClose=10000.0))
        ex = self.ex(L)
        run(ex, path("000010", [("09:00", 10000), ("09:10", 9990), ("09:30", 9990), ("09:50", 9980)]))
        self.assertEqual(sum(f["qty"] for f in ex.doc["orders"]["o1"]["fills"]), 100)

    def test_short_entry_waits_at_floor(self):
        L = ledger_with(order={"side": "short", "entry": 10000.0, "stop": 10500.0, "target": 9000.0})
        ex = self.ex(L)
        run(ex, path("000010", [("09:00", 10000), ("09:10", 10010), ("09:30", 10020)]))
        r = ex.doc["orders"]["o1"]
        self.assertEqual([f["qty"] for f in r["fills"]], [30, 69])               # 하한 −0.2%(100만) 까지 — 70만 ÷ 10,010 = 69주
        self.assertIn("netWait", r)

    def test_short_entry_leaves_room_for_pending_long_exit(self):
        # 위 test_short_entry_waits_at_floor 와 같되 Long 청산 30만(30주 × 1만)이 진행 중 → 하한 100만 중 70만만 Short 진입
        L = ledger_with(order={"side": "short", "entry": 10000.0, "stop": 10500.0, "target": 9000.0})
        ex = self.ex(L)
        ex.doc["exits"]["px"] = {"code": "000099", "side": "long", "planned": 30, "fills": [], "ref": 10000.0, "done": False}
        run(ex, path("000010", [("09:00", 10000), ("09:10", 10010), ("09:30", 10020)]))
        r = ex.doc["orders"]["o1"]
        self.assertEqual([f["qty"] for f in r["fills"]], [30, 39])               # 70만 − 시가 30만 = 40만 ÷ 10,010 = 39주
        self.assertEqual(r["netWait"]["pendingPct"], 0.06)                      # 30만 / 5억

    def test_off_when_order_mode(self):
        with mock.patch.object(X, "NET_ENFORCE", "order"):
            ex = self.ex(ledger_with(order={}, position={}))
        run(ex, path("000010", [("09:00", 10000), ("09:10", 9990), ("09:30", 9990), ("09:50", 9980)]))
        self.assertNotIn("netWait", ex.doc["orders"]["o1"])
        self.assertEqual(sum(f["qty"] for f in ex.doc["orders"]["o1"]["fills"]), 210)   # 시가 30 + VWAP 90·90(한도 없음)

    def test_settle_zero_fill_is_retryable_miss(self):
        ex = self.ex(ledger_with(order={}, position={"qty": 200}))              # 보유 100만 = 이미 한도 → 0주
        run(ex, path("000010", [("09:00", 10000), ("09:10", 9990), ("09:30", 9990)]))
        ex.doc["complete"] = True
        L = ledger_with(order={}, position={"qty": 200})
        ev = E.settle_day(L, D1, {"000010": b(10000, 10050, 9950, 9990), "000020": b(5000, 5000, 5000, 5000)}, intraday=ex.doc)
        o = ev["cancelled"][0]
        self.assertTrue(o["note"].startswith("미도달(순노출 한도 대기"))          # '미도달' → 다음 정산 재주문(_retry_unfilled)
        self.assertIn("netWait", o)


class TestDailyNoGate(_Intraday):
    NET_MAX = 0.08

    def test_orders_not_cut_by_net(self):
        keep = (X.GUARD_ON, X.BOOST_MAX)
        X.GUARD_ON, X.BOOST_MAX = True, 1.0
        self.addCleanup(lambda: (setattr(X, "GUARD_ON", keep[0]), setattr(X, "BOOST_MAX", keep[1]), setattr(E, "EXPOSURE_CTX", None)))
        codes = ["000010", "000030"]
        px = prices.DictPrices({"2026-09-21": flat(codes), "2026-09-22": flat(codes)})
        with mock.patch.object(X, "GLIDE_START", "2026-09-01"):
            run_ = daily.run_day("2026-09-21", self.st, WAgent(0.10), px, wb("2026-09-21", up=[("000010", "화학"), ("000030", "건설")]))
        L = self.st.get("ledger")
        self.assertEqual([(o["code"], o["qty"]) for o in L["orders"]], [("000010", 2500), ("000030", 2500)])   # 종목 상한 5% 만
        self.assertNotIn("gate", run_["exposure"])
        self.assertEqual(run_["exposure"]["limits"]["enforce"], "intraday")


class TestLivePmT8(_Intraday):
    NET_MAX, NET_MIN = 0.15, -0.15

    def test_t8_trigger_and_snapshot(self):
        r = DayRunner(self.st, D1, long_ledger(qty=100000), LA.MockLivePM(), intraday_fn=lambda t: {}, log=lambda *_: None)
        seen = []
        orig = SN.build

        def spy(*a, **kw):
            snap, text = orig(*a, **kw)
            seen.append(snap)
            return snap, text
        with mock.patch.object(SN, "build", spy):
            for t in path("000020", [("09:00", 5000), ("09:05", 5000)]):
                r.on_entry(entry([t]))
        net = r.net_info({})
        self.assertEqual(net["breach"]["side"], "long")                          # Long 5억 / 평가액 10억 = +50%
        trig = [t["id"] for s in seen for t in s["trigger"]]
        self.assertIn("T8", trig)
        ne = seen[0]["netExposure"]
        self.assertEqual((ne["over"], ne["limitMaxPct"]), ("long", 15.0))
        self.assertGreater(ne["excessKrw"], 0)
        self.assertIn("T8", LA.SYSTEM)


if __name__ == "__main__":
    unittest.main()
