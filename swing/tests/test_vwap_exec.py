"""Live Trader(장중 집행기)(PLAN_LIVE_TRADER.md) — 시세 파싱·VWAP 상태·분할 진입·장중 손절·정산 반영 오프라인 테스트."""
import datetime
import tempfile
import unittest

from swing import agent_iface as A
from swing import config, daily, engine as E, prices, store
from swing.intraday.feed import KST, PollingFeed, ReplayFeed, Tick, parse_prices
from swing.tests.test_daily import FixedAgent, b, wb
from swing.vwap import config as C
from swing.vwap.executor import Executor, slice_plan, tranche_qtys
from swing.vwap.state import VwapState

D0, D1 = "2026-10-16", "2026-10-19"          # 금 → 월
ORIG_TRANCHES, ORIG_GAP = C.TRANCHES, C.MIN_GAP_MIN


def setUpModule():
    C.TRANCHES, C.MIN_GAP_MIN = 3, 20        # 시나리오 테스트는 3분할·20분 간격 기준으로 읽기 쉽게(운용 기본값은 60분할·5분)


def tearDownModule():
    C.TRANCHES, C.MIN_GAP_MIN = ORIG_TRANCHES, ORIG_GAP


def ts(hm, date=D1, sec=0):
    h, m = map(int, hm.split(":"))
    return datetime.datetime.fromisoformat(date).replace(hour=h, minute=m, second=sec, tzinfo=KST).isoformat()


def path(code, rows, dv=1000, date=D1):
    """rows = [(HH:MM, 가격[, 당일저가 덮어쓰기, 당일고가 덮어쓰기])] → 누적 거래량·대금·고저가 맞는 Tick."""
    out, vol, val, hi, lo, op = [], 0.0, 0.0, None, None, rows[0][1]
    for r in rows:
        hm, p = r[0], r[1]
        vol += dv
        val += dv * p
        hi, lo = max(hi or p, p), min(lo or p, p)
        if len(r) > 2 and r[2] is not None:
            lo = min(lo, r[2])
        if len(r) > 3 and r[3] is not None:
            hi = max(hi, r[3])
        out.append(Tick(code=code, ts=ts(hm, date), price=p, volume=vol, value=val, high=hi, low=lo, open=op, prev_close=op))
    return out


def run(ex, ticks):
    for t_, batch in ReplayFeed(ticks).batches():
        ex.on_batch(t_, batch)


def ledger_with(order=None, position=None):
    L = E.new_ledger(500_000_000, start=D0)
    L["asof"] = D0
    if order is not None:
        L["orders"].append({"id": "o1", "code": "000010", "name": "A", "side": "long", "type": "entry", "created": D0,
                            "validDate": D1, "entry": 10000.0, "stop": 9500.0, "target": 11000.0, "weight": 0.1,
                            "qty": 300, "status": "open", "source": "matrix", **order})
    if position is not None:
        L["positions"].append({"id": "p1", "code": "000020", "name": "B", "side": "long", "qty": 100, "entry": 5000.0,
                               "stop": 4800.0, "target": None, "fillDate": D0, "holdDay": 1, "lastClose": 5000.0,
                               "sellPending": None, **position})
    return L


