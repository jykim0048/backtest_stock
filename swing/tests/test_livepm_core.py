"""Live PM M1 판단 코어(PLAN_PM.md) — LiveBook·거래 상태·트리거·스냅샷·판단·RiskGate·하루 러너 오프라인 테스트."""
import json
import tempfile
import unittest

from swing import store
from swing.livepm import agent as A
from swing.livepm import config as C
from swing.livepm import gate as G
from swing.livepm import snapshot as SN
from swing.livepm import __main__ as M
from swing.livepm.book import LiveBook
from swing.livepm.runner import DayRunner, intraday_at
from swing.livepm.state import KILL_KEY, TradingState
from swing.livepm.triggers import Triggers, sector_match
from swing.tests.test_vwap_exec import D1, ledger_with, path, ts


def entry(ticks):
    return {"ts": ticks[0].ts, "ticks": ticks, "vwap": {}}


def feed(book, rows, code="000020"):
    for t in path(code, rows):
        book.update(entry([t]))


def long_ledger(**kw):
    return ledger_with(position=dict({"sector": "전기·전자", "target": 5500.0, "thesis": "수주 기대",
                                      "invalidation": "4,800 이탈"}, **kw))


class TestBook(unittest.TestCase):
    def test_long_metrics(self):
        b = LiveBook(long_ledger())
        feed(b, [("09:00", 5000), ("09:05", 4950), ("09:10", 4900)])
        m = b.metrics("p1")
        self.assertAlmostEqual(m["pnlPct"], -0.02)
        self.assertAlmostEqual(m["stopDistUsed"], 0.5)
        self.assertAlmostEqual(m["ret5m"], 4900 / 4950 - 1)
        self.assertGreater(m["adv5m"], 0)                               # Long 하락 = 불리
        self.assertLess(b.day_pnl(), 0)

    def test_short_metrics(self):
        b = LiveBook(ledger_with(position={"side": "short", "stop": 5200.0}))
        feed(b, [("09:00", 5000), ("09:05", 5100)])
        m = b.metrics("p1")
        self.assertAlmostEqual(m["pnlPct"], -0.02)
        self.assertAlmostEqual(m["stopDistUsed"], 0.5)
        self.assertGreater(m["adv5m"], 0)                               # Short 상승 = 불리


class TestState(unittest.TestCase):
    def test_x2_sticky_reducing(self):
        s = TradingState(log=lambda *_: None)
        self.assertEqual(s.evaluate("t", day_pnl=-0.031, feed_age_s=0, trader_alive=True), "reducing")
        self.assertEqual(s.evaluate("t", day_pnl=0.0, feed_age_s=0, trader_alive=True), "reducing")
        self.assertTrue(s.allows("exit"))

    def test_x3_kill_and_x4_recover(self):
        st = store.FileStore(tempfile.mkdtemp())
        s = TradingState(st, log=lambda *_: None)
        self.assertEqual(s.evaluate("t", day_pnl=0, feed_age_s=0, trader_alive=False), "halted")
        self.assertFalse(s.allows("hold"))
        self.assertEqual(s.evaluate("t", day_pnl=0, feed_age_s=0, trader_alive=True), "active")
        self.assertEqual(s.evaluate("t", day_pnl=0, feed_age_s=C.FEED_STALE_S, trader_alive=True), "halted")
        st.put(KILL_KEY, {"state": "halted", "reason": "수동"})
        self.assertEqual(s.evaluate("t", day_pnl=0, feed_age_s=0, trader_alive=True), "halted")


