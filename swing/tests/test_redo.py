"""판단 실패 재분석(2026-10-18, swing/redo.py) — 오프라인."""
import tempfile
import unittest

from swing import daily, prices, redo, store
from swing.tests.test_daily import FixedAgent, b, wb


class FlakyAgent(FixedAgent):
    """처음 fail 번은 LLM 실패, 이후 FixedAgent 와 같다."""

    def __init__(self, entry=None, review="Hold", fail=1):
        super().__init__(entry, review)
        self.fail = fail

    def decide(self, req):
        if self.fail > 0:
            self.fail -= 1
            self.calls.append((req.code, req.purpose))
            raise RuntimeError("LLMError: all LLM links failed")
        return super().decide(req)


class TestRedo(unittest.TestCase):
    def setUp(self):
        self.st = store.FileStore(tempfile.mkdtemp())
        self.px = prices.DictPrices({
            "2026-09-21": {"000010": b(10000, 10100, 9900, 10050), "000020": b(5000, 5050, 4950, 5000)},
            "2026-09-22": {"000010": b(10000, 10100, 9900, 10050), "000020": b(5000, 5050, 4950, 5000)},
        })
        self.entry = {"000010": ("Buy", "Buy", 10000, 9500, 10600), "000020": ("Buy", "Buy", 5000, 4700, 5400)}

    def _day(self, ag, date="2026-09-21", buy=(("000010", "화학"), ("000020", "철강"))):
        return daily.run_day(date, self.st, ag, self.px, wb(date, buy=buy), log=lambda *a: None)

    def test_failed_candidate_redone_and_ordered(self):
        ag = FlakyAgent(self.entry, fail=1)
        run = self._day(ag)
        rows = {r["code"]: r for r in run["candidates"]}
        self.assertTrue(rows["000010"]["why"].startswith("판단 실패"))
        self.assertTrue(rows["000020"]["ordered"])
        self.assertIn("reason", rows["000010"]["req"])
        self.assertEqual(redo.count_failed(run), 1)
        res = redo.redo_failed("2026-09-21", self.st, ag, self.px, log=lambda *a: None)
        self.assertEqual((res["entries"], res["entryOk"], res["ordered"]), (1, 1, 1))
        run2 = self.st.get("run/2026-09-21")
        r = next(x for x in run2["candidates"] if x["code"] == "000010")
        self.assertTrue(r["ordered"])
        self.assertIn("판단 실패", r["redo"]["prevWhy"])
        self.assertEqual(redo.count_failed(run2), 0)
        L = self.st.get("ledger")
        o = next(x for x in L["orders"] if x["code"] == "000010")
        self.assertEqual((o["validDate"], o["entry"]), ("2026-09-22", 10000))
        self.assertTrue(any("판단 실패 재분석" in n for n in run2["notes"]))
        self.assertEqual(self.st.get(r["decisionKey"])["error"], None)

    def test_no_redo_after_next_run(self):
        ag = FlakyAgent(self.entry, fail=1)
        self._day(ag)
        self._day(ag, "2026-09-22", buy=())
        res = redo.redo_failed("2026-09-21", self.st, ag, self.px, log=lambda *a: None)
        self.assertIn("asof", res["skip"])

    def test_still_failing_keeps_row_failed(self):
        ag = FlakyAgent(self.entry, fail=3)        # 첫 런 두 종목 실패 + 재분석 첫 종목도 실패
        self._day(ag)
        res = redo.redo_failed("2026-09-21", self.st, ag, self.px, log=lambda *a: None)
        self.assertEqual((res["entries"], res["entryOk"]), (2, 1))
        self.assertEqual(redo.count_failed(self.st.get("run/2026-09-21")), 1)

    def test_rebuild_matrix_without_req(self):
        ag = FlakyAgent(self.entry, fail=1)
        run = self._day(ag)
        self.st.put("signals/2026-09-21", self.st.get("signals/2026-09-21"))
        r = dict(next(x for x in run["candidates"] if x["code"] == "000010"))
        r.pop("req")
        c = redo._rebuild(self.st, "2026-09-21", r)
        self.assertEqual((c["code"], c["source"], c["signal"]), ("000010", "matrix", r["signal"]))


if __name__ == "__main__":
    unittest.main()
