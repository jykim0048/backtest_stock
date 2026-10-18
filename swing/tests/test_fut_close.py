"""주식선물 미상장 Short 정리(2026-10-16) — 그날 종가 환매, 재주문 안 함. 오프라인."""
import tempfile
import unittest

from swing import config, daily, engine as E, prices, store
from swing.tests.test_daily import FixedAgent, b, wb


class FutUni:
    def __init__(self, fut):
        self.fut = fut

    def fut_set(self, date):
        return self.fut

    def build_or_load(self, date, wb_, ledger):
        raise RuntimeError("no universe in test")

    def attach_scores(self, cands, side, date, source="matrix"):
        return cands


def _held_shorts(codes):
    """선물 필터 이전에 체결된 Short 보유(엔진 직접 정산 — daily 의 체결 전 확인을 거치지 않음)."""
    L = E.new_ledger(10_000_000, start="2026-09-10")
    for code in codes:
        E.place_entry(L, code=code, name=code, date="2026-09-10", valid_date="2026-09-11",
                      entry=10000, stop=10500, target=9000, weight=0.1, side="short")
    E.settle_day(L, "2026-09-11", {c: b(10000, 10100, 9900, 10000) for c in codes})
    return L


class TestFutClose(unittest.TestCase):
    def test_non_futures_short_closed_at_close(self):
        st = store.FileStore(tempfile.mkdtemp())
        st.put("ledger", _held_shorts(("000001", "000002")))         # 000001 = 선물 미상장, 000002 = 상장
        px = prices.DictPrices({"2026-09-14": {"000001": b(10000, 10100, 9900, 9950), "000002": b(10000, 10100, 9900, 9950)}})
        r = daily.run_day("2026-09-14", st, FixedAgent(), px, wb("2026-09-14"), uni=FutUni({"000002"}), log=lambda *a: None)
        L = st.get("ledger")
        self.assertEqual([p["code"] for p in L["positions"]], ["000002"])
        t = L["trades"][0]
        self.assertEqual((t["code"], t["reason"], t["exitPrice"]), ("000001", "fut_none", 9950))
        self.assertEqual(r["futClosed"], ["000001"])
        self.assertTrue(any("종가 환매 1종목" in n for n in r["notes"]))

    def test_pending_non_futures_short_order_cancelled_before_fill(self):
        st = store.FileStore(tempfile.mkdtemp())
        L = E.new_ledger(10_000_000, start="2026-09-11")
        for code in ("000001", "000002"):
            E.place_entry(L, code=code, name=code, date="2026-09-11", valid_date="2026-09-14",
                          entry=10000, stop=10500, target=9000, weight=0.1, side="short")
        st.put("ledger", L)
        px = prices.DictPrices({"2026-09-14": {"000001": b(10000, 10100, 9900, 9950), "000002": b(10000, 10100, 9900, 9950)}})
        r = daily.run_day("2026-09-14", st, FixedAgent(), px, wb("2026-09-14"), uni=FutUni({"000002"}), log=lambda *a: None)
        L = st.get("ledger")
        self.assertEqual([p["code"] for p in L["positions"]], ["000002"])
        self.assertEqual(r["futCancelled"], ["000001"])
        o = next(x for x in L["orderLog"] if x["code"] == "000001")
        self.assertEqual(o["status"], "cancelled")
        self.assertIn("주식선물 미상장", o["note"])
        self.assertEqual(L["trades"], [])

    def test_new_entries_guard_skips_non_futures(self):
        st = store.FileStore(tempfile.mkdtemp())
        L = E.new_ledger(10_000_000, start="2026-09-11")
        ag = FixedAgent({"000001": ("Sell", "Sell", 10000, 10500, 9000)})
        rows = daily._new_entries(L, st, ag, [{"code": "000001", "name": "A", "sector": "x", "signal": "동반약세", "reason": ""}],
                                  {"000001": b(10000, 10100, 9900, 10000)}, "2026-09-11", "2026-09-14", "short", fut={"000002"})
        self.assertFalse(rows[0]["ordered"])
        self.assertIn("주식선물 미상장", rows[0]["why"])
        self.assertEqual(ag.calls, [])                               # 분석(LLM) 호출 없음
        self.assertEqual(L["orders"], [])

    def test_no_retry_for_non_futures_short(self):
        st = store.FileStore(tempfile.mkdtemp())
        L = E.new_ledger(10_000_000, start="2026-09-11")
        E.place_entry(L, code="000001", name="A", date="2026-09-11", valid_date="2026-09-14",
                      entry=10000, stop=10500, target=9000, weight=0.1, side="short")
        st.put("ledger", L)
        px = prices.DictPrices({"2026-09-14": {"000001": b(9800, 9900, 9700, 9850)}})   # 미도달이어도 체결 전 확인에서 먼저 취소
        r = daily.run_day("2026-09-14", st, FixedAgent(), px, wb("2026-09-14"), uni=FutUni({"000002"}), log=lambda *a: None)
        self.assertEqual(r["futCancelled"], ["000001"])
        self.assertEqual(r["retries"], [])                           # 미도달 재주문 대상이 되지 않음

    def test_off(self):
        old = config.SHORT_FUT_CLOSE
        config.SHORT_FUT_CLOSE = False
        try:
            st = store.FileStore(tempfile.mkdtemp())
            st.put("ledger", _held_shorts(("000001",)))
            px = prices.DictPrices({"2026-09-14": {"000001": b(10000, 10100, 9900, 9950)}})
            daily.run_day("2026-09-14", st, FixedAgent(), px, wb("2026-09-14"), uni=FutUni({"000002"}), log=lambda *a: None)
            self.assertEqual(len(st.get("ledger")["positions"]), 1)
        finally:
            config.SHORT_FUT_CLOSE = old


if __name__ == "__main__":
    unittest.main()
