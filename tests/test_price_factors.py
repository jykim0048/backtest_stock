"""주가 영향 분해(추정) — analysis/price_factors.py 순수 함수 + generate_analysis 스키마 배선 (오프라인).

실행: python tests/test_price_factors.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analysis import price_factors as PF  # noqa: E402


def f(cat, title, direction, strength, evidence="근거"):
    return {"category": cat, "title": title, "direction": direction, "strength": str(strength), "evidence": evidence}


def total(rows):
    return round(sum(x["pp"] for x in rows), 1)


def test_clean_filters_and_caps():
    raw = [f("flow", "외국인 3일 연속 순매수", "up", 4), f("flow", "외국인 3일 연속 순매수", "up", 4),       # 중복
           f("macro", "금리", "down", 3),                                                                    # 모르는 항목
           f("dart", "", "up", 3), f("dart", "자기주식 취득 결정", "sideways", 3),                           # 제목 없음 · 방향 오류
           {"category": "news", "title": "지배구조 기사", "direction": "neutral", "strength": "9", "evidence": "x"},   # 강도 범위 밖 → 5
           {"category": "peers", "title": "미국 엔진주 하락", "direction": "하락", "strength": None, "evidence": ""},   # 한글 방향 · 강도 없음 → 2
           "쓰레기", None]
    got = PF.clean(raw)
    assert [(x["cat"], x["title"], x["dir"], x["strength"]) for x in got] == [
        ("flow", "외국인 3일 연속 순매수", 1, 4), ("news", "지배구조 기사", 0, 5), ("peers", "미국 엔진주 하락", -1, 2)], got
    assert PF.clean(None) == [] and PF.clean({"a": 1}) == []
    many = [f("flow", "요인%d" % i, "up", i % 5 + 1) for i in range(9)]
    kept = PF.clean(many)
    assert len(kept) == PF.MAX_PER_CAT and min(x["strength"] for x in kept) >= 3              # 강한 것부터 4개
    assert [x["title"] for x in kept] == sorted([x["title"] for x in kept], key=lambda t: int(t[2:]))   # 원래 순서 유지
    wide = [f(c, "%s%d" % (c, i), "up", 3) for c in PF.CATS for i in range(4)]
    assert len(PF.clean(wide)) == PF.MAX_TOTAL


KST = __import__("datetime").timezone(__import__("datetime").timedelta(hours=9))


def at(y, m, d, hh, mm):
    return __import__("datetime").datetime(y, m, d, hh, mm, tzinfo=KST)


def test_basis_falls_back_to_latest_row():
    night = at(2026, 11, 10, 20, 0)                                                   # 장 마감 뒤 — 표의 최신 행이 오늘
    flow = {"trend": [{"date": "20261110", "rate": 9.7, "close": 27150}, {"date": "20261109", "rate": 1.03, "close": 24600}]}
    assert PF.basis(flow, None, night) == {"date": "2026-11-10", "ratePct": 9.7, "kind": "today"}
    early = at(2026, 11, 11, 8, 40)                                                   # 장 시작 전 — 현재가가 있어도 당일 등락으로 보지 않는다
    assert PF.basis(flow, 27400, early) == {"date": "2026-11-10", "ratePct": 9.7, "kind": "last"}
    assert PF.basis(flow, None, at(2026, 11, 11, 10, 40)) == {"date": "2026-11-10", "ratePct": 9.7, "kind": "last"}   # 현재가 모름
    assert PF.basis({"trend": [{"date": "20261110", "rate": None}, {"date": "2026-11-09", "rate": -1.2}]}, None, early) == {"date": "2026-11-09", "ratePct": -1.2, "kind": "last"}
    assert PF.basis({"trend": []}) is None and PF.basis(None) is None and PF.basis({"trend": [{"date": "x", "rate": 1.0}]}) is None


def test_today_move_from_current_price():
    flow = {"trend": [{"date": "20261110", "rate": 9.7, "close": 27150}, {"date": "20261109", "rate": 1.03, "close": 24600}]}
    now = at(2026, 11, 11, 10, 40)
    tm = PF.today_move(flow, 26900, now)                                              # 26,900 ÷ 27,150 − 1 = −0.92%
    assert tm == {"date": "2026-11-11", "ratePct": -0.92, "price": 26900.0, "prevClose": 27150.0}, tm
    assert PF.basis(flow, 26900, now) == {"date": "2026-11-11", "ratePct": -0.92, "kind": "today"}
    assert PF.today_move(flow, 26900, at(2026, 11, 11, 9, 0)) is None                 # 09:05 전
    assert PF.today_move(flow, 26900, at(2026, 11, 14, 11, 0)) is None                # 토요일
    assert PF.today_move(flow, None, now) is None and PF.today_move({"trend": [{"date": "20261110", "rate": 9.7}]}, 26900, now) is None   # 현재가·종가 없음
    after = {"trend": [{"date": "20261111", "rate": -1.1, "close": 26850}, {"date": "20261110", "rate": 9.7, "close": 27150}]}
    assert PF.today_move(after, 99999, at(2026, 11, 11, 16, 0)) == {"date": "2026-11-11", "ratePct": -1.1, "price": 26850.0, "prevClose": 27150.0}   # 표에 오늘 행이 있으면 그 값


def test_attribute_sums_to_rate_and_keeps_signs():
    items = PF.clean([f("flow", "외국인 순매수", "up", 4), f("dart", "자사주 취득", "up", 4), f("research", "매수 유지", "up", 2),
                      f("dart", "영업이익 하회", "down", 4), f("peers", "미국 엔진주 하락", "down", 1), f("news", "지배구조 기사", "neutral", 1)])
    rows = PF.attribute(items, 9.7)
    assert total(rows) == 9.7
    assert all((x["pp"] > 0) == (x["dir"] > 0) and (x["pp"] < 0) == (x["dir"] < 0) for x in rows), rows
    assert [x["pp"] for x in rows if x["dir"] == 0] == [0.0] and all(x["cat"] != "etc" for x in rows)
    up = {x["title"]: x["pp"] for x in rows if x["dir"] > 0}
    assert abs(up["외국인 순매수"] - up["자사주 취득"]) <= 0.11 and abs(up["외국인 순매수"] - 2 * up["매수 유지"]) <= 0.21   # 강도 비례(반올림 보정분 허용)
    # U=10, D=5 → 총량 = 9.7 × 15/5 = 29.1(상한 3배와 같음) → 상승 19.4 · 하락 9.7
    assert abs(sum(x["pp"] for x in rows if x["pp"] > 0) - 19.4) <= 0.11 and abs(sum(x["pp"] for x in rows if x["pp"] < 0) + 9.7) <= 0.11


def test_attribute_caps_gross_when_sides_nearly_cancel():
    items = PF.clean([f("flow", "상승 요인", "up", 5), f("dart", "하락 요인", "down", 4)])      # U−D = 1 → 상한 없으면 총량 9배
    rows = PF.attribute(items, 2.0)
    gross = sum(abs(x["pp"]) for x in rows)
    assert total(rows) == 2.0 and abs(gross - 2.0 * PF.GROSS_CAP) <= 0.11, rows                # 상승 +4.0 · 하락 −2.0


def test_attribute_when_llm_view_disagrees_with_price():
    # 요인은 상승 우위인데 주가는 내림 — 부호를 뒤집지 않고 하락 요인 쪽에 더 큰 몫
    items = PF.clean([f("research", "매수 유지", "up", 4), f("news", "수주 기대", "up", 3), f("flow", "기관 순매도", "down", 2)])
    rows = PF.attribute(items, -3.0)
    assert total(rows) == -3.0 and all((x["pp"] > 0) == (x["dir"] > 0) for x in rows)
    # U=7, D=2 → 총량 = 3.0 × 9/5 = 5.4 → 상승 +1.2 · 하락 −4.2
    assert [x["pp"] for x in rows if x["dir"] < 0] == [-4.2] and all(x["cat"] != "etc" for x in rows)
    assert abs(sum(x["pp"] for x in rows if x["dir"] > 0) - 1.2) <= 0.11


def test_attribute_missing_side_leaves_residual():
    # 주가는 내렸는데 하락 요인이 없다 → 상승 요인엔 작은 몫, 나머지는 '기타·시장' 한 줄
    items = PF.clean([f("research", "매수 유지", "up", 3), f("flow", "외국인 순매수", "up", 1)])
    rows = PF.attribute(items, -4.0)
    etc = [x for x in rows if x["cat"] == "etc"]
    assert total(rows) == -4.0 and len(etc) == 1 and etc[0]["pp"] == -6.0 and etc[0]["dir"] == -1, rows
    assert abs(sum(x["pp"] for x in rows if x["cat"] != "etc") - 2.0) <= 0.11
    # 반대(올랐는데 상승 요인 없음)
    rows = PF.attribute(PF.clean([f("dart", "유상증자", "down", 3)]), 5.0)
    assert total(rows) == 5.0 and [x["pp"] for x in rows if x["cat"] == "etc"] == [7.5]
    # 같은 방향뿐이면 나머지 없음
    rows = PF.attribute(PF.clean([f("dart", "자사주 취득", "up", 3), f("flow", "외국인 순매수", "up", 1)]), 4.0)
    assert total(rows) == 4.0 and [x["pp"] for x in rows] == [3.0, 1.0]


def test_attribute_flat_day_and_rounding():
    rows = PF.attribute(PF.clean([f("flow", "외국인 순매수", "up", 3), f("flow", "기관 순매도", "down", 3)]), 0.0)
    assert total(rows) == 0.0 and [x["pp"] for x in rows] == [0.5, -0.5]                       # 서로 상쇄(GROSS_FLOOR)
    rows = PF.attribute(PF.clean([f("flow", "외국인 순매수", "up", 3)]), 0.0)
    assert [x["pp"] for x in rows] == [0.0]
    three = PF.clean([f("flow", "a", "up", 1), f("dart", "b", "up", 1), f("news", "c", "up", 1)])
    rows = PF.attribute(three, 1.0)                                                             # 0.333… ×3 → 반올림 오차 보정
    assert total(rows) == 1.0 and sorted(x["pp"] for x in rows) == [0.3, 0.3, 0.4]


def test_build_document():
    flow = {"trend": [{"date": "20261110", "rate": 9.7, "close": 27150}]}
    raw = [f("flow", "외국인 순매수", "up", 4, "11/10 +8.8만 주"), f("dart", "영업이익 하회", "down", 3)]
    doc = PF.build(raw, flow, None, at(2026, 11, 11, 8, 40))
    assert doc["basis"] == {"date": "2026-11-10", "ratePct": 9.7, "kind": "last"} and doc["estimated"] is True
    live = PF.build(raw, flow, 26900, at(2026, 11, 11, 10, 40))                       # 장중 — 당일 등락(−0.92%)에 맞춘다
    assert live["basis"] == {"date": "2026-11-11", "ratePct": -0.92, "kind": "today"} and round(sum(x["pp"] for x in live["items"]), 1) == -0.9
    assert round(sum(doc["byCat"].values()), 1) == 9.7 and set(doc["byCat"]) == {"flow", "dart"}
    assert doc["items"][0]["evidence"] == "11/10 +8.8만 주" and set(doc["items"][0]) == {"cat", "title", "dir", "strength", "evidence", "pp"}
    assert PF.build(raw, None) is None and PF.build([], flow) is None and PF.build("x", flow) is None


def test_schema_wiring():
    """generate_analysis: 스위치가 켜져 있으면 스키마·프롬프트에 priceFactors 가 들어간다(무거운 의존성이 없으면 건너뜀)."""
    try:
        import generate_analysis as G
    except ImportError as e:                                  # yfinance·requests 등이 없는 환경(문법 오류는 숨기지 않는다)
        print("  (generate_analysis import 불가 — 배선 검사 생략:", type(e).__name__, ")")
        return
    assert G.PRICE_FACTORS_ON is True
    assert "priceFactors" in G.ANALYSIS_SCHEMA["properties"] and "priceFactors" in G.ANALYSIS_SCHEMA["required"]
    assert "priceFactors" in G.ANALYSIS_SCHEMA_FU["properties"]
    assert "priceFactors:" in G.SYSTEM and "priceFactors:" in G.REFINE_SYSTEM
    assert "여섯 항목 각각에 1~4개씩" in G.SYSTEM and "price_today" in G.SYSTEM                 # 항목별 요인 확대 · 당일 기준


def test_reanalyze_once_when_factors_missing_or_old():
    """증분 모드: 요인이 없거나 옛 판으로 만든 이월 분석은 한 번만 재분석 — 새로 분석해 _meta.pf 에 판 번호가 붙으면 다시 하지 않는다."""
    try:
        import generate_analysis as G
    except ImportError as e:
        print("  (generate_analysis import 불가 — 생략:", type(e).__name__, ")")
        return
    import datetime
    now = datetime.datetime(2026, 11, 11, 10, 40, tzinfo=G.KST)
    fresh = (now - datetime.timedelta(minutes=30)).isoformat()
    keep = G._latest_filing_key
    G._latest_filing_key = lambda code: "k1"                 # 신규 공시 없음(외부 호출 차단)
    try:
        stock = {"code": "082740", "name": "한화엔진"}

        def entry(meta, **extra):
            return {"analysis": dict({"catalyst": "x", "_meta": dict({"asOf": fresh, "filingKey": "k1", "price": 100}, **meta)}, **extra), "basePrice": 100}
        assert G._needs_analysis(stock, entry({}), now) == (True, "요인갱신(1회)")                                   # 요인 출력 이전 분석
        assert G._needs_analysis(stock, entry({"pf": 1}, priceFactors={"items": []}), now) == (True, "요인갱신(1회)")   # 옛 판(요인이 있어도)
        need, why = G._needs_analysis(stock, entry({"pf": PF.VERSION}), now)                                        # 새 판으로 분석됨(요인이 없어도 반복 안 함)
        assert need is False and why.startswith("캐시"), (need, why)
    finally:
        G._latest_filing_key = keep


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok", t.__name__)
    print(f"ALL PASS ({len(tests)} tests — price factors)")
