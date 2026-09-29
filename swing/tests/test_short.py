"""Short(공매도) 오프라인 테스트 — 엔진·신호·판정·파이프라인 맥락·하루 흐름(2026-09-29, PLAN_SHORT.md)."""
import tempfile
import unittest

from swing import agent_iface as A
from swing import config, daily, prices, store
from swing import engine as E
from swing import signals as S
from swing.agent import pipeline as PL


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def short_ledger(entry=10000, stop=10500, target=9400, weight=0.1, cap=1_000_000):
    L = E.new_ledger(cap, start="2026-09-01")
    E.place_entry(L, code="000001", name="A", date="2026-09-01", valid_date="2026-09-02",
                  entry=entry, stop=stop, target=target, weight=weight, side="short")
    return L


def filled(**kw):
    L = short_ledger(**kw)
    E.settle_day(L, "2026-09-02", {"000001": bar(9900, 10100, 9850, 10000)})
    return L


def fee(qty, entry, days):
    return qty * entry * config.BORROW_RATE * days / 365


class TestShortEngine(unittest.TestCase):
    def test_fill_when_high_reaches_entry_collateral_tax_fee(self):
        L = filled()
        p = L["positions"][0]
        self.assertEqual((p["side"], p["qty"], p["entry"], p["holdDay"]), ("short", 10, 10000, 1))
        tax = 100_000 * config.SELL_TAX
        self.assertAlmostEqual(p["entryTax"], tax)
        self.assertAlmostEqual(L["cash"], 1_000_000 - 100_000 - tax - fee(10, 10000, 1))
        # 평가 = 현금 + 담보 + (진입 − 종가) × 수량 → 종가 = 진입이면 비용만큼 감소
        self.assertAlmostEqual(E.equity_now(L), 1_000_000 - tax - fee(10, 10000, 1))

    def test_not_reached_when_high_below_entry(self):
        L = short_ledger()
        ev = E.settle_day(L, "2026-09-02", {"000001": bar(9700, 9900, 9600, 9800)})
        self.assertIn("미도달(고가", ev["cancelled"][0]["note"])
        self.assertFalse(L["positions"])

    def test_fill_day_stop_only_no_target(self):
        L = short_ledger()
        E.settle_day(L, "2026-09-02", {"000001": bar(9900, 10600, 9300, 10400)})   # 손절·목표 모두 닿음
        t = L["trades"][0]
        self.assertEqual((t["reason"], t["exitPrice"], t["note"]), ("stop", 10500, "체결일 손절(보수적)"))

    def test_gap_up_stop_at_open_and_pnl(self):
        L = filled()
        E.settle_day(L, "2026-09-03", {"000001": bar(10800, 11000, 10700, 10900)})
        t = L["trades"][0]
        self.assertEqual((t["reason"], t["exitPrice"]), ("stop_gap", 10800))
        tax, f = 100_000 * config.SELL_TAX, fee(10, 10000, 1)
        self.assertEqual(t["pnl"], round(10 * (10000 - 10800) - tax - f))
        self.assertLess(t["retPct"], 0)
        self.assertAlmostEqual(L["cash"], E.equity_now(L))          # 포지션 없음 → 평가 = 현금

    def test_target_flags_review_and_stop_priority(self):
        L = filled()
        E.settle_day(L, "2026-09-03", {"000001": bar(9800, 9900, 9350, 9500)})
        p = L["positions"][0]
        self.assertEqual(p["reviewFlag"], "target")
        self.assertEqual(p["targetHit"]["low"], 9350)
        L2 = filled()
        E.settle_day(L2, "2026-09-03", {"000001": bar(10000, 10600, 9300, 10000)})
        self.assertEqual(L2["trades"][0]["note"], "목표·손절 동시 도달 → 손절 우선")

    def test_cover_next_open_profit_and_borrow_fee_calendar_days(self):
        L = filled()
        E.settle_day(L, "2026-09-04", {"000001": bar(9700, 9800, 9600, 9650)})   # 2일 경과(9/2 → 9/4)
        E.schedule_sell(L, "000001", "PM Buy", kind="pm_sell")
        E.settle_day(L, "2026-09-07", {"000001": bar(9500, 9600, 9400, 9550)})
        t = L["trades"][0]
        f = fee(10, 10000, 1) + fee(10, 10000, 2)
        self.assertEqual(t["borrowFee"], round(f))
        self.assertEqual(t["pnl"], round(10 * (10000 - 9500) - 100_000 * config.SELL_TAX - f))
        self.assertAlmostEqual(L["cash"], 1_000_000 + t["pnl"], delta=1)

    def test_apply_hold_after_target_breakeven_stop_min(self):
        L = filled()
        E.settle_day(L, "2026-09-03", {"000001": bar(9800, 9900, 9350, 9500)})
        E.apply_hold(L, "000001", target=9000, stop=10300)            # 새 손절이 진입가 위 → 본전(진입가)로
        p = L["positions"][0]
        self.assertEqual((p["target"], p["stop"], p["extended"]), (9000, 10000, True))
        E.apply_hold(L, "000001", target=9800, stop=9400)            # 목표가 현재가 위·손절 현재가 아래 → 무시
        self.assertEqual((p["target"], p["stop"]), (9000, 10000))

    def test_expiry_flag(self):
        L = filled()
        for d in range(3, 3 + config.HOLD_DAYS - 1):
            E.settle_day(L, f"2026-09-{d:02d}", {"000001": bar(10000, 10100, 9900, 10000)})
        self.assertEqual(L["positions"][0]["reviewFlag"], "expiry")

    def test_short_gross_cap_skips(self):
        L = E.new_ledger(1_000_000, start="2026-09-01")
        o1 = E.place_entry(L, code="000001", name="A", date="2026-09-01", valid_date="2026-09-02",
                           entry=10000, stop=10500, target=None, weight=0.1, side="short")
        o2 = E.place_entry(L, code="000002", name="B", date="2026-09-01", valid_date="2026-09-02",
                           entry=10000, stop=10500, target=None, weight=0.1, side="short")
        o3 = E.place_entry(L, code="000003", name="C", date="2026-09-01", valid_date="2026-09-02",
                           entry=10000, stop=10500, target=None, weight=0.1, side="short")
        o4 = E.place_entry(L, code="000004", name="D", date="2026-09-01", valid_date="2026-09-02",
                           entry=10000, stop=10500, target=None, weight=0.1, side="short")
        self.assertEqual([o["status"] for o in (o1, o2, o3, o4)], ["open", "open", "open", "skipped"])
        self.assertIn("총노출", o4["note"])
        # Long 주문은 Short 상한과 무관
        o5 = E.place_entry(L, code="000005", name="E", date="2026-09-01", valid_date="2026-09-02",
                           entry=10000, stop=9500, target=None, weight=0.1)
        self.assertEqual(o5["status"], "open")

    def test_raise_cash_closes_losing_short(self):
        L = filled(weight=0.3)                                      # Short 총노출 상한(30%)까지
        E.place_entry(L, code="000002", name="B", date="2026-09-02", valid_date="2026-09-03",
                      entry=10000, stop=9500, target=None, weight=0.8)  # 남은 현금(~70만)보다 큰 Long
        E.settle_day(L, "2026-09-03", {"000001": bar(10300, 10400, 10200, 10300),   # 숏 평가손실
                                       "000002": bar(10000, 10100, 9900, 10000)})
        self.assertEqual(L["trades"][0]["reason"], "cash")
        self.assertEqual(L["positions"][0]["code"], "000002")


