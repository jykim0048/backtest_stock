"""트레이딩 히스토리(2026-11-03) — 흩어진 기록을 포지션 회차 하나로 모으기. 오프라인."""
import tempfile
import unittest

from swing import history, store

KEY = "decision/2026-10-27/006110/entry"


def order(oid, created, valid, qty, fill, *, status="filled", planned=None, topup=None):
    o = {"id": oid, "code": "006110", "name": "삼아알미늄", "side": "long", "type": "entry", "created": created, "validDate": valid, "entry": 62300.0,
         "stop": 56000.0, "target": 70000.0, "weight": 0.03, "qty": qty, "status": status, "decisionKey": KEY, "fillPrice": fill, "rating": "Overweight"}
    if planned:
        o["partial"] = {"planned": planned, "filled": qty, "remain": planned - qty}
    if topup:
        o.update(topupOf="p1", topup=1)
    return o


def trade(tid, date, qty, px, pnl, reason="stop", partial=True):
    t = {"id": tid, "code": "006110", "name": "삼아알미늄", "side": "long", "qty": qty, "entryDate": "2026-10-28", "entryPrice": 65563.0, "exitDate": date,
         "exitPrice": float(px), "reason": reason, "holdDays": 3, "pnl": pnl, "retPct": -12.0, "decisionKey": KEY, "note": "장중 분할"}
    if partial:
        t["partial"] = True
    return t


def bars(code, d1, d2):
    return {"2026-10-28": {"o": 65800.0, "h": 66000.0, "l": 62000.0, "c": 63000.0}, "2026-10-29": {"o": 62100.0, "h": 67000.0, "l": 62000.0, "c": 65000.0},
            "2026-10-30": {"o": 60000.0, "h": 60500.0, "l": 55000.0, "c": 57000.0}, "2026-11-02": {"o": 56000.0, "h": 57000.0, "l": 55000.0, "c": 55600.0}}


def setup(position=True):
    st = store.FileStore(tempfile.mkdtemp())
    L = {"cash": 0.0, "asof": "2026-11-02", "positions": [], "orders": [],
         "orderLog": [order("o1", "2026-10-27", "2026-10-28", 27, 65281.0, planned=232), order("o2", "2026-10-28", "2026-10-29", 205, 65600.0, topup=True)],
         "trades": [trade("t1", "2026-10-30", 115, 57558, -933825), trade("t2", "2026-11-02", 68, 57026, -588264)],
         "equity": [{"date": d, "equity": 480_000_000} for d in ("2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02")]}
    if position:
        L["positions"].append({"id": "p1", "code": "006110", "name": "삼아알미늄", "side": "long", "qty": 49, "entry": 65563.0, "stop": 56000.0, "fillDate": "2026-10-28",
                               "lastClose": 55600.0, "decisionKey": KEY, "holdDay": 4, "weight": 0.03})
    else:
        L["trades"].append(trade("t3", "2026-11-02", 49, 48651, -833000, partial=False))
    st.put("ledger", L)
    st.put(KEY, {"rating": "Overweight"})
    st.put("intraday/2026-10-29", {"orders": {"o2": {"fills": [{"ts": "2026-10-29T09:00:30+09:00", "qty": 21, "price": 62100, "mode": "open"},
                                                               {"ts": "2026-10-29T09:01:00+09:00", "qty": 184, "price": 66000, "mode": "force"}]}}, "exits": {}})
    st.put("run/2026-10-29", {"reviews": [{"code": "006110", "side": "long", "rating": "Hold", "action": "hold_updated", "stop": 57000, "target": 71000,
                                           "decisionKey": "decision/2026-10-29/006110/review"}]})
    return st


