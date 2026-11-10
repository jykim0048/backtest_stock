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
        self.assertIn("10-29 목표 비중 도달", h["summary"])
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

    def test_unfilled_topups_grouped_into_one_wait_row(self):
        st = setup()
        L = st.get("ledger")
        for i, d in enumerate(("2026-10-30", "2026-11-02")):
            L["orderLog"].append(order(f"w{i}", "2026-10-29", d, 300, None, status="cancelled", topup=True))
        st.put("ledger", L)
        h = history.build(st, KEY, log=lambda *a: None)
        w = [e for e in h["events"] if e.get("kind") == "wait"]
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0]["date"], w[0]["text"]), ("2026-10-30", "2일 미체결 · 10-30~11-02"))

    def test_switch_exit_label_has_tier(self):
        st = setup(position=False)
        L = st.get("ledger")
        L["trades"][-1]["reason"] = "switch"
        st.put("ledger", L)
        st.put("intraday/2026-11-02", {"exits": {"s:p1": {"code": "006110", "side": "long", "fills": [{"ts": "2026-11-02T10:00:00+09:00", "qty": 49, "price": 48651, "mode": "swA"}]}}, "orders": {}})
        h = history.build(st, KEY, log=lambda *a: None)
        self.assertIn("스위칭 A", [e["label"] for e in h["events"]])

    def test_live_price_before_settlement(self):
        st = setup()
        st.put("intraday/2026-11-03", {"vwap": {"006110": {"price": 50000.0, "ts": "2026-11-03T10:15:30+09:00"}}, "orders": {}, "exits": {}})
        h = history.build(st, KEY, log=lambda *a: None)
        self.assertEqual((h["last"], h["lastTs"]), (50000.0, "10:15"))
        self.assertAlmostEqual(h["vsAvgPct"], (50000 / 65563 - 1) * 100, places=1)

    def test_settled_day_uses_ledger_close(self):
        st = setup()
        st.put("intraday/2026-11-02", {"vwap": {"006110": {"price": 50000.0, "ts": "2026-11-02T15:20:00+09:00"}}, "orders": {}, "exits": {}})
        h = history.build(st, KEY, log=lambda *a: None)              # 원장 asof 11-02 = 이미 정산된 날
        self.assertEqual((h["last"], h["lastTs"]), (55600.0, None))

    def test_positions_api_marks_todays_intraday_exit(self):
        from swing import server
        st = setup()
        st.put("intraday/2026-11-03", {"vwap": {}, "orders": {}, "exits": {"p1": {"code": "006110", "side": "long", "kind": "stop", "trigger": "stop",
                                                                                  "fills": [{"ts": "2026-11-03T09:03:15+09:00", "qty": 49, "price": 48350, "mode": "stop"}]}}})
        old, server.STORE = server.STORE, st
        try:
            p = server.api_positions({})[0]
        finally:
            server.STORE = old
        self.assertEqual(p["liveExit"], {"kind": "stop", "qty": 49, "done": True, "at": "09:03"})

    def test_retry_order_live_fill_extends_span(self):
        """첫 주문 미체결 → 다음 날 재주문이 장중 체결(원장 포지션 없음) — 그래프가 오늘까지 이어져 매수가 붙는다(2026-11-06 녹십자)."""
        st = store.FileStore(tempfile.mkdtemp())
        o1 = {**order("o1", "2026-10-27", "2026-10-28", 50, None, status="cancelled"), "note": "미도달"}
        o2 = {**order("o2", "2026-10-28", "2026-10-29", 50, None, status="open")}
        st.put("ledger", {"cash": 0.0, "asof": "2026-10-28", "positions": [], "orders": [o2], "orderLog": [o1], "trades": [],
                          "equity": [{"date": "2026-10-28", "equity": 480_000_000}]})
        st.put(KEY, {"rating": "Overweight"})
        st.put("intraday/2026-10-29", {"orders": {"o2": {"code": "006110", "fills": [{"ts": "2026-10-29T09:00:30+09:00", "qty": 5, "price": 62100, "mode": "open"}]}},
                                       "exits": {}, "vwap": {}})
        h = history.build(st, KEY, log=lambda *a: None)
        self.assertIn("2026-10-29", [x["d"] for x in h["days"]])
        buy = [e for e in h["events"] if e["label"] == "매수"][0]
        self.assertEqual((buy["date"], buy["qty"], buy["held"]), ("2026-10-29", 5, 5))
        self.assertGreater(buy["weight"], 0)

    def test_positions_api_hold_day_counts_live_day(self):
        """보유일(체결일=1)은 정산 때 +1 — 장중(정산 전)엔 오늘을 더해 보여 준다(2026-11-05 11/4 진입이 1칸)."""
        from swing import server
        st = setup()
        old, server.STORE = server.STORE, st
        old_today, asof = server._today_kst, st.get("ledger")["asof"]
        try:
            server._today_kst = lambda: asof
            before = server.api_positions({})[0]["holdDay"]               # 정산 당일 저녁 = 원장 그대로
            server._today_kst = lambda: "2026-11-03"
            premarket = server.api_positions({})[0]["holdDay"]            # 장 시작 전(장중 기록 없음)도 오늘 포함(2026-11-06)
            st.put("intraday/2026-11-03", {"vwap": {}, "orders": {}, "exits": {}})
            during = server.api_positions({})[0]["holdDay"]
        finally:
            server.STORE, server._today_kst = old, old_today
        self.assertEqual((before, premarket, during), (4, 5, 5))

    def test_positions_api_uses_live_price(self):
        from swing import server
        st = setup()
        st.put("intraday/2026-11-03", {"vwap": {"006110": {"price": 50000.0, "ts": "2026-11-03T10:15:30+09:00"}}, "orders": {}, "exits": {}})
        old, server.STORE = server.STORE, st
        try:
            p = server.api_positions({})[0]
        finally:
            server.STORE = old
        self.assertEqual((p["price"], p["priceTs"], p["value"]), (50000.0, "10:15:30", 49 * 50000))
        self.assertAlmostEqual(p["unrealPct"], (50000 / 65563 - 1) * 100, places=1)

    def test_decision_api_attaches_latest_review_of_same_round(self):
        from swing import server
        st = setup()
        st.put(KEY, {"rating": "Overweight", "summary": "진입 요약"})
        st.put("decision/2026-10-29/006110/review", {"rating": "Hold", "summary": "10-29 재판별"})
        st.put("decision/2026-10-31/006110/review", {"rating": "Hold", "summary": "10-31 재판별"})
        st.put("decision/2026-10-20/006110/review", {"rating": "Sell", "summary": "이전 회차"})       # 진입 전 — 제외
        st.put("decision/2026-10-30/006110/review-short", {"rating": "Hold", "summary": "Short"})     # 방향 다름 — 제외
        old, server.STORE = server.STORE, st
        try:
            d = server.api_decision({"key": KEY})
            r = server.api_decision({"key": "decision/2026-10-29/006110/review"})
        finally:
            server.STORE = old
        self.assertEqual((d["summary"], d["latestReview"]["summary"], d["latestReview"]["count"]), ("진입 요약", "10-31 재판별", 2))
        self.assertNotIn("latestReview", r)

    def test_latest_review_uses_applied_stop_target(self):
        from swing import server
        st = setup()
        st.put("decision/2026-10-29/006110/review", {"rating": "Hold", "summary": "s", "action": "Hold", "stop": 50000, "target": 90000})
        old, server.STORE = server.STORE, st
        try:
            lr = server.api_decision({"key": KEY})["latestReview"]
        finally:
            server.STORE = old
        self.assertEqual((lr["stop"], lr["target"], lr["trader"], lr["reviewAction"]), (57000, 71000, "Hold", "hold_updated"))   # run 기록(적용값) 우선

    def test_live_api_prefers_fresher_bus_price(self):
        from swing import server
        st = setup()
        st.put("intraday/2026-11-03", {"vwap": {"006110": {"price": 50000.0, "ts": "2026-11-03T10:15:00+09:00"}}, "orders": {}, "exits": {}})
        old, server.STORE = server.STORE, st
        old_bus, history.live_bus = history.live_bus, lambda: {"006110": {"price": 50500.0, "ts": "2026-11-03T10:15:45+09:00", "prev_close": 50000.0},
                                                               "000000": {"price": 1.0, "ts": "2026-11-02T15:00:00+09:00"}}
        try:
            r = server.api_live({})
        finally:
            server.STORE, history.live_bus = old, old_bus
        p = r["positions"][0]
        self.assertEqual((r["live"], r["source"], r["liveTs"], p["price"], p["value"]), (True, "bus", "10:15:45", 50500.0, 49 * 50500))
        self.assertEqual(p["chgPct"], 1.0)                                   # KIS 전일 종가 대비

    def test_live_api_off_after_settlement(self):
        from swing import server
        old, server.STORE = server.STORE, setup()
        try:
            self.assertEqual(server.api_live({}), {"live": False})
        finally:
            server.STORE = old

    def test_positions_api_adds_todays_new_entry_as_live_row(self):
        from swing import server
        st = setup()
        L = st.get("ledger")
        L["orders"].append({"id": "o9", "code": "000660", "name": "SK하이닉스", "side": "long", "type": "entry", "entry": 200000.0, "stop": 190000.0,
                            "target": 220000.0, "qty": 50, "status": "open", "decisionKey": "decision/2026-11-02/000660/entry", "rating": "Buy", "weight": 0.02})
        st.put("ledger", L)
        st.put("intraday/2026-11-03", {"date": "2026-11-03", "exits": {}, "vwap": {"000660": {"price": 202000.0, "ts": "2026-11-03T10:20:00+09:00"}},
                                       "orders": {"o9": {"code": "000660", "side": "long", "planned": 50, "done": False,
                                                         "fills": [{"ts": "2026-11-03T09:00:15+09:00", "qty": 5, "price": 199000}, {"ts": "2026-11-03T09:05:15+09:00", "qty": 5, "price": 201000}]}}})
        old, server.STORE = server.STORE, st
        try:
            ps = server.api_positions({})
            lv = server.api_live({})
        finally:
            server.STORE = old
        x = next(p for p in ps if p["id"] == "live:o9")
        self.assertEqual((x["qty"], x["entry"], x["price"], x["liveEntry"]["planned"], x["decisionKey"]), (10, 200000.0, 202000.0, 50, "decision/2026-11-02/000660/entry"))
        self.assertEqual(lv["equity"] - (L["cash"] + 49 * 55600), 10 * 2000)      # 평가액엔 체결가 대비 손익만

    def test_todays_new_entry_card_has_average_price(self):
        st = setup()
        L = st.get("ledger")
        L["orders"].append({"id": "o9", "code": "069620", "name": "대웅제약", "side": "short", "type": "entry", "created": "2026-11-02", "validDate": "2026-11-03",
                            "entry": 102600.0, "stop": 108000.0, "qty": 50, "status": "open", "decisionKey": "decision/2026-11-02/069620/entry-short", "weight": 0.05})
        st.put("ledger", L)
        st.put("intraday/2026-11-03", {"date": "2026-11-03", "exits": {}, "vwap": {"069620": {"price": 100000.0, "ts": "2026-11-03T10:00:00+09:00"}},
                                       "orders": {"o9": {"code": "069620", "side": "short", "fills": [{"ts": "2026-11-03T09:00:15+09:00", "qty": 10, "price": 103000, "mode": "open"},
                                                                                                  {"ts": "2026-11-03T09:05:15+09:00", "qty": 30, "price": 102000, "mode": "vwap"}]}}})
        h = history.build(st, "decision/2026-11-02/069620/entry-short", log=lambda *a: None)
        self.assertEqual((h["status"], h["qty"], h["avg"]), ("open", 40, 102250.0))
        self.assertAlmostEqual(h["vsAvgPct"], (1 - 100000 / 102250) * 100, places=1)       # Short 은 하락이 이익

    def test_same_day_entry_then_stop_is_flat(self):
        from swing import server
        st = setup()
        L = st.get("ledger")
        L["orders"].append({"id": "o9", "code": "000150", "name": "두산", "side": "short", "type": "entry", "created": "2026-11-02", "validDate": "2026-11-03",
                            "entry": 850000.0, "stop": 900000.0, "qty": 16, "status": "open", "decisionKey": "decision/2026-11-02/000150/entry-short", "weight": 0.03})
        st.put("ledger", L)
        st.put("intraday/2026-11-03", {"date": "2026-11-03", "vwap": {"000150": {"price": 841000.0, "ts": "2026-11-03T11:00:00+09:00"}},
                                       "orders": {"o9": {"code": "000150", "side": "short", "planned": 16, "done": True, "fills": [
                                           {"ts": "2026-11-03T09:00:15+09:00", "qty": 2, "price": 872000, "mode": "open"}, {"ts": "2026-11-03T09:05:00+09:00", "qty": 1, "price": 894000, "mode": "vwap"}]}},
                                       "exits": {"o:o9": {"code": "000150", "side": "short", "kind": "stop", "trigger": "stop", "fills": [
                                           {"ts": "2026-11-03T09:07:00+09:00", "qty": 1, "price": 900000, "mode": "stop"}, {"ts": "2026-11-03T09:22:30+09:00", "qty": 2, "price": 890500, "mode": "vwap"}]}}})
        old, server.STORE = server.STORE, st
        try:
            x = next(p for p in server.api_positions({}) if p["id"] == "live:o9")
        finally:
            server.STORE = old
        self.assertEqual((x["qty"], x["value"], x["liveExit"]["done"], x["liveExit"]["at"]), (0, 0, True, "09:22"))
        self.assertEqual(x["liveRealized"], round((879333.33 - 893666.67) * 3))       # Short — 오른 만큼 손실
        h = history.build(st, "decision/2026-11-02/000150/entry-short", log=lambda *a: None)
        self.assertEqual((h["status"], h["qty"]), ("closed", 0))
        self.assertLess(h["realized"], 0)

    def test_summary_equity_uses_live_price(self):
        from swing import server
        st = setup()
        L = st.get("ledger")
        L.update(capital0=500_000_000, cash=100_000_000, equity=[dict(e, retPct=-4.0) for e in L["equity"]])
        st.put("ledger", L)
        st.put("intraday/2026-11-03", {"vwap": {"006110": {"price": 50000.0, "ts": "2026-11-03T10:15:30+09:00"}}, "orders": {}, "exits": {}})
        old, server.STORE = server.STORE, st
        try:
            s = server.api_summary({})
        finally:
            server.STORE = old
        self.assertEqual((s["equity"], s["liveTs"], s["closeEquity"]), (100_000_000 + 49 * 50000, "10:15:30", 480_000_000))
        self.assertAlmostEqual(s["retPct"], ((100_000_000 + 49 * 50000) / 500_000_000 - 1) * 100, places=2)

    def _topup_today(self, exit_fills=None):
        """오늘(11-03) 비중 채우기 주문 o7(topupOf p1)이 장중 30주 체결 — 정산 전(2026-11-04)."""
        st = setup()
        L = st.get("ledger")
        L.update(capital0=500_000_000, cash=100_000_000)
        L["orders"].append(dict(order("o7", "2026-11-02", "2026-11-03", 60, None, status="open", topup=True)))
        st.put("ledger", L)
        doc = {"date": "2026-11-03", "vwap": {"006110": {"price": 50000.0, "ts": "2026-11-03T10:15:30+09:00"}},
               "orders": {"o7": {"code": "006110", "side": "long", "topupOf": "p1", "planned": 60, "fills": [
                   {"ts": "2026-11-03T09:00:15+09:00", "qty": 10, "price": 49000, "mode": "open"},
                   {"ts": "2026-11-03T09:30:00+09:00", "qty": 20, "price": 49600, "mode": "vwap"}]}},
               "exits": {}}
        if exit_fills:
            doc["exits"]["p1"] = {"code": "006110", "side": "long", "kind": "pm_sell", "fills": exit_fills}
        st.put("intraday/2026-11-03", doc)
        return st

    def test_positions_api_merges_todays_topup_fills(self):
        from swing import server
        st = self._topup_today()
        old, server.STORE = server.STORE, st
        try:
            p = server.api_positions({})[0]
            lv = server.api_live({})
        finally:
            server.STORE = old
        avg = (49 * 65563 + 10 * 49000 + 20 * 49600) / 79
        self.assertEqual((p["qty"], p["qtyLedger"], p["value"]), (79, 49, 79 * 50000))
        self.assertAlmostEqual(p["entry"], avg, places=2)
        self.assertEqual(p["liveAdd"], {"qty": 30, "price": 49400, "at": "09:30"})
        self.assertAlmostEqual(p["unrealPct"], (50000 / avg - 1) * 100, places=2)
        # 평가액 = 현금 + 원장 49주 × 현재가 + 오늘 추가분의 (현재가 − 체결가)
        self.assertEqual(lv["equity"], round(100_000_000 + 49 * 50000 + 10 * 1000 + 20 * 400))
        lp = lv["positions"][0]
        self.assertEqual((lp["qty"], lp["qtyLedger"], lp["liveAdd"]["qty"]), (79, 49, 30))

    def test_positions_api_subtracts_todays_partial_exit(self):
        from swing import server
        st = self._topup_today(exit_fills=[{"ts": "2026-11-03T11:00:00+09:00", "qty": 9, "price": 51000, "mode": "vwap"}])
        old, server.STORE = server.STORE, st
        try:
            p = server.api_positions({})[0]
            lv = server.api_live({})
        finally:
            server.STORE = old
        self.assertEqual((p["qty"], p["liveExit"]["qty"], p["liveExit"]["done"]), (70, 9, False))
        self.assertEqual(lv["equity"], round(100_000_000 + 49 * 50000 + 10 * 1000 + 20 * 400 + 9 * 1000))

    def test_full_exit_shows_realized_not_unrealized(self):
        """한화솔루션(2026-11-04) — 장중 전량 청산이 이익인데 현재가 기준 평가손익(−)이 보이던 문제."""
        from swing import config, server
        st = setup()
        st.put("intraday/2026-11-03", {"vwap": {"006110": {"price": 50000.0, "ts": "2026-11-03T15:10:00+09:00"}}, "orders": {},
                                       "exits": {"p1": {"code": "006110", "side": "long", "kind": "stop",
                                                        "fills": [{"ts": "2026-11-03T10:52:00+09:00", "qty": 49, "price": 70000, "mode": "force"}]}}})
        old, server.STORE = server.STORE, st
        try:
            p = server.api_positions({})[0]
            lp = server.api_live({})["positions"][0]
        finally:
            server.STORE = old
        want = 49 * (70000 - 65563) - 70000 * 49 * config.SELL_TAX
        self.assertEqual((p["qty"], p["unrealPct"]), (0, None))
        self.assertEqual(p["liveRealized"], round(want))
        self.assertAlmostEqual(p["liveRealizedPct"], want / (49 * 65563) * 100, places=2)
        self.assertEqual((lp["liveRealized"], lp["unrealPct"]), (round(want), None))

    def test_queue_follows_entry_priority_and_budget(self):
        """진입 우선순위 패널(2026-11-04) — 실행기와 같은 순서(PM 등급 → 높은 비중), 누적 필요 금액 vs 가용 현금."""
        from swing import server
        st = setup()
        L = st.get("ledger")
        L["cash"] = 3_000_000.0
        base = {"type": "entry", "side": "long", "status": "open", "created": "2026-11-02", "validDate": "2026-11-03", "stop": 1.0}
        L["orders"] += [dict(base, id="a", code="000001", name="A", rating="Overweight", weight=0.05, qty=10, entry=100000.0),
                        dict(base, id="b", code="000002", name="B", rating="Buy", weight=0.02, qty=10, entry=100000.0),
                        dict(base, id="c", code="000003", name="C", rating="Overweight", weight=0.07, qty=20, entry=100000.0),
                        dict(base, id="z", code="000004", name="Z", rating="Buy", weight=0.09, qty=1, entry=1.0, validDate="2026-11-04")]
        st.put("ledger", L)
        old, server.STORE = server.STORE, st
        try:
            q = server.api_queue({})
        finally:
            server.STORE = old
        self.assertEqual((q["session"], q["live"]), ("2026-11-03", False))          # 가장 이른 세션만
        it = q["long"]["items"]
        self.assertEqual([r["id"] for r in it], ["b", "c", "a"])                     # Buy 먼저, 같은 Overweight 는 7% > 5%
        self.assertEqual([r["cum"] for r in it], [1_000_000, 3_000_000, 4_000_000])
        self.assertEqual([r["fits"] for r in it], [True, True, False])
        self.assertEqual(q["short"]["items"], [])

    def test_queue_wait_reason(self):
        from swing import server
        o = {"qty": 100, "entry": 10000.0, "stop": 9000.0}
        w = lambda lv, side="long", filled=0, fits=True, rec=None, done=False: server._wait_reason(o, rec or {}, lv, side, filled, fits, done)   # noqa: E731
        self.assertEqual(w({"price": 10300, "vwap": 10200})[0], "리밋 이탈 +3.0%")
        self.assertEqual(server._wait_reason({"qty": 100, "entry": 10000.0, "stop": 11000.0}, {}, {"price": 9700, "vwap": 9800},
                                             "short", 0, False, False)[0], "리밋 이탈 -3.0%")   # Short 는 담보로 막지 않음
        self.assertEqual(w({"price": 9900, "vwap": 9800})[0], "VWAP 위")
        # 2026-11-10: 신규 진입 주문은 진입 첫날 손절가 너머에서도 VWAP 분할을 계속 → '손절가 밖' 으로 막지 않는다(비중 채우기는 종전대로)
        self.assertEqual(w({"price": 8900, "vwap": 9800})[0], "분할 간격")
        self.assertEqual(server._wait_reason({**o, "topupOf": "p1"}, {}, {"price": 8900, "vwap": 9800}, "long", 0, True, False)[0], "손절가 밖")
        self.assertEqual(w({"price": 9700, "vwap": 9800}, fits=False)[0], "현금 대기")
        self.assertEqual(w({"price": 9700, "vwap": 9800}, filled=30)[0], "분할 간격")
        self.assertEqual(w({}, done=False)[0], "시세 대기")
        self.assertEqual(w({"price": 9700}, done=True)[0], "분할 미완")         # 마감 — 조건은 맞았음
        self.assertEqual(w({"price": 10300}, done=True, rec={"done": True, "why": "3/100주 체결"})[0], "리밋 이탈 +3.0%")   # 마감 시 리밋 밖
        self.assertEqual(w({"price": 9700}, rec={"done": True, "why": "본 포지션 손절 — 남은 분할 취소"})[0], "손절로 중단")
        self.assertIsNone(w({"price": 9700}, filled=100))
        self.assertIsNone(w({"price": 9700}, rec={"done": True}))

    def test_history_avg_includes_todays_topup(self):
        h = history.build(self._topup_today(), KEY, log=lambda *a: None)
        avg = (49 * 65563 + 10 * 49000 + 20 * 49600) / 79
        self.assertEqual(h["qty"], 79)
        self.assertAlmostEqual(h["avg"], avg, places=2)
        self.assertAlmostEqual(h["vsAvgPct"], (50000 / avg - 1) * 100, places=1)
        self.assertAlmostEqual(h["nowWeight"], 79 * 50000 / 480_000_000 * 100, places=1)   # 오늘 비중 = 장중 현재가

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


