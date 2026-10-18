"""장중 VWAP 실행기(PLAN_VWAP.md) — 시세 파싱·VWAP 상태·분할 진입·장중 손절·정산 반영 오프라인 테스트."""
import datetime
import tempfile
import unittest

from swing import agent_iface as A
from swing import config, daily, engine as E, prices, store
from swing.intraday.feed import KST, PollingFeed, ReplayFeed, Tick, parse_prices
from swing.tests.test_daily import FixedAgent, b, wb
from swing.vwap import config as C
from swing.vwap.executor import Executor, tranche_qtys
from swing.vwap.state import VwapState

D0, D1 = "2026-10-16", "2026-10-19"          # 금 → 월


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
        self.assertEqual(tranche_qtys(301), [100, 100, 101])
        self.assertEqual(tranche_qtys(2), [2])
        self.assertEqual(tranche_qtys(0), [])


class TestExecutor(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())

    def ex(self, L):
        return Executor(self.st, D1, L, log=lambda *_: None)

    def test_long_split_entry_cap_vwap_gap(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 10200), ("09:05", 10100),      # 상한 위
                                ("09:10", 9990),                          # ≤ 상한·VWAP → 1차
                                ("09:15", 9980),                          # 간격 5분 < 20 → 대기
                                ("09:30", 9990),                          # 2차
                                ("09:40", 9960), ("09:50", 9970)]))       # 3차
        r = ex.doc["orders"]["o1"]
        self.assertEqual([(f["ts"][11:16], f["qty"], f["price"]) for f in r["fills"]],
                         [("09:10", 100, 9990), ("09:30", 100, 9990), ("09:50", 100, 9970)])
        self.assertIn("o1", ex.doc["watched"]["orders"])

    def test_no_fill_above_vwap(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 9800), ("09:10", 9850), ("09:30", 9900)]))   # 상한 아래지만 계속 VWAP 위
        self.assertEqual(ex.doc["orders"]["o1"]["fills"], [])

    def test_short_mirror(self):
        ex = self.ex(ledger_with(order={"side": "short", "entry": 10000.0, "stop": 10500.0}))
        run(ex, path("000010", [("09:00", 9800), ("09:10", 9950), ("09:20", 10010)]))  # 10010 ≥ 하한·VWAP
        self.assertEqual([f["price"] for f in ex.doc["orders"]["o1"]["fills"]], [10010])

    def test_fill_then_stop_cancels_rest(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 10200), ("09:05", 10100), ("09:10", 9990), ("09:20", 9700, 9480)]))   # 꼬리로 손절 9500 터치
        r = ex.doc["orders"]["o1"]
        self.assertEqual((len(r["fills"]), r["done"], r["stop_hit"]["price"]), (1, True, 9500.0))

    def test_position_stop_tail_and_first_gap(self):
        ex = self.ex(ledger_with(position={}))
        run(ex, path("000020", [("09:00", 5000), ("09:30", 4900, 4790)]))
        self.assertEqual((ex.doc["stops"]["p1"]["price"], ex.doc["stops"]["p1"]["kind"]), (4800.0, "stop"))
        st2 = store.FileStore(tempfile.mkdtemp())
        ex2 = Executor(st2, D1, ledger_with(position={}), log=lambda *_: None)
        run(ex2, path("000020", [("10:00", 4750, 4700)], date=D1))            # 기동 지연 — 시가(4750)부터 손절 아래
        self.assertEqual(ex2.doc["stops"]["p1"]["kind"], "stop_gap")

    def test_excludes_sell_pending_and_held(self):
        L = ledger_with(order={"code": "000020"}, position={"sellPending": {"kind": "pm_sell"}})
        ex = self.ex(L)
        self.assertEqual((ex.doc["watched"]["orders"], ex.doc["watched"]["positions"]), ([], []))

    def test_restart_keeps_doc(self):
        ex = self.ex(ledger_with(order={}))
        run(ex, path("000010", [("09:00", 10200), ("09:05", 10100), ("09:10", 9990)]))
        ex.save(ticks=True)
        ex2 = self.ex(ledger_with(order={}))
        self.assertEqual(len(ex2.doc["orders"]["o1"]["fills"]), 1)
        self.assertAlmostEqual(ex2.states["000010"].vwap, (10200 + 10100 + 9990) / 3)


