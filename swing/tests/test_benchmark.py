"""평가액 추이 코스피 비교선(2026-11-10) — /api/swing/benchmark. 오프라인(허브·Redis 호출 없음)."""
import tempfile
import time
import unittest

from swing import engine, server, store


def ledger(dates, asof):
    L = engine.new_ledger()
    L["asof"] = asof
    L["equity"] = [{"date": d, "equity": 500_000_000 + i * 1_000_000, "retPct": i * 0.2, "cash": 1e8, "positions": 3} for i, d in enumerate(dates)]
    return L


class TestBenchmark(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())
        self.old = (server.STORE, server.index_closes_fn, server._live_index)
        server.STORE = self.st
        server._Closes._idx_fail.clear()

    def tearDown(self):
        server.STORE, server.index_closes_fn, server._live_index = self.old
        server._Closes._idx_fail.clear()

    def _wait_fill(self):
        for _ in range(100):                                  # 백그라운드 채움 스레드가 끝날 때까지(최대 2초)
            if not server._Closes._running[0]:
                return
            time.sleep(0.02)

    def test_empty_ledger(self):
        b = server.api_benchmark({})
        self.assertEqual((b["code"], b["name"], b["base"], b["closes"], b["pending"]), ("0001", "코스피", None, {}, 0))

    def test_base_is_prev_trading_day_and_closes_follow_equity_dates(self):
        # 첫 평가 기록 2026-11-02(월) → 기준 = 직전 영업일 10-30(금) 종가(주말 건너뜀)
        self.st.put("ledger", ledger(["2026-11-02", "2026-11-03", "2026-11-04"], "2026-11-04"))
        self.st.put("attrib/closes", {"IDX:0001": {"2026-10-30": 5000.0, "2026-11-02": 5050.0, "2026-11-03": 4950.0, "2026-11-04": 5100.0}})
        server.index_closes_fn = lambda *a: self.fail("캐시에 있으면 허브를 부르지 않는다")
        b = server.api_benchmark({})
        self.assertEqual((b["baseDate"], b["base"]), ("2026-10-30", 5000.0))
        self.assertEqual(b["closes"], {"2026-11-02": 5050.0, "2026-11-03": 4950.0, "2026-11-04": 5100.0})
        self.assertEqual((b["pending"], b["wait"], b["live"], b["liveDate"]), (0, False, None, None))

    def test_missing_closes_are_filled_in_background_then_served(self):
        self.st.put("ledger", ledger(["2026-11-02", "2026-11-03"], "2026-11-03"))
        calls = []

        def fake(ic, d1, d2):
            calls.append((ic, d1, d2))
            return {"2026-10-30": 5000.0, "2026-11-02": 5050.0, "2026-11-03": 4950.0}
        server.index_closes_fn = fake
        first = server.api_benchmark({})
        self.assertEqual((first["base"], first["closes"]), (None, {}))      # 아직 없음 — 화면은 평가액만 그린다
        self.assertEqual(first["pending"], 3)
        self._wait_fill()
        self.assertEqual(calls, [("0001", "2026-10-30", "2026-11-03")])     # 한 번에 구간 조회
        b = server.api_benchmark({})
        self.assertEqual((b["base"], b["pending"]), (5000.0, 0))
        self.assertEqual(b["closes"], {"2026-11-02": 5050.0, "2026-11-03": 4950.0})

    def test_hub_failure_sets_wait_and_does_not_hammer(self):
        self.st.put("ledger", ledger(["2026-11-02"], "2026-11-02"))
        n = []

        def boom(*a):
            n.append(1)
            raise RuntimeError("허브 실패")
        server.index_closes_fn = boom
        server.api_benchmark({})
        self._wait_fill()
        b = server.api_benchmark({})                                         # 5분 안에는 다시 부르지 않는다(_Closes.IDX_RETRY)
        self._wait_fill()
        self.assertEqual((len(n), b["wait"], b["pending"], b["base"]), (1, True, 0, None))

    def test_live_point_when_intraday_doc_is_newer_than_ledger(self):
        self.st.put("ledger", ledger(["2026-11-02"], "2026-11-02"))
        self.st.put("attrib/closes", {"IDX:0001": {"2026-10-30": 5000.0, "2026-11-02": 5050.0}})
        self.st.put("intraday/2026-11-03", {"vwap": {}, "orders": {}, "exits": {}})
        server._live_index = lambda: {"0001": 5270.79, "1001": 665.24}
        b = server.api_benchmark({})
        self.assertEqual((b["liveDate"], b["live"]), ("2026-11-03", 5270.79))
        self.assertEqual(b["closes"], {"2026-11-02": 5050.0})               # 장중 날짜는 종가에 넣지 않는다


if __name__ == "__main__":
    unittest.main()
