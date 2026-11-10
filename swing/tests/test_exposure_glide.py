"""순노출·현금 단계 한도(2026-11-10 사용자 결정: 주당 10%p) — 주차·한도 계산, 방향 게이트, 비중 확대. 오프라인."""
import tempfile
import unittest

from swing import agent_iface as A
from swing import daily, prices, store
from swing import engine as E
from swing import exposure as X

TOP = ["005930", "000660"]
G = {"start": "2026-11-11", "net0": 0.378, "cash0": 0.395}      # 2026-11-10(화) 종가 장부에서 출발, 1주차 = 11/11(수)부터 7일


def pos(code, side, sector, qty, price, **kw):
    return {"id": "p" + code, "code": code, "name": code, "side": side, "sector": sector, "qty": qty, "entry": price,
            "lastClose": price, "sellPending": None, **kw}


def order(code, side, sector, qty, entry, **kw):
    return {"code": code, "side": side, "sector": sector, "qty": qty, "entry": entry, "type": "entry", "status": "open", **kw}


class TestGlideLimits(unittest.TestCase):
    def test_week_is_seven_days_from_start(self):
        self.assertEqual([X.glide_week("2026-11-11", d) for d in ("2026-11-11", "2026-11-13", "2026-11-16", "2026-11-17")], [1, 1, 1, 1])
        self.assertEqual([X.glide_week("2026-11-11", d) for d in ("2026-11-18", "2026-11-24", "2026-11-25", "2026-12-09")], [2, 2, 3, 5])
        self.assertEqual(X.glide_week("2026-11-11", "2026-11-10"), 1)             # 시작 전 날짜(정산 당일 저녁 대시보드)도 1주차

    def test_limits_step_10pp_per_week_to_final(self):
        w1 = X.glide_limits(G, "2026-11-11")
        self.assertEqual((w1["netMax"], w1["netMin"], w1["cashMax"], w1["week"], w1["final"]), (0.278, -0.10, 0.295, 1, False))
        self.assertEqual(X.glide_limits(G, "2026-11-17")["netMax"], 0.278)        # 다음 주 화요일까지 1주차
        w2 = X.glide_limits(G, "2026-11-18")                                      # 11/17 정산의 주문(11/18 진입)부터 2주차
        self.assertEqual((w2["netMax"], w2["cashMax"], w2["final"]), (0.178, 0.195, False))
        w3 = X.glide_limits(G, "2026-11-25")
        self.assertEqual((w3["netMax"], w3["cashMax"], w3["final"]), (0.15, 0.10, True))     # 3주차에 최종 한도
        self.assertEqual(X.glide_limits(G, "2027-01-04")["netMax"], 0.15)

    def test_inside_band_or_no_state_is_final_at_once(self):
        f = X.glide_limits(None, "2026-11-11")
        self.assertEqual((f["netMin"], f["netMax"], f["cashMax"], f["final"], f["week"]), (-0.10, 0.15, 0.10, True, None))
        ok = X.glide_limits({"start": "2026-11-10", "net0": 0.05, "cash0": 0.08}, "2026-11-11")
        self.assertEqual((ok["netMin"], ok["netMax"], ok["cashMax"], ok["final"]), (-0.10, 0.15, 0.10, True))
        low = X.glide_limits({"start": "2026-11-10", "net0": -0.33, "cash0": 0.08}, "2026-11-11")   # 하한 쪽도 대칭
        self.assertEqual((low["netMin"], low["netMax"], low["final"]), (-0.23, 0.15, False))
        self.assertEqual(X.glide_limits({"start": "2026-11-10", "net0": -0.33, "cash0": 0.08}, "2026-11-24")["netMin"], -0.10)


