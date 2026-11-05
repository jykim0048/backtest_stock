"""CA(예탁원 기업행위) 캘린더 — 펼치기·영향도·실패 보존·장중/주간 발췌 회귀 (오프라인 픽스처).

실행: python tests/test_ca_calendar.py
허브 /ca 응답 필드명은 KIS 문서 기준(실서비스 미검증) — 여기 픽스처는 명세 형태를 따른다.
"""
import datetime
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fetch_ca_calendar as fc

KST = datetime.timezone(datetime.timedelta(hours=9))
NAMES = {"005930": "삼성전자", "000660": "SK하이닉스", "123456": "테스트전자"}


def _ev(typ, code, milestones, fields=None, name=None, record=None, label=None):
    return {"type": typ, "label": label or fc.TYPE_LABELS.get(typ), "code": code, "name": name,
            "recordDate": record, "milestones": milestones, "fields": fields or {}}


def _rows_by(rows, **kw):
    return [r for r in rows if all(r.get(k) == v for k, v in kw.items())]


def test_flatten_paidin_and_names():
    ev = _ev("paidin", "5930", [
        {"kind": "record", "label": "기준일", "date": "2026-11-06"},
        {"kind": "ex", "label": "권리락", "date": "20261105"},
        {"kind": "subscribe", "label": "청약", "date": "2026-12-01", "end": "2026-12-02"},
        {"kind": "pay", "label": "납입", "date": "2026-12-05"},
        {"kind": "listing", "label": "신주 상장", "date": "2026-12-18", "note": "예정"},
    ], fields={"newSharesPct": 12.5, "discountPct": "20", "issuePrice": None,
               "nested": {"x": 1}, "blank": " "})
    rows = fc.flatten([ev], NAMES)
    # ex 가 있으면 record(기준일) 행은 생략
    assert not _rows_by(rows, kind="record"), rows
    assert [r["date"] for r in rows] == ["2026-11-05", "2026-12-01", "2026-12-05", "2026-12-18"]
    ex = rows[0]
    assert ex["code"] == "005930" and ex["name"] == "삼성전자", ex
    assert ex["typeLabel"] == "유상증자" and ex["milestoneLabel"] == "권리락"
    assert ex["importance"] == 3
    assert ex["fields"] == {"newSharesPct": 12.5, "discountPct": "20"}, ex["fields"]
    sub = _rows_by(rows, kind="subscribe")[0]
    assert sub["end"] == "2026-12-02" and sub["importance"] == 2
    assert _rows_by(rows, kind="pay")[0]["importance"] == 1
    lst = _rows_by(rows, kind="listing")[0]
    assert lst["importance"] == 3 and lst["note"] == "예정"


def test_importance_rules():
    imp = fc._importance
    # 배당락: 시가배당률 3%+ 또는 미상 → 2, 저배당 → 1
    assert imp("dividend", "ex", "배당락(추정)", {"cashYieldPct": 4.1}) == 2
    assert imp("dividend", "ex", "배당락(추정)", {}) == 2
    assert imp("dividend", "ex", "배당락(추정)", {"faceDivPct": 566, "highDividend": "N"}) == 1
    assert imp("dividend", "ex", "배당락(추정)", {"faceDivPct": 566, "highDividend": "Y"}) == 2
    assert imp("dividend", "ex", "배당락(추정)", {"cashYieldPct": "1.2"}) == 1
    assert imp("dividend", "pay", "지급", {"cashYieldPct": 9}) == 1
    # 추가상장: 신주 비중 5%+ → 3, 미만·미상 → 2 (문자열 '%' 허용, pctOfTotal 폴백)
    assert imp("listing", "listing", "추가 상장", {"newSharesPct": 7}) == 3
    assert imp("listing", "listing", "추가 상장", {"newSharesPct": 100, "reason": "액면병합"}) == 1
    assert imp("listing", "listing", "추가 상장", {"newSharesPct": 33, "reason": "무상증자"}) == 2
    assert imp("listing", "listing", "추가 상장", {"newSharesPct": "6.5%"}) == 3
    assert imp("listing", "listing", "추가 상장", {"pctOfTotal": 5}) == 3
    assert imp("listing", "listing", "추가 상장", {"newSharesPct": 2}) == 2
    assert imp("listing", "listing", "추가 상장", {}) == 2
    # 합병·감자·액면교체: 매매정지·재상장 → 3
    assert imp("merger", "halt", "매매정지", {}) == 3
    assert imp("capdcrs", "listing", "재상장", {}) == 3
    assert imp("revsplit", "halt", "매매정지", {}) == 3
    assert imp("merger", "meeting", "주주총회", {}) == 1
    # 의무예치 3 · 무상 권리락 2 · 매수청구 마감 2 · 기타 1
    assert imp("deposit", "deposit", "보호예수 시작", {}) == 1          # 예치 시작은 소음
    assert imp("deposit", "release", "보호예수 해제", {"pctOfTotal": 1.87}) == 3
    assert imp("deposit", "release", "보호예수 해제", {"pctOfTotal": 0.01}) == 2
    assert imp("bonus", "ex", "권리락", {}) == 2
    assert imp("bonus", "record", "기준일", {}) == 1
    assert imp("purreq", "deadline", "청구 마감", {}) == 2
    for t in ("ipo", "forfeit", "meeting", "unknown"):
        assert imp(t, "subscribe", "청약", {}) == 1, t


