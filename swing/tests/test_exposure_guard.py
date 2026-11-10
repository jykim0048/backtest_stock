"""노출 한도의 주문 단계 적용(2026-11-10, PLAN_EXPOSURE.md) — 종목 상한·섹터 브레이크·우선 검토·현금 담보 우선. 오프라인."""
import unittest

from swing import config, daily
from swing import engine as E
from swing import exposure as X

TOP = ["005930", "000660"]


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def pos(code, side, sector, qty, price, **kw):
    return {"id": "p" + code, "code": code, "name": code, "side": side, "sector": sector, "qty": qty, "entry": price,
            "lastClose": price, "sellPending": None, **kw}


def order(code, side, sector, qty, entry, **kw):
    return {"code": code, "side": side, "sector": sector, "qty": qty, "entry": entry, "type": "entry", "status": "open", **kw}


class TestGuardOrder(unittest.TestCase):
    def ctx(self, brakes=None, eq=1_000_000):
        return {"eq": eq, "top": TOP, "brakes": brakes or {}, "px": {}}

    def test_stock_cap_5pct_and_top2_10pct(self):
        c = self.ctx()
        self.assertEqual(X.guard_order(c, [], [], order("035720", "long", "IT 서비스", 8, 10000)),
                         (5, "종목 상한 5% — 5/8주로 축소"))                                  # 8만 → 5만(5%)
        self.assertEqual(X.guard_order(c, [], [], order("005930", "long", "전기·전자", 10, 10000)), (10, None))   # 시총 1위 10% 까지
        self.assertEqual(X.guard_order(c, [], [], order("005930", "long", "전기·전자", 12, 10000))[0], 10)
        self.assertEqual(X.guard_order(c, [], [], order("035720", "short", "IT 서비스", 5, 10000)), (5, None))    # Short 도 같은 상한

    def test_cap_counts_holdings_and_pending_orders(self):
        c = self.ctx()
        held = [pos("035720", "long", "IT 서비스", 3, 10000)]                                  # 3% 보유
        self.assertEqual(X.guard_order(c, held, [], order("035720", "long", "IT 서비스", 5, 10000, topupOf="p035720"))[0], 2)
        pend = [order("035720", "long", "IT 서비스", 2, 10000)]                                # + 대기 2% → 여유 0
        q, why = X.guard_order(c, held, pend, order("035720", "long", "IT 서비스", 5, 10000))
        self.assertEqual((q, why), (0, "종목 상한 5% 도달 — 주문 안 함"))
        over = [pos("035720", "long", "IT 서비스", 7, 10000)]                                  # 이미 상한 초과 보유 → 추가만 막음
        self.assertEqual(X.guard_order(c, over, [], order("035720", "long", "IT 서비스", 1, 10000))[0], 0)
        gone = [pos("035720", "long", "IT 서비스", 7, 10000, sellPending={"kind": "pm_sell"})]   # 청산 예약분은 내일 나가므로 0
        self.assertEqual(X.guard_order(c, gone, [], order("035720", "long", "IT 서비스", 5, 10000))[0], 5)

    def test_sector_room_to_10pct(self):
        c = self.ctx()
        held = [pos("A", "long", "IT 서비스", 4, 10000), pos("B", "long", "IT 서비스", 4, 10000)]     # 섹터 +8%
        q, why = X.guard_order(c, held, [], order("C", "long", "IT 서비스", 5, 10000))
        self.assertEqual((q, why), (2, "섹터 IT 서비스 순노출 한도 ±10% — 2/5주로 축소"))            # 남은 여유 2%
        self.assertEqual(X.guard_order(c, held, [], order("C", "short", "IT 서비스", 5, 10000)), (5, None))   # 반대 방향은 여유 18%
        self.assertEqual(X.guard_order(c, held, [], order("D", "long", "화학", 5, 10000)), (5, None))         # 다른 섹터는 무관

    def test_braked_sector_blocks_same_side_only(self):
        c = self.ctx(brakes={"IT 서비스": "long", "전기·전자": "short"})
        q, why = X.guard_order(c, [], [], order("C", "long", "IT 서비스", 5, 10000))
        self.assertEqual(q, 0)
        self.assertIn("Long 신규·비중 채우기 중단", why)
        self.assertEqual(X.guard_order(c, [], [], order("C", "short", "IT 서비스", 5, 10000))[0], 5)
        self.assertEqual(X.guard_order(c, [], [], order("E", "short", "전기·전자", 5, 10000))[0], 0)

    def test_tiny_room_skips_order(self):
        c = self.ctx()
        held = [pos("035720", "long", "IT 서비스", 498, 100)]                                  # 4.98% — 여유 0.02% < 0.3%
        self.assertEqual(X.guard_order(c, held, [], order("035720", "long", "IT 서비스", 100, 100))[0], 0)

    def test_no_ctx_is_noop(self):
        self.assertEqual(X.guard_order(None, [], [], order("X", "long", "s", 99, 10000)), (99, None))


