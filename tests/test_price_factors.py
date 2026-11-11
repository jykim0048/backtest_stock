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


def test_basis_takes_latest_row_with_rate():
    flow = {"trend": [{"date": "20261110", "rate": 9.7, "close": 27150}, {"date": "20261109", "rate": 1.03}]}
    assert PF.basis(flow) == {"date": "2026-11-10", "ratePct": 9.7}
    assert PF.basis({"trend": [{"date": "20261110", "rate": None}, {"date": "2026-11-09", "rate": -1.2}]}) == {"date": "2026-11-09", "ratePct": -1.2}
    assert PF.basis({"trend": []}) is None and PF.basis(None) is None and PF.basis({"trend": [{"date": "x", "rate": 1.0}]}) is None


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
    flow = {"trend": [{"date": "20261110", "rate": 9.7}]}
    raw = [f("flow", "외국인 순매수", "up", 4, "11/10 +8.8만 주"), f("dart", "영업이익 하회", "down", 3)]
    doc = PF.build(raw, flow)
    assert doc["basis"] == {"date": "2026-11-10", "ratePct": 9.7} and doc["estimated"] is True
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


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print("  ok", t.__name__)
    print(f"ALL PASS ({len(tests)} tests — price factors)")