def test_flatten_defensive():
    rows = fc.flatten([
        None, "x", {"type": ""},                                     # 비정상 → 건너뜀
        _ev("meeting", "000660", [], record="2026/11/20"),           # 마일스톤 없음 → 기준일 1행
        _ev("bonus", "123456", [{"kind": "ex", "label": "권리락", "date": "bad"},
                                {"kind": "ex", "label": "권리락", "date": "2026-11-10"},
                                {"kind": "ex", "label": "권리락", "date": "2026-11-10"}]),  # 중복 제거
        _ev("merger", "999999", [{"kind": "halt", "label": "매매정지", "date": "2026-11-09",
                                  "end": "2026-11-09"}]),            # end==date → end 생략
        {"type": "ipo", "code": None, "milestones": [{"kind": "subscribe", "date": "2026-11-12"}]},
    ], NAMES)
    assert len(rows) == 4, rows
    mt = _rows_by(rows, type="meeting")[0]
    assert mt["date"] == "2026-11-20" and mt["kind"] == "record" and mt["importance"] == 1
    assert mt["name"] == "SK하이닉스"
    assert len(_rows_by(rows, type="bonus")) == 1
    mg = _rows_by(rows, type="merger")[0]
    assert "end" not in mg and mg["name"] is None and mg["importance"] == 3
    ipo = _rows_by(rows, type="ipo")[0]
    assert ipo["typeLabel"] == "공모주청약" and ipo["milestoneLabel"] == "subscribe"
    # 정렬: 날짜 → 영향도 내림차순
    assert [r["date"] for r in rows] == sorted(r["date"] for r in rows)


def test_listed_only_filter():
    """상장 종목만(2026-11-05) — 예탁원은 비상장 예탁 종목도 준다(네파 신주 25,019% 등)."""
    evs = [{"type": "paidin", "code": "157820", "name": "네파", "milestones": [{"kind": "ex", "label": "권리락", "date": "2026-11-05"}],
            "fields": {"newSharesPct": 25019.77}},
           {"type": "listing", "code": "247540", "milestones": [{"kind": "listing", "label": "추가 상장", "date": "2026-11-05"}],
            "fields": {"reason": "유상증자", "newSharesPct": 9.19}}]
    rows = fc.flatten(evs, {}, {"247540"})
    assert [r["code"] for r in rows] == ["247540"]
    assert len(fc.flatten(evs, {}, None)) == 2                     # 목록 없으면 거르지 않음
    resp = {"status": "success", "events": evs, "from": "20261101", "to": "20261130"}
    p = fc.build(resp, None, datetime.datetime(2026, 11, 5, 9, 0), {}, {"247540"})
    assert p["unlistedSkipped"] == 1 and len(p["rows"]) == 1


