"""딥리서치 CA(예탁원 기업행위) — 결정적 피처·스키마·LLM 해석 병합·analyze_stock 배선 회귀 (오프라인).

실행: python tests/test_research_ca.py
허브 /ca 는 호출하지 않는다 — 명세 형태 픽스처. analyze_stock 은 모든 수집 leg·LLM 을 목으로 대체.
"""
import copy
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis import ca as ca_mod

D = datetime.date
HOL = {"2026-12-25", "2026-12-31"}          # 결정성 — 레포 휴장일 파일과 무관하게
BASE = "2026-11-04"                          # 수요일

# 현대제철(004020) 예시 — 미리보기와 같은 형태(실제 일정 아님)
PAYLOAD = {
    "status": "success", "code": "004020", "from": "20251104", "to": "20270503",
    "asof": "2026-11-04 09:00", "counts": {"listing": 1, "deposit": 1, "meeting": 1, "dividend": 1},
    "errors": {},
    "events": [
        {"type": "dividend", "label": "배당", "code": "004020", "name": None, "recordDate": "2026-12-31",
         "milestones": [{"kind": "record", "label": "기준일", "date": "2026-12-31"},
                        {"kind": "ex", "label": "배당락(추정)", "date": "2026-12-30"}],
         "fields": {"cashDps": 1000, "cashYieldPct": 3.47, "divKind": "결산"}},
        {"type": "meeting", "label": "주주총회", "code": "004020", "name": None, "recordDate": "2026-10-28",
         "milestones": [{"kind": "record", "label": "기준일", "date": "2026-10-28"},
                        {"kind": "meeting", "label": "임시주총", "date": "2026-11-27"}],
         "fields": {"agenda": "정관 변경(배당기준일 이사회 결정)"}},
        {"type": "deposit", "label": "의무예치", "code": "004020", "name": None, "recordDate": None,
         "milestones": [{"kind": "deposit", "label": "의무예치", "date": "2026-11-13"}],
         "fields": {"shares": 2500000, "pctOfTotal": 1.87, "reason": "제3자배정 유상증자 의무보유 반환"}},
        {"type": "listing", "label": "상장정보", "code": "004020", "name": None, "recordDate": None,
         "milestones": [{"kind": "listing", "label": "추가 상장", "date": "20261110", "note": "전환사채 전환"}],
         "fields": {"reason": "전환사채 전환", "shares": 4200000, "pctOfTotal": 3.15, "issuePrice": "24,500",
                    "nested": {"x": 1}}},
        # 60일보다 오래 끝난 CA → 제외 (cutoff = 2026-09-05)
        {"type": "dividend", "label": "배당", "code": "004020", "recordDate": "2026-06-30",
         "milestones": [{"kind": "ex", "label": "배당락(추정)", "date": "2026-06-29"}], "fields": {}},
        "garbage", {"type": "bonus", "milestones": [{"kind": "ex", "date": "bad"}]},   # 비정상 → 제외
    ],
}


def _feat(payload=PAYLOAD, close=28850, date=BASE):
    return ca_mod.features(copy.deepcopy(payload), date, close, holidays=HOL)


def test_features_hyundai_steel():
    f = _feat()
    assert f["status"] == "ok" and f["date"] == BASE and f["windowDays"] == 20
    assert f["windowEnd"] == "2026-12-02", f["windowEnd"]
    assert f["close"] == 28850.0
    types = [x["type"] for x in f["items"]]
    assert types == ["listing", "deposit", "meeting", "dividend"], types   # 구간 내 → D 순
    lst, dep, mt, dv = f["items"]
    assert lst["stage"] == "추가 상장 D+4" and lst["inWindow"] is True
    assert lst["sizePct"] == 3.15 and lst["gapPct"] == 17.76 and lst["gapRef"] == "발행가"
    assert lst["milestones"][0]["date"] == "2026-11-10" and lst["milestones"][0]["note"] == "전환사채 전환"
    assert "nested" not in lst["fields"]
    assert dep["stage"] == "의무예치 D+7" and dep["sizePct"] == 1.87 and "gapPct" not in dep
    assert mt["stage"] == "임시주총 D+17" and mt["inWindow"] is True
    assert [m["d_n"] for m in mt["milestones"]] == [-5, 17]               # 기준일(과거) 포함, 정렬
    # 배당: 배당락(추정) 12/30 = D+39(12/25 휴장 제외), 기준일 12/31(휴장)도 같은 영업일 수
    assert dv["stage"] == "배당락(추정) D+39" and dv["inWindow"] is False, dv["stage"]
    assert [m["label"] for m in dv["milestones"]] == ["배당락(추정)", "기준일"]
    assert dv["milestones"][1]["d_n"] == 39
    assert f["inWindow"] == 3 and f["haltInWindow"] is False
    assert len(f["items"]) == 4                                           # 오래된·비정상 제외


