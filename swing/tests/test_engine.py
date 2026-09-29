"""원장 엔진 오프라인 테스트 — python -m unittest discover -s swing/tests -t ."""
import unittest

from swing import config, engine as E


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def ledger_with_order(entry=10000, stop=9500, target=10600, weight=0.1, cap=1_000_000):
    L = E.new_ledger(cap, start="2026-09-01")
    E.place_entry(L, code="000001", name="A", date="2026-09-01", valid_date="2026-09-02",
                  entry=entry, stop=stop, target=target, weight=weight)
    return L


class TestEntry(unittest.TestCase):
    def test_fill_at_entry_price(self):
        L = ledger_with_order()
        ev = E.settle_day(L, "2026-09-02", {"000001": bar(10200, 10300, 9900, 10100)})
        self.assertEqual(len(ev["filled"]), 1)
        p = L["positions"][0]
        self.assertEqual((p["qty"], p["entry"], p["holdDay"]), (10, 10000, 1))
        self.assertEqual(L["cash"], 1_000_000 - 100_000)

    def test_fill_at_entry_even_if_open_below(self):
        L = ledger_with_order()
        E.settle_day(L, "2026-09-02", {"000001": bar(9800, 10100, 9700, 10000)})
        self.assertEqual(L["positions"][0]["entry"], 10000)      # 에이전트 가격 그대로

    def test_not_reached_cancel(self):
        L = ledger_with_order()
        ev = E.settle_day(L, "2026-09-02", {"000001": bar(10300, 10500, 10100, 10400)})
        self.assertEqual(ev["cancelled"][0]["status"], "cancelled")
        self.assertFalse(L["positions"])
        self.assertFalse(L["orders"])

    def test_no_bar_cancel(self):
        L = ledger_with_order()
        E.settle_day(L, "2026-09-02", {})
        self.assertEqual(L["orderLog"][-1]["note"], "시세 없음")

    def test_fill_day_stop_conservative_and_no_target(self):
        L = ledger_with_order()
        E.settle_day(L, "2026-09-02", {"000001": bar(10000, 10700, 9400, 9600)})
        t = L["trades"][0]
        self.assertEqual((t["reason"], t["exitPrice"]), ("stop", 9500))
        L = ledger_with_order()
        E.settle_day(L, "2026-09-02", {"000001": bar(10000, 10700, 9900, 10500)})
        self.assertEqual(len(L["positions"]), 1)                 # 체결일 목표 미적용

    def test_qty_zero_skipped(self):
        L = E.new_ledger(10_000)
        o = E.place_entry(L, code="000001", name="A", date="d1", valid_date="2026-09-02",
                          entry=50_000, stop=45_000, target=None, weight=0.1)
        self.assertEqual(o["status"], "skipped")