def doc(orders=None, stops=None, watched_o=(), watched_p=(), complete=True):
    return {"date": D1, "complete": complete, "asof": ts("15:30"),
            "watched": {"orders": list(watched_o), "positions": list(watched_p)},
            "orders": orders or {}, "stops": stops or {}}


def fills(*rows):
    return {"tranches": [100, 100, 100], "fills": [{"ts": ts(h), "qty": q, "price": p} for h, q, p in rows], "stop_hit": None}


class TestSettleIntraday(unittest.TestCase):
    def test_avg_price_and_partial_remainder(self):
        L = ledger_with(order={})
        I = doc(orders={"o1": fills(("09:10", 100, 9990), ("09:30", 100, 9970))}, watched_o=["o1"])
        ev = E.settle_day(L, D1, {"000010": b(10100, 10200, 9950, 10000)}, intraday=I)
        p = L["positions"][0]
        self.assertEqual((p["qty"], p["entry"]), (200, 9980))
        self.assertEqual(ev["partials"][0]["partial"]["remain"], 100)
        self.assertEqual(L["orderLog"][-1]["entry"], 10000.0)          # 상한은 유지(잔여분 레벨)

    def test_zero_fill_cancels_even_if_daily_low_reached(self):
        L = ledger_with(order={})
        I = doc(orders={"o1": fills()}, watched_o=["o1"])
        ev = E.settle_day(L, D1, {"000010": b(10100, 10200, 9900, 10000)}, intraday=I)
        self.assertFalse(L["positions"])
        self.assertTrue(ev["cancelled"][0]["note"].startswith("미도달(VWAP"))

    def test_intraday_stop_closes_at_record(self):
        L = ledger_with(position={})
        I = doc(stops={"p1": {"ts": ts("13:05"), "price": 4800.0, "kind": "stop"}}, watched_p=["p1"])
        ev = E.settle_day(L, D1, {"000020": b(5000, 5050, 4790, 4900)}, intraday=I)
        self.assertEqual((ev["exits"][0]["reason"], ev["exits"][0]["exitPrice"]), ("stop", 4800.0))

    def test_stop_gap_record(self):
        L = ledger_with(position={})
        I = doc(stops={"p1": {"ts": ts("09:00"), "price": 4700.0, "kind": "stop_gap"}}, watched_p=["p1"])
        ev = E.settle_day(L, D1, {"000020": b(4700, 4750, 4650, 4700)}, intraday=I)
        self.assertEqual((ev["exits"][0]["reason"], ev["exits"][0]["exitPrice"]), ("stop_gap", 4700.0))

    def test_complete_without_record_ignores_daily_low(self):
        L = ledger_with(position={})
        E.settle_day(L, D1, {"000020": b(5000, 5050, 4790, 4900)}, intraday=doc(watched_p=["p1"]))
        self.assertEqual(len(L["positions"]), 1)

    def test_incomplete_falls_back_to_daily(self):
        L = ledger_with(position={})
        ev = E.settle_day(L, D1, {"000020": b(5000, 5050, 4790, 4900)}, intraday=doc(watched_p=["p1"], complete=False))
        self.assertEqual(ev["exits"][0]["reason"], "stop")

    def test_fill_day_stop_from_intraday_closes_same_day(self):
        L = ledger_with(order={})
        rec = fills(("09:10", 100, 9990))
        rec["stop_hit"] = {"ts": ts("11:00"), "price": 9500.0, "kind": "stop"}
        ev = E.settle_day(L, D1, {"000010": b(10100, 10200, 9400, 9600)}, intraday=doc(orders={"o1": rec}, watched_o=["o1"]))
        self.assertFalse(L["positions"])
        self.assertEqual((ev["exits"][0]["exitPrice"], ev["exits"][0]["qty"]), (9500.0, 100))
        self.assertFalse(ev.get("deferredStops"))

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
            I = {"date": "2026-09-22", "complete": True, "watched": {"orders": [oid], "positions": []}, "stops": {},
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