def test_features_halt_past_and_record_only():
    payload = {"events": [
        {"type": "merger", "label": "합병/분할",
         "milestones": [{"kind": "halt", "label": "매매정지", "date": "2026-11-02", "end": "2026-11-06"},
                        {"kind": "listing", "label": "재상장", "date": "2026-11-09"}],
         "fields": {"buyReqPrice": 30000}},
        {"type": "paidin", "label": "유상증자",
         "milestones": [{"kind": "listing", "label": "신주 상장", "date": "2026-09-20"}], "fields": {}},
        {"type": "meeting", "label": "주주총회", "recordDate": "2026-12-15", "milestones": []},
        {"type": "revsplit", "label": "액면교체",
         "milestones": [{"kind": "halt", "label": "매매정지", "date": "2026-12-10", "end": "2026-12-20"}]},
    ]}
    f = _feat(payload, close=27000)
    by = {x["type"]: x for x in f["items"]}
    mg = by["merger"]
    assert mg["stage"] == "매매정지 진행 중" and mg["inWindow"] is True
    assert mg["haltInWindow"] == {"from": "2026-11-02", "to": "2026-11-06"}
    assert mg["milestones"][0]["d_n"] == -2 and mg["milestones"][0]["d_n_end"] == 2
    assert mg["gapPct"] == -10.0 and mg["gapRef"] == "매수청구가"
    pi = by["paidin"]                                                     # 9/20 종료 — 60일 이내라 유지
    assert pi["stage"] == "일정 종료(과거)" and pi["inWindow"] is False and pi["nextMilestone"] is None
    rec = by["meeting"]                                                   # 기준일만 있는 일정
    assert rec["stage"].startswith("기준일 D+") and rec["inWindow"] is False
    assert by["revsplit"]["haltInWindow"] is None                         # 창(~12/2) 밖 정지
    assert f["haltInWindow"] is True
    # 정렬: 구간 내 먼저
    assert f["items"][0]["type"] == "merger"
    # close 없음 → gapPct 생략
    assert "gapPct" not in _feat(payload, close=None)["items"][0]


def test_features_empty_and_base_date():
    f = ca_mod.features({"events": []}, BASE, 1000, holidays=HOL)
    assert f["status"] == "empty" and f["items"] == [] and f["inWindow"] == 0
    assert ca_mod.features(None, BASE, None, holidays=HOL)["status"] == "empty"
    assert ca_mod.base_date(D(2026, 11, 7), holidays=set()) == "2026-11-06"           # 토 → 금
    assert ca_mod.base_date(D(2026, 12, 25), holidays=HOL) == "2026-12-24"            # 휴장 → 직전
    kst = datetime.timezone(datetime.timedelta(hours=9))
    assert ca_mod.base_date(datetime.datetime(2026, 11, 4, 1, 0, tzinfo=kst), holidays=set()) == BASE
    # UTC 전날 밤 = KST 오늘
    utc = datetime.timezone.utc
    assert ca_mod.base_date(datetime.datetime(2026, 11, 3, 16, 0, tzinfo=utc), holidays=set()) == BASE


def test_llm_view_slim():
    v = ca_mod.llm_view(_feat())
    assert v["status"] == "ok" and v["baseDate"] == BASE and v["windowDays"] == 20
    x = v["items"][0]
    assert set(x) >= {"type", "stage", "inWindow", "milestones", "fields", "sizePct", "gapPct", "gapRef"}
    assert "nextMilestone" not in x and x["milestones"][0]["d_n"] == 4
    u = ca_mod.llm_view(ca_mod.unavailable("HTTPError 404"))
    assert u["status"] == "unavailable" and u["items"] == [] and "404" in u["error"]