class TestBuild(unittest.TestCase):
    def setUp(self):
        self.old = history.bars_fn
        history.bars_fn = bars
        history._bars_cache.clear()

    def tearDown(self):
        history.bars_fn = self.old

    def test_open_position_rolls_up(self):
        h = history.build(setup(), KEY, log=lambda *a: None)
        self.assertEqual((h["status"], h["qty"], h["avg"], h["last"]), ("open", 49, 65563.0, 55600.0))
        self.assertEqual(h["targetWeight"], 3.0)
        self.assertEqual(h["peakDay"], "2026-10-29")
        self.assertAlmostEqual(h["peakWeight"], 232 * 65000 / 480_000_000 * 100, places=1)
        self.assertAlmostEqual(h["nowWeight"], 49 * 55600 / 480_000_000 * 100, places=1)
        self.assertEqual(h["realized"], -933825 - 588264)
        self.assertLess(h["vsAvgPct"], -15)
        self.assertTrue(h["hasBars"])
        self.assertIn("10-29 목표 도달", h["summary"])
        self.assertIn("스위칭 없음", h["summary"])

    def test_events_in_time_order_with_running_quantity(self):
        h = history.build(setup(), KEY, log=lambda *a: None)
        labels = [(e["date"][5:], e["label"]) for e in h["events"]]
        self.assertEqual(labels, [("10-27", "판단"), ("10-28", "매수"), ("10-29", "추가 매수"), ("10-29", "재판별"), ("10-30", "손절"), ("11-02", "손절")])
        self.assertEqual([e["held"] for e in h["events"]], [0, 27, 232, 232, 117, 49])

    def test_buy_above_cap_flagged_with_split_detail(self):
        h = history.build(setup(), KEY, log=lambda *a: None)
        e = next(x for x in h["events"] if x["label"] == "추가 매수")
        self.assertTrue(e["warn"])
        self.assertIn("5.3% 불리", e["sub"])
        self.assertIn("전량 1회 184주 @66,000", e["more"])
        self.assertEqual(h["deviations"], 2)

    def test_chart_covers_entry_to_exit_only(self):
        h = history.build(setup(position=False), KEY, log=lambda *a: None)
        self.assertEqual([d["d"] for d in h["days"]], ["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"])

    def test_review_key_finds_same_round(self):
        h = history.build(setup(), "decision/2026-10-29/006110/review", log=lambda *a: None)
        self.assertEqual((h["status"], h["qty"]), ("open", 49))

    def test_closed_round(self):
        h = history.build(setup(position=False), KEY, log=lambda *a: None)
        self.assertEqual((h["status"], h["nowWeight"], h["exitDate"]), ("closed", 0.0, "2026-11-02"))
        self.assertIn("전량 청산", h["summary"])

    def test_stop_filled_far_below_stop_is_a_deviation(self):
        h = history.build(setup(position=False), KEY, log=lambda *a: None)
        e = [x for x in h["events"] if x["label"] == "손절"][-1]
        self.assertTrue(e["warn"])

    def test_unknown_decision_has_no_round(self):
        self.assertEqual(history.build(setup(), "decision/2026-10-27/000000/entry")["status"], "none")
        self.assertEqual(history.build(setup(), "bad-key")["status"], "none")

    def test_unsettled_exit_today_appears(self):
        st = setup()
        st.put("intraday/2026-11-03", {"exits": {"p1": {"code": "006110", "side": "long", "kind": "stop", "why": "전량",
                                                         "fills": [{"ts": "2026-11-03T09:03:15+09:00", "qty": 49, "price": 48350, "mode": "force"}]}}, "orders": {}})
        h = history.build(st, KEY, log=lambda *a: None)
        self.assertEqual((h["events"][-1]["held"], h["events"][-1]["live"]), (0, True))
        self.assertEqual(h["qty"], 0)

    def test_bars_failure_falls_back_to_decision_chart(self):
        def boom(*a):
            raise RuntimeError("no token")
        history.bars_fn = boom
        st = setup()
        st.put(KEY, {"rating": "Overweight", "chart": {"dates": ["2026-10-28"], "o": [65800], "h": [66000], "l": [62000], "c": [63000]}})
        h = history.build(st, KEY, log=lambda *a: None)
        self.assertTrue(h["hasBars"])

    def test_no_bars_at_all_still_answers(self):
        def boom(*a):
            raise RuntimeError("no token")
        history.bars_fn = boom
        h = history.build(setup(), KEY, log=lambda *a: None)
        self.assertFalse(h["hasBars"])
        self.assertEqual(h["status"], "open")


if __name__ == "__main__":
    unittest.main()