class TestBrakes(unittest.TestCase):
    def test_engage_at_10_release_inside_8(self):
        self.assertEqual(X.update_brakes({}, {"IT": 0.143, "음식료": 0.126, "전기전자": -0.112, "유통": 0.098}),
                         {"IT": "long", "음식료": "long", "전기전자": "short"})
        prev = {"IT": "long", "전기전자": "short"}
        self.assertEqual(X.update_brakes(prev, {"IT": 0.09, "전기전자": -0.085}), prev)          # ±8~10% 사이 — 유지
        self.assertEqual(X.update_brakes(prev, {"IT": 0.079, "전기전자": -0.07}), {})            # ±8% 안 — 해제
        self.assertEqual(X.update_brakes({"IT": "long"}, {"IT": -0.12}), {"IT": "short"})        # 반대로 넘어가면 방향 교체

    def test_book_and_sector_net(self):
        b = X.book([pos("A", "long", "IT", 10, 1000), pos("B", "short", "IT", 4, 1000), pos("C", "long", "IT", 9, 1000, sellPending={"k": 1})],
                   [order("D", "short", "화학", 5, 1000), order("E", "long", "화학", 5, 1000, status="filled")], {"A": 1100})
        self.assertEqual(b["sector"], {"IT": {"long": 11000.0, "short": 4000.0}, "화학": {"long": 0.0, "short": 5000.0}})
        self.assertEqual(X.sector_net_pct(b, 100_000), {"IT": 0.07, "화학": -0.05})


class TestPlaceEntryGuard(unittest.TestCase):
    def tearDown(self):
        E.EXPOSURE_CTX = None

    def test_place_entry_applies_guard_only_with_ctx(self):
        L = E.new_ledger(1_000_000, start="2026-09-01")
        o = E.place_entry(L, code="035720", name="K", date="2026-09-01", valid_date="2026-09-02", entry=10000, stop=9500, target=11000,
                          weight=0.10, sector="IT 서비스")
        self.assertEqual((o["qty"], o["status"], o.get("guard")), (10, "open", None))              # ctx 없으면 종전 그대로
        L = E.new_ledger(1_000_000, start="2026-09-01")
        E.EXPOSURE_CTX = {"eq": 1_000_000, "top": TOP, "brakes": {"화학": "short"}, "px": {}}
        o = E.place_entry(L, code="035720", name="K", date="2026-09-01", valid_date="2026-09-02", entry=10000, stop=9500, target=11000,
                          weight=0.10, sector="IT 서비스")
        self.assertEqual((o["qty"], o["status"], o["guardFrom"]), (5, "open", 10))                 # 10% → 종목 상한 5%
        self.assertIn("종목 상한 5%", o["guard"])
        o2 = E.place_entry(L, code="051910", name="C", date="2026-09-01", valid_date="2026-09-02", entry=10000, stop=10500, target=9000,
                           weight=0.05, sector="화학", side="short")
        self.assertEqual(o2["status"], "skipped")                                                 # 브레이크 섹터 같은 방향
        self.assertIn("섹터 화학 순노출 한도", o2["note"])
        o3 = E.place_entry(L, code="005930", name="S", date="2026-09-01", valid_date="2026-09-02", entry=10000, stop=9500, target=11000,
                           weight=0.10, sector="전기·전자")
        self.assertEqual((o3["qty"], o3["status"]), (10, "open"))                                 # 시총 1위는 10%, 섹터도 딱 10%
        o4 = E.place_entry(L, code="000660", name="H", date="2026-09-01", valid_date="2026-09-02", entry=10000, stop=9500, target=11000,
                           weight=0.10, sector="전기·전자")
        self.assertEqual(o4["status"], "skipped")                                                 # 같은 섹터 대기 주문 10% → 여유 없음
        self.assertEqual(len(L["orders"]), 2)

    def test_guard_switch_off(self):
        old, X.GUARD_ON = X.GUARD_ON, False
        try:
            L = E.new_ledger(1_000_000, start="2026-09-01")
            E.EXPOSURE_CTX = {"eq": 1_000_000, "top": TOP, "brakes": {}, "px": {}}
            o = E.place_entry(L, code="035720", name="K", date="2026-09-01", valid_date="2026-09-02", entry=10000, stop=9500, target=11000,
                              weight=0.10, sector="IT 서비스")
            self.assertEqual(o["qty"], 10)
        finally:
            X.GUARD_ON = old