def test_schema_includes_corporate_actions():
    import generate_analysis as ga
    for sch in (ga.ANALYSIS_SCHEMA, ga.ANALYSIS_SCHEMA_FU):
        p = sch["properties"]
        assert "corporateActions" in p and "corporateActions" in sch["required"]
        it = p["corporateActions"]["properties"]["items"]["items"]
        assert set(it["required"]) == {"type", "stage", "direction", "magnitude", "window",
                                       "implication", "rationale"}
        assert it["properties"]["direction"]["enum"] == ["호재", "악재", "중립"]
        assert it["properties"]["magnitude"]["enum"] == ["작음", "중간", "큼"]
    assert "followUp" not in ga.ANALYSIS_SCHEMA["properties"]
    assert "corporate_actions" in ga.SYSTEM and "corporate_actions" in ga.REFINE_SYSTEM
    assert "corporateActions 도 1차와 같은" in ga.REFINE_SYSTEM


def test_merge_interp():
    f = _feat()
    llm = {"summary": "전환 물량 오버행이 핵심.", "items": [
        {"type": "deposit", "stage": "의무예치 D+7", "direction": "악재"},
        {"type": "listing", "stage": "추가 상장 D+4", "direction": "악재"},
        {"type": "ipo", "stage": "x", "direction": "중립"},                 # 피처에 없는 type → 무시
        {"type": "meeting", "stage": "임시주총 D+17", "direction": "중립"},
    ]}
    m = ca_mod.merge_interp(llm, f)
    assert m["summary"] == "전환 물량 오버행이 핵심."
    assert [b and b["type"] for b in m["byItem"]] == ["listing", "deposit", "meeting", None]
    # 같은 type 여러 건 — 순서 매칭, 모자라면 None
    two = {"status": "ok", "items": [{"type": "dividend"}, {"type": "dividend"}]}
    m2 = ca_mod.merge_interp({"items": [{"type": "dividend", "window": "a"}]}, two)
    assert m2["byItem"][0]["window"] == "a" and m2["byItem"][1] is None
    # 일정 없음 / 수집 실패 — LLM 이 지어낸 행 차단 + 규칙 문구 고정
    e = ca_mod.merge_interp(llm, {"status": "empty", "items": []})
    assert e == {"summary": ca_mod.EMPTY_SUMMARY, "items": [], "byItem": []}
    u = ca_mod.merge_interp(llm, ca_mod.unavailable("x"))
    assert u["summary"] == ca_mod.FAIL_SUMMARY and u["byItem"] == []
    assert ca_mod.merge_interp(None, f)["byItem"] == [None] * 4         # LLM 누락에도 안전


def test_build_and_attach():
    import generate_analysis as ga
    u = ga.build_ca_features(None, RuntimeError("hub 404"), 1000)
    assert u["status"] == "unavailable" and "hub 404" in u["error"]
    orig = ga.ca_mod.features
    try:
        ga.ca_mod.features = lambda *a, **k: (_ for _ in ()).throw(ValueError("boom"))
        assert ga.build_ca_features(PAYLOAD, None, 1000)["status"] == "unavailable"
    finally:
        ga.ca_mod.features = orig
    ok = ga.build_ca_features(copy.deepcopy(PAYLOAD), None, 28850,
                              now=datetime.date(2026, 11, 4))
    assert ok["status"] == "ok" and ok["date"] == BASE
    a = ga._attach_ca({"corporateActions": {"summary": "s", "items": [{"type": "listing"}]}}, ok)
    assert a["caStatus"] == "ok" and a["ca"] is ok
    assert len(a["corporateActions"]["byItem"]) == len(ok["items"])
    assert json.loads(json.dumps(a, ensure_ascii=False))["caStatus"] == "ok"   # 캐시·응답 직렬화 가능