class TestNetGate(unittest.TestCase):
    def ctx(self, net_max=0.25, net_min=-0.10, final=False, eq=1_000_000, **kw):
        return {"eq": eq, "top": TOP, "brakes": {}, "px": {},
                "limits": {"netMin": net_min, "netMax": net_max, "cashMax": 0.3, "week": 1, "final": final}, **kw}

    def book(self):
        return [pos("000100", "long", "화학", 30, 10000), pos("000200", "short", "유통", 10, 10000)]   # Long 30% · Short 10% → 순 +20%

    def test_net_room_counts_same_side_pending_only(self):
        c, ps = self.ctx(), self.book()
        self.assertEqual(X.net_room(c, ps, [], "long"), 50_000)                              # 25% − 20%
        pend = [order("000300", "long", "건설", 3, 10000), order("000400", "short", "철강", 9, 10000)]
        self.assertEqual(X.net_room(c, ps, pend, "long"), 20_000)                            # 대기 Long 3% 만 차감, 대기 Short 는 미체결로 봄
        self.assertEqual(X.net_room(c, ps, pend, "short"), 210_000)                          # (20% − 대기 Short 9%) − (−10%)
        self.assertIsNone(X.net_room({"eq": 1_000_000, "px": {}}, ps, [], "long"))           # 한도 없음 → 보지 않는다
        gone = [pos("000100", "long", "화학", 30, 10000, sellPending={"kind": "pm_sell"})] + ps[1:]
        self.assertEqual(X.net_room(c, gone, [], "long"), 350_000)                           # 청산 예약분은 내일 나감

    def test_guard_trims_long_to_stage_cap_and_blocks_at_cap(self):
        ps = self.book()
        o = order("000300", "long", "건설", 4, 10000)
        self.assertEqual(X.guard_order(self.ctx(0.25), ps, [], o), (4, None))                # 여유 5% 안
        self.assertEqual(X.guard_order(self.ctx(0.23), ps, [], o), (3, "순노출 단계 한도 +23.0% — 3/4주로 축소"))
        self.assertEqual(X.guard_order(self.ctx(0.20), ps, [], o), (0, "순노출 단계 한도 +20.0% 도달 — 주문 안 함"))
        self.assertEqual(X.guard_order(self.ctx(0.15, final=True), ps, [], o), (0, "순노출 한도 +15.0% 도달 — 주문 안 함"))
        self.assertEqual(X.guard_order(self.ctx(0.20), ps, [], order("000400", "short", "철강", 4, 10000)), (4, None))   # 반대 방향은 그대로
        # 같은 여유면 종목 상한이 사유(종목 → 섹터 → 순노출 순)
        self.assertEqual(X.guard_order(self.ctx(0.25), ps, [], order("000300", "long", "건설", 8, 10000))[1], "종목 상한 5% — 5/8주로 축소")

    def test_guard_blocks_short_below_floor(self):
        ps = [pos("000100", "long", "화학", 5, 10000), pos("000200", "short", "유통", 13, 10000)]   # 순 −8%
        q, why = X.guard_order(self.ctx(), ps, [], order("000400", "short", "철강", 4, 10000))
        self.assertEqual((q, why), (2, "순노출 한도 -10.0% — 2/4주로 축소"))            # 하한은 최종 한도 그대로
        self.assertEqual(X.guard_order(self.ctx(), ps, [], order("000300", "long", "건설", 4, 10000)), (4, None))


class TestBoost(unittest.TestCase):
    def ctx(self, left):
        return {"eq": 1_000_000, "top": TOP, "brakes": {}, "px": {}, "boostLeft": left}

    def test_boost_doubles_within_stock_cap_and_cash_excess(self):
        self.assertEqual(X.boost_weight(self.ctx(100_000), 0.02, "035720"), 0.04)            # 2배
        self.assertEqual(X.boost_weight(self.ctx(100_000), 0.03, "035720"), 0.05)            # 종목 상한 5%
        self.assertEqual(X.boost_weight(self.ctx(100_000), 0.06, "005930"), 0.10)            # 시총 1위는 10%
        self.assertEqual(X.boost_weight(self.ctx(10_000), 0.02, "035720"), 0.03)             # 남은 현금 초과분 1% 까지만
        self.assertEqual(X.boost_weight(self.ctx(2_000), 0.02, "035720"), 0.02)              # 초과분이 0.3% 미만이면 그대로
        self.assertEqual(X.boost_weight(self.ctx(100_000), 0.05, "035720"), 0.05)            # 이미 상한
        self.assertEqual(X.boost_weight(self.ctx(0), 0.02, "035720"), 0.02)
        self.assertEqual(X.boost_weight({"eq": 1_000_000}, 0.02, "035720"), 0.02)            # 단계 한도 꺼짐(boostLeft 없음)

    def test_boost_max_one_disables(self):
        old, X.BOOST_MAX = X.BOOST_MAX, 1.0
        try:
            self.assertEqual(X.boost_weight(self.ctx(100_000), 0.02, "035720"), 0.02)
        finally:
            X.BOOST_MAX = old


class WAgent:
    """Long = Buy(비중 w) · Short = Underweight(비중 w)."""
    name = "w"

    def __init__(self, w):
        self.w, self.calls = w, []

    def decide(self, req):
        self.calls.append((req.code, req.side))
        d = A.Decision(code=req.code, date=req.date, purpose=req.purpose, agent=self.name, side=req.side)
        if req.purpose == "review":
            d.rating = "Hold"
        elif req.side == "short":
            d.rating, d.action, d.entry, d.stop, d.target, d.weight = "Underweight", "Sell", 10000, 10500, 9300, self.w
        else:
            d.rating, d.action, d.entry, d.stop, d.target, d.weight = "Buy", "Buy", 10000, 9500, 10600, self.w
        return d


def wb(date, up=(), down=()):
    return {"asof": f"{date} 16:13 KST", "sectorFlow": {"rows": []},
            "sectorScreen": {"matrix": {"동반강세": [{"code": c, "name": c, "sector": s} for c, s in up],
                                        "수급유입": [], "수급이탈": [],
                                        "동반약세": [{"code": c, "name": c, "sector": "유통"} for c in down]}}}