class TestFeedState(unittest.TestCase):
    def test_parse_prices(self):
        body = {"stocks": {"1": {"price": 100, "volume": 10, "value": 1000, "high": 101, "low": 99},
                           "2": {"price": 0, "volume": 0}}}
        self.assertEqual([(t.code, t.value, t.high) for t in parse_prices(body, "t")], [("1", 1000.0, 101.0)])

    def test_polling_chunks(self):
        urls = []
        f = PollingFeed("http://h", get=lambda u: urls.append(u) or {"stocks": {}}, chunk=2,
                        clock=lambda: datetime.datetime(2026, 10, 19, 9, 0, tzinfo=KST))
        f.poll(["1", "2", "3"])
        self.assertEqual(len(urls), 2)

    def test_exact_and_approx_vwap(self):
        st = VwapState("1")
        for t in path("1", [("09:00", 100), ("09:00", 104), ("09:01", 96)]):
            st.update(t)
        self.assertAlmostEqual(st.vwap, 100)
        self.assertEqual(st.vwap_source, "exact")
        st2 = VwapState("1")
        for t in path("1", [("09:00", 100), ("09:01", 110)]):
            t.value = None
            st2.update(t)
        self.assertEqual((st2.vwap_source, st2.vwap), ("approx", 110))

    def test_tranches(self):
        self.assertEqual(tranche_qtys(300), [100, 100, 100])
        self.assertEqual(tranche_qtys(301), [101, 100, 100])
        self.assertEqual(tranche_qtys(2), [1, 1])                       # 분할 수보다 적으면 1주씩
        self.assertEqual(tranche_qtys(0), [])

    def test_default_60_tranches(self):
        self.assertEqual((ORIG_TRANCHES, ORIG_GAP), (60, 5))
        q = tranche_qtys(270, 60)
        self.assertEqual((len(q), sum(q), q.count(5), q.count(4)), (60, 270, 30, 30))
        self.assertEqual(tranche_qtys(45, 60), [1] * 45)


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())

    def ex(self, L):
        return Executor(self.st, D1, L, log=lambda *_: None)

    def fills(self, r):
        return [(f["ts"][11:16], f["qty"], f["price"], f["mode"]) for f in r["fills"]]

    def test_slice_plan(self):
        self.assertEqual(slice_plan(300), [30, 90, 90, 90])
        self.assertEqual(slice_plan(5), [1, 2, 1, 1])
        self.assertEqual(slice_plan(1), [1])

    def test_long_open_slice_ignores_cap_then_vwap_tranches(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 10200), ("09:05", 10100),      # 시가 10% 는 상한(10,000) 위라도 체결
                                ("09:10", 9990),                          # ≤ 상한·VWAP → 1차
                                ("09:15", 9980),                          # 간격 5분 < 20 → 대기
                                ("09:30", 9990), ("09:40", 9960), ("09:50", 9970)]))
        r = ex.doc["orders"]["o1"]
        self.assertEqual(self.fills(r), [("09:00", 30, 10200, "open"), ("09:10", 90, 9990, "vwap"),
                                         ("09:30", 90, 9990, "vwap"), ("09:50", 90, 9970, "vwap")])
        self.assertTrue(r["done"])

    def test_no_vwap_fill_above_vwap_keeps_open_slice(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 9800), ("09:10", 9850), ("09:30", 9900)]))
        self.assertEqual(self.fills(ex.doc["orders"]["o1"]), [("09:00", 30, 9800, "open")])

    def test_force_all_on_5pct_move(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 9800), ("09:10", 10300)]))      # 기준가 9,800 대비 +5.1% → 상한 무시 전량
        r = ex.doc["orders"]["o1"]
        self.assertEqual(self.fills(r)[-1], ("09:10", 270, 10300, "force"))
        self.assertTrue(r["done"])

    def test_no_force_when_waiting_is_cheaper(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 10200), ("09:05", 10100), ("09:10", 9650)]))      # Long 진입 중 −5.4% → 전량 아님, VWAP 분할 1개만
        self.assertEqual(self.fills(ex.doc["orders"]["o1"])[-1], ("09:10", 90, 9650, "vwap"))
        ex2 = Executor(store.FileStore(tempfile.mkdtemp()), D1,
                       ledger_with(position={"sellPending": {"kind": "pm_sell"}}), log=lambda *_: None)
        run(ex2, path("000020", [("09:00", 5000), ("09:05", 5100), ("09:10", 5300)]))      # Long 청산 중 +6% 반등 → 전량 아님, VWAP 분할 1개만
        self.assertEqual(self.fills(ex2.doc["exits"]["p1"])[-1], ("09:10", 30, 5300, "vwap"))

    def test_short_force_directions(self):
        ex = self.ex(ledger_with(order={"side": "short", "entry": 10000.0, "stop": 11000.0}))
        run(ex, path("000010", [("09:00", 10000), ("09:10", 9450)]))      # Short 진입 중 −5.5% → 전량
        self.assertEqual(self.fills(ex.doc["orders"]["o1"])[-1][3], "force")
        ex2 = Executor(store.FileStore(tempfile.mkdtemp()), D1,
                       ledger_with(position={"side": "short", "stop": 5500.0, "sellPending": {"kind": "pm_sell"}}),
                       log=lambda *_: None)
        run(ex2, path("000020", [("09:00", 5000), ("09:10", 5300)]))      # Short 청산 중 +6% → 전량 환매
        self.assertEqual(self.fills(ex2.doc["exits"]["p1"])[-1], ("09:10", 90, 5300, "force"))

    def test_short_mirror(self):
        ex = self.ex(ledger_with(order={"side": "short", "entry": 10000.0, "stop": 10500.0}))
        run(ex, path("000010", [("09:00", 9800), ("09:10", 9950), ("09:20", 10010)]))
        self.assertEqual(self.fills(ex.doc["orders"]["o1"]), [("09:00", 30, 9800, "open"), ("09:20", 90, 10010, "vwap")])

    def test_open_beyond_stop_skips_open_slice(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 9400), ("09:05", 9450)]))
        r = ex.doc["orders"]["o1"]
        self.assertEqual((r["fills"], r["plan"][:2]), ([], [0, 120]))

    def test_fill_then_stop_opens_exit_slices(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 10200), ("09:05", 10100), ("09:10", 9990),
                                ("09:20", 9700, 9480),                    # 꼬리로 손절 9,500 터치
                                ("09:25", 9000)]))                        # 손절가 대비 −5.3% → 남은 청산 전량
        r, x = ex.doc["orders"]["o1"], ex.doc["exits"]["o:o1"]
        self.assertTrue(r["done"])
        self.assertEqual((x["planned"], x["trigger"]), (120, "stop"))
        self.assertEqual(self.fills(x), [("09:20", 12, 9500.0, "stop"), ("09:25", 108, 9000, "force")])

    def test_position_stop_then_vwap_sell(self):
        ex = self.ex(ledger_with(position={}))
        run(ex, path("000020", [("09:00", 5000), ("09:30", 4900, 4790),  # 손절 → 10주 @ 4,800
                                ("09:40", 4950)]))                        # VWAP(4,950) 이상 → 30주
        x = ex.doc["exits"]["p1"]
        self.assertEqual(self.fills(x), [("09:30", 10, 4800.0, "stop"), ("09:40", 30, 4950, "vwap")])

    def test_first_observation_gap(self):
        ex = self.ex(ledger_with(position={}))
        run(ex, path("000020", [("10:00", 4750, 4700)]))                 # 기동 지연 — 시가(4,750)부터 손절 아래
        self.assertEqual(ex.doc["exits"]["p1"]["kind"], "stop_gap")
        self.assertEqual(ex.doc["exits"]["p1"]["fills"][0]["price"], 4750)

    def test_scheduled_exit_open_slice_and_vwap(self):
        ex = self.ex(ledger_with(position={"sellPending": {"kind": "pm_sell", "reason": "PM Sell"}}))
        run(ex, path("000020", [("09:00", 5000), ("09:10", 4900), ("09:20", 5050)]))
        x = ex.doc["exits"]["p1"]
        self.assertEqual((x["trigger"], x["kind"]), ("scheduled", "pm_sell"))
        self.assertEqual(self.fills(x), [("09:00", 10, 5000, "open"), ("09:20", 30, 5050, "vwap")])
        self.assertNotIn("p1", ex.positions)                             # 예약 청산 중엔 손절 감시 대신 분할 청산

    def test_excludes_held_code_order(self):
        ex = self.ex(ledger_with(order={"code": "000020"}, position={}))
        self.assertEqual((ex.doc["watched"]["orders"], ex.doc["watched"]["positions"]), ([], ["p1"]))

    def test_restart_keeps_doc(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 10200), ("09:05", 10100), ("09:10", 9990)]))
        ex.save(ticks=True)
        ex2 = self.ex(ledger_with(order={}))
        self.assertEqual(len(ex2.doc["orders"]["o1"]["fills"]), 2)
        self.assertAlmostEqual(ex2.states["000010"].vwap, (10200 + 10100 + 9990) / 3)