class TestExits(unittest.TestCase):
    def filled(self, **kw):
        L = ledger_with_order(**kw)
        E.settle_day(L, "2026-09-02", {"000001": bar(10100, 10200, 9950, 10100)})
        return L

    def test_target_flags_review_not_sell(self):
        L = self.filled()
        E.settle_day(L, "2026-09-03", {"000001": bar(10200, 10700, 10100, 10650)})
        self.assertFalse(L["trades"])                        # 목표 도달 = 청산 아님(재판별 대기)
        p = L["positions"][0]
        self.assertEqual((p["reviewFlag"], p["targetHit"]["high"]), ("target", 10700))

    def test_review_sell_next_open_with_kind_and_tax(self):
        L = self.filled()
        E.settle_day(L, "2026-09-03", {"000001": bar(10200, 10700, 10100, 10650)})
        E.schedule_sell(L, "000001", "PM Sell — 목표 도달", "d/r", "2026-09-03", kind="review_target")
        E.settle_day(L, "2026-09-04", {"000001": bar(10550, 10600, 10400, 10500)})
        t = L["trades"][0]
        self.assertEqual((t["reason"], t["exitPrice"], t["exitDecisionKey"]), ("review_target", 10550, "d/r"))
        self.assertEqual(t["tax"], round(10 * 10550 * config.SELL_TAX))
        self.assertEqual(t["pnl"], round(10 * 10550 * (1 - config.SELL_TAX) - 100_000))

    def test_apply_hold_after_target_breakeven_stop(self):
        L = self.filled()                                    # 진입 10000 · 손절 9500 · 목표 10600
        E.settle_day(L, "2026-09-03", {"000001": bar(10200, 10700, 10100, 10650)})
        E.apply_hold(L, "000001", target=11200, stop=9800, decision_key="d/h", date="2026-09-03")
        p = L["positions"][0]
        self.assertEqual((p["target"], p["stop"], p["extended"], p["reviewFlag"]), (11200, 10000, True, None))
        E.apply_hold(L, "000001", target=10500, stop=10300, date="2026-09-04")   # 목표 ≤ 현재가 → 무시, 손절 상향
        self.assertEqual((p["target"], p["stop"]), (11200, 10300))

    def test_apply_hold_target_without_new_target(self):
        L = self.filled()
        E.settle_day(L, "2026-09-03", {"000001": bar(10200, 10700, 10100, 10650)})
        E.apply_hold(L, "000001", target=None, stop=None)
        p = L["positions"][0]
        self.assertEqual((p["target"], p["stop"]), (None, 10000))   # 목표 없음 · 본전 손절

    def test_stop_priority_when_both(self):
        L = self.filled()
        E.settle_day(L, "2026-09-03", {"000001": bar(10000, 10700, 9400, 10000)})
        self.assertEqual(L["trades"][0]["reason"], "stop")
        self.assertEqual(L["trades"][0]["exitPrice"], 9500)

    def test_gap_down_below_stop_uses_open(self):
        L = self.filled()
        E.settle_day(L, "2026-09-03", {"000001": bar(9000, 9300, 8800, 9100)})
        t = L["trades"][0]
        self.assertEqual((t["reason"], t["exitPrice"]), ("stop_gap", 9000))

    def test_expiry_flags_review_not_sell(self):
        L = self.filled()                                   # 체결일 = 1일째
        days = [f"2026-09-{d:02d}" for d in range(3, 3 + config.HOLD_DAYS - 1)]
        for d in days:
            E.settle_day(L, d, {"000001": bar(10100, 10300, 10000, 10200)})
        p = L["positions"][0]
        self.assertFalse(L["trades"])
        self.assertEqual((p["holdDay"], p["reviewFlag"]), (config.HOLD_DAYS, "expiry"))
        self.assertEqual(L["positions"][0]["lastClose"], 10200)

    def test_max_hold_schedules_sell_next_open(self):
        L = self.filled()
        L["positions"][0]["holdDay"] = config.MAX_HOLD_DAYS - 1
        ev = E.settle_day(L, "2026-09-03", {"000001": bar(10100, 10300, 10000, 10200)})
        p = L["positions"][0]
        self.assertEqual((p["sellPending"]["kind"], p["reviewFlag"]), ("max_hold", None))
        self.assertEqual(ev["maxHold"][0]["code"], "000001")
        E.settle_day(L, "2026-09-04", {"000001": bar(10150, 10300, 10000, 10200)})
        self.assertEqual((L["trades"][0]["reason"], L["trades"][0]["exitPrice"]), ("max_hold", 10150))

    def test_no_target_only_stop_expiry(self):
        L = self.filled(target=None)
        E.settle_day(L, "2026-09-03", {"000001": bar(10100, 20000, 10000, 10200)})
        self.assertEqual(len(L["positions"]), 1)

    def test_pm_sell_at_open(self):
        L = self.filled()
        self.assertTrue(E.schedule_sell(L, "000001", "섹터 동반약세", "d/k", "2026-09-02"))
        E.settle_day(L, "2026-09-03", {"000001": bar(9800, 10700, 9700, 10000)})
        t = L["trades"][0]
        self.assertEqual((t["reason"], t["exitPrice"], t["exitDecisionKey"]),
                         ("pm_sell", 9800, "d/k"))

    def test_idempotent(self):
        L = self.filled()
        r = E.settle_day(L, "2026-09-02", {"000001": bar(1, 1, 1, 1)})
        self.assertIn("skip", r)
        self.assertEqual(len(L["equity"]), 1)

    def test_missing_bar_keeps_position(self):
        L = self.filled()
        E.settle_day(L, "2026-09-03", {})
        self.assertEqual(L["positions"][0]["holdDay"], 2)
        self.assertEqual(L["equity"][-1]["value"], 10 * 10100)