def flat(codes):
    return {c: {"open": 10000, "high": 10000, "low": 10000, "close": 10000} for c in codes}


class TestRunDayGlide(unittest.TestCase):
    def setUp(self):
        self._keep = (X.GUARD_ON, X.GLIDE_ON, X.NET_MAX, X.BOOST_MAX)
        X.GUARD_ON, X.GLIDE_ON = True, True
        self.st = store.FileStore(tempfile.mkdtemp())

    def tearDown(self):
        X.GUARD_ON, X.GLIDE_ON, X.NET_MAX, X.BOOST_MAX = self._keep
        E.EXPOSURE_CTX = None

    def test_anchor_gate_and_judge_skip(self):
        # 최종 상한을 8%로 낮춰 두 종목으로 한도에 닿게 한다(빈 장부 출발 → 단계 없이 바로 최종 한도)
        X.NET_MAX, X.BOOST_MAX = 0.08, 1.0
        ag = WAgent(0.10)
        codes = ["000010", "000030", "000040", "000050"]
        px = prices.DictPrices({"2026-09-21": flat(codes), "2026-09-22": flat(codes)})
        r1 = daily.run_day("2026-09-21", self.st, ag, px, wb("2026-09-21", up=[("000010", "화학"), ("000030", "건설")]))
        L = self.st.get("ledger")
        self.assertEqual(L["exposureGlide"], {"start": "2026-09-22", "asof": "2026-09-21", "net0": 0.0, "cash0": 1.0})   # 1주차 = 주문 진입일부터, 한 번만 기록
        self.assertEqual([(o["code"], o["qty"]) for o in L["orders"]], [("000010", 2500), ("000030", 1500)])   # 5%(종목 상한) + 3%(순노출 여유)
        self.assertIn("순노출 한도 +8.0%", L["orders"][1]["guard"])
        self.assertEqual((r1["exposure"]["limits"]["netMax"], r1["exposure"]["limits"]["cashMax"], r1["exposure"]["limits"]["week"]), (8.0, 90.0, 1))
        self.assertNotIn("gate", r1["exposure"])
        # D2: 두 주문 체결 → 순노출 +8% = 상한 → Long 후보는 판단하지 않고, Short 후보는 평소대로 판단·주문
        n = len(ag.calls)
        r2 = daily.run_day("2026-09-22", self.st, ag, px, wb("2026-09-22", up=[("000040", "철강")], down=["000050"]))
        self.assertEqual(ag.calls[n:], [("000050", "short")])
        self.assertEqual(r2["exposure"]["gate"], "long")
        row = r2["candidates"][0]
        self.assertEqual((row["code"], row["ordered"], row["braked"]), ("000040", False, True))
        self.assertIn("순노출 한도 +8.0% — Long 신규 중단(판단 생략)", row["why"])
        self.assertTrue(r2["shortCandidates"][0]["ordered"])
        self.assertTrue(any("Long 신규 중단" in x for x in r2["notes"]))
        self.assertEqual(self.st.get("ledger")["exposureGlide"]["start"], "2026-09-22")               # 출발점 유지
        self.assertIsNone(E.EXPOSURE_CTX)

    def test_cash_over_stage_cap_boosts_weight(self):
        # 빈 장부(현금 100%) → 1주차 현금 한도 90% → 초과분 10% 안에서 PM 비중 2% 를 4% 로
        ag = WAgent(0.02)
        px = prices.DictPrices({"2026-09-21": flat(["000010", "000030", "000050"])})
        r = daily.run_day("2026-09-21", self.st, ag, px, wb("2026-09-21", up=[("000010", "화학"), ("000030", "건설")], down=["000050"]))
        L = self.st.get("ledger")
        self.assertEqual([(o["code"], o["weight"], o.get("boostFrom"), o["qty"]) for o in L["orders"]],
                         [("000010", 0.04, 0.02, 2000), ("000030", 0.04, 0.02, 2000), ("000050", 0.04, 0.02, 2000)])
        self.assertEqual((r["candidates"][0]["weight"], r["candidates"][0]["boostFrom"]), (0.04, 0.02))

    def test_glide_off_keeps_display_only(self):
        X.GLIDE_ON, X.NET_MAX = False, 0.08
        ag = WAgent(0.02)
        px = prices.DictPrices({"2026-09-21": flat(["000010"])})
        r = daily.run_day("2026-09-21", self.st, ag, px, wb("2026-09-21", up=[("000010", "화학")]))
        L = self.st.get("ledger")
        self.assertNotIn("exposureGlide", L)
        self.assertNotIn("limits", r["exposure"])
        self.assertEqual((L["orders"][0]["weight"], L["orders"][0]["qty"]), (0.02, 1000))


if __name__ == "__main__":
    unittest.main()
