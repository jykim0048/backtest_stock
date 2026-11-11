"""전종목 원본 수집·저장(collect_stock_data.py · stock_db.py) 회귀 — 오프라인.

실행: python tests/test_stock_data_collect.py
허브·네이버·DART·Postgres 는 부르지 않는다 — 응답 형태 픽스처와 가짜 커서.
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import collect_stock_data as C
import stock_db as S

FLOW = {  # 허브 /flow 응답 형태(generate_weekly_briefing._flow_raw · backfill_netbuy_history 가 읽는 키)
    "status": "success",
    "daily": [{"date": "20261110", "close": 7820, "rate": 13.17, "prsn": -1200.5, "frgn": 800.0, "orgn": 400.5, "fund": 10.0,
               "scrt": 5.0, "insu": 1.0, "ivtr": 300.0, "pe": 50.0, "etc": 0.0},
              {"date": "20261109", "close": 6910, "rate": 14.59, "prsn": 500.0, "frgn": -6400.0, "orgn": 6900.0}],
    "shorts": [{"date": "20261110", "qty": 438102.0, "volRlim": 6.98, "pbmn": 30.3, "pbmnRlim": 7.0}],
    "loans": [{"date": "20261110", "newQty": 566576.0, "rdmpQty": 349316.0, "rmndChg": 217260.0, "rmndQty": 9364693.0, "rmndAmt": 636.8},
              {"date": "20261106", "newQty": 1.0, "rdmpQty": 2.0, "rmndChg": -1.0, "rmndQty": 9000000.0, "rmndAmt": 600.0}],
}
NAVER = [  # m.stock.naver.com trend 응답 형태
    {"bizdate": "20261110", "foreignerPureBuyQuant": "+102,345", "organPureBuyQuant": "-50,000", "individualPureBuyQuant": "-52,345",
     "foreignerHoldRatio": "13.22%", "closePrice": "7,820", "compareToPreviousClosePrice": "910"},
    {"bizdate": "20261105", "foreignerPureBuyQuant": "-1", "organPureBuyQuant": "2", "individualPureBuyQuant": "-1",
     "foreignerHoldRatio": "14.00%", "closePrice": "6,000", "compareToPreviousClosePrice": "-30"},
    {"bizdate": "", "foreignerPureBuyQuant": "1"},
]


def test_flow_rows_merges_daily_shorts_loans():
    m = C.flow_rows("036540", FLOW)
    assert sorted(m) == ["2026-11-06", "2026-11-09", "2026-11-10"]
    d = m["2026-11-10"]
    assert d["amt_frgn"] == 800.0 and d["amt_etc"] == 0.0 and d["close"] == 7820 and d["change_pct"] == 13.17
    assert d["short_qty"] == 438102.0 and d["short_amt_pct"] == 7.0 and d["loan_bal_qty"] == 9364693.0
    assert "amt_fund" not in m["2026-11-09"] and "short_qty" not in m["2026-11-09"]      # 없는 값은 키 자체가 없다(COALESCE 로 보존)
    assert set(m["2026-11-06"]) == {"code", "date", "loan_new", "loan_rdmp", "loan_bal_chg", "loan_bal_qty", "loan_bal_amt"}
    assert C.flow_rows("036540", {}) == {} and C.flow_rows("036540", None) == {}


def test_naver_rows_and_merge():
    n = C.naver_rows("036540", NAVER)
    assert sorted(n) == ["2026-11-05", "2026-11-10"]
    assert n["2026-11-10"]["qty_frgn"] == 102345 and n["2026-11-10"]["qty_orgn"] == -50000 and n["2026-11-10"]["hold_ratio"] == 13.22
    assert n["2026-11-10"]["change_pct"] == round(910 / (7820 - 910) * 100, 2)
    assert n["2026-11-05"]["change_pct"] == round(-30 / 6030 * 100, 2)
    assert C.naver_rows("036540", {"error": 1}) == {}
    rows = C.merge_rows(C.flow_rows("036540", FLOW), n)
    by = {r["date"]: r for r in rows}
    assert [r["date"] for r in rows] == sorted(by)                                         # 날짜 오름차순
    assert by["2026-11-10"]["change_pct"] == 13.17 and by["2026-11-10"]["qty_frgn"] == 102345   # KIS 값 우선, 네이버가 빈 칸만 채움
    assert by["2026-11-05"]["qty_prsn"] == -1 and "amt_frgn" not in by["2026-11-05"]


def test_dart_rows_and_ca_key():
    items = [{"stock_code": "005930", "rcept_no": "20261110000123", "rcept_dt": "20261110", "corp_name": "삼성전자",
              "report_nm": "주요사항보고서(자기주식취득결정)  ", "flr_nm": "삼성전자"},
             {"stock_code": "", "rcept_no": "20261110000124", "rcept_dt": "20261110", "report_nm": "비상장"},
             {"stock_code": "000660", "rcept_no": "", "rcept_dt": "20261110", "report_nm": "번호 없음"}]
    rows = C.dart_rows(items, "Y")
    assert len(rows) == 1 and rows[0]["date"] == "2026-11-10" and rows[0]["title"] == "주요사항보고서(자기주식취득결정)" and rows[0]["corp_cls"] == "Y"
    cal = [{"date": "2026-12-30", "kind": "ex", "milestoneLabel": "배당락(추정)", "type": "dividend", "typeLabel": "배당",
            "code": "004020", "name": "현대제철", "importance": 2, "fields": {"cashDps": 1000}},
           {"date": "2026-12-30", "kind": "ex", "type": "dividend", "code": ""}]
    out = C.ca_rows(cal, "2026-11-11")
    assert len(out) == 1 and len(out[0]["event_key"]) == 40 and out[0]["seen"] == "2026-11-11" and out[0]["payload"]["fields"] == {"cashDps": 1000}
    assert C.ca_key(cal[0]) == C.ca_key(dict(cal[0], importance=3, fields={}))             # 내용이 바뀌어도 같은 일정은 같은 키
    assert C.ca_key(cal[0]) != C.ca_key(dict(cal[0], date="2026-12-29"))                   # 날짜가 바뀌면 새 키(옛 키는 last_seen 정지)


def test_universe():
    uni = C.build_universe(mode="index")
    assert 300 <= len(uni) <= 400 and all(len(c) == 6 for c in uni) and "005930" in uni
    arch = C.build_universe(mode="archive")
    assert set(uni) <= set(arch)
    listed = C.load_listed()
    assert listed and set(arch) <= listed and "000060" not in arch            # 상장폐지 종목(메리츠화재)은 빠진다
    saved = C.load_listed
    C.load_listed = lambda root=C.ROOT: None                                   # 상장 목록을 못 읽으면 필터하지 않는다
    try:
        assert len(C.build_universe(mode="archive")) >= len(arch)
    finally:
        C.load_listed = saved


def test_normalize():
    f = S.normalize_flow({"code": "36540", "date": "20261110", "amt_frgn": "1,234.5", "qty_frgn": 10.0, "bogus": 1, "close": None})
    assert f["code"] == "036540" and f["trade_date"] == "2026-11-10" and f["amt_frgn"] == 1234.5 and f["qty_frgn"] == 10
    assert "bogus" not in f and f["close"] is None and set(f) == {"code", "trade_date", *S.FLOW_COLS}
    assert S.normalize_flow({"code": "036540", "date": "2026-11-10"}) is None            # 값 없는 행
    assert S.normalize_flow({"code": "x", "date": "2026-11-10", "close": 1}) is None
    assert S.normalize_flow({"code": "036540", "date": "2026-13-40", "close": 1}) is None
    assert S.normalize_flow({"code": "0220W0", "date": "2026-11-10", "close": 1})["code"] == "0220W0"   # 영문 혼합 코드
    assert S.normalize_flow({"code": "036540", "date": "2026-11-10", "close": float("nan"), "amt_etc": 0})["amt_etc"] == 0.0
    tag = lambda t: ("bullish", "buyback") if "자기주식취득결정" in t else (None, None)
    d = S.normalize_disclosure({"rcept_no": "20261110000123", "stock_code": "005930", "date": "2026-11-10",
                                "title": "주요사항보고서(자기주식취득결정)", "corp_cls": "Y"}, tag)
    assert d["rule_dir"] == "bullish" and d["rule_kind"] == "buyback" and d["filed_date"] == "2026-11-10"
    assert S.normalize_disclosure({"rcept_no": "123", "stock_code": "005930", "date": "2026-11-10", "title": "t"}) is None
    assert S.normalize_disclosure({"rcept_no": "20261110000123", "stock_code": "005930", "date": "2026-11-10", "title": ""}) is None
    k = "a" * 40
    c = S.normalize_ca({"event_key": k, "code": "004020", "type": "dividend", "kind": "ex", "date": "2026-12-30", "seen": "2026-11-11",
                        "importance": "2", "payload": {"fields": {"x": 1}}})
    assert c["importance"] == 2 and c["payload"] == '{"fields": {"x": 1}}' and c["seen"] == "2026-11-11"
    assert S.normalize_ca({"event_key": "zz", "code": "004020", "type": "dividend", "date": "2026-12-30", "seen": "2026-11-11", "payload": {}}) is None
    assert S.normalize_ca({"event_key": k, "code": "004020", "type": "dividend", "date": "2026-12-30", "seen": "2026-11-11", "payload": "x"}) is None
    assert S.normalize_run({"date": "2026-11-11", "part": "flow", "universe": 520, "ok": 500, "failed": 20, "rows": 9000})["ok"] == 500
    assert S.normalize_run({"date": "2026-11-11", "part": "drop table"}) is None


def test_real_tagger_from_sources():
    """서버에서 쓰는 실제 규칙 태그(analysis.sources.dart_rule_direction) — 임포트되면 유상증자는 bearish."""
    t = S._tagger()
    if t is None:                                   # 경량 환경(의존성 없음)에서는 태그 없이 동작해야 한다
        assert S.normalize_disclosure({"rcept_no": "20261110000123", "stock_code": "005930", "date": "2026-11-10", "title": "t"})["rule_dir"] is None
        return
    assert t("주요사항보고서(유상증자결정)") == ("bearish", "dilution") and t("분기보고서") == (None, None)


class _Cur:
    def __init__(self):
        self.calls = []

    def execute(self, sql, args=None):
        self.calls.append((sql, args))

    def mogrify(self, tpl, args):
        return ("(" + ",".join(repr(a) for a in args) + ")").encode("utf-8")

    connection = type("C", (), {"encoding": "UTF8"})()


def test_upsert_rows_sql_and_dedup():
    """upsert_rows — 검증 통과 행만, 같은 키는 마지막 것만, 수급은 컬럼별 COALESCE(다른 소스가 채운 값 보존)."""
    sql, cols = S.flow_upsert_sql()
    assert cols[:2] == ("code", "trade_date") and len(cols) == 2 + len(S.FLOW_COLS)
    assert "amt_frgn = COALESCE(EXCLUDED.amt_frgn, stock_flow_daily.amt_frgn)" in sql and "ON CONFLICT (code, trade_date)" in sql
    seen = {}

    def fake_run(op, *a):
        import psycopg2.extras as ex
        orig = ex.execute_values
        ex.execute_values = lambda cur, sql, rows, template=None, page_size=100: seen.update(sql=sql, rows=list(rows), template=template)
        try:
            return op(_Cur(), *a)
        finally:
            ex.execute_values = orig
    saved = S.report_db._run
    S.report_db._run = fake_run
    try:
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            print("    (psycopg2 없음 — SQL 실행 경로 생략)")
            return
        out = S.upsert_rows("flow", [{"code": "036540", "date": "2026-11-10", "close": 1},
                                     {"code": "036540", "date": "20261110", "close": 2},        # 같은 키 → 마지막 것
                                     {"code": "bad", "date": "2026-11-10", "close": 3}, "문자열"])
        assert out == {"accepted": 1, "rejected": 2} and len(seen["rows"]) == 1 and seen["rows"][0][cols.index("close")] == 2.0
        out = S.upsert_rows("ca", [{"event_key": "b" * 40, "code": "004020", "type": "dividend", "date": "2026-12-30",
                                    "seen": "2026-11-11", "payload": {"a": 1}}])
        assert out == {"accepted": 1, "rejected": 0} and "LEAST(stock_ca_events.first_seen" in seen["sql"] and "::jsonb" in seen["template"]
        assert seen["rows"][0][-2:] == ("2026-11-11", "2026-11-11")
        out = S.upsert_rows("disclosures", [{"rcept_no": "20261110000123", "stock_code": "005930", "date": "2026-11-10", "title": "t"}])
        assert out["accepted"] == 1 and "DO NOTHING" in seen["sql"]
        assert S.upsert_rows("users", []) is None and S.upsert_rows("flow", "x") is None
        assert S.upsert_rows("flow", [{"code": "bad"}]) == {"accepted": 0, "rejected": 1}
    finally:
        S.report_db._run = saved


def test_main_guards_and_dry_run():
    """main — 장 마감 전이면 수급 파트를 건너뛰고(외부 호출 없음), dry-run 은 전송하지 않는다."""
    calls = []
    saved = (C.collect_flow, C.collect_dart, C.collect_ca, C.post_rows, C._is_trading_day)
    saved_new = (C.collect_financials, C.collect_holders, C.collect_research, C.load_corp_map)
    saved_val = C.collect_valuation
    C.collect_valuation = lambda codes, date, workers=6: (calls.append(("valuation", len(codes), date)) or
                                                         ([{"kind": "valuation", "code": "005930", "date": date, "payload": {"per": 1}}],
                                                          {"ok": len(codes), "failed": 0, "errs": []}))
    C.load_corp_map = lambda root=None: {"005930": "00126380", "000660": "00164779"}
    C.collect_financials = lambda corps, periods, on_chunk=None, chunk=100, workers=4: (
        calls.append(("financials", len(corps), len(periods))) or ([], 1))
    C.collect_holders = lambda pairs, workers=4, on_chunk=None, chunk=200: (calls.append(("holders", [c for c, _ in pairs])) or
                                                                            {"ok": len(pairs), "failed": 0, "rows": 0, "errs": []})
    C.collect_research = lambda codes, cutoff, pages=1, workers=6, detail=True, on_chunk=None, **k: (
        calls.append(("research", len(codes), cutoff, pages)) or
        {"ok": len(codes), "failed": 0, "rows": 0, "detail_ok": 0, "detail_failed": 0, "max_pages": 1, "errs": []})
    C.collect_flow = lambda codes, parts, *a, **k: (calls.append(("flow", len(codes), tuple(parts))) or
                                                    ([{"code": "036540", "date": "2026-11-10", "close": 1}],
                                                     {p: {"ok": len(codes), "failed": 0, "rows": 1} for p in parts}))
    def fake_dart(bgn, end, on_chunk=None, chunk_days=30):
        calls.append(("dart", str(bgn), str(end)))
        on_chunk([{"rcept_no": "1", "stock_code": "005930", "title": "주식등의대량보유상황보고서(약식)"},
                  {"rcept_no": "2", "stock_code": "000660", "title": "분기보고서"}], bgn, end)      # 창 하나씩 넘긴다
        return [], 2
    C.collect_dart = fake_dart
    C.collect_ca = lambda today, f=None, t=None: (calls.append(("ca", f, t)) or ([], 0, [], {}))
    posted = []
    C.post_rows = lambda table, rows, dry=False: (posted.append((table, len(rows), dry)) or (len(rows), 0))
    C._is_trading_day = lambda d: True
    try:
        now = datetime.datetime.now(C.KST)
        rc = C.main(["--dry-run", "--limit", "3", "--dart-from", "2026-11-01", "--dart-to", "2026-11-10"])
        assert rc == 0
        kinds = [c[0] for c in calls]
        assert ("flow" in kinds) == ((now.hour, now.minute) >= (15, 40))                 # 마감 전에는 수급 생략
        assert ("dart", "2026-11-01", "2026-11-10") in calls and ("ca", None, None) in calls
        assert all(dry for _, _, dry in posted) and not any(t == "runs" for t, _, _ in posted)   # dry-run 은 실행 기록도 안 보냄
        assert ("holders", ["005930"]) in calls                                          # 공시 제목에 '대량보유'가 뜬 종목만 다시 받는다
        fin = [c for c in calls if c[0] == "financials"][0]
        assert fin[1] == 2 and fin[2] == len(C.fin_periods(now.date()))                  # 고유번호 2개 × 최근 결산 보고서
        assert [c for c in calls if c[0] == "research"][0][3] == 1
        assert ("valuation", 3, now.date().isoformat()) in calls and ("snapshots", 1, True) in posted   # 그날 날짜로 스냅샷
        calls.clear(); posted.clear()
        rc = C.main(["--parts", "flow,naver", "--force", "--limit", "3"])
        assert rc == 0 and calls[0][0] == "flow" and calls[0][1] == 3 and calls[0][2] == ("flow", "naver")
        assert [t for t, _, _ in posted] == ["flow", "runs"]
        # 커버리지 미달이면 실패 종료
        C.collect_flow = lambda codes, parts, *a, **k: ([], {p: {"ok": 1, "failed": len(codes) - 1, "rows": 0} for p in parts})
        assert C.main(["--parts", "flow", "--force", "--limit", "10"]) == 1
        # 휴장일이면 수급 생략
        C._is_trading_day = lambda d: False
        calls.clear()
        assert C.main(["--parts", "flow"]) == 0 and calls == []
    finally:
        C.collect_flow, C.collect_dart, C.collect_ca, C.post_rows, C._is_trading_day = saved
        C.collect_financials, C.collect_holders, C.collect_research, C.load_corp_map = saved_new
        C.collect_valuation = saved_val


def test_ca_windows_and_backfill():
    """CA 백필 — 넓은 구간은 36일 창·28일 간격으로 나눠 창마다 적재하고, 한 창이 실패해도 나머지는 계속한다."""
    assert C.ca_windows() == [(None, None)] and C.ca_windows("20261101", None) == [("20261101", None)]
    assert C.ca_windows("20261028", "20261226") == [("20261028", "20261226")]              # 기본 창(59일)은 한 번에
    wins = C.ca_windows("20251111", "20261111")
    d = lambda s: datetime.datetime.strptime(s, "%Y%m%d").date()
    assert wins[0][0] == "20251104" and wins[-1][1] == "20261111"                          # 첫 창은 7일 앞에서, 끝은 to 에서
    assert all((d(t) - d(f)).days <= C.CA_WINDOW_DAYS - 1 for f, t in wins)
    assert all((d(b[0]) - d(a[0])).days == C.CA_STEP_DAYS for a, b in zip(wins, wins[1:]))
    # 의무예치 해제 목표(창 시작 +7 ~ +35일)가 빈틈 없이 이어진다
    assert all(d(b[0]) + datetime.timedelta(days=7) <= d(a[0]) + datetime.timedelta(days=35) + datetime.timedelta(days=1)
               for a, b in zip(wins, wins[1:]))

    narrow = C.ca_windows("20251111", "20251210", 10)                                    # --ca-window: 겹치지 않는 좁은 창
    assert narrow == [("20251111", "20251120"), ("20251121", "20251130"), ("20251201", "20251210")]
    assert C.ca_windows("20251111", "20251115", 10) == [("20251111", "20251115")]

    calls, posted = [], []
    saved = (C.collect_ca, C.post_rows, C.time.sleep)

    def fake_ca(today, f=None, t=None):
        calls.append((f, t))
        if len(calls) == 2:
            raise RuntimeError("timeout")
        return [{"event_key": f"k{len(calls)}"}], 3, [], ({"dividend": ["20251231"]} if len(calls) == 3 else {})
    C.collect_ca = fake_ca
    C.post_rows = lambda table, rows, dry=False: (posted.append((table, rows, dry)) or (len(rows), 0))
    C.time.sleep = lambda s: None
    try:
        rc = C.main(["--parts", "ca", "--ca-from", "20251111", "--ca-to", "20261111"])
        assert rc == 1 and calls == wins                                                   # 실패 창이 있으면 실패 종료, 그래도 끝까지 돈다
        ca_posts = [p for p in posted if p[0] == "ca"]
        assert len(ca_posts) == len(wins) - 1
        run = [p for p in posted if p[0] == "runs"][0][1][0]
        assert run["ok"] == len(wins) - 1 and run["failed"] == 1 and run["rows"] == len(wins) - 1
        assert "truncated 1" in run["note"] and "실패창" in run["note"] and len(run["note"]) <= 200
    finally:
        C.collect_ca, C.post_rows, C.time.sleep = saved


FIN = [  # DART fnlttMultiAcnt 응답 형태(다중회사 주요계정)
    {"rcept_no": "20260315000123", "bsns_year": "2025", "stock_code": "005930", "reprt_code": "11011", "account_nm": "자산총계",
     "fs_div": "CFS", "sj_div": "BS", "thstrm_dt": "2025.12.31 현재", "thstrm_amount": "514,531,948,000,000",
     "frmtrm_amount": "455,905,980,000,000", "bfefrmtrm_amount": "448,424,507,000,000", "currency": "KRW"},
    {"rcept_no": "20260315000123", "bsns_year": "2025", "stock_code": "005930", "reprt_code": "11011", "account_nm": "매출액",
     "fs_div": "CFS", "sj_div": "IS", "thstrm_dt": "2025.01.01 ~ 2025.12.31", "thstrm_amount": "300,870,903,000,000",
     "frmtrm_amount": "258,935,494,000,000", "currency": "KRW"},
    {"rcept_no": "20260315000123", "bsns_year": "2025", "stock_code": "005930", "reprt_code": "11011", "account_nm": "당기순이익(손실)",
     "fs_div": "CFS", "sj_div": "IS", "thstrm_amount": "-1,000", "thstrm_add_amount": "-", "currency": "KRW"},
    {"rcept_no": "20260315000123", "bsns_year": "2025", "stock_code": "005930", "reprt_code": "11011", "account_nm": "법인세차감전 순이익",
     "fs_div": "OFS", "sj_div": "IS", "thstrm_amount": "5", "currency": "KRW"},
    {"bsns_year": "2025", "stock_code": "", "reprt_code": "11011", "account_nm": "매출액", "fs_div": "CFS", "thstrm_amount": "1"},
]
HOLD = [{"rcept_no": "20261105000321", "rcept_dt": "2026-11-05", "corp_code": "00126380", "report_tp": "약식", "repror": "국민연금공단",
         "stkqy": "452,817,480", "stkqy_irds": "-3,112,000", "stkrt": "7.68", "stkrt_irds": "-0.05", "ctr_stkqy": "-", "ctr_stkrt": "-",
         "report_resn": "단순 장내 매매"},
        {"rcept_no": "", "rcept_dt": "2026-11-05", "repror": "x"}]
RSCH = [{"researchId": 96231, "writeDate": "2026-11-10", "title": " 4Q 프리뷰:  기대 이상 ", "brokerName": "미래에셋증권",
         "previewContent": "요약\n본문"},
        {"researchId": 90001, "writeDate": "2025-10-01", "title": "오래된 리포트", "brokerName": "키움증권"},
        {"researchId": "", "writeDate": "2026-11-10", "title": "id 없음"}]


def test_financials_holders_research_rows():
    """새 3종(재무·대량보유·증권사 리포트) — 응답 → 행 변환과 서버 검증."""
    fin = {(r["code"], r["fs_div"]): r for r in C.fin_rows(FIN)}
    c = fin[("005930", "CFS")]
    assert c["year"] == 2025 and c["reprt"] == "11011" and c["assets"] == 514531948000000 and c["revenue"] == 300870903000000
    assert c["net_income"] == -1000 and c["period_end"] == "2025-12-31" and c["rcept_no"] == "20260315000123"
    assert c["payload"]["자산총계"] == {"sj": "BS", "th": 514531948000000, "frm": 455905980000000, "bfe": 448424507000000}
    assert c["payload"]["당기순이익"] == {"sj": "IS", "th": -1000}                          # '(손실)' 제거, '-' 는 값 없음
    assert fin[("005930", "OFS")]["pretax_income"] == 5 and len(fin) == 2                  # 계정명 공백 제거, 종목코드 없는 행은 버림
    n = S.normalize_financial(c)
    assert n["bsns_year"] == 2025 and n["revenue"] == 300870903000000.0 and n["period_end"] == "2025-12-31" and '"자산총계"' in n["payload"]
    assert S.normalize_financial(dict(c, reprt="99999")) is None and S.normalize_financial(dict(c, payload={})) is None
    # 보고서 선택: 백필은 연도 × 4종, 매일은 결산일이 최근 200일 안인 것만
    assert C.fin_periods(datetime.date(2026, 11, 11), [2024]) == [(2024, r) for r in C.FIN_REPRT]
    assert C.fin_periods(datetime.date(2026, 11, 11), [2023], reprts=["11011", "11014"]) == [(2023, "11014"), (2023, "11011")]
    # 한 보고서의 콜은 나란히 보내고(100개사씩), 보고서가 끝날 때마다 넘긴다
    saved_j, saved_k, seen_urls, chunks = C._get_json, C.DART_KEY, [], []

    def fake_dart(url, timeout=30, retries=2, headers=None):
        seen_urls.append(url)
        return {"status": "000", "list": FIN} if "corp_code=c0," in url else {"status": "013"}
    C._get_json, C.DART_KEY = fake_dart, "k"
    try:
        _, n_calls = C.collect_financials([f"c{i}" for i in range(250)], [(2025, "11011"), (2025, "11014")],
                                          on_chunk=lambda rows, y, r: chunks.append((y, r, len(rows))), workers=3)
        assert n_calls == 6 and len(seen_urls) == 6 and chunks == [(2025, "11011", 2), (2025, "11014", 2)]
    finally:
        C._get_json, C.DART_KEY = saved_j, saved_k
    assert C.fin_periods(datetime.date(2026, 11, 11)) == [(2026, "11012"), (2026, "11014")]
    assert C.fin_periods(datetime.date(2026, 4, 1)) == [(2025, "11014"), (2025, "11011"), (2026, "11013")]

    h = C.holder_rows("005930", HOLD)
    assert len(h) == 1 and h[0]["shares"] == 452817480 and h[0]["shares_chg"] == -3112000 and h[0]["stake_pct"] == 7.68
    assert h[0]["ctr_shares"] is None and h[0]["date"] == "2026-11-05"
    nh = S.normalize_holder(h[0])
    assert nh["report_date"] == "2026-11-05" and nh["reporter"] == "국민연금공단" and nh["stake_chg"] == -0.05
    assert S.normalize_holder(dict(h[0], reporter="")) is None and S.normalize_holder(dict(h[0], rcept_no="1")) is None

    r = C.research_rows("005930", RSCH)
    assert [x["report_key"] for x in r] == ["naver:96231", "naver:90001"] and r[0]["title"] == "4Q 프리뷰: 기대 이상"
    assert r[0]["summary"] == "요약 본문" and r[0]["url"].endswith("/company/96231") and "target_price" not in r[0]
    C._research_fill(r[0], {"goalPrice": "95,000", "opinion": " 매수 ", "attachUrl": "https://x/y.pdf"})
    nr = S.normalize_research(r[0])
    assert nr["target_price"] == 95000.0 and nr["opinion"] == "매수" and nr["pdf_url"] == "https://x/y.pdf" and nr["source"] == "naver"
    assert S.normalize_research(dict(r[0], report_key="bad key")) is None and S.normalize_research(dict(r[0], target_price=0))["target_price"] is None
    assert C.research_rows("005930", {"error": 1}) == [] and C._plain("<p>a&amp;b</p><br/>c", 10) == "a&b c"

    # 목록을 cutoff 까지만 거슬러 받는다(오래된 행이 나오면 멈춤)
    saved = (C._get_json, C.time.sleep)
    pages = []

    def fake(url, timeout=30, retries=2, headers=None):
        pages.append(url)
        return RSCH[:2] + [{"researchId": 100 + i, "writeDate": "2026-11-09", "title": f"t{i}"} for i in range(18)] if "page=1&" in url else []
    C._get_json, C.time.sleep = fake, (lambda s: None)
    try:
        rows, last = C.fetch_research("005930", "2026-01-01", pages=5)
        assert last == 1 and len(pages) == 1 and len(rows) == 19 and all(x["date"] >= "2026-01-01" for x in rows)
    finally:
        C._get_json, C.time.sleep = saved


def test_upsert_new_tables():
    """재무는 같은 키면 덮어쓰기(정정), 대량보유는 DO NOTHING, 리포트는 COALESCE(목록만 본 행이 목표주가를 지우지 않게)."""
    seen = {}

    def fake_run(op, *a):
        import psycopg2.extras as ex
        orig = ex.execute_values
        ex.execute_values = lambda cur, sql, rows, template=None, page_size=100: seen.update(sql=sql, rows=list(rows), template=template)
        try:
            return op(_Cur(), *a)
        finally:
            ex.execute_values = orig
    saved = S.report_db._run
    S.report_db._run = fake_run
    try:
        try:
            import psycopg2  # noqa: F401
        except ImportError:
            print("    (psycopg2 없음 — SQL 실행 경로 생략)")
            return
        fin = C.fin_rows(FIN)
        out = S.upsert_rows("financials", fin + [dict(fin[0])])
        assert out == {"accepted": 2, "rejected": 0} and "ON CONFLICT (code, bsns_year, reprt_code, fs_div) DO UPDATE" in seen["sql"]
        assert seen["template"].endswith("%s::jsonb)") and seen["template"].count("%s") == len(seen["rows"][0])
        out = S.upsert_rows("holders", C.holder_rows("005930", HOLD))
        assert out == {"accepted": 1, "rejected": 0} and "DO NOTHING" in seen["sql"] and "stock_major_holders" in seen["sql"]
        out = S.upsert_rows("research", C.research_rows("005930", RSCH))
        assert out["accepted"] == 2 and "target_price = COALESCE(EXCLUDED.target_price, stock_research.target_price)" in seen["sql"]
        assert S.normalize_run({"date": "2026-11-11", "part": "research", "ok": 1})["part"] == "research"
        assert set(S.TABLES) >= {"financials", "holders", "research"}
    finally:
        S.report_db._run = saved


VAL = {  # m.stock.naver.com/api/stock/<code>/integration 응답 형태
    "totalInfos": [{"code": "per", "key": "PER", "value": "12.25배"}, {"code": "eps", "value": "22,292원"}, {"code": "cnsPer", "value": "N/A"},
                   {"code": "pbr", "value": "1.31배"}, {"code": "dividendYieldRatio", "value": "0.61%"},
                   {"code": "highPriceOf52Weeks", "value": "99,000"}, {"code": "highPriceOf52WeeksAdjusted", "value": "98,000"}, "잡음"],
    "consensusInfo": {"recommMean": "4.00", "priceTargetMean": "95,000"},
}


def test_valuation_snapshot():
    """밸류에이션 일별 스냅샷 — 응답 → (kind, code, date, payload jsonb). 없는 값은 키가 없고, 원문 지표는 info 에 그대로."""
    r = C.valuation_row("005930", VAL, "2026-11-11")
    p = r["payload"]
    assert r["kind"] == "valuation" and r["code"] == "005930" and r["date"] == "2026-11-11"
    assert p["per"] == 12.25 and p["eps"] == 22292.0 and p["pbr"] == 1.31 and p["dividendYield"] == 0.61
    assert p["high52w"] == 98000.0 and p["opinionScore"] == 4.0 and p["targetPrice"] == 95000.0     # 수정주가 우선
    assert "estPer" not in p and p["info"]["cnsPer"] == "N/A" and len(p["info"]) == 7
    assert C.valuation_row("005930", {}, "2026-11-11") is None and C.valuation_row("005930", None, "2026-11-11") is None
    n = S.normalize_snapshot(r)
    assert n["kind"] == "valuation" and n["snap_date"] == "2026-11-11" and '"per": 12.25' in n["payload"]
    assert S.normalize_snapshot(dict(r, kind="board")) is None                             # 허용 종류만
    assert S.normalize_snapshot(dict(r, payload={"x": "가" * 20000})) is None              # 크기 상한
    assert S.normalize_snapshot(dict(r, payload={})) is None and "snapshots" in S.TABLES and "valuation" in S.PARTS
    saved = C._get_json
    C._get_json = lambda url, timeout=30, retries=2, headers=None: VAL if "/005930/" in url else {}
    try:
        rows, stat = C.collect_valuation(["005930", "000660"], "2026-11-11", workers=2)
        assert len(rows) == 1 and stat["ok"] == 1 and stat["failed"] == 1 and "지표 없음" in stat["errs"][0]
    finally:
        C._get_json = saved


def test_server_routes():
    """railway_server 의 /api/ingest/rows · /api/stockdata/* — 인증·비활성·화이트리스트(소켓 없이 핸들러 메서드만)."""
    import io
    import json
    from urllib.parse import urlparse
    import railway_server as rs

    class Stub:
        def __init__(self, path, headers=None, body=b""):
            self.path, self.headers, self.rfile, self.sent = path, headers or {}, io.BytesIO(body), None

        def _send(self, code, payload):
            self.sent = (code, payload)

    def get(path, headers=None):
        h = Stub(path, headers)
        rs.Handler._stockdata(h, urlparse(path))
        return h.sent

    def post(body, headers):
        raw = json.dumps(body).encode("utf-8")
        h = Stub("/api/ingest/rows", dict(headers, **{"Content-Length": str(len(raw))}), raw)
        rs.Handler._ingest_rows(h)
        return h.sent

    saved = (rs.INGEST_TOKEN, rs.STOCKDATA_READ_TOKEN, rs.stock_db.enabled, rs.stock_db.status, rs.stock_db.upsert_rows, rs.stock_db.flow)
    try:
        rs.INGEST_TOKEN, rs.STOCKDATA_READ_TOKEN = "", ""
        rs.stock_db.enabled = lambda: False
        assert get("/api/stockdata/status")[0] == 503                                   # DB 비활성
        assert post({"table": "flow", "rows": []}, {})[0] == 401                        # 토큰 미설정이면 쓰기 차단
        rs.stock_db.enabled = lambda: True
        rs.stock_db.status = lambda: {"flow": {"rows": 0}}
        assert get("/api/stockdata/status") == (200, {"status": "ok", "tables": {"flow": {"rows": 0}}, "db": rs.report_db.status()})
        assert get("/api/stockdata/flow?code=005930")[0] == 401                         # 토큰이 하나도 없으면 읽기도 차단
        rs.INGEST_TOKEN = "w"
        rs.stock_db.flow = lambda code, f, t: [{"trade_date": "2026-11-10"}] if code == "005930" else None
        assert get("/api/stockdata/flow?code=005930", {"Authorization": "Bearer w"})[0] == 200   # 읽기 토큰 없으면 INGEST_TOKEN
        assert get("/api/stockdata/flow?code=005930&token=w")[1]["data"] == [{"trade_date": "2026-11-10"}]
        rs.STOCKDATA_READ_TOKEN = "r"
        assert get("/api/stockdata/flow?code=005930", {"Authorization": "Bearer w"})[0] == 401   # 읽기 토큰이 있으면 그것만
        assert get("/api/stockdata/flow?code=005930", {"Authorization": "Bearer r"})[0] == 200
        assert get("/api/stockdata/flow?code=zz", {"Authorization": "Bearer r"})[0] == 400
        assert get("/api/stockdata/nope?code=005930", {"Authorization": "Bearer r"})[0] == 404
        rs.stock_db.upsert_rows = lambda table, rows: {"accepted": len(rows), "rejected": 0}
        assert post({"table": "flow", "rows": [{}]}, {"Authorization": "Bearer r"})[0] == 401     # 쓰기는 INGEST_TOKEN 만
        assert post({"table": "flow", "rows": [{}, {}]}, {"Authorization": "Bearer w"}) == (200, {"status": "ok", "table": "flow", "accepted": 2, "rejected": 0})
        assert post({"table": "users", "rows": []}, {"Authorization": "Bearer w"})[0] == 400
        assert post({"table": "flow", "rows": "x"}, {"Authorization": "Bearer w"})[0] == 400
        rs.stock_db.upsert_rows = lambda table, rows: None
        assert post({"table": "flow", "rows": [{}]}, {"Authorization": "Bearer w"})[0] == 500
        assert rs.STOCKDATA_WF == "stock_data_collect.yml" and os.path.exists(os.path.join(ROOT, ".github", "workflows", rs.STOCKDATA_WF))
    finally:
        rs.INGEST_TOKEN, rs.STOCKDATA_READ_TOKEN, rs.stock_db.enabled, rs.stock_db.status, rs.stock_db.upsert_rows, rs.stock_db.flow = saved


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok {t.__name__}")
    print(f"ALL PASS ({len(tests)} tests — stock data collect)")


if __name__ == "__main__":
    main()
