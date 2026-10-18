"""Redis 시세 버스(swing/intraday/bus.py) — Live Trader 발행 · Live PM 구독 · 실패 격리 오프라인 테스트."""
import datetime
import tempfile
import unittest

from swing import store
from swing.intraday import bus as BUS
from swing.intraday.feed import KST
from swing.tests.test_vwap_exec import D1, ledger_with, path
from swing.vwap import __main__ as M
from swing.vwap.state import VwapState


class FakeRedis:
    """xadd·xrange·xread·hset·hgetall·set·get 만 흉내(decode_responses=True 동작)."""

    def __init__(self, fail=False):
        self.streams, self.hashes, self.kv, self.n, self.fail = {}, {}, {}, 0, fail

    def _chk(self):
        if self.fail:
            raise ConnectionError("down")

    def xadd(self, name, fields, maxlen=None, approximate=True):
        self._chk()
        self.n += 1
        eid = f"{self.n}-0"
        s = self.streams.setdefault(name, [])
        s.append((eid, {k: str(v) for k, v in fields.items()}))
        if maxlen and len(s) > maxlen:
            del s[:len(s) - maxlen]
        return eid

    def xrange(self, name, mn, mx):
        return list(self.streams.get(name, []))

    def xread(self, streams, count=None, block=None):
        out = []
        for name, last in streams.items():
            n = int(last.split("-")[0])
            es = [e for e in self.streams.get(name, []) if int(e[0].split("-")[0]) > n][:count]
            if es:
                out.append([name, es])
        return out

    def hset(self, name, mapping):
        self._chk()
        self.hashes.setdefault(name, {}).update(mapping)

    def hgetall(self, name):
        return dict(self.hashes.get(name, {}))

    def set(self, name, value, ex=None):
        self._chk()
        self.kv[name] = value

    def get(self, name):
        return self.kv.get(name)


class TestBus(unittest.TestCase):
    def test_publish_then_history_poll_last(self):
        r = FakeRedis()
        pub = BUS.PriceBus(client=r, log=lambda *_: None)
        ticks = path("000010", [("09:00", 10000), ("09:00", 10100)])
        st = VwapState("000010")
        for t in ticks:
            st.update(t)
        self.assertTrue(pub.publish(D1, ticks[1].ts, [ticks[1]], {"000010": st}))
        pub.heartbeat(D1, ticks[1].ts, ["000010"])
        sub = BUS.BusFeed(client=r)
        h = sub.history(D1)
        self.assertEqual(len(h), 1)
        self.assertEqual((h[0]["ticks"][0].price, h[0]["vwap"]["000010"]["vwap"]), (10100, 10050))
        self.assertEqual(sub.poll(), [])                         # 따라잡은 뒤엔 새 항목 없음
        t3 = path("000010", [("09:01", 10200)])[0]
        pub.publish(D1, t3.ts, [t3])
        self.assertEqual([x["ticks"][0].price for x in sub.poll()], [10200])
        self.assertEqual(sub.last()["000010"]["price"], 10200)
        self.assertEqual(sub.alive()["codes"], ["000010"])

    def test_history_filters_date(self):
        r = FakeRedis()
        pub = BUS.PriceBus(client=r, log=lambda *_: None)
        t = path("000010", [("09:00", 10000)])[0]
        pub.publish("2026-10-16", t.ts, [t])
        pub.publish(D1, t.ts, [t])
        self.assertEqual(len(BUS.BusFeed(client=r).history(D1)), 1)

    def test_failure_is_swallowed_and_backs_off(self):
        logs = []
        pub = BUS.PriceBus(client=FakeRedis(fail=True), log=logs.append, retry_s=60)
        t = path("000010", [("09:00", 10000)])[0]
        self.assertFalse(pub.publish(D1, t.ts, [t]))
        self.assertFalse(pub.publish(D1, t.ts, [t]))            # 재시도 대기 중 — 시도도 안 함
        self.assertEqual((pub.errors, len(logs)), (1, 1))

    def test_disabled_without_url(self):
        pub = BUS.PriceBus(None, log=lambda *_: None)
        self.assertFalse(pub.enabled)
        self.assertFalse(pub.publish(D1, "t", path("000010", [("09:00", 1)])))


class FakeFeed:
    def __init__(self, ticks_by_call):
        self.batches, self.i = ticks_by_call, 0

    def poll(self, codes):
        b = self.batches[self.i] if self.i < len(self.batches) else []
        self.i += 1
        return b


class TestSessionPublishes(unittest.TestCase):
    def test_run_session_publishes_each_poll_and_heartbeat(self):
        st = store.FileStore(tempfile.mkdtemp())
        st.put("ledger", ledger_with(order={}))
        ticks = path("000010", [("09:00", 10200), ("09:05", 10100), ("09:10", 9990)])
        t0 = datetime.datetime(2026, 10, 19, 9, 0, tzinfo=KST)
        now = [t0]

        def clock():
            return now[0]

        def sleep(s):
            now[0] = now[0] + datetime.timedelta(minutes=5)

        r = FakeRedis()
        ex = M.run_session(st, D1, feed=FakeFeed([[t] for t in ticks]), bus=BUS.PriceBus(client=r, log=lambda *_: None),
                           clock=clock, sleep=sleep, log=lambda *_: None)
        self.assertEqual(len(r.streams[BUS.STREAM]), 3)
        self.assertIn(BUS.HEARTBEAT, r.kv)
        self.assertEqual(len(ex.doc["orders"]["o1"]["fills"]), 2)      # 집행은 그대로(시가 + VWAP 1차)

    def test_run_session_survives_redis_down(self):
        st = store.FileStore(tempfile.mkdtemp())
        st.put("ledger", ledger_with(order={}))
        ticks = path("000010", [("09:00", 10200), ("09:05", 10100), ("09:10", 9990)])
        now = [datetime.datetime(2026, 10, 19, 9, 0, tzinfo=KST)]
        ex = M.run_session(st, D1, feed=FakeFeed([[t] for t in ticks]),
                           bus=BUS.PriceBus(client=FakeRedis(fail=True), log=lambda *_: None),
                           clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + datetime.timedelta(minutes=5)),
                           log=lambda *_: None)
        self.assertEqual(len(ex.doc["orders"]["o1"]["fills"]), 2)


if __name__ == "__main__":
    unittest.main()