def ticks(day, prices, start="09:00:15", gap=15, value=True):
    """15초 간격 틱 — 누적 거래량 100주씩, 거래대금은 가격 × 거래량 누적."""
    h, m, s = map(int, start.split(":"))
    t0, out, vol, val = h * 3600 + m * 60 + s, [], 0, 0.0
    for i, px in enumerate(prices):
        sec = t0 + i * gap
        vol += 100
        val += px * 100
        out.append({"code": "006110", "ts": f"{day}T{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}+09:00", "price": px, "volume": vol,
                    "value": val if value else None})
    return out


class TestMinuteBars(unittest.TestCase):
    def test_step_by_holding_days(self):
        self.assertEqual([history.step_for(n) for n in (1, 2, 3, 4, 5, 9, 10, 17, 18)], [5, 10, 15, 15, 30, 30, 60, 60, None])

    def test_buckets_and_official_close(self):
        bars = history.minute_bars(ticks("2026-10-29", [100, 103, 99, 101] * 20, start="09:00:00"), 5, {"o": 98, "h": 110, "l": 90, "c": 97})
        self.assertEqual(bars[0]["t"], "09:00")
        self.assertEqual(bars[0]["o"], 98)                  # 시가 = KRX 확정
        self.assertEqual(bars[-1]["c"], 97)                 # 종가 동시호가 = KRX 확정
        self.assertEqual(bars[-1]["l"], 97)
        self.assertEqual((bars[1]["h"], bars[1]["l"]), (103, 99))
        self.assertEqual(len(bars), 4)                      # 80틱 × 15초 = 20분 → 5분봉 4개
        self.assertAlmostEqual(bars[0]["vw"], 100.75, places=1)

    def test_ticks_outside_session_ignored(self):
        bars = history.minute_bars(ticks("2026-10-29", [100, 101], start="08:59:45") + ticks("2026-10-29", [105], start="15:35:00"), 30)
        self.assertEqual([b["t"] for b in bars], ["09:00"])