class TestCash(unittest.TestCase):
    def setUp(self):
        # 자본 100만: A(-8% 손실)·B(+5% 이익) 보유, 현금 소진 상태 만들기
        L = E.new_ledger(1_000_000, start="2026-09-01")
        E.place_entry(L, code="00000A", name="A", date="2026-09-01", valid_date="2026-09-02",
                      entry=10000, stop=5000, target=None, weight=0.45)
        E.place_entry(L, code="00000B", name="B", date="2026-09-01", valid_date="2026-09-02",
                      entry=10000, stop=5000, target=None, weight=0.45)
        E.settle_day(L, "2026-09-02", {"00000A": bar(10000, 10000, 10000, 10000),
                                       "00000B": bar(10000, 10000, 10000, 10000)})
        self.L = L
        self.assertEqual(L["cash"], 100_000)

    def test_liquidate_loser_then_enter(self):
        L = self.L
        E.place_entry(L, code="00000C", name="C", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=9000, target=None, weight=0.3)     # 30주 = 30만
        ev = E.settle_day(L, "2026-09-03", {"00000A": bar(9200, 9300, 9100, 9200),
                                            "00000B": bar(10500, 10600, 10400, 10500),
                                            "00000C": bar(10000, 10100, 9900, 10000)})
        reasons = {t["code"]: t["reason"] for t in L["trades"]}
        self.assertEqual(reasons, {"00000A": "cash"})           # 이익 종목 B 는 유지
        self.assertEqual(L["trades"][0]["exitPrice"], 9200)
        self.assertEqual(len(ev["filled"]), 1)
        self.assertEqual(sorted(p["code"] for p in L["positions"]), ["00000B", "00000C"])

    def test_skip_when_losers_insufficient(self):
        L = self.L
        E.place_entry(L, code="00000C", name="C", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=9000, target=None, weight=0.9)     # 90만 필요
        E.settle_day(L, "2026-09-03", {"00000A": bar(9200, 9300, 9100, 9200),
                                       "00000B": bar(10500, 10600, 10400, 10500),
                                       "00000C": bar(10000, 10100, 9900, 10000)})
        self.assertFalse(L["trades"])                           # 아무것도 팔지 않음
        self.assertEqual(L["orderLog"][-1]["status"], "skipped")

    def test_losers_sorted_by_loss_rate(self):
        L = E.new_ledger(1_000_000, start="2026-09-01")
        for c, w in (("00000A", 0.3), ("00000B", 0.3), ("00000D", 0.3)):
            E.place_entry(L, code=c, name=c, date="2026-09-01", valid_date="2026-09-02",
                          entry=10000, stop=5000, target=None, weight=w)
        E.settle_day(L, "2026-09-02", {c: bar(10000, 10000, 10000, 10000)
                                       for c in ("00000A", "00000B", "00000D")})
        E.place_entry(L, code="00000C", name="C", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=9000, target=None, weight=0.3)
        E.settle_day(L, "2026-09-03", {"00000A": bar(9500, 9500, 9500, 9500),
                                       "00000B": bar(9000, 9000, 9000, 9000),
                                       "00000D": bar(9800, 9800, 9800, 9800),
                                       "00000C": bar(10000, 10000, 9900, 10000)})
        self.assertEqual(L["trades"][0]["code"], "00000B")      # 손실률 가장 큰 것부터


if __name__ == "__main__":
    unittest.main()
