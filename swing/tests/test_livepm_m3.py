"""Live PM M3a(PLAN_PM.md, 2026-11-09) — llm effort·usage, 실사용 비용, 스냅샷 축소, 재기동 복구, 장중 루프(가짜 버스·시계)."""
import datetime
import os
import tempfile
import unittest
from unittest import mock

import llm
from swing import store
from swing.intraday.feed import KST
from swing.livepm import agent as A
from swing.livepm import config as C
from swing.livepm import live as LV
from swing.livepm import snapshot as SN
from swing.livepm.book import LiveBook
from swing.livepm.runner import DayRunner
from swing.tests.test_livepm_core import entry, long_ledger
from swing.tests.test_vwap_exec import D1, path, ts


def quiet(*_a, **_k):
    pass


class TestLlmEffortUsage(unittest.TestCase):
    def test_passthrough_and_usage(self):
        seen = {}

        def fake(model, system, user, max_tokens, schema, effort=None, usage=None):
            seen.update(model=model, effort=effort, max_tokens=max_tokens)
            usage.update(provider="anthropic", model=model, input_tokens=100, output_tokens=20)
            return '{"ok": 1}'
        u = {}
        with mock.patch.dict(llm._ADAPTERS, {"anthropic": fake}), mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            data, model = llm.generate_json("s", "u", chain="anthropic:claude-haiku-5-5", effort="low", usage=u,
                                            max_tokens=8000, return_model=True)
        self.assertEqual((data, model), ({"ok": 1}, "claude-haiku-5-5"))
        self.assertEqual(seen, {"model": "claude-haiku-5-5", "effort": "low", "max_tokens": 8000})
        self.assertEqual((u["input_tokens"], u["output_tokens"]), (100, 20))

    def test_no_options_keeps_five_arg_adapters(self):
        with mock.patch.dict(llm._ADAPTERS, {"anthropic": lambda m, s, u, mt, sc: '{"a": 1}'}), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            self.assertEqual(llm.generate_json("s", "u", chain="anthropic:m"), {"a": 1})


class TestCost(unittest.TestCase):
    def test_config_decision(self):
        self.assertTrue(C.LLM_CHAIN.startswith("anthropic:claude-haiku-5-5"))
        self.assertEqual((C.LLM_EFFORT, C.MAX_CALLS, C.MAX_COST_KRW), ("low", 40, 3000))

    def test_usage_cost(self):
        u = {"model": "claude-haiku-5-5", "input_tokens": 8000, "output_tokens": 1000}
        self.assertAlmostEqual(A.usage_cost(u), (8000 * 0.10 + 1000 * 0.50) / 1e6 * C.USD_KRW)
        self.assertIsNone(A.usage_cost({"model": "unknown"}))
        self.assertIsNone(A.usage_cost({}))

    def test_agent_uses_real_usage(self):
        ok = {"assessment": "x", "decisions": []}

        def gen(system, user, **kw):
            self.assertEqual((kw["effort"], kw["max_tokens"]), (C.LLM_EFFORT, C.LLM_MAX_TOKENS))
            kw["usage"].update(model="claude-haiku-5-5", input_tokens=8000, output_tokens=1000)
            return ok, "claude-haiku-5-5"
        d, meta = A.LLMAgent(gen).decide("{}" * 5000)
        self.assertAlmostEqual(meta["costKrw"], A.usage_cost(meta["usage"]))
        self.assertLess(meta["costKrw"], 3)                               # 문자 수 추정(약 60원)보다 훨씬 작음


class TestSnapshotShrink(unittest.TestCase):
    def test_thesis_only_for_triggered_or_near_stop(self):
        L = long_ledger(thesis="가" * 200)
        L["positions"].append(dict(L["positions"][0], id="p2", code="000030", thesis="나" * 200))
        b = LiveBook(L)
        b.update(entry(path("000020", [("09:00", 5000)]) + path("000030", [("09:00", 5000)])))
        snap, _ = SN.build(ts("09:10"), "active", [{"id": "T1", "code": "000020", "pid": "p1", "detail": "x"}], b)
        rows = {r["id"]: r for r in snap["positions"]}
        self.assertEqual(len(rows["p1"]["thesis"]), 200)                  # 트리거 → 전문
        self.assertNotIn("thesis", rows["p2"])                            # 트리거 없음·손절에서 멂 → 한 줄
        self.assertEqual(rows["p2"]["thesisShort"], "나" * 60)
        b.update(entry(path("000030", [("09:20", 4890)])))               # 손절 거리 55% 소진 → 전문
        snap, _ = SN.build(ts("09:20"), "active", [], b)
        self.assertEqual(len({r["id"]: r for r in snap["positions"]}["p2"]["thesis"]), 200)