def test_analyze_stock_wiring():
    """analyze_stock 배선 — 수집 leg·LLM 전부 목. CA 성공/실패 두 경로."""
    import generate_analysis as ga
    seen = {}

    def fake_llm(system, user, max_tokens=None, schema=None, return_model=False):
        seen["user"], seen["schema"] = user, schema
        out = {"catalyst": "c", "direction": "neutral", "flowComment": "수급 데이터 없음",
               "peers": {"summary": "", "reddit": [], "stocktwits": []},
               "news": {"summary": "", "items": []},
               "community": {"summary": "", "sentimentLabel": "", "naver": []},
               "dart": {"summary": "", "highlights": []}, "research": {"summary": "", "items": []},
               "corporateActions": {"summary": "LLM 요약", "items": [
                   {"type": "listing", "stage": "추가 상장 D+4", "direction": "악재", "magnitude": "중간",
                    "window": "D-3~D+2", "implication": "매수 감점", "rationale": "r"}]}}
        return (out, "fake-model") if return_model else out

    patches = {
        (ga, "resolve_peers"): lambda *a, **k: [],
        (ga, "fetch_investor_flow"): lambda *a, **k: {},
        (ga, "_fetch_base_price"): lambda *a, **k: None,
        (ga, "fetch_peer_reddit"): lambda *a, **k: {"posts": [], "status": "empty", "queried": []},
        (ga.social, "stocktwits_for_peers"): lambda *a, **k: [],
        (ga.sources, "naver_search"): lambda *a, **k: [],
        (ga.sources, "web_search"): lambda *a, **k: [],
        (ga.sources, "naver_board"): lambda *a, **k: [],
        (ga.sources, "combined_research"): lambda *a, **k: [],
        (ga.sources, "naver_investor_trend_full"): lambda *a, **k: [],
        (ga.sources, "dart_corp_code"): lambda *a, **k: None,
        (ga.sources, "get_peer_quotes"): lambda *a, **k: [],
        (ga.llm, "generate_json"): fake_llm,
        (ga.ca_mod, "fetch"): lambda code, **k: copy.deepcopy(PAYLOAD),
        (ga.ca_mod, "base_date"): lambda now=None, holidays=None: BASE,
    }
    saved = {k: getattr(*k) for k in patches}
    stock = {"code": "004020", "name": "현대제철", "market": "KOSPI", "basePrice": 28850}
    try:
        for (mod, name), fn in patches.items():
            setattr(mod, name, fn)
        a = ga.analyze_stock(dict(stock), {})
        assert '"corporate_actions"' in seen["user"] and "추가 상장 D+" in seen["user"]
        assert seen["schema"] is ga.ANALYSIS_SCHEMA
        assert a["caStatus"] == "ok" and a["ca"]["items"][0]["type"] == "listing"
        assert a["ca"]["items"][0]["gapPct"] == round((28850 / 24500 - 1) * 100, 2)
        assert a["corporateActions"]["byItem"][0]["implication"] == "매수 감점"
        assert a["corporateActions"]["byItem"][1] is None
        assert a["dart"]["recentFilings"] == []                            # 기존 후처리 유지

        def boom(code, **k):
            raise RuntimeError("HTTP 404 /ca")
        ga.ca_mod.fetch = boom
        b = ga.analyze_stock(dict(stock), {})
        assert b["caStatus"] == "unavailable" and b["ca"]["items"] == []
        assert b["corporateActions"] == {"summary": ca_mod.FAIL_SUMMARY, "items": [], "byItem": []}
        assert '"status": "unavailable"' in seen["user"]
    finally:
        for (mod, name), fn in saved.items():
            setattr(mod, name, fn)


def test_ca_fail_short_cache():
    """CA 수집 실패(_caOk=False) 결과는 FLOW_RETRY_TTL(2분) 뒤 캐시 만료 — 허브 복구 후 재리서치(2026-11-04)."""
    import time as _t
    import railway_server as rs
    ga_mod = __import__("generate_analysis")
    ok = ga_mod._attach_ca({}, {"status": "empty", "items": []})
    bad = ga_mod._attach_ca({}, {"status": "unavailable", "items": []})
    assert ok["_caOk"] is True and bad["_caOk"] is False
    now = _t.time()
    with rs._CACHE_LOCK:
        rs._CACHE["T1"] = {"result": {"_flowOk": True, "_caOk": False}, "ts": now - rs.FLOW_RETRY_TTL - 1}
        rs._CACHE["T2"] = {"result": {"_flowOk": True, "_caOk": False}, "ts": now - 10}
        rs._CACHE["T3"] = {"result": {"_flowOk": True, "_caOk": True}, "ts": now - rs.FLOW_RETRY_TTL - 1}
        rs._CACHE["T4"] = {"result": {"_flowOk": True}, "ts": now - rs.FLOW_RETRY_TTL - 1}   # CA 이전 결과
    try:
        assert rs._cached("T1") is None                  # 실패 + 2분 경과 → 재리서치
        assert rs._cached("T2") is not None              # 실패지만 2분 안 → 캐시
        assert rs._cached("T3") is not None              # 정상 → 6시간 캐시
        assert rs._cached("T4") is not None              # _caOk 없는 구버전 결과도 그대로
    finally:
        with rs._CACHE_LOCK:
            for k in ("T1", "T2", "T3", "T4"):
                rs._CACHE.pop(k, None)


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok {t.__name__}")
    print(f"ALL PASS ({len(tests)} tests — research CA)")


if __name__ == "__main__":
    main()
