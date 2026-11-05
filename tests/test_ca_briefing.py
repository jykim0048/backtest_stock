"""예탁원 CA — 모닝브리핑 워치리스트 부착·오늘의 CA·주간 일별 CA 회귀 (오프라인 픽스처).

실행: python tests/test_ca_briefing.py
행 형태는 fetch_ca_calendar 산출(public/ca_calendar.json) 그대로. 2026-11-05 실데이터 예:
에코프로비엠 247540 11/05 '추가 상장'(유상증자 9.19%, ★3).
"""
import datetime
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ca_events as ce

D = datetime.date
HOL = {"2026-11-11"}          # 가상 휴장일(창 계산 검증용)
TODAY = "2026-11-05"          # 목요일


def R(date, code, typ, kind, label, imp, fields=None, end=None, name=None, tl=None):
    r = {"date": date, "kind": kind, "milestoneLabel": label, "type": typ,
         "typeLabel": tl or ce.__dict__.get("_TL", {}).get(typ, typ), "code": code,
         "name": name, "importance": imp, "fields": fields or {}}
    if end:
        r["end"] = end
    return r


CAL = {"asof": "2026-11-05 07:52 KST", "rows": [
    R("2026-11-05", "247540", "listing", "listing", "추가 상장", 3,
      {"reason": "유상증자", "newShares": 9900990.0, "totalShares": 107731424.0,
       "issuePrice": 89500.0, "newSharesPct": 9.19}, name="에코프로비엠", tl="상장정보"),
    R("2026-11-09", "247540", "deposit", "release", "보호예수 해제", 3,
      {"shares": 2000000, "pctOfTotal": 1.9, "releaseDate": "2026-11-09"}, name="에코프로비엠", tl="의무예치"),
    R("2026-11-05", "247540", "dividend", "pay", "배당금 지급", 1, {"cashDps": 100}, name="에코프로비엠"),
    R("2026-11-16", "247540", "listing", "listing", "추가 상장", 3, {"newSharesPct": 30}),     # 창 밖(5영업일 후 다음)
    R("2026-11-03", "304840", "revsplit", "halt", "매매정지", 3,
      {"faceBefore": 500, "faceAfter": 2500, "direction": "merge"}, end="2026-11-06",
      name="타임엑스에이아이", tl="액면교체"),                                                   # 기간이 오늘에 걸침
    R("2026-11-06", "111111", "paidin", "ex", "권리락", 3, {"newSharesPct": 41.67, "issuePrice": 1000},
      name="바이오", tl="유상증자"),
    R("2026-11-06", "222222", "listing", "listing", "추가 상장", 3, {"reason": "국내CB행사", "newSharesPct": 6.2},
      name="씨비", tl="상장정보"),
    R("2026-11-06", "333333", "listing", "listing", "추가 상장", 2, {"reason": "액면병합", "newSharesPct": 1.0}),
    R("2026-11-09", "444444", "capdcrs", "halt", "매매정지", 3, {"reduceType": "무상감자", "reduceRate": "0.05"},
      end="2026-11-20", name="감자", tl="자본감소"),
    R("2026-11-04", "555555", "listing", "listing", "추가 상장", 3, {"newSharesPct": 50}),      # 어제 — 제외
    # 기술적 상장(액면병합 100%, 비악재) — 물량이 커도 희석성 신주상장 뒤로
    R("2026-11-05", "777777", "listing", "listing", "추가 상장", 3, {"reason": "액면병합", "newSharesPct": 100.0},
      name="병합", tl="상장정보"),
    # 허브 분리 전 구형 의무예치 기간 행(수년, ★3) — 브리핑 발췌에서 제외(release 만 유효)
    R("2026-11-02", "359410", "deposit", "deposit", "의무예치", 3, {"pctOfTotal": 6188.0},
      end="2029-05-02", name="영광", tl="의무예치"),
    R("2026-11-05", "666666", "deposit", "deposit", "보호예수 시작", 1, {"pctOfTotal": 2.0},
      name="예치시작", tl="의무예치"),
]}


def test_bd_diff_and_days():
    d = D(2026, 11, 5)
    assert ce.bd_diff(d, "2026-11-05", HOL) == 0
    assert ce.bd_diff(d, "2026-11-06", HOL) == 1
    assert ce.bd_diff(d, "2026-11-09", HOL) == 2          # 주말 건너뜀
    assert ce.bd_diff(d, "2026-11-12", HOL) == 4          # 11/11 휴장 제외
    assert ce.bd_diff(d, "2026-11-04", HOL) == -1
    assert ce.bd_diff(d, "2026-11-02", HOL) == -3
    assert [x.isoformat() for x in ce.next_trading_days(d, 5, HOL)] == \
        ["2026-11-06", "2026-11-09", "2026-11-10", "2026-11-12", "2026-11-13"]


