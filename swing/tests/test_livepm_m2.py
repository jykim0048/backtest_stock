"""Live PM M2 — 지시 인터페이스(Live PM → Live Trader → 정산) 오프라인 테스트(PLAN_PM M2, PLAN_LIVE_TRADER §5-2)."""
import tempfile
import unittest

from swing import daily, engine as E, prices, store
from swing.livepm import agent as A
from swing.livepm.runner import DayRunner
from swing.tests.test_daily import FixedAgent, b, wb
from swing.tests.test_vwap_exec import D1, ledger_with, path, run, ts
from swing.vwap import config as VC
from swing.vwap.executor import Executor

ORIG = (VC.TRANCHES, VC.MIN_GAP_MIN)


def setUpModule():
    VC.TRANCHES, VC.MIN_GAP_MIN = 3, 20      # 읽기 쉬운 3분할·20분 기준(운용 기본값 60·5)


def tearDownModule():
    VC.TRANCHES, VC.MIN_GAP_MIN = ORIG


def ins(iid, action, at="09:30", **kw):
    return dict({"id": iid, "ts": ts(at), "code": "000020", "positionId": "p1", "action": action, "reason": "테스트"}, **kw)


def fills(r):
    return [(f["ts"][11:16], f["qty"], f["price"], f["mode"]) for f in r["fills"]]


class TestTraderApplies(unittest.TestCase):
    def ex(self, **pos):
        return Executor(store.FileStore(tempfile.mkdtemp()), D1, ledger_with(position=pos), log=lambda *_: None)

    def test_exit_first_slice_now_then_vwap_and_idempotent(self):
        ex = self.ex()
        run(ex, path("000020", [("09:00", 5000), ("09:05", 4950)]))
        L = [ins("a1", "exit")]
        self.assertEqual(ex.apply_instructions(L, ts("09:30")), 1)
        self.assertEqual(ex.apply_instructions(L, ts("09:31")), 0)          # 같은 지시 재집행 없음
        run(ex, path("000020", [("09:31", 4960), ("09:40", 5000)]))        # 4960 즉시 10%, 5000 ≥ VWAP → 1차
        x = ex.doc["exits"]["p1"]
        self.assertEqual((x["trigger"], x["kind"]), ("livepm", "livepm_exit"))
        self.assertEqual(fills(x), [("09:31", 10, 4960, "livepm"), ("09:40", 30, 5000, "vwap")])
        self.assertEqual(ex.doc["instructions"]["a1"]["status"], "done")

    def test_trim_then_stop_sells_only_remainder(self):
        ex = self.ex()
        run(ex, path("000020", [("09:00", 5000), ("09:05", 4950)]))
        ex.apply_instructions([ins("a1", "trim", pct=0.5)], ts("09:30"))
        run(ex, path("000020", [("09:31", 4900), ("09:40", 4790, 4780)]))   # trim 첫 분할 5주 → 손절
        t, s = ex.doc["exits"]["i:a1"], ex.doc["exits"]["p1"]
        self.assertTrue(t["done"])
        self.assertEqual((t["planned"], sum(f["qty"] for f in t["fills"])), (50, 5))
        self.assertEqual((s["trigger"], s["planned"]), ("stop", 95))

    def test_exit_replaces_open_trim(self):
        ex = self.ex()
        run(ex, path("000020", [("09:00", 5000)]))
        ex.apply_instructions([ins("a1", "trim", pct=0.5)], ts("09:30"))
        run(ex, path("000020", [("09:31", 4990)]))                            # trim 5주
        ex.apply_instructions([ins("a2", "exit", at="10:10")], ts("10:10"))
        self.assertEqual((ex.doc["exits"]["i:a1"]["done"], ex.doc["exits"]["p1"]["planned"]), (True, 95))

    def test_tighten_stop_then_hit_new_stop(self):
        ex = self.ex()
        run(ex, path("000020", [("09:00", 5000), ("09:05", 5100)]))
        ex.apply_instructions([ins("a1", "tighten_stop", newStop=4700), ins("a2", "tighten_stop", newStop=4950)], ts("09:30"))
        self.assertEqual([ex.doc["instructions"][k]["status"] for k in ("a1", "a2")], ["rejected", "done"])
        run(ex, path("000020", [("09:31", 4940)]))                            # 원래 손절 4,800 위, 새 손절 4,950 아래
        x = ex.doc["exits"]["p1"]
        self.assertEqual((x["trigger"], x["fills"][0]["price"]), ("stop", 4950.0))
        self.assertEqual(ex.doc["stopMoves"]["p1"]["newStop"], 4950.0)

    def test_flag_review_and_reject_on_scheduled_exit(self):
        ex = self.ex()
        ex.apply_instructions([ins("a1", "flag_review")], ts("09:30"))
        self.assertIn("p1", ex.doc["reviews"])
        ex2 = self.ex(sellPending={"kind": "pm_sell"})
        ex2.apply_instructions([ins("a1", "exit")], ts("09:30"))
        self.assertEqual(ex2.doc["instructions"]["a1"]["status"], "rejected")


def idoc(exits=None, stopMoves=None, reviews=None, watched=("p1",)):
    return {"date": D1, "complete": True, "watched": {"orders": [], "positions": list(watched)}, "orders": {},
            "exits": exits or {}, "stopMoves": stopMoves or {}, "reviews": reviews or {}}


def rec(trigger, kind, *rows, **kw):
    return dict({"trigger": trigger, "kind": kind, "fills": [{"ts": ts(h), "qty": q, "price": p, "mode": "vwap"} for h, q, p in rows]}, **kw)