def test_build_failures_keep_file():
    now = datetime.datetime(2026, 11, 4, 9, 7, tzinfo=KST)
    assert fc.build(None, {}, now) is None
    assert fc.build({"status": "error", "errors": {"all": "x"}}, {}, now) is None
    assert fc.build({"status": "success", "events": None}, {}, now) is None
    assert fc.build({"status": "success", "events": [], "errors": {"paidin": "timeout"}}, {}, now) is None
    # 정상 빈 응답(오류 없음)은 저장
    p = fc.build({"status": "success", "events": [], "from": "20261021", "to": "20261219"}, {}, now)
    assert p["rows"] == [] and p["from"] == "2026-10-21" and p["to"] == "2026-12-19"
    assert p["asof"] == "2026-11-04 09:07 KST" and p["source"] == fc.SOURCE


def test_build_carries_errored_types():
    now = datetime.datetime(2026, 11, 4, 9, 7, tzinfo=KST)
    prev = {"rows": [
        {"date": "2026-11-12", "kind": "deposit", "milestoneLabel": "의무예치", "type": "deposit",
         "typeLabel": "의무예치", "code": "123456", "name": "테스트전자", "importance": 3, "fields": {}},
        {"date": "2026-10-01", "kind": "deposit", "milestoneLabel": "의무예치", "type": "deposit",
         "typeLabel": "의무예치", "code": "123457", "name": None, "importance": 3, "fields": {}},  # 창 밖
        {"date": "2026-11-13", "kind": "ex", "milestoneLabel": "권리락", "type": "paidin",
         "typeLabel": "유상증자", "code": "005930", "name": "삼성전자", "importance": 3, "fields": {}},
    ]}
    resp = {"status": "success", "from": "20261021", "to": "20261219", "asof": "2026-11-04 09:05",
            "counts": {"paidin": 1}, "errors": {"deposit": "EGW00201", "paidin": "partial"},
            "events": [_ev("paidin", "000660", [{"kind": "ex", "label": "권리락", "date": "2026-11-06"}])]}
    p = fc.build(resp, prev, now, NAMES)
    # deposit 은 이번 응답 0건 → 창 안 직전 행만 이월. paidin 은 이번 응답에 있으므로 이월 안 함.
    dep = _rows_by(p["rows"], type="deposit")
    assert len(dep) == 1 and dep[0]["carried"] is True and dep[0]["code"] == "123456", dep
    assert [r["code"] for r in _rows_by(p["rows"], type="paidin")] == ["000660"]
    assert p["rowCounts"] == {"paidin": 1, "deposit": 1}
    assert p["hubAsof"] == "2026-11-04 09:05" and p["counts"] == {"paidin": 1}


def test_fresh_gate():
    now = datetime.datetime(2026, 11, 4, 10, 37, tzinfo=KST)
    assert fc._fresh({"asof": "2026-11-04 10:07 KST"}, now, 90)
    assert not fc._fresh({"asof": "2026-11-04 08:30 KST"}, now, 90)
    assert not fc._fresh({"asof": "2026-11-03 10:07 KST"}, now, 90 * 100)   # 다른 날
    assert not fc._fresh({}, now, 90)
    assert not fc._fresh({"asof": "2026-11-04 10:07 KST"}, now, 0)


def test_main_writes_and_preserves():
    """main() 왕복 — 허브 호출은 목(mock)으로 대체(네트워크 없음). 실패 시 기존 파일 보존."""
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "ca_calendar.json")
    orig = (fc.OUT_PATH, fc.fetch_hub)
    try:
        fc.OUT_PATH = out
        fc.fetch_hub = lambda: {"status": "success", "from": "20261021", "to": "20261219",
                                "events": [_ev("paidin", "005930",
                                               [{"kind": "ex", "label": "권리락", "date": "2026-11-05"}])]}
        assert fc.main(["--force"]) == 0
        with open(out, encoding="utf-8") as f:
            saved = json.load(f)
        assert len(saved["rows"]) == 1 and saved["rows"][0]["name"] == "삼성전자"  # krx_companies 매핑
        # 직후 재실행 → 신선도 게이트로 호출 생략(파일 불변)
        fc.fetch_hub = lambda: (_ for _ in ()).throw(AssertionError("호출되면 안 됨"))
        assert fc.main([]) == 0
        # 강제 실행 + 허브 장애 → 기존 파일 보존, exit 0
        def boom():
            raise RuntimeError("hub down")
        fc.fetch_hub = boom
        assert fc.main(["--force"]) == 0
        with open(out, encoding="utf-8") as f:
            assert json.load(f) == saved
        # status=error → 보존
        fc.fetch_hub = lambda: {"status": "error", "errors": {"all": "KIS 토큰 만료"}}
        assert fc.main(["--force"]) == 0
        with open(out, encoding="utf-8") as f:
            assert json.load(f) == saved
    finally:
        fc.OUT_PATH, fc.fetch_hub = orig