def test_is_adverse_and_badge():
    rows = {}
    for r in CAL["rows"]:
        rows.setdefault((r["code"], r["kind"]), r)          # 첫 행 우선(247540 은 11/16 행도 있음)
    eco = rows[("247540", "listing")]
    assert ce.is_adverse(eco)
    assert ce.badge_text(eco, 0) == "오늘 신주상장 9.19%(유상증자)"
    rel = rows[("247540", "release")]
    assert ce.is_adverse(rel) and ce.badge_text(rel, 2) == "D+2 보호예수 해제 1.9%"
    assert ce.is_adverse(rows[("222222", "listing")])                  # CB 행사
    assert ce.badge_text(rows[("222222", "listing")], 1) == "D+1 추가상장 6.2%(국내CB행사)"
    assert not ce.is_adverse(rows[("333333", "listing")])              # 액면병합
    assert ce.is_adverse({"type": "listing", "kind": "listing", "fields": {"reason": "STOCKOPTION행사"}})
    assert ce.is_adverse(rows[("444444", "halt")])                     # 무상감자·매매정지
    assert not ce.is_adverse({"type": "capdcrs", "kind": "record", "fields": {"reduceType": "유상감자"}})
    assert ce.is_adverse({"type": "capdcrs", "kind": "record", "fields": {"reduceType": "무상감자"}})
    assert ce.badge_text(rows[("304840", "halt")], 0) == "오늘 매매정지(액면교체)"
    assert ce.badge_text(rows[("111111", "ex")], 1) == "D+1 권리락 41.67%(유상증자)"


def test_attach_picks_window_and_filter():
    picks = [{"code": "247540", "name": "에코프로비엠"}, {"code": "304840", "name": "타임엑스"},
             {"code": "005930", "name": "삼성전자"}, {"code": "444444"}, "bad", {"code": "359410"}]
    n = ce.attach_picks(picks, CAL, TODAY, hol=HOL)
    assert n == 3, n
    assert "ca" not in picks[5]                                       # 구형 예치 기간 행 제외
    eco = picks[0]["ca"]
    # 오늘 신주상장(★3) + D+2 보호예수 해제(★3). 배당 지급(★1)·11/16(창 밖) 제외. 날짜순.
    assert [c["badge"] for c in eco] == ["오늘 신주상장 9.19%(유상증자)", "D+2 보호예수 해제 1.9%"], eco
    assert eco[0]["adverse"] and eco[0]["sizePct"] == 9.19 and eco[0]["dn"] == 0
    assert eco[0]["fields"] == {"reason": "유상증자", "newSharesPct": 9.19, "newShares": 9900990.0,
                                "issuePrice": 89500.0}
    assert picks[1]["ca"][0]["dn"] == 0                              # 기간 진행 중(11/3~11/6)
    assert "ca" not in picks[2]                                       # 일정 없음 → 키 없음
    assert picks[3]["ca"][0]["dn"] == 2
    # 휴장 반영: 창 끝 = 11/13 → 11/16 행은 여전히 밖, horizon 6 이면 포함
    p2 = [{"code": "247540"}]
    ce.attach_picks(p2, CAL, TODAY, hol=HOL, horizon_bd=6)
    assert len(p2[0]["ca"]) == 3
    # 상한
    p3 = [{"code": "247540"}]
    ce.attach_picks(p3, CAL, TODAY, hol=HOL, horizon_bd=6, per_pick=1)
    assert len(p3[0]["ca"]) == 1
    # 캘린더 없음 → 0, picks 불변
    p4 = [{"code": "247540"}]
    assert ce.attach_picks(p4, {}, TODAY) == 0 and "ca" not in p4[0]


def test_ca_today_sort_and_cap():
    ct = ce.ca_today(CAL, TODAY, hol=HOL)
    assert ct["date"] == TODAY and ct["nextDate"] == "2026-11-06" and ct["asof"].startswith("2026-11-05")
    t_codes = [r["code"] for r in ct["today"]]
    n_codes = [r["code"] for r in ct["next"]]
    assert t_codes == ["247540", "304840"], t_codes   # 악재성 먼저(9.19 → 정지) · 기술적 상장(777777 액면병합)은 제외
    assert n_codes == ["111111", "222222"], n_codes                  # 41.67 → 6.2, ★2 제외
    assert ct["total"] == 4
    assert all(r["importance"] == 3 for r in ct["today"] + ct["next"])
    small = ce.ca_today(CAL, TODAY, hol=HOL, max_n=2)
    assert [r["code"] for r in small["next"] + small["today"]] == ["111111", "247540"]  # 물량 큰 순 상위 2
    assert ce.ca_today({}, TODAY) == {}
    assert ce.ca_today({"rows": [R("2026-11-05", "1", "meeting", "meeting", "주총", 1)]}, TODAY, hol=HOL) == {}
    v = ce.llm_view(ct)
    assert v[0]["when"] == "오늘" and v[0]["stock"] == "에코프로비엠" and v[0]["sizePct"] == 9.19
    assert v[0]["fields"] == {"reason": "유상증자", "issuePrice": 89500.0}
    assert {x["when"] for x in v} == {"오늘", "다음 영업일"}