class TestResume(unittest.TestCase):
    def test_restart_keeps_debounce_calls_and_schedule(self):
        st = store.FileStore(tempfile.mkdtemp())
        L = long_ledger()
        r = DayRunner(st, D1, L, A.MockLivePM(), log=quiet)
        r.on_entry(entry(path("000020", [("09:00", 5000)])))
        r.on_entry(entry([path("000020", [("09:10", 4850)])[0]]))       # T1(손절 거리 75%)·T7 09:10
        calls, last = r.trig.calls, dict(r.trig.last)
        self.assertGreater(calls, 0)
        r2 = DayRunner(st, D1, L, A.MockLivePM(), log=quiet)             # 같은 날 재기동
        self.assertEqual((r2.trig.calls, r2.trig.last, r2.trig.sched_done), (calls, last, {"09:10"}))
        self.assertEqual(r2.memory, r.memory)
        r2.catch_up([entry([path("000020", [("09:11", 4850)])[0]])])     # 따라잡기 = 시세만, 판단 없음
        self.assertEqual(r2.trig.calls, calls)
        r2.step(ts("09:12"))                                              # 디바운스 15분 안 — 같은 T1 재호출 없음
        self.assertEqual(r2.trig.calls, calls)
        self.assertEqual(DayRunner(st, D1, L, A.MockLivePM(), log=quiet, resume=False).trig.calls, 0)


class FakeBus:
    """15초마다 한 항목 — poll 이 가짜 시계를 15초 당긴다. alive 는 heartbeat 흉내."""

    def __init__(self, clock, prices):
        self.clock, self.prices, self.polls = clock, prices, 0

    def history(self, date):
        return [dict(entry(path("000020", [("08:59", 5000)])), date=date)]

    def poll(self, block_ms=20000):
        self.clock.t += datetime.timedelta(seconds=15)
        self.polls += 1
        hm = self.clock.t.strftime("%H:%M")
        if not ("09:00" <= hm < "15:30"):
            return []
        px = self.prices(hm)
        t = path("000020", [(hm, px)])[0]
        return [{"date": D1, "ts": self.clock.t.isoformat(), "ticks": [t], "vwap": {}}]

    def alive(self):
        return {"ts": self.clock.t.isoformat()}


class Clock:
    def __init__(self, hm):
        h, m = map(int, hm.split(":"))
        self.t = datetime.datetime.fromisoformat(D1).replace(hour=h, minute=m, tzinfo=KST)

    def __call__(self):
        return self.t


class TestLiveSession(unittest.TestCase):
    def test_one_day(self):
        st = store.FileStore(tempfile.mkdtemp())
        st.put("ledger", long_ledger())
        clock = Clock("08:55")
        bus = FakeBus(clock, lambda hm: 4850 if hm >= "11:00" else 5000)  # 11:00 부터 손절선 근접(T1)
        mono = iter(range(0, 10 ** 7, 15))
        r = LV.run_session(st, D1, bus=bus, agent=A.MockLivePM(), status_fn=lambda ts: None, clock=clock,
                           sleep=lambda s: None, mono=lambda: next(mono), log=quiet)
        self.assertGreater(bus.polls, 1000)                                # 08:55~15:30 15초 폴링
        run = st.get(f"livepm/{D1}/run")
        self.assertEqual(run["resume"]["schedDone"], list(C.SCHEDULE))     # 정기 점검 4회 모두
        self.assertTrue(any(i["action"] == "trim" for i in st.get(f"livepm/{D1}/instructions") or []))   # mock T1 → trim
        hb = st.get(f"livepm/{D1}/heartbeat")
        self.assertEqual((hb["state"], hb["traderAlive"]), ("active", True))
        self.assertEqual(r.trig.calls, run["calls"])

    def test_status_feed_cache_and_failure(self):
        calls, now = [], [0.0]

        def get(url):
            calls.append(url)
            if len(calls) == 2:
                raise OSError("down")
            return {"vi": [len(calls)]}
        sf = LV.StatusFeed("http://hub", get=get, every_s=60, mono=lambda: now[0], log=quiet)
        self.assertEqual(sf(), {"vi": [1]})
        now[0] = 30
        self.assertEqual((sf(), len(calls)), ({"vi": [1]}, 1))           # 60초 캐시
        now[0] = 61
        self.assertEqual(sf(), {"vi": [1]})                               # 실패 → 직전 값
        self.assertEqual(calls[0], "http://hub/status")


if __name__ == "__main__":
    unittest.main()