class TestIntradayCard(unittest.TestCase):
    def setUp(self):
        self.old = history.bars_fn
        history.bars_fn = bars
        history._bars_cache.clear()

    def tearDown(self):
        history.bars_fn = self.old

    def put_ticks(self, st, days):
        for d in days:
            st.put(f"intraday/{d}/ticks/006110", ticks(d, [60000 + i for i in range(1500)]))

    def test_intraday_bars_when_every_day_has_ticks(self):
        st = setup(position=False)
        self.put_ticks(st, ("2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"))
        h = history.build(st, KEY, log=lambda *a: None)
        self.assertEqual(h["step"], 15)                     # 4일 → 15분봉
        self.assertEqual({b["d"] for b in h["ibars"]}, {"2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"})
        self.assertEqual(h["ibars"][-1]["c"], 55600.0)      # 마지막 날 종가 = 일봉
        self.assertEqual(len(h["tickDays"]), 4)

    def test_missing_tick_day_keeps_daily(self):
        st = setup(position=False)
        self.put_ticks(st, ("2026-10-29", "2026-10-30", "2026-11-02"))
        h = history.build(st, KEY, log=lambda *a: None)
        self.assertIsNone(h["step"])
        self.assertEqual(h["ibars"], [])
        self.assertEqual(h["tickDays"], ["2026-10-29", "2026-10-30", "2026-11-02"])

    def test_fills_signed_and_day_view(self):
        st = setup()
        self.put_ticks(st, ("2026-10-29",))
        h = history.build(st, KEY, day="2026-10-29", log=lambda *a: None)
        self.assertEqual([(f["q"], f["mode"]) for f in h["fills"]], [(21, "open"), (184, "force")])
        dv = h["dayView"]
        self.assertEqual((dv["step"], dv["bars"][0]["t"], len(dv["fills"])), (5, "09:00", 2))
        self.assertIn("vw", dv["bars"][0])

    def test_exit_fills_are_negative(self):
        st = setup()
        st.put("intraday/2026-11-03", {"exits": {"p1": {"code": "006110", "side": "long", "kind": "stop", "why": "전량",
                                                         "fills": [{"ts": "2026-11-03T09:03:15+09:00", "qty": 49, "price": 48350, "mode": "force"}]}}, "orders": {}})
        h = history.build(st, KEY, log=lambda *a: None)
        self.assertEqual(h["fills"][-1]["q"], -49)


if __name__ == "__main__":
    unittest.main()