class TestShortSignals(unittest.TestCase):
    def wb(self):
        return {"asof": "2026-09-28 16:13 KST",
                "sectorFlow": {"rows": [{"name": "화학", "signal": "동반강세"}, {"name": "유통", "signal": "동반약세"}]},
                "sectorScreen": {"matrix": {
                    "동반강세": [{"code": "000010", "name": "a", "sector": "화학"}],
                    "수급유입": [],
                    "수급이탈": [{"code": "000030", "name": "c", "sector": "증권"},
                              {"code": "000020", "name": "dup", "sector": "유통"}],
                    "동반약세": [{"code": "000020", "name": "b", "sector": "유통"}]}}}

    def test_short_candidates_order_dedupe_exclude(self):
        self.assertEqual([x["code"] for x in S.short_candidates(self.wb())], ["000020", "000030"])
        self.assertEqual([x["code"] for x in S.short_candidates(self.wb(), {"000020"})], ["000030"])

    def test_review_triggers_by_side(self):
        pos = [{"code": "000099", "name": "s1", "sector": "화학", "side": "short"},    # 섹터 동반강세 전환
               {"code": "000010", "name": "s2", "sector": "건설", "side": "short"},    # 종목 상방 칸
               {"code": "000098", "name": "s3", "sector": "유통", "side": "short"},    # 하방 신호 → 트리거 아님
               {"code": "000097", "name": "l1", "sector": "유통"}]                      # Long 은 기존 규칙
        t = {x["code"]: x for x in S.review_triggers(pos, self.wb())}
        self.assertEqual(set(t), {"000099", "000010", "000097"})
        self.assertEqual((t["000099"]["side"], t["000099"]["signal"]), ("short", "동반강세"))
        self.assertIn("종목 동반강세 칸 등재", t["000010"]["reasons"])
        self.assertEqual(t["000097"]["side"], "long")


