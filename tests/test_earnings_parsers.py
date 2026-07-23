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
                                     parse_naver_finance, parse_wcomp_cns,
                                     parse_dart_document, _dart_num,
                                     enrich_calendar, _yoy_calc, _target_period,
                                     build, _recent_quarters)


def _fixture_text(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


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
         "corp_name": "삼성전자", "rcept_dt": "20260722", "rcept_no": "1"},
        {"report_nm": "[정정]연결재무제표기준영업(잠정)실적", "stock_code": "005930",
         "corp_name": "삼성전자", "rcept_dt": "20260722", "rcept_no": "2"},
        {"report_nm": "주요사항보고서", "stock_code": "000660",
         "corp_name": "SK하이닉스", "rcept_dt": "20260722", "rcept_no": "3"},
    ])
    assert len(dart) == 1 and dart[0]["code"] == "005930"
    assert dart[0]["name"] == "삼성전자", "DART corp_name 추출(240600 코드 표시 사고 방지)"
    print("parse_dart OK (실적만 통과, 정정 제외, 이름 추출)")

    # DART 단독 종목도 이름이 병합되는지 (유진테크놀로지 240600 사례)
    only_dart = build([], [], [{"date": "2026-07-23", "code": "240600",
                                "name": "유진테크놀로지", "title": "영업(잠정)실적",
                                "url": "u"}], datetime.date(2026, 7, 23))
    assert only_dart["released"][0]["name"] == "유진테크놀로지"
    print("build DART-only name OK")

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

    # ── 네이버 finance/quarter 파서 (실측 픽스처: LS ELECTRIC·유진테크놀로지) ──
    nv_ls = parse_naver_finance(_fixture("naver_finance_quarter_010120.json"))
    assert nv_ls["periods"]["202606"] is True     # 컨센서스 플래그
    assert nv_ls["periods"]["202603"] is False    # 발표 실적
    assert nv_ls["metrics"]["op"]["202606"] == 1640.0
    assert nv_ls["metrics"]["op"]["202506"] == 1086.0   # 전년동기(YoY 기반)
    nv_yj = parse_naver_finance(_fixture("naver_finance_quarter_240600.json"))
    assert nv_yj["metrics"]["op"]["202603"] == -17.0    # 스몰캡 적자 분기
    assert "202606" not in nv_yj["metrics"]["op"]       # 컨센 없음('-')
    print("parse_naver_finance OK")

    # ── FnGuide wcomp 컨센서스 파서 ─────────────────────────────────────────
    wc_ls = parse_wcomp_cns(_fixture("wcomp_cns_trend_010120.json"))
    assert wc_ls["periods"]["202606"] is True
    assert round(wc_ls["metrics"]["op"]["202606"]) == 1640
    assert wc_ls["yoy"]["op"]["202603"] == 44.96
    assert wc_ls["consGap"]["op"]["202603"] == -4.98
    wc_yj = parse_wcomp_cns(_fixture("wcomp_cns_trend_240600.json"))
    assert wc_yj["yoy"]["op"]["202603"] == "적자지속"   # 텍스트 보존
    print("parse_wcomp_cns OK")

    # ── YoY 계산 규칙 (적자 관례 텍스트) ────────────────────────────────────
    assert _yoy_calc(150.0, 100.0) == 50.0
    assert _yoy_calc(50.0, -10.0) == "흑전"
    assert _yoy_calc(-5.0, -10.0) == "적지"
    assert _yoy_calc(-5.0, 10.0) == "적전"
    assert _target_period({"date": "2026-07-23"}, datetime.date(2026, 7, 23)) == "202606"
    assert _target_period({"period": "202603", "date": "2026-07-23"},
                          datetime.date(2026, 7, 23)) == "202603"
    print("_yoy_calc / _target_period OK")

    # ── DART 공시 원문 파서 (실측 픽스처: LS일렉트릭 이익 / 유진테크 매출단독) ──
    assert _dart_num("1,576,998") == 1576998.0
    assert _dart_num("△1,691") == -1691.0
    assert _dart_num("(1,691)") == -1691.0
    assert _dart_num("-1,691") == -1691.0
    assert _dart_num("-") is None and _dart_num("") is None
    doc_ls = parse_dart_document(_fixture_text("dart_doc_010120.html"))
    assert doc_ls["op"] == 1785.2, doc_ls          # 178,520 백만원 → 억
    assert doc_ls["sales"] == 15770.0
    assert doc_ls["np"] == 1158.4
    assert doc_ls["opYoY"] == 64.39
    doc_yj = parse_dart_document(_fixture_text("dart_doc_240600.html"))
    assert doc_yj["sales"] == 99.0                 # 매출만 공시(9,905 백만원)
    assert "op" not in doc_yj                       # 영업이익 미공시('-') → 제외
    doc_sh = parse_dart_document(_fixture_text("dart_doc_055550.html"))
    assert doc_sh["sales"] == 250631.6             # 금융지주 매출액(이자+수수료+기타, 25.06조)
    assert doc_sh["op"] == 24762.8                 # 영업이익 2,476,281 백만원
    print("parse_dart_document / _dart_num OK")

    # ── enrich: DART 원문으로 실제 실적 + 네이버 컨센서스 → 서프라이즈 갭 ──────
    rel_doc = {"date": "2026-07-23", "code": "010120", "name": "LS ELECTRIC",
               "period": "202606", "quarter": None, "fs": None,
               "sales": None, "op": None, "np": None,
               "salesYoY": None, "opYoY": None, "npYoY": None, "tag": None,
               "consensus": {"op": None, "np": None},
               "surprise": {"opGap": None, "npGap": None},
               "dartUrl": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260723800150",
               "dartTitle": "t", "sources": ["dart"]}
    doc_html = _fixture_text("dart_doc_010120.html")
    enrich_calendar([rel_doc], [], datetime.date(2026, 7, 23),
                    fetch_naver=lambda c: _fixture("naver_finance_quarter_010120.json"),
                    fetch_wcomp=lambda c: _fixture("wcomp_cns_trend_010120.json"),
                    fetch_doc=lambda rc: parse_dart_document(doc_html))
    assert rel_doc["op"] == 1785.2, rel_doc          # DART 원문 실적
    assert rel_doc["sales"] == 15770.0
    assert rel_doc["opYoY"] == 64.39
    assert rel_doc["consensus"]["op"] == 1640.0      # 네이버 컨센서스
    assert rel_doc["surprise"]["opGap"] == 8.9       # (1785.2-1640)/1640 → 컨상 +8.9%
    assert "dart-doc" in rel_doc["sources"]
    print("enrich_calendar DART 원문 실적+서프라이즈 OK")

    # ── enrich: op 는 집계됐지만 매출 결손(금융지주) → DART 원문이 매출을 채운다 ──
    #    신한지주 2026-07-23: 집계 사이트가 op 만 주고 매출액을 비워, op None 조건이면
    #    DART-doc 이 스킵돼 매출이 영영 누락되던 회귀.
    rel_sh = {"date": "2026-07-23", "code": "055550", "name": "신한지주",
              "period": "202606", "quarter": None, "fs": None,
              "sales": None, "op": 24763.0, "np": None,     # op 만 집계됨
              "salesYoY": None, "opYoY": None, "npYoY": None, "tag": None,
              "consensus": {"op": 22532.0, "np": None},
              "surprise": {"opGap": None, "npGap": None},
              "dartUrl": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260723800269",
              "dartTitle": "t", "sources": ["wisereport", "dart"]}
    sh_html = _fixture_text("dart_doc_055550.html")
    enrich_calendar([rel_sh], [], datetime.date(2026, 7, 23),
                    fetch_naver=lambda c: {"financeInfo": {"trTitleList": [], "rowList": []}},
                    fetch_wcomp=lambda c: {"dataset": {"header": [], "data": []}},
                    fetch_doc=lambda rc: parse_dart_document(sh_html))
    assert rel_sh["sales"] == 250631.6, rel_sh     # DART 원문이 매출 채움
    assert rel_sh["op"] == 24763.0                  # 기존 op 는 보존(덮어쓰지 않음)
    assert "dart-doc" in rel_sh["sources"]
    print("enrich_calendar 매출 결손(금융지주) 보강 OK")

    # ── enrich: DART 단독 행(수치 전무)에 컨센서스 채움 + upcoming 컨센 보강 ──
    t = datetime.date(2026, 7, 23)
    rel_row = {"date": "2026-07-23", "code": "010120", "name": "엘에스일렉트릭",
               "period": None, "quarter": None, "fs": None,
               "sales": None, "op": None, "np": None,
               "salesYoY": None, "opYoY": None, "npYoY": None, "tag": None,
               "consensus": {"op": None, "np": None},
               "surprise": {"opGap": None, "npGap": None},
               "dartUrl": "u", "dartTitle": "t", "sources": ["dart"]}
    up_row = {"date": "2026-07-23", "code": "010120", "name": "LS ELECTRIC",
              "period": "202606", "consensus": {"op": None, "np": None, "yoy": None,
                                                "qoq": None},
              "provisional": {"op": None, "np": None},
              "surprise": {"opGap": None, "npGap": None}}
    fx_naver = {"010120": _fixture("naver_finance_quarter_010120.json")}
    fx_wcomp = {"010120": _fixture("wcomp_cns_trend_010120.json")}
    enrich_calendar([rel_row], [up_row], t,
                    fetch_naver=lambda c: fx_naver[c], fetch_wcomp=lambda c: fx_wcomp[c])
    assert rel_row["consensus"]["op"] == 1640.0, rel_row["consensus"]
    assert rel_row["op"] is None                  # 실적은 아직 미집계 — 채우면 안 됨
    assert "naver" in rel_row["sources"]
    assert up_row["consensus"]["op"] == 1640.0
    assert up_row["consensus"]["yoy"] == 51.0     # 컨센 1640 vs 전년동기 1086
    print("enrich_calendar 컨센서스 보강 OK")

    # ── enrich: 실적이 집계된 경우 (컨센서스 기간을 실적으로 바꾼 가공 픽스처) ──
    fx2 = json.loads(json.dumps(fx_naver["010120"]))
    for tt in fx2["financeInfo"]["trTitleList"]:
        if tt["key"] == "202606":
            tt["isConsensus"] = "N"               # 잠정 집계 완료 상황 시뮬레이션
    rel2 = json.loads(json.dumps(rel_row))
    rel2.update({"sales": None, "op": None, "np": None, "opYoY": None,
                 "sources": ["dart"], "consensus": {"op": 1600.0, "np": None},
                 "surprise": {"opGap": None, "npGap": None}})
    enrich_calendar([rel2], [], t,
                    fetch_naver=lambda c: fx2, fetch_wcomp=lambda c: (_ for _ in ()).throw(RuntimeError("skip")))
    assert rel2["op"] == 1640.0
    assert rel2["opYoY"] == 51.0                  # 전년동기 1086 대비 계산
    assert rel2["surprise"]["opGap"] == 2.5       # (1640-1600)/1600
    print("enrich_calendar 실적 보강 OK")

    # ── enrich: 오늘자 우선 — 과거 released 가 예산을 가로채지 않음 (두산밥캣 회귀) ──
    # ENRICH_MAX 를 넘는 과거 released 를 앞에 두고, 오늘 예정(두산밥캣)이 그래도
    # 조회·보강되는지 확인 (2026-07-23: 과거 released 24건이 10 예산을 소진해 실패).
    import fetch_earnings_calendar as _fc
    old_rels = [{"date": "2026-07-20", "code": f"9000{i:02d}", "name": f"과거{i}",
                 "period": None, "quarter": None, "fs": None,
                 "sales": None, "op": None, "np": None,
                 "salesYoY": None, "opYoY": None, "npYoY": None, "tag": None,
                 "consensus": {"op": None, "np": None},
                 "surprise": {"opGap": None, "npGap": None},
                 "dartUrl": "u", "dartTitle": "t", "sources": ["dart"]}
                for i in range(_fc.ENRICH_MAX + 5)]
    doosan = {"date": "2026-07-23", "code": "241560", "name": "두산밥캣",
              "period": "202606", "consensus": {"op": None, "np": None, "yoy": None,
                                                "qoq": None},
              "provisional": {"op": None, "np": None},
              "surprise": {"opGap": None, "npGap": None}}
    fx_d_naver = {"241560": _fixture("naver_finance_quarter_241560.json")}
    calls = []
    def _naver_probe(c):
        calls.append(c)
        return fx_d_naver[c]      # 오늘자(241560)만 픽스처 보유 — 과거코드면 KeyError
    enrich_calendar(old_rels, [doosan], datetime.date(2026, 7, 23),
                    fetch_naver=_naver_probe,
                    fetch_wcomp=lambda c: (_ for _ in ()).throw(RuntimeError("skip")))
    assert "241560" in calls, "오늘 예정 종목이 조회되지 않음(과거 released 가 예산 소진)"
    assert all(not c.startswith("9000") for c in calls), "과거 released(비오늘)가 조회 대상에 포함됨"
    assert doosan["consensus"]["op"] == 2053.0, doosan["consensus"]
    assert doosan["consensus"]["yoy"] == -1.1
    print("enrich_calendar 오늘자 우선(두산밥캣 회귀) OK")

    # ── build: 같은 날짜 released 종목은 upcoming 에서 제거 ──────────────────
    dd = build([], [], [{"date": "2026-07-23", "code": "010120",
                         "name": "엘에스일렉트릭", "title": "영업(잠정)실적", "url": "u"}],
               datetime.date(2026, 7, 23))
    dup_wise = [{"date": "2026-07-23", "code": "010120", "name": "LS ELECTRIC",
                 "sector": None, "fs": None, "period": "202606", "periodType": "분기",
                 "opinion": None, "targetPrice": None,
                 "consensus": {"op": None, "np": None, "yoy": None, "qoq": None},
                 "provisional": {"op": None, "np": None},
                 "surprise": {"opGap": None, "npGap": None}}]
    dd2 = build(dup_wise, [], [{"date": "2026-07-23", "code": "010120",
                                "name": "엘에스일렉트릭", "title": "영업(잠정)실적",
                                "url": "u"}], datetime.date(2026, 7, 23))
    assert len(dd2["released"]) == 1
    assert not dd2["upcoming"], "released 와 같은 날짜의 upcoming 중복이 남음"
    assert dd["released"][0]["code"] == "010120"
    print("build released/upcoming dedupe OK")
    print("ALL PASS")


if __name__ == "__main__":
    main()