def doc(orders=None, exits=None, watched_o=(), watched_p=(), complete=True):
    return {"date": D1, "complete": complete, "asof": ts("15:30"),
            "watched": {"orders": list(watched_o), "positions": list(watched_p)},
            "orders": orders or {}, "exits": exits or {}}


def fills(*rows):
    return {"plan": [30, 90, 90, 90], "fills": [{"ts": ts(h), "qty": q, "price": p, "mode": "vwap"} for h, q, p in rows]}


def xrec(trigger, kind, *rows):
    return {"trigger": trigger, "kind": kind, "fills": [{"ts": ts(h), "qty": q, "price": p, "mode": m} for h, q, p, m in rows]}


class TestSettleIntraday(unittest.TestCase):
    def test_avg_price_and_partial_remainder(self):
        L = ledger_with(order={})
        I = doc(orders={"o1": fills(("09:00", 30, 10200), ("09:30", 90, 9970))}, watched_o=["o1"])
        ev = E.settle_day(L, D1, {"000010": b(10200, 10250, 9950, 10000)}, intraday=I)
        p = L["positions"][0]
        self.assertEqual((p["qty"], round(p["entry"], 2)), (120, round((30 * 10200 + 90 * 9970) / 120, 2)))
        self.assertEqual(ev["partials"][0]["partial"]["remain"], 180)
        self.assertEqual(L["orderLog"][-1]["entry"], 10000.0)          # 상한은 유지(잔여분 레벨)

    def test_zero_fill_cancels_even_if_daily_low_reached(self):
        L = ledger_with(order={})
        ev = E.settle_day(L, D1, {"000010": b(10100, 10200, 9900, 10000)}, intraday=doc(orders={"o1": fills()}, watched_o=["o1"]))
        self.assertFalse(L["positions"])
        self.assertTrue(ev["cancelled"][0]["note"].startswith("미도달(VWAP"))

    def test_stop_exit_partial_carries_remainder(self):
        L = ledger_with(position={})
        I = doc(exits={"p1": xrec("stop", "stop", ("13:05", 10, 4800.0, "stop"), ("13:30", 30, 4850.0, "vwap"))},
                watched_p=["p1"])
        ev = E.settle_day(L, D1, {"000020": b(5000, 5050, 4790, 4900)}, intraday=I)
        t = ev["exits"][0]
        self.assertEqual((t["reason"], t["qty"], t["exitPrice"], t.get("partial")), ("stop", 40, 4837.5, True))
        p = L["positions"][0]
        self.assertEqual((p["qty"], p["sellPending"]["kind"]), (60, "stop"))
        ev2 = E.settle_day(L, "2026-10-20", {"000020": b(4700, 4750, 4650, 4700)})   # 실행기 없는 다음 날 → 시가 청산
        self.assertEqual((ev2["exits"][0]["qty"], ev2["exits"][0]["exitPrice"]), (60, 4700))
        self.assertFalse(L["positions"])

    def test_stop_gap_record(self):
        L = ledger_with(position={})
        I = doc(exits={"p1": xrec("stop", "stop_gap", ("09:00", 100, 4700.0, "stop_gap"))}, watched_p=["p1"])
        ev = E.settle_day(L, D1, {"000020": b(4700, 4750, 4650, 4700)}, intraday=I)
        self.assertEqual((ev["exits"][0]["reason"], ev["exits"][0]["exitPrice"]), ("stop_gap", 4700.0))
        self.assertFalse(L["positions"])

    def test_scheduled_exit_full(self):
        L = ledger_with(position={"sellPending": {"kind": "pm_sell", "reason": "PM Sell"}})
        I = doc(exits={"p1": xrec("scheduled", "pm_sell", ("09:00", 10, 5000.0, "open"), ("10:00", 90, 5100.0, "force"))},
                watched_p=["p1"])
        ev = E.settle_day(L, D1, {"000020": b(5000, 5150, 4950, 5100)}, intraday=I)
        self.assertEqual((ev["exits"][0]["reason"], ev["exits"][0]["qty"], ev["exits"][0]["exitPrice"]), ("pm_sell", 100, 5090.0))
        self.assertFalse(L["positions"])

    def test_scheduled_exit_no_fills_complete_carries(self):
        L = ledger_with(position={"sellPending": {"kind": "pm_sell", "reason": "PM Sell"}})
        ev = E.settle_day(L, D1, {"000020": b(5000, 5150, 4950, 5100)}, intraday=doc(watched_p=["p1"]))
        self.assertFalse(ev["exits"])
        self.assertTrue(L["positions"][0]["sellPending"])

    def test_short_partial_close_sums_to_full_pnl(self):
        def short_ledger():
            return ledger_with(position={"side": "short", "entry": 5000.0, "stop": 5300.0, "entryTax": 1000.0, "borrowFee": 200.0})
        full = short_ledger()
        t = E.close_position(full, full["positions"][0], D1, 4800.0, "x")
        part = short_ledger()
        p = part["positions"][0]
        t1 = E._close_position(part, p, D1, 4800.0, "x", qty=30)
        t2 = E._close_position(part, p, D1, 4800.0, "x")
        self.assertAlmostEqual(t1["pnl"] + t2["pnl"], t["pnl"], delta=1)
        self.assertAlmostEqual(part["cash"], full["cash"])
        self.assertFalse(part["positions"])

    def test_complete_without_record_ignores_daily_low(self):
        L = ledger_with(position={})
        E.settle_day(L, D1, {"000020": b(5000, 5050, 4790, 4900)}, intraday=doc(watched_p=["p1"]))
        self.assertEqual(len(L["positions"]), 1)

    def test_incomplete_falls_back_to_daily(self):
        L = ledger_with(position={})
        ev = E.settle_day(L, D1, {"000020": b(5000, 5050, 4790, 4900)}, intraday=doc(watched_p=["p1"], complete=False))
        self.assertEqual(ev["exits"][0]["reason"], "stop")

    def test_fill_day_stop_from_intraday(self):
        L = ledger_with(order={})
        I = doc(orders={"o1": fills(("09:00", 30, 10100), ("09:10", 90, 9990))},
                exits={"o:o1": xrec("stop", "stop", ("11:00", 12, 9500.0, "stop"), ("11:05", 108, 9000.0, "force"))},
                watched_o=["o1"])
        ev = E.settle_day(L, D1, {"000010": b(10100, 10200, 8900, 9100)}, intraday=I)
        self.assertFalse(L["positions"])
        self.assertEqual(ev["exits"][0]["qty"], 120)
        self.assertFalse(ev.get("deferredStops"))

    def test_no_data_day_falls_back_to_daily(self):
        """2026-10-19 사고 재현 — 시세를 한 번도 못 받은(asof 없음) 날은 감시 주문도 일봉 규칙으로 체결."""
        L = ledger_with(order={})
        I = doc(orders={"o1": fills()}, watched_o=["o1"], complete=False)
        I["asof"] = None
        E.settle_day(L, D1, {"000010": b(10100, 10200, 9900, 10000)}, intraday=I)
        self.assertEqual(L["positions"][0]["entry"], 10000.0)

    def test_incomplete_zero_fill_order_falls_back(self):
        L = ledger_with(order={})
        E.settle_day(L, D1, {"000010": b(10100, 10200, 9900, 10000)},
                     intraday=doc(orders={"o1": fills()}, watched_o=["o1"], complete=False))
        self.assertEqual(L["positions"][0]["entry"], 10000.0)                # 미완주 + 무체결 → 일봉 규칙

    def test_other_date_doc_ignored(self):
        L = ledger_with(order={})
        I = doc(orders={"o1": fills()}, watched_o=["o1"])
        I["date"] = "2026-10-20"
        E.settle_day(L, D1, {"000010": b(10100, 10200, 9900, 10000)}, intraday=I)
        self.assertEqual(L["positions"][0]["entry"], 10000.0)            # 종전 일봉 규칙(진입가 체결)