class TestShortVerdicts(unittest.TestCase):
    def d(self, **kw):
        base = dict(code="000001", date="2026-09-28", purpose="entry", side="short", rating="Underweight",
                    action="Sell", entry=10100, stop=10500, target=9400)
        base.update(kw)
        return A.Decision(**base)

    def test_entry_verdict_short(self):
        self.assertEqual(A.entry_verdict(self.d(), last_close=10000), (True, ""))
        self.assertEqual(A.entry_verdict(self.d(rating="Hold"))[1], "PM Hold")
        self.assertEqual(A.entry_verdict(self.d(action="Buy"))[1], "Trader Buy")
        self.assertIn("손절가 ≤ 진입가", A.entry_verdict(self.d(stop=10000))[1])
        self.assertIn("업틱룰", A.entry_verdict(self.d(entry=9900, stop=10300), last_close=10000)[1])

    def test_cover_verdict_and_target(self):
        self.assertTrue(A.sell_verdict(self.d(purpose="review", rating="Buy")))
        self.assertFalse(A.sell_verdict(self.d(purpose="review", rating="Hold")))
        self.assertFalse(A.sell_verdict(self.d(purpose="review", rating="Sell")))
        self.assertEqual(A.effective_target(self.d()), 9400)
        self.assertIsNone(A.effective_target(self.d(target=10600)))

    def test_mock_short_paths(self):
        ag = A.MockAgent()
        d = ag.decide(A.DecisionRequest(code="000020", name="x", date="2026-09-28", purpose="entry",
                                        side="short", last_close=10000))
        self.assertEqual((d.side, d.rating, d.action, d.entry, d.stop, d.target), ("short", "Underweight", "Sell", 10100, 10500, 9400))
        self.assertTrue(A.entry_verdict(d, last_close=10000)[0])
        self.assertEqual(tuple(d.reports), A.REPORT_KEYS)
        r = ag.decide(A.DecisionRequest(code="000020", name="x", date="2026-09-28", purpose="review",
                                        side="short", last_close=9800))
        self.assertTrue(A.sell_verdict(r))                           # 끝자리 짝수 → Buy = 환매


class TestShortContext(unittest.TestCase):
    def test_entry_and_review_context(self):
        e = PL.strategy_context(A.DecisionRequest(code="000001", name="x", date="2026-09-28", purpose="entry",
                                                  side="short", last_close=10000, signal="동반약세"))
        self.assertIn("Sell = 공매도 신규 진입", e)
        self.assertIn("10,000원", e)
        self.assertIn("Stop Loss 는 Entry Price **위**", e)
        r = PL.strategy_context(A.DecisionRequest(code="000001", name="x", date="2026-09-28", purpose="review",
                                                  side="short", position={"entry": 10000, "stop": 10500,
                                                                          "holdDay": 2}))
        self.assertIn("환매 검토", r)
        self.assertIn("Buy/Overweight(상승 전망)면 다음 영업일 시가 환매", r)
        # Long 맥락은 그대로
        self.assertNotIn("공매도", PL.strategy_context(A.DecisionRequest(code="1", name="x", date="d", purpose="entry")))


