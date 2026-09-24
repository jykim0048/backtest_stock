"""krx_calendar 회귀 — KRX 휴장일 판정·특일정보 API 파싱·KRX 고유 휴장 규칙 (오프라인).

실행: python tests/test_krx_calendar.py
"""
import os
import sys
import json
import datetime
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import krx_calendar as kc

# 공공데이터포털 특일정보(getRestDeInfo, _type=json) 응답 발췌 형태 — 2026년 9월
RESTDE_JSON = {
    "response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                 "body": {"items": {"item": [
                     {"dateKind": "01", "dateName": "추석", "isHoliday": "Y", "locdate": 20260924, "seq": 1},
                     {"dateKind": "01", "dateName": "추석", "isHoliday": "Y", "locdate": 20260925, "seq": 1},
                     {"dateKind": "01", "dateName": "추석", "isHoliday": "Y", "locdate": 20260926, "seq": 1},
                 ]}, "numOfRows": 100, "pageNo": 1, "totalCount": 3}}}
RESTDE_SINGLE = {   # 1건이면 item 이 리스트가 아니라 dict 로 온다(공공데이터포털 관례)
    "response": {"header": {"resultCode": "00"},
                 "body": {"items": {"item": {"dateName": "한글날", "isHoliday": "Y", "locdate": 20261009}},
                          "totalCount": 1}}}
RESTDE_EMPTY = {"response": {"header": {"resultCode": "00"}, "body": {"items": "", "totalCount": 0}}}


def main():
    D = datetime.date

    # ── parse_restde: 리스트/단건/빈 응답, isHoliday=N 제외, locdate → ISO ─────────
    assert kc.parse_restde(RESTDE_JSON) == {"2026-09-24": "추석", "2026-09-25": "추석", "2026-09-26": "추석"}
    assert kc.parse_restde(RESTDE_SINGLE) == {"2026-10-09": "한글날"}
    assert kc.parse_restde(RESTDE_EMPTY) == {}
    non = {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": [
        {"dateName": "기념일", "isHoliday": "N", "locdate": 20260601}]}}}}
    assert kc.parse_restde(non) == {}
    bad = {"response": {"header": {"resultCode": "30", "resultMsg": "SERVICE KEY IS NOT REGISTERED ERROR."}}}
    try:
        kc.parse_restde(bad)
        assert False, "resultCode!=00 은 예외여야 한다(빈 목록으로 오인 금지)"
    except kc.RestDeError:
        pass

    # ── krx_extra_holidays: 근로자의날 + 연말휴장(12/31, 주말이면 직전 영업일) ─────────
    assert kc.krx_extra_holidays(2026) == {"2026-05-01": "근로자의날", "2026-12-31": "연말휴장"}   # 12/31 목
    assert kc.krx_extra_holidays(2028)["2028-12-29"] == "연말휴장"                                # 12/31 일 → 금
    assert "2028-12-31" not in kc.krx_extra_holidays(2028)
    assert kc.krx_extra_holidays(2027)["2027-12-31"] == "연말휴장"                                # 12/31 금

    # ── build_year: API 공휴일 + KRX 규칙 병합, 주말 제외(영업일 판정에 무의미), 정렬 ──
    year = kc.build_year(2026, kc.parse_restde(RESTDE_JSON))
    assert year == {"2026-05-01": "근로자의날", "2026-09-24": "추석", "2026-09-25": "추석",
                    "2026-12-31": "연말휴장"}, year          # 9/26(토) 제외
    assert list(year) == sorted(year)

    # ── is_trading_day: 주말 False, 휴장일 False, 그 외 True ──────────────────────────
    hol = set(year)
    assert kc.is_trading_day(D(2026, 9, 23), hol) is True
    assert kc.is_trading_day(D(2026, 9, 24), hol) is False    # 추석 연휴(목)
    assert kc.is_trading_day(D(2026, 9, 26), hol) is False    # 토
    assert kc.is_trading_day(D(2026, 9, 28), hol) is True
    assert kc.is_trading_day(D(2026, 5, 1), hol) is False     # 근로자의날(금)
    assert kc.is_trading_day(D(2026, 9, 24), set()) is True   # 목록 없으면 평일 = 영업일(fail-open)

    # ── 파일 입출력: save → load 왕복, 연도 dict → 날짜 집합, 손상 파일은 빈 집합 ─────
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "krx_holidays.json")
        kc.save_holidays(p, {2026: year, 2027: {"2027-01-01": "1월1일"}}, source="test")
        doc = json.load(open(p, encoding="utf-8"))
        assert doc["holidays"]["2026"] == list(year) and doc["names"]["2027-01-01"] == "1월1일"
        assert doc["source"] == "test" and doc["updatedAt"]
        assert kc.load_holidays(p) == set(year) | {"2027-01-01"}
        assert kc.load_holidays(os.path.join(td, "missing.json")) == set()
        open(p, "w").write("{broken")
        assert kc.load_holidays(p) == set()

    # ── 기본 파일 경로(data/krx_holidays.json)도 읽힌다 ────────────────────────────────
    assert isinstance(kc.load_holidays(), set)

    print("ALL PASS (krx_calendar: parse/extra rules/build/is_trading_day/file io)")


def main_cli_and_scheduler():
    """CLI 가드(--guard: 영업일 0 / 휴장·주말 1) + railway_server 스케줄러 판정."""
    import subprocess, tempfile
    D = datetime.date
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "krx_holidays.json")
        kc.save_holidays(p, {2026: {"2026-09-24": "추석"}})
        def guard(day):
            r = subprocess.run([sys.executable, os.path.join(ROOT, "krx_calendar.py"), "--guard",
                                "--date", day, "--path", p], capture_output=True, text=True, encoding="utf-8")
            return r.returncode, (r.stdout + r.stderr)
        rc, out = guard("2026-09-24"); assert rc == 1 and "휴장" in out, out
        rc, out = guard("2026-09-26"); assert rc == 1, out                     # 토요일
        rc, out = guard("2026-09-23"); assert rc == 0 and "영업일" in out, out

        # 스케줄러: KR 파이프라인 발화 여부는 요일이 아니라 캘린더로 판정
        import railway_server as rs
        rs.KR_HOLIDAYS = kc.HolidayCache(path=p, fetcher=None)
        assert rs._kr_market_day(datetime.datetime(2026, 9, 24, 7, 43, tzinfo=rs.KST)) is False
        assert rs._kr_market_day(datetime.datetime(2026, 9, 23, 7, 43, tzinfo=rs.KST)) is True
        assert rs._kr_market_day(datetime.datetime(2026, 9, 27, 7, 43, tzinfo=rs.KST)) is False   # 일요일
    print("ALL PASS (krx_calendar cli guard + scheduler market-day decision)")


if __name__ == "__main__":
    main()
    main_cli_and_scheduler()