class TestDailyIntegration(unittest.TestCase):
    def test_run_day_reads_intraday_doc_and_switch(self):
        for on in (True, False):
            st = store.FileStore(tempfile.mkdtemp())
            ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600)})
            px = prices.DictPrices({"2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
                                    "2026-09-22": {"000010": b(10050, 10100, 9950, 10000)}})
            daily.run_day("2026-09-21", st, ag, px, wb("2026-09-21", buy=[("000010", "화학")]))
            oid = st.get("ledger")["orders"][0]["id"]
            q = st.get("ledger")["orders"][0]["qty"]
            I = {"date": "2026-09-22", "complete": True, "asof": "2026-09-22T15:30:00+09:00", "watched": {"orders": [oid], "positions": []}, "stops": {},
                 "orders": {oid: {"tranches": tranche_qtys(q), "stop_hit": None,
                                  "fills": [{"ts": "2026-09-22T09:10:00+09:00", "qty": q, "price": 9960.0}]}}}
            st.put("intraday/2026-09-22", I)
            old = config.INTRADAY_SETTLE
            config.INTRADAY_SETTLE = on
            try:
                r = daily.run_day("2026-09-22", st, ag, px, wb("2026-09-22"))
            finally:
                config.INTRADAY_SETTLE = old
            p = st.get("ledger")["positions"][0]
            self.assertEqual(p["entry"], 9960.0 if on else 10000.0)
            self.assertEqual("intraday" in r, on)


if __name__ == "__main__":
    unittest.main()