def test_briefing_llm_input():
    import generate_briefing as gb
    sel = {"picks": [{"code": "247540", "name": "에코프로비엠", "market": "KOSDAQ", "catalyst": "c",
                      "ca": [{"badge": "x"}]}], "usMarket": {}}
    ct = ce.ca_today(CAL, TODAY, hol=HOL)
    payload = json.loads(gb._llm_input(sel, [], [], None, None, ct))
    assert payload["caToday"][0]["stock"] == "에코프로비엠"
    assert "ca" not in payload["picks"][0]                            # 워치리스트 ca 는 LLM 미경유
    assert "caToday" not in json.loads(gb._llm_input(sel, [], [], None, None, {}))
    assert "caToday" in gb.SYSTEM


def test_weekly_day_ca_archive_and_fallback():
    import generate_weekly_briefing as gw
    today_rows = [r for r in CAL["rows"] if ce.covers(r, "2026-11-05")]
    archive = {"rounds": [{"caEvents": {"today": today_rows[:1]}},
                          {"caEvents": {"today": today_rows}}, {"caEvents": {}}]}
    got = gw._day_ca("2026-11-05", archive)
    # 마지막(유효) 회차 today 사용 — ★2+ 만, 중요도 → 물량% 순
    assert [r["code"] for r in got] == ["247540", "304840"], got
    assert got[0]["sizePct"] == 9.19 and got[0]["adverse"] is True
    # 아카이브에 caEvents 없음 → 캘린더 폴백
    orig = gw._fetch
    gw._CA_CAL.clear()
    gw._fetch = lambda rel: CAL if rel == "ca_calendar.json" else None
    try:
        fb = gw._day_ca("2026-11-06", {"rounds": [{"econEvents": {}}]})
        assert [r["code"] for r in fb] == ["111111", "222222", "304840"], [r["code"] for r in fb]   # 333333 액면병합 제외
        fb2 = gw._day_ca("2026-11-06", None)
        assert len(fb2) == 3
        # _day_summary 통합: 모닝·장중 아카이브 없이 캘린더만이면 day 자체가 None(휴장 취급) — 기존 동작 유지
        assert gw._day_summary("2026-11-06") is None
        gw._fetch = lambda rel: ({"stance": "중립", "usReview": {"bullets": []}} if rel == "briefing/2026-11-06.json"
                                 else CAL if rel == "ca_calendar.json" else None)
        day = gw._day_summary("2026-11-06")
        assert [r["code"] for r in day["ca"]][:2] == ["111111", "222222"]
    finally:
        gw._fetch = orig
        gw._CA_CAL.clear()


def test_weekly_top_and_llm_view():
    days = [{"date": "2026-11-05", "ca": ce.day_pick([r for r in CAL["rows"] if ce.covers(r, "2026-11-05")])},
            {"date": "2026-11-06", "ca": ce.day_pick([r for r in CAL["rows"] if ce.covers(r, "2026-11-06")])},
            {"date": "2026-11-04"}]
    top = ce.weekly_top(days, top=10)
    codes = [r["code"] for r in top]
    # 정지 기간(304840)은 두 날 모두 등장하지만 1건으로, ★2(333333) 제외, 물량% 큰 순
    assert codes == ["111111", "247540", "222222", "304840"], codes   # 악재성 → 물량%
    assert top[0]["day"] == "2026-11-06" and top[1]["day"] == "2026-11-05"
    assert len(ce.weekly_top(days, top=2)) == 2
    v = ce.weekly_llm_view(top)
    assert v[1] == {"date": "2026-11-05", "stock": "에코프로비엠", "event": "상장정보",
                    "milestone": "추가 상장", "sizePct": 9.19, "reason": "유상증자"}
    assert ce.weekly_top([]) == []


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok {t.__name__}")
    print(f"ALL PASS ({len(tests)} tests — CA briefing)")


if __name__ == "__main__":
    main()
