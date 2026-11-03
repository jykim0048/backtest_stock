"""스위칭 4등급(2026-11-03) — 손실은 빨리, 수익은 길게. D 는 PM 비중 초과분만. 오프라인."""
import tempfile
import unittest

from swing import engine as E, store
from swing.tests.test_daily import b
from swing.tests.test_switch import book, merge
from swing.tests.test_vwap_exec import D1, path, run
from swing.vwap import config as VC
from swing.vwap.executor import Executor

ORIG = (VC.TRANCHES, VC.MIN_GAP_MIN)


def setUpModule():
    VC.TRANCHES, VC.MIN_GAP_MIN = 3, 20


def tearDownModule():
    VC.TRANCHES, VC.MIN_GAP_MIN = ORIG


BUY = [("09:00", 10000), ("09:05", 9990), ("09:10", 9980), ("09:30", 9970)]


def make(positions, rating=None, st=None):
    L = book(0, [p[:3] for p in positions])
    L["orders"][0]["rating"] = rating
    for x, p in zip(L["positions"], positions):
        x.update(p[3] if len(p) > 3 else {})
    return Executor(st or store.FileStore(tempfile.mkdtemp()), D1, L, log=lambda *_: None)


def sold(ex, pid):
    return sum(f["qty"] for f in (ex.doc["exits"].get("s:" + pid) or {}).get("fills") or [])


def modes(ex, pid):
    return {f["mode"] for f in ex.doc["exits"]["s:" + pid]["fills"]}


class TestTiers(unittest.TestCase):
    def test_loser_below_vwap_sold_though_old_rule_protected_it(self):
        ex = make([("000020", 5000, 1000)])
        run(ex, merge(path("000010", BUY), path("000020", [("09:00", 4950), ("09:05", 4930), ("09:10", 4900), ("09:30", 4890)])))
        self.assertEqual(modes(ex, "p1"), {"swA"})                 # −2%, VWAP 아래, 시가 대비 −5% 미만

    def test_loser_sold_before_small_gainer(self):
        ex = make([("000020", 5000, 1000), ("000030", 5000, 1000)])
        run(ex, merge(path("000010", BUY),
                      path("000020", [("09:00", 5100), ("09:05", 5150), ("09:10", 5200), ("09:30", 5210)]),     # +4%, VWAP 위 = C
                      path("000030", [("09:00", 4950), ("09:05", 4930), ("09:10", 4900), ("09:30", 4890)])))    # −2% = A
        self.assertGreater(sold(ex, "p2"), 0)
        self.assertEqual(sold(ex, "p1"), 0)

    def test_closer_to_stop_sold_first_within_a(self):
        ex = make([("000020", 5000, 1000, {"stop": 4000.0}), ("000030", 5000, 1000, {"stop": 4800.0})])
        down = [("09:00", 4950), ("09:05", 4930), ("09:10", 4900), ("09:30", 4890)]
        run(ex, merge(path("000010", BUY), path("000020", down), path("000030", down)))
        self.assertGreater(sold(ex, "p2"), 0)                      # 같은 손실이어도 손절선에 가까운 쪽
        self.assertEqual(sold(ex, "p1"), 0)

    def test_stale_position_sold_even_below_vwap(self):
        ex = make([("000020", 5000, 1000, {"holdDay": 6})])
        run(ex, merge(path("000010", BUY), path("000020", [("09:00", 5100), ("09:05", 5060), ("09:10", 5030), ("09:30", 5020)])))
        self.assertEqual(modes(ex, "p1"), {"swB"})                 # +0.4%, 6일째

    def test_small_gainer_below_vwap_is_left_alone(self):
        ex = make([("000020", 5000, 1000)])
        run(ex, merge(path("000010", BUY), path("000020", [("09:00", 5200), ("09:05", 5160), ("09:10", 5130), ("09:30", 5120)])))
        self.assertEqual(sold(ex, "p1"), 0)                        # +2.4%, VWAP 아래, 보유 2일 → 등급 없음

    def test_sharp_fall_on_small_gain_goes_to_a(self):
        ex = make([("000020", 5000, 1000)])
        run(ex, merge(path("000010", BUY), path("000020", [("09:00", 5500), ("09:05", 5400), ("09:10", 5200), ("09:30", 5150)])))
        self.assertEqual(modes(ex, "p1"), {"swA"})                 # 진입가 대비 +3%지만 시가 −5% 넘게 하락


class TestWinnerProtection(unittest.TestCase):
    UP = [("09:00", 6000), ("09:05", 6010), ("09:10", 6020), ("09:30", 6030)]

    def test_winner_kept_for_overweight_order(self):
        ex = make([("000020", 5000, 1000, {"weight": 0.05})], rating="Overweight")
        run(ex, merge(path("000010", BUY), path("000020", self.UP)))
        self.assertEqual(sold(ex, "p1"), 0)

    def test_buy_order_trims_only_excess_over_pm_weight(self):
        ex = make([("000020", 5000, 1000, {"weight": 0.95})], rating="Buy")
        ex.equity = 6_000_000.0                                    # 목표 570만, 평가 약 601만 → 초과분만
        run(ex, merge(path("000010", BUY), path("000020", self.UP)))
        total = sold(ex, "p1")
        self.assertGreater(total, 0)
        self.assertEqual(modes(ex, "p1"), {"swD"})
        self.assertLessEqual((1000 - total) * 6030, 5_750_000)
        self.assertGreaterEqual(1000 - total, 945)                 # 대부분 유지

    def test_no_excess_no_sale(self):
        ex = make([("000020", 5000, 1000, {"weight": 0.5})], rating="Buy")
        ex.equity = 100_000_000.0                                  # 평가 601만 = 6% < 목표 50%
        run(ex, merge(path("000010", BUY), path("000020", self.UP)))
        self.assertEqual(sold(ex, "p1"), 0)
        self.assertGreater(ex.doc["switch"].get("unmet"), 0)

    def test_target_hit_is_protected_below_winner_threshold(self):
        ex = make([("000020", 5000, 1000, {"targetHit": {"date": "2026-10-15"}, "weight": 0.5})], rating="Overweight")
        ex.equity = 100_000_000.0
        run(ex, merge(path("000010", BUY), path("000020", [("09:00", 5100), ("09:05", 5150), ("09:10", 5200), ("09:30", 5210)])))
        self.assertEqual(sold(ex, "p1"), 0)

    def test_legacy_position_reads_pm_weight_from_decision(self):
        st = store.FileStore(tempfile.mkdtemp())
        st.put("decision/2026-10-15/000020/entry", {"pm_weight": 0.03, "weight": 0.05})
        ex = make([("000020", 5000, 1000, {"decisionKey": "decision/2026-10-15/000020/entry"})], st=st)
        self.assertEqual(ex._pm_weight(ex.pool["p1"]), 0.03)

    def test_unknown_weight_means_no_excess(self):
        ex = make([("000020", 5000, 1000)])
        self.assertEqual(ex._pm_weight(ex.pool["p1"]), 1.0)


class TestPositionKeepsWeight(unittest.TestCase):
    def test_fill_copies_weight_and_rating(self):
        L = book(10_000_000)
        L["orders"][0].update(rating="Buy", weight=0.07)
        E.settle_day(L, D1, {"000010": b(10000, 10100, 9900, 10000)})
        p = L["positions"][0]
        self.assertEqual((p["weight"], p["rating"]), (0.07, "Buy"))


if __name__ == "__main__":
    unittest.main()
