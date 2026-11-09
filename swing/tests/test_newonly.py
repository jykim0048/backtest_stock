"""신규 후보 보충 판단(2026-11-09, swing/newonly.py) — 오프라인."""
import datetime
import os
import tempfile
import unittest
from unittest import mock

from swing import config, daily, engine, newonly, prices, store
from swing.tests.test_daily import FixedAgent, b, wb

D, N = "2026-09-21", "2026-09-22"                     # 월 → 화
EVENING = datetime.datetime(2026, 9, 21, 23, 0)
BUY = (("000010", "화학"), ("000020", "철강"))


def _snapshot(root):
    out = {}
    for dp, _, fs in os.walk(root):
        for f in fs:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[p] = fh.read()
    return out


class TestNewOnly(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.st = store.FileStore(self.root)
        self.px = prices.DictPrices({
            D: {"000010": b(10000, 10100, 9900, 10050), "000020": b(5000, 5050, 4950, 5000)},
            N: {"000010": b(10000, 10100, 9900, 10050), "000020": b(5000, 5050, 4950, 5000)},
        })
        self.ag = FixedAgent({"000010": ("Buy", "Buy", 10000, 9500, 10600), "000020": ("Buy", "Buy", 5000, 4700, 5400)})

    def _stale_day(self, st=None):
        """전주 금요일자(스테일) 브리핑으로 하루 처리 → 신규 검토 생략."""
        st = st or self.st
        run = daily.run_day(D, st, self.ag, self.px, wb("2026-09-18", buy=BUY), log=lambda *a: None)
        self.assertTrue(any("신규·신호 매도 검토 생략" in n for n in run["notes"]))
        self.assertEqual((run["candidates"], run["shortCandidates"]), ([], []))
        return run

    def test_supplements_skipped_day(self):
        self._stale_day()
        res = newonly.run_new_only(D, self.st, self.ag, self.px, wb(D, buy=BUY), now=EVENING, log=lambda *a: None)
        self.assertEqual((res["long"], res["short"], res["ordered"], res["entryDate"]), (2, 0, 2, N))
        run = self.st.get(f"run/{D}")
        self.assertEqual([r["code"] for r in run["candidates"]], ["000010", "000020"])
        self.assertTrue(all(r["ordered"] for r in run["candidates"]))
        self.assertEqual(run["newOnly"]["ordered"], 2)
        self.assertTrue(any("신규 후보 보충 판단" in n for n in run["notes"]))
        self.assertTrue(any("신규·신호 매도 검토 생략" in n for n in run["notes"]))   # 이력은 남긴다
        L = self.st.get("ledger")
        self.assertEqual(sorted((o["code"], o["validDate"]) for o in L["orders"]), [("000010", N), ("000020", N)])
        self.assertEqual(self.st.get("progress")["status"], "done")
        self.assertIsNotNone(self.st.get(f"signals/{D}"))
        self.assertEqual([c for c, p in self.ag.calls if p == "entry"], ["000010", "000020"])

    def test_second_run_is_noop(self):
        self._stale_day()
        newonly.run_new_only(D, self.st, self.ag, self.px, wb(D, buy=BUY), now=EVENING, log=lambda *a: None)
        n_orders = len(self.st.get("ledger")["orders"])
        res = newonly.run_new_only(D, self.st, self.ag, self.px, wb(D, buy=BUY), now=EVENING, log=lambda *a: None)
        self.assertIn("이미 신규 후보 판단 있음", res["skip"])
        self.assertEqual(len(self.st.get("ledger")["orders"]), n_orders)

    def test_guards(self):
        # 정상 런(생략 아님) → 거부
        daily.run_day(D, self.st, self.ag, self.px, wb(D, buy=BUY), log=lambda *a: None)
        res = newonly.run_new_only(D, self.st, self.ag, self.px, wb(D, buy=BUY), now=EVENING, log=lambda *a: None)
        self.assertIn("생략되지 않음", res["skip"])
        # 생략된 날 뒤에 다음 날 런이 돌면(원장 asof 이동) → 거부
        st2 = store.FileStore(tempfile.mkdtemp())
        self._stale_day(st2)
        daily.run_day(N, st2, self.ag, self.px, wb("2026-09-18"), log=lambda *a: None)
        res = newonly.run_new_only(D, st2, self.ag, self.px, wb(D, buy=BUY), now=EVENING, log=lambda *a: None)
        self.assertIn("원장 asof", res["skip"])
        # 브리핑이 여전히 스테일 → 거부
        st3 = store.FileStore(tempfile.mkdtemp())
        self._stale_day(st3)
        res = newonly.run_new_only(D, st3, self.ag, self.px, wb("2026-09-18", buy=BUY), now=EVENING, log=lambda *a: None)
        self.assertIn("여전히 스테일", res["skip"])
        # 장중 → 거부
        res = newonly.run_new_only(D, st3, self.ag, self.px, wb(D, buy=BUY),
                                   now=datetime.datetime(2026, 9, 21, 10, 0), log=lambda *a: None)
        self.assertIn("장중", res["skip"])
        # 다른 실행 진행 중 → 거부
        p = st3.get("progress")
        p["status"] = "running"
        st3.put("progress", p)
        res = newonly.run_new_only(D, st3, self.ag, self.px, wb(D, buy=BUY), now=EVENING, log=lambda *a: None)
        self.assertIn("progress running", res["skip"])
        self.assertEqual(st3.get("ledger")["orders"], [])

    def test_keeps_catalyst_rows_and_still_runs(self):
        """정규 런이 하방 촉매 Short 만 판단한 날(2026-11-09 실측) — 촉매 행은 보충 대상이 아니라 보존 대상."""
        self._stale_day()
        run = self.st.get(f"run/{D}")
        cat_row = {"code": "000099", "name": "촉매종목", "side": "short", "source": "catalyst", "ordered": True, "why": ""}
        run["shortCandidates"] = [cat_row]
        self.st.put(f"run/{D}", run)
        res = newonly.run_new_only(D, self.st, self.ag, self.px, wb(D, buy=BUY), now=EVENING, log=lambda *a: None)
        self.assertEqual((res["long"], res["ordered"]), (2, 2))
        run = self.st.get(f"run/{D}")
        self.assertEqual([r["code"] for r in run["shortCandidates"]], ["000099"])      # 촉매 행 보존
        self.assertEqual([r["code"] for r in run["candidates"]], ["000010", "000020"])
        res2 = newonly.run_new_only(D, self.st, self.ag, self.px, wb(D, buy=BUY), now=EVENING, log=lambda *a: None)
        self.assertIn("이미 신규 후보 판단 있음", res2["skip"])

    def test_dry_run_writes_nothing(self):
        self._stale_day()
        before = _snapshot(self.root)
        res = newonly.run_new_only(D, self.st, self.ag, self.px, wb(D, buy=BUY), dry_run=True, now=EVENING,
                                   log=lambda *a: None)
        self.assertTrue(res["dryRun"])
        self.assertEqual((res["long"], res["short"]), (2, 0))
        self.assertEqual([r["code"] for r in res["plan"]["long"]], ["000010", "000020"])
        self.assertEqual(_snapshot(self.root), before)
        self.assertEqual([c for c, p in self.ag.calls if p == "entry"], [])   # 판단(LLM) 호출 없음

    def test_excludes_pending_and_applies_budget(self):
        self._stale_day()
        L = self.st.get("ledger")
        engine.place_entry(L, code="000010", name="000010", date=D, valid_date=N, entry=10000, stop=9500, target=10600,
                           weight=0.1, side="long", source="matrix")      # 이미 대기 주문(재시도 등) → 제외
        self.st.put("ledger", L)
        with mock.patch.object(config, "JUDGE_PER_DAY", 1), mock.patch.object(config, "ENTRY_JUDGE_MIN", 1):
            res = newonly.run_new_only(D, self.st, self.ag, self.px, wb(D, buy=BUY + (("000030", "유통"),)),
                                       now=EVENING, log=lambda *a: None)
        self.assertEqual(res["long"], 1)
        run = self.st.get(f"run/{D}")
        self.assertEqual([r["code"] for r in run["candidates"]], ["000020"])
        self.assertEqual(run["judgeBudget"]["deferredLong"], ["000030"])
        self.assertEqual(sorted(o["code"] for o in self.st.get("ledger")["orders"]), ["000010", "000020"])


if __name__ == "__main__":
    unittest.main()