class TestTriggers(unittest.TestCase):
    def test_t1_debounce_and_t3(self):
        b, tr = LiveBook(long_ledger()), Triggers()
        feed(b, [("09:00", 5000), ("09:20", 4850)])                     # 손절 거리 75% + 5분 −3%
        ids = {t["id"] for t in tr.scan(ts("09:20"), b)}
        self.assertEqual(ids, {"T1", "T3", "T7"})                       # 09:10 정기 점검도 이때 한 번
        feed(b, [("09:25", 4850)])
        self.assertEqual(tr.scan(ts("09:25"), b), [])                    # 15분 디바운스
        feed(b, [("09:36", 4850)])
        self.assertEqual([t["id"] for t in tr.scan(ts("09:36"), b)], ["T1"])

    def test_t2_t4_t5_t6_t7(self):
        b, tr = LiveBook(long_ledger(qty=100000)), Triggers()           # 큰 수량 → 포트폴리오 손익 민감
        feed(b, [("09:00", 5000), ("09:12", 5600)])
        status = {"vi": [{"code": "000020", "vi_on": True}],
                  "sectors": [{"name": "전기·전자", "changePct": 2.0}] + [{"name": f"S{i}", "changePct": -i} for i in range(1, 6)]}
        ids = {t["id"] for t in tr.scan(ts("09:12"), b, status)}
        self.assertTrue({"T2", "T4", "T7"} <= ids and "T5" not in ids)  # 전기·전자 는 상승 → Long T5 아님
        status["sectors"][0]["changePct"] = -9
        feed(b, [("09:30", 4600)])
        ids = {t["id"] for t in tr.scan(ts("09:30"), b, status)}
        self.assertIn("T5", ids)
        self.assertIn("T6", ids)                                         # −8% × 비중 → 일중 −1.5% 넘음

    def test_budget(self):
        tr = Triggers()
        tr.calls = C.MAX_CALLS
        trig = [{"id": "T1"}, {"id": "T7"}]
        self.assertEqual([t["id"] for t in tr.budget(trig)], ["T7"])
        tr.cost = C.MAX_COST_KRW
        self.assertEqual(tr.budget(trig), [])

    def test_sector_match(self):
        self.assertTrue(sector_match("의료·정밀기기", "의료·정밀기"))
        self.assertTrue(sector_match("전기·전자", "전기전자"))
        self.assertFalse(sector_match("전기·전자", "전기가스"))


class TestAgent(unittest.TestCase):
    def test_llm_failure_and_schema_violation_hold(self):
        def boom(*a, **k):
            raise RuntimeError("all LLM links failed")
        d, meta = A.LLMAgent(boom).decide("{}")
        self.assertEqual((d["decisions"], meta["error"][:12]), ([], "RuntimeError"))
        d, meta = A.LLMAgent(lambda *a, **k: ({"decisions": [{"code": "1", "action": "buy"}]}, "m")).decide("{}")
        self.assertEqual(d["decisions"], [])
        ok = {"assessment": "x", "decisions": [{"code": "1", "action": "exit", "reason": "r", "confidence": 0.9, "thesisValid": False}]}
        d, meta = A.LLMAgent(lambda *a, **k: (ok, "anthropic:x")).decide("{}")
        self.assertEqual((d, meta["model"]), (ok, "anthropic:x"))

    def test_llm_called_with_chain_and_schema(self):
        seen = {}

        def gen(system, user, **kw):
            seen.update(kw)
            return {"assessment": "", "decisions": []}, "m"
        A.LLMAgent(gen).decide("{}")
        self.assertEqual((seen["chain"], seen["schema"]), (C.LLM_CHAIN, A.SCHEMA))


class TestGate(unittest.TestCase):
    def setUp(self):
        self.b = LiveBook(long_ledger())
        feed(self.b, [("09:00", 5000), ("10:00", 4900)])
        self.s = TradingState(log=lambda *_: None)

    def rev(self, d, at="10:00", **kw):
        return G.review([dict({"code": "000020", "reason": "r", "confidence": 0.9, "thesisValid": False}, **d)],
                        ts=ts(at), book=self.b, state=self.s, **kw)[0]

    def test_pass_and_g3_g7(self):
        self.assertEqual((self.rev({"action": "exit"})["outcome"]), "pass")
        r = self.rev({"action": "trim", "trimPct": 0.5, "confidence": 0.4})
        self.assertEqual((r["outcome"], r["action"]), ("downgrade", "flag_review"))
        self.assertEqual(self.rev({"action": "exit"}, at="15:25")["why"][:2], "G7")

    def test_g1_g2(self):
        self.assertEqual(G.review([{"code": "999999", "action": "exit"}], ts=ts("10:00"), book=self.b, state=self.s)[0]["why"][:2], "G1")
        self.assertEqual(self.rev({"action": "tighten_stop", "newStop": 4700})["why"][:2], "G2")    # 불리한 방향
        self.assertEqual(self.rev({"action": "tighten_stop", "newStop": 4950})["why"][:2], "G2")    # 현재가 위
        self.assertEqual(self.rev({"action": "tighten_stop", "newStop": 4850})["newStop"], 4850)

    def test_g4_g5_g8_g9(self):
        self.assertEqual(self.rev({"action": "exit"}, recent=[{"code": "000020", "ts": ts("09:45")}])["why"][:2], "G4")
        self.assertEqual(self.rev({"action": "exit"}, intraday={"orders": {"o": {"code": "000020", "done": False}}})["why"][:2], "G5")
        self.assertEqual(self.rev({"action": "exit"}, intraday={"exits": {"p1": {}}})["why"][:2], "G8")
        self.s.state = "halted"
        self.assertEqual(self.rev({"action": "exit"})["why"][:2], "G9")

    def test_g6_cap(self):
        L = ledger_with(position={"qty": 40000})
        L["positions"].append(dict(L["positions"][0], id="p2", code="000030", qty=30000))
        b = LiveBook(L)
        for c in ("000020", "000030"):
            feed(b, [("10:00", 5000)], code=c)
        rs = G.review([{"code": "000020", "action": "exit", "confidence": 0.9}, {"code": "000030", "action": "exit", "confidence": 0.9}],
                      ts=ts("10:00"), book=b, state=self.s)
        self.assertEqual([r["outcome"] for r in rs], ["pass", "downgrade"])           # 2억 + 1.5억 > 평가액 30%


