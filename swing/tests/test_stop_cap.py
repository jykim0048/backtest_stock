"""손절 폭 상한(2026-11-03) — min(ATR × 2, 진입가 × 10%), 넓은 손절가만 조인다. 오프라인."""
import tempfile
import unittest

from swing import agent_iface as A
from swing import chart, config, daily, engine as E, prices, store
from swing.tests.test_daily import FixedAgent, b, wb


def doc(tr, n=20, close=10000.0):
    """매일 고저 폭이 tr 인 평평한 차트."""
    return {"h": [close + tr / 2] * n, "l": [close - tr / 2] * n, "c": [close] * n}


class TestAtr(unittest.TestCase):
    def test_flat_range_gives_range(self):
        self.assertAlmostEqual(chart.last_atr(doc(300)), 300)

    def test_gap_counts_in_true_range(self):
        d = doc(100, 30)
        d["c"][-2] = 11000.0                                       # 전일 종가에서 마지막 날 고가까지의 간격이 범위보다 크다
        self.assertGreater(chart.last_atr(d), 100)

    def test_short_or_missing_history_is_none(self):
        self.assertIsNone(chart.last_atr(doc(100, 5)))
        self.assertIsNone(chart.last_atr(None))


class TestCappedStop(unittest.TestCase):
    def test_long_tightened_by_atr(self):
        stop, cap = A.capped_stop(10000, 8500, "long", 200)
        self.assertEqual(stop, 9600)
        self.assertEqual((cap["pm"], cap["basis"]), (8500, "atr"))

    def test_long_ten_percent_ceiling_when_atr_is_wide(self):
        stop, cap = A.capped_stop(10000, 8000, "long", 800)
        self.assertEqual((stop, cap["basis"]), (9000, "pct"))

    def test_short_mirrors_long(self):
        self.assertEqual(A.capped_stop(10000, 11500, "short", 200)[0], 10400)
        self.assertEqual(A.capped_stop(10000, 12000, "short", 800)[0], 11000)

    def test_tighter_stop_left_alone(self):
        self.assertEqual(A.capped_stop(10000, 9800, "long", 200), (9800, None))
        self.assertEqual(A.capped_stop(10000, 10200, "short", 200), (10200, None))

    def test_no_atr_uses_percent_only(self):
        self.assertEqual(A.capped_stop(10000, 8500, "long", None)[0], 9000)

    def test_stop_stays_on_loss_side_of_entry(self):
        self.assertLess(A.capped_stop(100, 50, "long", 0.1)[0], 100)
        self.assertGreater(A.capped_stop(100, 150, "short", 0.1)[0], 100)

    def test_off(self):
        old = config.STOP_CAP_ATR, config.STOP_CAP_PCT
        config.STOP_CAP_ATR, config.STOP_CAP_PCT = 0, 0
        try:
            self.assertEqual(A.capped_stop(10000, 5000, "long", 200), (5000, None))
        finally:
            config.STOP_CAP_ATR, config.STOP_CAP_PCT = old


class ChartAgent(FixedAgent):
    """판단에 차트(ATR 재료)를 붙이는 에이전트."""

    def __init__(self, entry=None, review="Hold", tr=200, new_stop=None):
        super().__init__(entry, review)
        self.tr, self.new_stop = tr, new_stop

    def decide(self, req):
        d = super().decide(req)
        d.chart = doc(self.tr)
        if req.purpose == "review":
            d.stop = self.new_stop
        return d


class TestDaily(unittest.TestCase):
    D = "2026-09-21"

    def _day(self, st, ag, date, px, buy=()):
        return daily.run_day(date, st, ag, px, wb(date, buy=buy), log=lambda *a: None)

    def test_order_uses_capped_stop_and_notes_it(self):
        st = store.FileStore(tempfile.mkdtemp())
        ag = ChartAgent({"000010": ("Buy", "Buy", 10000, 8000, 11000)})
        r = self._day(st, ag, self.D, prices.DictPrices({self.D: {"000010": b(10000, 10100, 9900, 10050)}}), [("000010", "화학")])
        o = st.get("ledger")["orders"][0]
        self.assertEqual(o["stop"], 9600)                          # ATR 200 × 2
        self.assertEqual(o["stopCap"]["pm"], 8000)
        self.assertEqual(r["candidates"][0]["stop"], 9600)
        self.assertTrue(any("손절 폭 상한 적용 1종목" in n for n in r["notes"]))

    def test_narrow_stop_keeps_no_cap_record(self):
        st = store.FileStore(tempfile.mkdtemp())
        ag = ChartAgent({"000010": ("Buy", "Buy", 10000, 9800, 11000)})
        self._day(st, ag, self.D, prices.DictPrices({self.D: {"000010": b(10000, 10100, 9900, 10050)}}), [("000010", "화학")])
        o = st.get("ledger")["orders"][0]
        self.assertEqual(o["stop"], 9800)
        self.assertNotIn("stopCap", o)

    def test_review_cannot_widen_past_cap(self):
        st = store.FileStore(tempfile.mkdtemp())
        L = E.new_ledger(10_000_000, start="2026-09-18")
        L["asof"] = "2026-09-18"
        L["positions"].append({"id": "p1", "code": "000010", "name": "A", "side": "long", "qty": 10, "entry": 10000.0, "stop": 9700.0,
                               "target": 10100.0, "fillDate": "2026-09-14", "holdDay": 5, "lastClose": 10050.0, "sellPending": None,
                               "reviewFlag": "expiry"})
        st.put("ledger", L)
        ag = ChartAgent(review="Hold", new_stop=8000)
        self._day(st, ag, self.D, prices.DictPrices({self.D: {"000010": b(10050, 10060, 10000, 10050)}}))
        self.assertEqual(st.get("ledger")["positions"][0]["stop"], 9600)


if __name__ == "__main__":
    unittest.main()