class TestSectorSplit(unittest.TestCase):
    def test_same_side_dropped_opposite_first(self):
        c = lambda code, sec: {"code": code, "name": code, "sector": sec}   # noqa: E731
        brakes = {"IT": "long", "전기전자": "short"}
        kept, prio, braked = daily._sector_split([[c("a", "IT"), c("b", "화학")], [c("d", "전기전자"), c("e", "IT")]], "long", brakes)
        self.assertEqual([[x["code"] for x in k] for k in kept], [["b"], []])
        self.assertEqual([x["code"] for x in braked], ["a", "e"])            # IT 는 Long 중단 → 판단 생략
        self.assertEqual([x["code"] for x in prio], ["d"])                    # 전기전자는 Short 과다 → Long 후보 우선 검토
        kept, prio, braked = daily._sector_split([[c("s1", "IT"), c("s2", "전기전자"), c("s3", "화학")]], "short", brakes)
        self.assertEqual(([x["code"] for x in kept[0]], [x["code"] for x in prio], [x["code"] for x in braked]), (["s3"], ["s1"], ["s2"]))
        self.assertEqual(daily._sector_split([[c("a", "IT")]], "long", {}), ([[c("a", "IT")]], [], []))
        self.assertTrue(daily._braked_row(c("a", "IT"), "long")["braked"])


class TestCashFirstCollateral(unittest.TestCase):
    def setUp(self):
        self._cfg = (config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN)
        config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN = "cash_first", 0.7, 0.5

    def tearDown(self):
        config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN = self._cfg

    def _book(self, long_w):
        L = E.new_ledger(1_000_000, start="2026-09-01")
        E.place_entry(L, code="000010", name="L", date="2026-09-01", valid_date="2026-09-02", entry=10000, stop=9000, target=12000, weight=long_w)
        E.settle_day(L, "2026-09-02", {"000010": bar(10000, 10100, 9950, 10000)})
        return L

    def _short(self, L, w, code="000001"):
        E.place_entry(L, code=code, name="S", date="2026-09-02", valid_date="2026-09-03", entry=10000, stop=10500, target=9400, weight=w, side="short")

    def test_cash_collateral_when_cash_is_enough(self):
        L = self._book(0.60)                                           # Long 60만 · 현금 40만
        self._short(L, 0.30)
        E.settle_day(L, "2026-09-03", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(9900, 10100, 9850, 10000)})
        s = next(p for p in L["positions"] if p["side"] == "short")
        self.assertEqual((s["qty"], s["collateral"]), (30, "cash"))    # 현금 40만 ≥ 30만 + 매도세 → 현금 담보
        self.assertLess(L["cash"], 100_000)                             # 담보 30만이 현금에서 묶임 → 남는 현금 약 10만(10%)
        self.assertGreater(L["cash"], 99_000)

    def test_substitute_when_cash_is_short(self):
        L = self._book(0.90)                                           # Long 90만 · 현금 10만
        self._short(L, 0.30)
        E.settle_day(L, "2026-09-03", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(9900, 10100, 9850, 10000)})
        s = next(p for p in L["positions"] if p["side"] == "short")
        self.assertEqual((s["qty"], s["collateral"]), (30, "subst"))   # 현금 10만 < 30만 → 대용(증거금률 50%)
        c = E.collateral(L)
        self.assertAlmostEqual(c["req"], 150_000)                       # 30만 × 50%
        self.assertAlmostEqual(c["subst"], 630_000)                     # Long 90만 × 70%

    def test_subst_capacity_is_140pct_of_long_at_half_margin(self):
        L = self._book(0.90)
        for i, w in enumerate((0.25, 0.25, 0.25, 0.25)):               # Short 합 100% — 대용 한도(63만 ÷ 50% = 126만) 안
            self._short(L, w, code=f"00000{i + 1}")
        bars = {"000010": bar(10000, 10000, 10000, 10000)}
        bars.update({f"00000{i + 1}": bar(9900, 10100, 9850, 10000) for i in range(4)})
        E.settle_day(L, "2026-09-03", bars)
        shorts = [p for p in L["positions"] if p["side"] == "short"]
        self.assertEqual(sum(p["qty"] for p in shorts), 100)
        self.assertEqual({p["collateral"] for p in shorts}, {"subst"})

    def test_long_orders_keep_their_cash(self):
        # 같은 날 Long 30만·Short 30만 주문, 현금 40만 — Long 몫(30만)을 떼면 여유 10만 < 30만 → Short 는 대용, Long 은 전량 체결
        L = self._book(0.60)
        E.place_entry(L, code="000020", name="L2", date="2026-09-02", valid_date="2026-09-03", entry=10000, stop=9000, target=12000, weight=0.30)
        self._short(L, 0.30)
        E.settle_day(L, "2026-09-03", {"000010": bar(10000, 10000, 10000, 10000), "000020": bar(10000, 10100, 9900, 10000),
                                       "000001": bar(9900, 10100, 9850, 10000)})
        by = {p["code"]: p for p in L["positions"]}
        self.assertEqual(by["000020"]["qty"], 30)
        self.assertEqual(by["000001"]["collateral"], "subst")


if __name__ == "__main__":
    unittest.main()