class TestSnapshot(unittest.TestCase):
    def test_shape_and_live_trader_state(self):
        b = LiveBook(long_ledger())
        feed(b, [("09:00", 5000), ("09:20", 4850)])
        trig = [{"id": "T1", "code": "000020", "pid": "p1", "detail": "x"}]
        snap, text = SN.build(ts("09:20"), "active", trig, b,
                              intraday={"exits": {"p1": {"trigger": "stop", "planned": 100, "fills": [{"qty": 10}]}}})
        p = snap["positions"][0]
        self.assertEqual((p["stopDistUsedPct"], p["triggered"], p["liveTrader"]["filled"]), (75, True, 10))
        self.assertEqual(json.loads(text)["trigger"][0]["id"], "T1")


class TestRunner(unittest.TestCase):
    def test_day_emits_instructions_and_records(self):
        st = store.FileStore(tempfile.mkdtemp())
        r = DayRunner(st, D1, long_ledger(), A.MockLivePM(), intraday_fn=lambda t: {}, log=lambda *_: None)
        out = []
        for t in path("000020", [("09:00", 5000), ("09:05", 5000), ("09:20", 4850), ("09:25", 4850), ("09:40", 4850)]):
            out += r.on_entry(entry([t]))
        acts = [(i["ts"][11:16], i["action"]) for i in out]
        self.assertEqual(acts, [("09:20", "exit")])                       # T1+T3 → mock exit, 09:40 T1 재발은 G4(30분)로 거부
        self.assertEqual(len(st.get(f"livepm/{D1}/instructions")), 1)
        self.assertTrue(st.keys(f"livepm/{D1}/calls/"))
        r.save()
        self.assertEqual(st.get(f"livepm/{D1}/run")["byAction"], {"exit": 1})

    def test_halted_makes_no_calls(self):
        st = store.FileStore(tempfile.mkdtemp())
        r = DayRunner(st, D1, long_ledger(), A.MockLivePM(), intraday_fn=lambda t: {}, log=lambda *_: None)
        for t in path("000020", [("09:00", 5000), ("09:20", 4850)]):
            r.on_entry(entry([t]), trader_alive=False)
        self.assertEqual(r.calls, 0)

    def test_intraday_at_cuts_future(self):
        doc = {"exits": {"p1": {"fills": [{"ts": ts("13:00"), "qty": 10}]}},
               "orders": {"o1": {"planned": 30, "fills": [{"ts": ts("09:00"), "qty": 3}, {"ts": ts("14:00"), "qty": 27}]}}}
        v = intraday_at(doc, ts("10:00"))
        self.assertEqual((v["exits"], v["orders"]["o1"]["done"]), ({}, False))
        self.assertTrue(intraday_at(doc, ts("14:00"))["orders"]["o1"]["done"])

    def test_replay_cli(self):
        src, dst = store.FileStore(tempfile.mkdtemp()), store.FileStore(tempfile.mkdtemp())
        src.put("ledger", long_ledger())
        src.put(f"intraday/{D1}/ticks/000020",
                [t.to_dict() for t in path("000020", [("09:00", 5000), ("09:05", 5000), ("09:20", 4850)])])
        r = M.replay(src, dst, D1, log=lambda *_: None)
        self.assertEqual(r.summary()["instructions"], 1)
        self.assertEqual(dst.get(f"livepm/{D1}/run")["agent"], "mock")


if __name__ == "__main__":
    unittest.main()