def _cal_rows():
    def r(date, imp, code, typ="paidin", kind="ex", end=None, fields=None):
        x = {"date": date, "kind": kind, "milestoneLabel": kind, "type": typ, "typeLabel": typ,
             "code": code, "name": NAMES.get(code), "importance": imp, "fields": fields or {}}
        if end:
            x["end"] = end
        return x
    rows = [
        r("2026-11-03", 3, "000001"),                                   # 과거 → 제외
        r("2026-11-04", 1, "000002", kind="record"),                    # 오늘(영향도 1도 표시)
        r("2026-11-04", 3, "005930", fields={"newSharesPct": 12.5, "issuePrice": 50000,
                                             "irrelevant": "x"}),
        r("2026-11-02", 3, "000660", typ="merger", kind="halt", end="2026-11-06"),  # 기간 → 오늘
        r("2026-11-05", 2, "000003", typ="bonus"),                      # 내일 ★2 → upcoming
        r("2026-11-05", 1, "000004", kind="record"),                    # 내일 ★1 → 제외
        r("2026-11-09", 3, "000005", typ="deposit", kind="release",
          fields={"pctOfTotal": 4.2}),                                  # 다음 주 해제 → 장중 제외
        r("2026-11-12", 3, "000006"),                                   # 6영업일 후 → 제외
        r("2026-11-06", 3, "000007", typ="deposit", kind="release",
          fields={"pctOfTotal": 1.5}),                                  # 이번 주(금) 해제
        r("2026-11-04", 1, "000009", typ="deposit", kind="deposit"),    # 보호예수 시작 → 제외
        r("2025-11-05", 3, "000008", typ="deposit", kind="deposit", end="2026-11-05",
          fields={"pctOfTotal": 9.0}),                                  # 구형 기간 행 → 11/5 해제로
        r("2026-10-30", 3, "000010", typ="deposit", kind="deposit", end="2029-10-30"),  # 구형·먼 해제 → 제외
    ]
    rows += [r("2026-11-06", 2, f"1000{i:02d}", typ="dividend") for i in range(15)]  # 일 상한
    return rows