class TestSettleApplies(unittest.TestCase):
    def test_trim_keeps_position_without_carry(self):
        L = ledger_with(position={})
        ev = E.settle_day(L, D1, {"000020": b(5000, 5050, 4900, 5000)},
                          intraday=idoc({"i:a1": rec("livepm", "livepm_trim", ("09:31", 30, 4990), positionId="p1", planned=50)}))
        self.assertEqual((ev["exits"][0]["reason"], ev["exits"][0]["qty"]), ("livepm_trim", 30))
        p = L["positions"][0]
        self.assertEqual((p["qty"], p.get("sellPending")), (70, None))

    def test_livepm_exit_partial_carries(self):
        L = ledger_with(position={})
        E.settle_day(L, D1, {"000020": b(5000, 5050, 4900, 5000)},
                     intraday=idoc({"p1": rec("livepm", "livepm_exit", ("09:31", 10, 4990), ("09:51", 30, 5010))}))
        p = L["positions"][0]
        self.assertEqual((p["qty"], p["sellPending"]["kind"]), (60, "livepm_exit"))

    def test_trim_then_stop_same_day(self):
        L = ledger_with(position={})
        ev = E.settle_day(L, D1, {"000020": b(5000, 5050, 4700, 4750)}, intraday=idoc({
            "i:a1": rec("livepm", "livepm_trim", ("09:31", 5, 4900), positionId="p1"),
            "p1": rec("stop", "stop", ("09:40", 95, 4800))}))
        self.assertEqual([(t["reason"], t["qty"]) for t in ev["exits"]], [("livepm_trim", 5), ("stop", 95)])
        self.assertFalse(L["positions"])

    def test_stop_move_applied_after_day_check(self):
        L = ledger_with(position={})
        ev = E.settle_day(L, D1, {"000020": b(5000, 5100, 4900, 5050)},          # 저가 4,900 < 새 손절 4,950
                          intraday=idoc(stopMoves={"p1": {"ts": ts("13:00"), "newStop": 4950.0}}))
        p = L["positions"][0]
        self.assertFalse(ev["exits"])
        self.assertEqual((p["stop"], p["stopHistory"][0]["by"]), (4950.0, "livepm"))
        L2 = ledger_with(position={})
        E.settle_day(L2, D1, {"000020": b(5000, 5100, 4900, 5050)}, intraday=idoc(stopMoves={"p1": {"newStop": 4700.0}}))
        self.assertEqual(L2["positions"][0]["stop"], 4800.0)                    # 불리한 방향은 무시


class TestReviewFlow(unittest.TestCase):
    def _run(self, review):
        st = store.FileStore(tempfile.mkdtemp())
        ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600)}, review=review)
        px = prices.DictPrices({"2026-09-21": {"000010": b(10000, 10100, 9900, 10050)},
                                "2026-09-22": {"000010": b(10050, 10100, 9950, 10000)},
                                "2026-09-23": {"000010": b(10000, 10100, 9900, 10000)}})
        daily.run_day("2026-09-21", st, ag, px, wb("2026-09-21", buy=[("000010", "화학")]), log=lambda *_: None)
        daily.run_day("2026-09-22", st, ag, px, wb("2026-09-22"), log=lambda *_: None)
        pid = st.get("ledger")["positions"][0]["id"]
        st.put("intraday/2026-09-23", {"date": "2026-09-23", "complete": True, "watched": {"orders": [], "positions": [pid]},
                                       "orders": {}, "exits": {}, "reviews": {pid: {"ts": "2026-09-23T10:00:00+09:00",
                                                                                   "reason": "업종 급락"}}})
        r = daily.run_day("2026-09-23", st, ag, px, wb("2026-09-23"), log=lambda *_: None)
        return r, st.get("ledger")["positions"][0]

    def test_review_sell_schedules_livepm_review(self):
        r, p = self._run("Sell")
        self.assertEqual(r["reviews"][0]["kinds"][0], "livepm")
        self.assertEqual(p["sellPending"]["kind"], "livepm_review")
        self.assertNotIn("livepmReview", p)

    def test_review_hold_keeps_without_extension(self):
        r, p = self._run("Hold")
        self.assertEqual(r["reviews"][0]["action"], "hold")
        self.assertFalse(p.get("extended"))
        self.assertNotIn("livepmReview", p)


class TestEndToEnd(unittest.TestCase):
    def test_pm_instruction_to_ledger(self):
        st = store.FileStore(tempfile.mkdtemp())
        L = ledger_with(position={"thesis": "x"})
        pm = DayRunner(st, D1, L, A.MockLivePM(), intraday_fn=lambda t: st.get(f"intraday/{D1}"), log=lambda *_: None)
        tr = Executor(st, D1, L, log=lambda *_: None)
        rows = [("09:00", 5000), ("09:05", 5000), ("09:20", 4850), ("09:21", 4860), ("09:45", 4900)]
        for t in path("000020", rows):
            e = {"ts": t.ts, "ticks": [t], "vwap": {}}
            tr.apply_instructions(st.get(f"livepm/{D1}/instructions"), t.ts)
            tr.on_batch(t.ts, [t])
            tr.save()
            pm.on_entry(e)
        x = tr.doc["exits"]["p1"]
        self.assertEqual((x["kind"], x["fills"][0]["price"]), ("livepm_exit", 4860))
        ev = E.settle_day(L, D1, {"000020": b(5000, 5000, 4850, 4900)}, intraday=st.get(f"intraday/{D1}"))
        self.assertEqual(ev["exits"][0]["reason"], "livepm_exit")


if __name__ == "__main__":
    unittest.main()
