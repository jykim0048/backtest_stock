"""fetch_earnings_calendar 파서·병합 회귀 — 실측 픽스처 기반 (오프라인).

실행: python tests/test_earnings_parsers.py
"""
import os
import sys
import json
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")

from fetch_earnings_calendar import (parse_wisereport, parse_fnguide, parse_dart,
                                     build, _recent_quarters)


def _fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def main():
    # ── WiseReport 파서: memo 슬롯 라벨 파싱 ─────────────────────────────────
    wise = parse_wisereport(_fixture("wisereport_calendar_sample.json"))
    assert wise, "wisereport 파싱 0건"
    w0 = wise[0]
    for k in ("date", "code", "name", "consensus", "provisional", "surprise"):
        assert k in w0, f"wise 행에 {k} 누락"
    assert any(w["consensus"]["op"] is not None for w in wise), "컨센서스 영업이익 파싱 실패"
    print(f"parse_wisereport OK ({len(wise)}행, 예: {w0['name']} {w0['date']} "
          f"컨센 op={w0['consensus']['op']})")

    # ── FnGuide 파서: YoY 숫자/텍스트(흑전 등) 보존 ─────────────────────────
    fng = parse_fnguide(_fixture("fnguide_proresult_sample.json"))
    assert fng, "fnguide 파싱 0건"
    f0 = fng[0]
    assert f0["date"] and f0["date"].startswith("20"), f"DIS_DT 변환 실패: {f0['date']}"
    assert any(isinstance(f.get("opYoY"), str) or isinstance(f.get("opYoY"), float)
               for f in fng), "opYoY 파싱 실패"
    print(f"parse_fnguide OK ({len(fng)}행, 예: {f0['name']} {f0['quarter']} op={f0['op']})")

    # ── DART 필터: 실적 공시만, 정정 제외 ───────────────────────────────────
    dart = parse_dart([
        {"report_nm": "연결재무제표기준영업(잠정)실적", "stock_code": "005930",
         "rcept_dt": "20260722", "rcept_no": "1"},
        {"report_nm": "[정정]연결재무제표기준영업(잠정)실적", "stock_code": "005930",
         "rcept_dt": "20260722", "rcept_no": "2"},
        {"report_nm": "주요사항보고서", "stock_code": "000660",
         "rcept_dt": "20260722", "rcept_no": "3"},
    ])
    assert len(dart) == 1 and dart[0]["code"] == "005930"
    print("parse_dart OK (실적만 통과, 정정 제외)")

    # ── build 병합: 픽스처 날짜 기준 upcoming/released 분리 ─────────────────
    dates = sorted({w["date"] for w in wise if w["date"]})
    today = datetime.date.fromisoformat(dates[len(dates) // 2])
    merged = build(wise, fng, dart, today)
    assert isinstance(merged["upcoming"], list) and isinstance(merged["released"], list)
    for u in merged["upcoming"]:
        assert u["date"] >= today.isoformat(), "upcoming 에 과거 날짜"
        assert u["provisional"]["op"] is None, "잠정치 있는 행은 released 여야"
    print(f"build OK (기준일 {today}: upcoming {len(merged['upcoming'])} / "
          f"released {len(merged['released'])})")

    # ── 분기 말월 계산 ──────────────────────────────────────────────────────
    assert _recent_quarters(datetime.date(2026, 7, 23)) == ["202606", "202603"]
    assert _recent_quarters(datetime.date(2026, 1, 5)) == ["202512", "202509"]
    print("_recent_quarters OK")
    print("ALL PASS")


if __name__ == "__main__":
    main()
