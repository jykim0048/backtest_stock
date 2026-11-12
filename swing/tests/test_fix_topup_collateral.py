"""swing/tools/fix_topup_collateral.py — 2026-11-12 Short 추가 진입 담보 오기록 보정."""
import copy
import unittest

from swing import config, engine as E
from swing.tools import fix_topup_collateral as F


def bar(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


class TestFixTopupCollateral(unittest.TestCase):
    def setUp(self):
        self._cfg = (config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN)
        config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN = "subst", 0.7, 0.5

    def tearDown(self):
        config.SHORT_COLLATERAL, config.SHORT_SUBST_RATE, config.SHORT_MARGIN = self._cfg

    def _good(self):
        """Long 60만 + 대용 Short 30주 + 9/04 대용 추가 10주(올바른 원장)."""
        L = E.new_ledger(1_000_000, start="2026-09-01")
        E.place_entry(L, code="000010", name="L", date="2026-09-01", valid_date="2026-09-02", entry=10000, stop=9000, target=12000, weight=0.6)
        E.settle_day(L, "2026-09-02", {"000010": bar(10000, 10100, 9950, 10000)})
        E.place_entry(L, code="000001", name="S", date="2026-09-02", valid_date="2026-09-03", entry=10000, stop=10500, target=9400, weight=0.3, side="short")
        E.settle_day(L, "2026-09-03", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(9900, 10100, 9850, 10000)})
        s = next(p for p in L["positions"] if p["side"] == "short")
        E.place_entry(L, code="000001", name="S", date="2026-09-03", valid_date="2026-09-04", entry=10000, stop=10500, target=9400,
                      weight=0.0, side="short", qty=10, extra={"topupOf": s["id"], "topup": True})
        E.settle_day(L, "2026-09-04", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(10000, 10100, 9900, 9800)})
        E.settle_day(L, "2026-09-07", {"000010": bar(10000, 10000, 10000, 10000), "000001": bar(9800, 9900, 9700, 9800)})
        return L

    def _broken(self, good):
        """사고 때처럼 — 추가분을 현금 담보로 기록(현금에서 진입 금액이 더 빠짐, 9/04 이후 평가액도 같은 만큼 낮음)."""
        L = copy.deepcopy(good)
        o = next(x for x in L["orderLog"] if x.get("topupOf"))
        miss = o["qty"] * o["fillPrice"]
        o["collateral"] = "cash"
        L["cash"] -= miss
        for r in L["equity"]:
            if r["date"] >= o["validDate"]:
                r["cash"] = round(r["cash"] - miss)
                r["equity"] = round(r["equity"] - miss)
                r["retPct"] = round((r["equity"] / L["capital0"] - 1) * 100, 3)
        return L, miss

    def test_fix_restores_cash_and_equity_history(self):
        good = self._good()
        L, miss = self._broken(good)
        self.assertEqual(miss, 100_000)
        done = F.apply_fix(L, "2026-09-08")
        self.assertEqual([(o["id"], r) for o, r in done], [(next(x for x in good["orderLog"] if x.get("topupOf"))["id"], 100_000)])
        self.assertAlmostEqual(L["cash"], good["cash"], places=6)
        self.assertEqual([(r["date"], r["cash"], r["equity"], r["retPct"]) for r in L["equity"]],
                         [(r["date"], r["cash"], r["equity"], r["retPct"]) for r in good["equity"]])
        o = next(x for x in L["orderLog"] if x.get("topupOf"))
        self.assertEqual((o["collateral"], o["collateralFix"]["was"], o["collateralFix"]["refund"]), ("subst", "cash", 100_000))
        self.assertEqual(L["collateral"], good["collateral"])

    def test_idempotent_and_ignores_correct_ledgers(self):
        good = self._good()
        self.assertEqual(F.apply_fix(copy.deepcopy(good), "2026-09-08"), [])     # 올바른 원장은 건드리지 않음
        L, _ = self._broken(good)
        F.apply_fix(L, "2026-09-08")
        snap = copy.deepcopy(L)
        self.assertEqual(F.apply_fix(L, "2026-09-08"), [])                       # 두 번째는 할 일 없음
        self.assertEqual(L, snap)

    def test_closed_base_position_is_reported_not_touched(self):
        L, _ = self._broken(self._good())
        L["positions"] = [p for p in L["positions"] if p["side"] != "short"]
        fix, closed = F.find(L)
        self.assertEqual((len(fix), len(closed)), (0, 1))


if __name__ == "__main__":
    unittest.main()