def test_load_ca_events_today_and_deposit_week():
    """장중 시황 CA = 당일 일정만 + 이번 주 보호예수 해제(2026-11-05 사용자 결정)."""
    import generate_intraday_briefing as g
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "ca_calendar.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"asof": "2026-11-04 08:20 KST", "rows": _cal_rows()}, f, ensure_ascii=False)
    now = datetime.datetime(2026, 11, 4, 10, 7, tzinfo=KST)   # 수요일
    ca = g._load_ca_events(now, path=path)
    assert ca["date"] == "2026-11-04" and ca["asof"] == "2026-11-04 08:20 KST"
    assert "upcoming" not in ca and "horizonEnd" not in ca
    t_codes = [r["code"] for r in ca["today"]]
    assert set(t_codes) == {"000002", "005930", "000660"}, t_codes     # 내일·보호예수 시작/기간 행 없음
    assert ca["today"][-1]["code"] == "000002"            # 영향도 내림차순 → ★1 마지막
    assert ca["todayTotal"] == 3
    # 이번 주(11/2~11/8) 해제만 — 구형 기간 행은 반환일(11/5)을 해제로, 날짜순
    dep = ca["depositWeek"]
    assert [(r["code"], r["date"], r["kind"]) for r in dep] == [("000008", "2026-11-05", "release"),
                                                               ("000007", "2026-11-06", "release")], dep
    assert all(r["typeLabel"] == "보호예수" and r["milestoneLabel"] == "보호예수 해제" for r in dep)
    assert dep[0]["fields"]["releaseDate"] == "2026-11-05" and "end" not in dep[0]
    assert (ca["depositWeekTotal"], ca["weekFrom"], ca["weekTo"]) == (2, "2026-11-02", "2026-11-08")

    # LLM 축약: 영향도 2+, 오늘 + 이번 주 해제, 핵심 필드만
    view = g._ca_llm_view(ca)
    codes = {x["code"] for x in view}
    assert codes == {"005930", "000660", "000008", "000007"}, codes
    sam = [x for x in view if x["code"] == "005930"][0]
    assert sam["stock"] == "삼성전자" and sam["fields"] == {"newSharesPct": 12.5, "issuePrice": 50000}
    assert [x for x in view if x["code"] == "000660"][0]["end"] == "2026-11-06"
    assert len(view) <= g.CA_LLM_MAX

    # 보호예수만 있는 날도 카드 유지 / 파일 없음·빈 rows → {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rows": [r for r in _cal_rows() if r["code"] == "000007"]}, f)
    only = g._load_ca_events(now, path=path)
    assert only["today"] == [] and [r["code"] for r in only["depositWeek"]] == ["000007"]
    assert g._load_ca_events(now, path=os.path.join(tmp, "nope.json")) == {}
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"rows": []}, f)
    assert g._load_ca_events(now, path=path) == {}
    assert g._ca_llm_view({}) == []


def test_weekly_preview_ca_buckets():
    import generate_weekly_briefing as gw
    cal = {"rows": _cal_rows()}
    orig = gw._fetch
    gw._fetch = lambda rel: cal if rel == "ca_calendar.json" else {}
    try:
        pv = gw._next_week_preview(base=datetime.date(2026, 11, 4))
        rest, nxt = pv["thisRest"]["ca"], pv["next"]["ca"]
        # 이번 주 잔여 = 11/4~11/8, 다음 주 = 11/9~11/15, 영향도 2+
        assert all("2026-11-04" <= r["date"] <= "2026-11-08" for r in rest)
        assert all("2026-11-09" <= r["date"] <= "2026-11-15" for r in nxt)
        assert {r["code"] for r in nxt} == {"000005", "000006"}          # 다음 주 해제 보호예수 포함
        assert {r["code"] for r in rest if r["type"] == "deposit"} == {"000007", "000008"}  # 구형은 해제일로
        assert all(r["milestoneLabel"] == "보호예수 해제" for r in rest + nxt if r["type"] == "deposit")
        assert all(r["importance"] >= 2 for r in rest + nxt)
        assert "000002" not in {r["code"] for r in rest}          # ★1 제외
        assert "000660" not in {r["code"] for r in rest}          # 기준일 이전 시작(11/2) → 제외
        sam = [r for r in rest if r["code"] == "005930"][0]
        assert sam["fields"] == {"newSharesPct": 12.5, "issuePrice": 50000}
        assert [r["date"] for r in rest] == sorted(r["date"] for r in rest)
        # 상한: 영향도 우선으로 남김
        pv2 = gw._next_week_preview(base=datetime.date(2026, 11, 4), ca_n=3)
        r2 = pv2["thisRest"]["ca"]
        assert len(r2) == 3 and r2[0]["code"] == "005930", r2
        # 기존 econ/earnings 버킷 키 유지
        assert set(pv["thisRest"]) == {"econ", "earnings", "ca"}
        # 파일 없음 → 빈 리스트
        gw._fetch = lambda rel: None
        pv3 = gw._next_week_preview(base=datetime.date(2026, 11, 4))
        assert pv3["thisRest"]["ca"] == [] and pv3["next"]["ca"] == []
    finally:
        gw._fetch = orig


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok {t.__name__}")
    print(f"ALL PASS ({len(tests)} tests — CA calendar)")


if __name__ == "__main__":
    main()