class SideAgent:
    """방향별 고정 판단 — entry(short) → Underweight/Sell, review(short) → review 등급."""
    name = "side"

    def __init__(self, review="Hold"):
        self.review, self.calls = review, []

    def decide(self, req):
        self.calls.append((req.code, req.purpose, req.side))
        d = A.Decision(code=req.code, date=req.date, purpose=req.purpose, side=req.side, agent=self.name)
        if req.purpose == "review":
            d.rating = self.review
        elif req.side == "short":
            d.rating, d.action, d.entry, d.stop, d.target, d.weight = "Underweight", "Sell", 10100, 10600, 9300, 0.1
        return d


def wb(date, up=(), down=()):
    return {"asof": f"{date} 16:13 KST", "sectorFlow": {"rows": []},
            "sectorScreen": {"matrix": {"동반강세": [{"code": c, "name": c, "sector": "화학"} for c in up],
                                        "수급유입": [], "수급이탈": [],
                                        "동반약세": [{"code": c, "name": c, "sector": "유통"} for c in down]}}}


class TestShortDaily(unittest.TestCase):
    def test_short_entry_fill_cover_flow(self):
        st, ag = store.FileStore(tempfile.mkdtemp()), SideAgent(review="Buy")
        px = prices.DictPrices({
            "2026-09-21": {"000020": bar(10000, 10050, 9950, 10000)},
            "2026-09-22": {"000020": bar(10000, 10200, 9900, 10000)},     # 고가 ≥ 10,100 → 공매도 체결
            "2026-09-23": {"000020": bar(9900, 9950, 9800, 9850)},
            "2026-09-28": {"000020": bar(9700, 9800, 9650, 9700)}})
        r1 = daily.run_day("2026-09-21", st, ag, px, wb("2026-09-21", down=["000020"]))
        c = r1["shortCandidates"][0]
        self.assertEqual((c["side"], c["ordered"], c["decisionKey"]),
                         ("short", True, "decision/2026-09-21/000020/entry-short"))
        self.assertEqual(st.get("decision/2026-09-21/000020/entry-short")["side"], "short")
        daily.run_day("2026-09-22", st, ag, px, wb("2026-09-22", down=["000020"]))
        p = st.get("ledger")["positions"][0]
        self.assertEqual((p["side"], p["entry"]), ("short", 10100))
        # 보유 숏 종목이 동반강세 칸 → 환매 검토 → PM Buy → 다음 영업일 시가 환매
        r3 = daily.run_day("2026-09-23", st, ag, px, wb("2026-09-23", up=["000020"]))
        rv = r3["reviews"][0]
        self.assertEqual((rv["side"], rv["sell"]), ("short", True))
        self.assertIn(("000020", "review", "short"), ag.calls)
        self.assertEqual(r3["candidates"], [])                       # 숏 보유 종목은 Long 후보에서 제외
        daily.run_day("2026-09-28", st, ag, px, wb("2026-09-28"))
        t = st.get("ledger")["trades"][0]
        self.assertEqual((t["side"], t["reason"], t["exitPrice"]), ("short", "pm_sell", 9700))
        self.assertGreater(t["pnl"], 0)

    def test_short_disabled_and_cap(self):
        st, ag = store.FileStore(tempfile.mkdtemp()), SideAgent()
        px = prices.DictPrices({"2026-09-21": {}})
        old_on, old_cap = config.SHORT_ENABLED, config.MAX_NEW_SHORT_PER_DAY
        try:
            config.MAX_NEW_SHORT_PER_DAY = 1
            r = daily.run_day("2026-09-21", st, ag, px, wb("2026-09-21", down=["000020", "000030"]))
            self.assertEqual(([c["code"] for c in r["shortCandidates"]], r["shortCapSkipped"]), (["000020"], ["000030"]))
            config.SHORT_ENABLED = False
            r = daily.run_day("2026-09-22", store.FileStore(tempfile.mkdtemp()), ag, px,
                              wb("2026-09-22", down=["000020"]))
            self.assertEqual(r["shortCandidates"], [])
        finally:
            config.SHORT_ENABLED, config.MAX_NEW_SHORT_PER_DAY = old_on, old_cap


if __name__ == "__main__":
    unittest.main()
