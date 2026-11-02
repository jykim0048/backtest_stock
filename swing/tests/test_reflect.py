"""매매 복기(2026-11-02 1단계) — 끝난 포지션 묶기·시장 대비·저장·이어 하기. 오프라인."""
import tempfile
import unittest

from swing import config, reflect, store

KEY = "decision/2026-10-20/000001/entry"


def tr(tid, exit_date, qty, price, pnl, *, code="000001", side="long", partial=False, key=KEY, entry_date="2026-10-21"):
    t = {"id": tid, "code": code, "name": "가", "side": side, "qty": qty, "entryDate": entry_date, "entryPrice": 10000.0,
         "exitDate": exit_date, "exitPrice": float(price), "reason": "stop", "holdDays": 3, "pnl": pnl, "decisionKey": key}
    if partial:
        t["partial"] = True
    return t


def make_store(trades):
    st = store.FileStore(tempfile.mkdtemp())
    st.put("ledger", {"trades": trades})
    st.put(KEY, {"rating": "Buy", "entry": 10000, "stop": 9500, "target": 11000, "summary": "돌파", "reports": {"pm": "PM 원문"}})
    return st


class Llm:
    def __init__(self, fail_at=None):
        self.users, self.fail_at = [], fail_at

    def __call__(self, system, user, max_tokens, schema):
        self.users.append(user)
        if self.fail_at == len(self.users):
            raise RuntimeError("quota")
        return {"markdown": "복기 본문"}, "m"


def bench(code, d1, d2):
    return 2.0, "코스피"


def quiet(*a):
    pass


class TestRounds(unittest.TestCase):
    def test_partial_exits_merge_after_last_exit(self):
        rs = reflect.closed_rounds([tr("t1", "2026-10-22", 40, 10500, 19000, partial=True), tr("t2", "2026-10-23", 60, 11000, 59000)])
        self.assertEqual(len(rs), 1)
        r = rs[0]
        self.assertEqual((r["qty"], r["exits"], r["exitDate"], r["pnl"]), (100, 2, "2026-10-23", 78000))
        self.assertEqual(r["exitPrice"], 10800)
        self.assertEqual(r["retPct"], 7.8)

    def test_position_still_open_is_left_out(self):
        self.assertEqual(reflect.closed_rounds([tr("t1", "2026-10-22", 40, 10500, 19000, partial=True)]), [])


class TestRun(unittest.TestCase):
    def test_writes_with_alpha_and_never_repeats(self):
        st = make_store([tr("t1", "2026-10-23", 100, 11000, 90000)])
        llm = Llm()
        r = reflect.run(st, "2026-10-23", llm_fn=llm, bench_fn=bench, log=quiet)
        self.assertEqual((r["written"], r["left"], r["error"]), (1, 0, None))
        it = st.get("reflection/2026-10-23")["items"][0]
        self.assertEqual((it["text"], it["alphaPct"], it["benchName"]), ("복기 본문", 7.0, "코스피"))
        self.assertTrue(it["right"])
        self.assertIn("PM 원문", llm.users[0])
        self.assertEqual(reflect.run(st, "2026-10-23", llm_fn=llm, bench_fn=bench, log=quiet)["written"], 0)
        self.assertEqual(len(llm.users), 1)

    def test_short_alpha_subtracts_market_fall(self):
        st = make_store([tr("t1", "2026-10-23", 100, 9700, 30000, side="short")])
        reflect.run(st, "2026-10-23", llm_fn=Llm(), bench_fn=lambda c, a, b: (-4.0, "코스피"), log=quiet)
        it = st.get("reflection/2026-10-23")["items"][0]
        self.assertEqual(it["alphaPct"], -1.0)                       # +3.0% 수익이지만 시장이 4% 빠져 초과수익은 −1%
        self.assertFalse(it["right"])

    def test_bench_failure_keeps_going_on_raw_return(self):
        def boom(*a):
            raise RuntimeError("no data")
        st = make_store([tr("t1", "2026-10-23", 100, 11000, 90000)])
        reflect.run(st, "2026-10-23", llm_fn=Llm(), bench_fn=boom, log=quiet)
        it = st.get("reflection/2026-10-23")["items"][0]
        self.assertNotIn("alphaPct", it)
        self.assertTrue(it["right"])

    def test_llm_failure_stops_and_next_run_resumes(self):
        st = make_store([tr("t1", "2026-10-22", 100, 11000, 90000), tr("t2", "2026-10-23", 100, 11000, 90000, code="000002")])
        r = reflect.run(st, "2026-10-23", llm_fn=Llm(fail_at=2), bench_fn=bench, log=quiet)
        self.assertEqual((r["written"], r["left"]), (1, 1))
        self.assertIn("quota", r["error"])
        self.assertEqual(reflect.run(st, "2026-10-23", llm_fn=Llm(), bench_fn=bench, log=quiet)["written"], 1)

    def test_limit_takes_latest_exit_first(self):
        st = make_store([tr("t1", "2026-10-22", 100, 11000, 90000), tr("t2", "2026-10-23", 100, 11000, 90000, code="000002")])
        r = reflect.run(st, "2026-10-23", llm_fn=Llm(), bench_fn=bench, log=quiet, limit=1)
        self.assertEqual((r["written"], r["left"]), (1, 1))
        self.assertEqual(st.keys("reflection/"), ["reflection/2026-10-23"])

    def test_missing_decision_marked_once_without_llm(self):
        st = make_store([tr("t1", "2026-10-23", 100, 11000, 90000, key=None)])
        llm = Llm()
        r = reflect.run(st, "2026-10-23", llm_fn=llm, bench_fn=bench, log=quiet)
        self.assertEqual((r["skipped"], len(llm.users)), (1, 0))
        self.assertEqual(reflect.run(st, "2026-10-23", llm_fn=llm, bench_fn=bench, log=quiet)["skipped"], 0)

    def test_exits_before_cutoff_ignored(self):
        st = make_store([tr("t1", "2026-10-16", 100, 11000, 90000, entry_date="2026-10-13")])
        self.assertEqual(config.REFLECT_FROM, "2026-10-19")
        self.assertEqual(reflect.run(st, "2026-10-23", llm_fn=Llm(), bench_fn=bench, log=quiet)["written"], 0)


class TestPmUnchanged(unittest.TestCase):
    def test_pm_prompt_still_has_no_past_context(self):
        from swing.agent import pipeline
        self.assertEqual(pipeline.PAST_NONE, "과거 맥락 없음")


if __name__ == "__main__":
    unittest.main()
