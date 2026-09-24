"""fetch_krx_holidays 회귀 — 특일정보 API 호출·연도 병합·실패 시 기존 유지, HolidayCache 갱신 (오프라인).

실행: python tests/test_fetch_krx_holidays.py
"""
import os
import sys
import json
import datetime
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import krx_calendar as kc
import fetch_krx_holidays as fk


class _Resp:
    def __init__(self, payload=None, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


def _restde(*rows):
    return {"response": {"header": {"resultCode": "00"}, "body": {"items": {"item": list(rows)}, "totalCount": len(rows)}}}


def main():
    calls = []

    def fake_get(url, params=None, timeout=None, **kw):
        calls.append((url, dict(params or {})))
        y = int(params["solYear"])
        if y == 2026:
            return _Resp(_restde({"dateName": "추석", "isHoliday": "Y", "locdate": 20260924},
                                 {"dateName": "추석", "isHoliday": "Y", "locdate": 20260925},
                                 {"dateName": "개천절", "isHoliday": "Y", "locdate": 20261003},
                                 {"dateName": "대체공휴일(개천절)", "isHoliday": "Y", "locdate": 20261005}))
        if y == 2027:
            return _Resp(status=500)                          # 다음 해는 API 장애
        return _Resp(_restde())

    fk.requests.get = fake_get

    # ── fetch_public_holidays: 연도 단위 1콜, 키·JSON 형식·행수 파라미터, 파싱 결과 ─────
    ph = fk.fetch_public_holidays(2026, "KEY")
    assert ph == {"2026-09-24": "추석", "2026-09-25": "추석", "2026-10-03": "개천절",
                  "2026-10-05": "대체공휴일(개천절)"}, ph
    url, params = calls[-1]
    assert url.endswith("/getRestDeInfo") and params["solYear"] == "2026" and "solMonth" not in params
    assert params["serviceKey"] == "KEY" and params["_type"] == "json" and int(params["numOfRows"]) >= 50

    # ── build_all: 올해+내년. 실패한 해는 기존 파일의 그 해를 유지(없으면 KRX 규칙만이라도) ──
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "krx_holidays.json")
        kc.save_holidays(p, {2027: {"2027-01-01": "1월1일(기존)", "2027-05-01": "근로자의날"}}, source="old")
        years, failed = fk.build_all(2026, "KEY", existing_path=p)
        assert failed == [2027], failed
        assert years[2026]["2026-09-24"] == "추석" and years[2026]["2026-05-01"] == "근로자의날"
        assert years[2026]["2026-12-31"] == "연말휴장" and "2026-10-03" not in years[2026]   # 토요일 제외
        assert years[2027] == {"2027-01-01": "1월1일(기존)", "2027-05-01": "근로자의날"}          # 기존 유지
        # 기존 파일에도 없는 해가 실패하면 KRX 규칙만으로라도 채운다(빈 해 방지)
        years2, failed2 = fk.build_all(2026, "KEY", existing_path=os.path.join(td, "none.json"))
        assert failed2 == [2027] and years2[2027] == kc.krx_extra_holidays(2027)

        # ── main: 파일 생성, 전부 실패면 기존 파일 보존 + 종료코드 1 ─────────────────────
        rc = fk.main(["--year", "2026", "--out", p, "--key", "KEY"])
        assert rc == 0
        doc = json.load(open(p, encoding="utf-8"))
        assert "2026-09-24" in doc["holidays"]["2026"] and doc["names"]["2026-10-05"] == "대체공휴일(개천절)"
        assert "2027-01-01" in doc["holidays"]["2027"]
        before = open(p, encoding="utf-8").read()
        fk.requests.get = lambda *a, **k: _Resp(status=503)
        rc = fk.main(["--year", "2026", "--out", p, "--key", "KEY"])
        assert rc == 1 and open(p, encoding="utf-8").read() == before      # 전부 실패 → 무변경
        rc = fk.main(["--year", "2026", "--out", p, "--key", ""])
        assert rc == 2                                                      # 키 없음 → 명확한 종료코드

    # ── HolidayCache: 로컬 파일 즉시 로드, fetcher 로 하루 1회 갱신, 갱신 실패는 이전 값 유지 ──
    with tempfile.TemporaryDirectory() as td:
        p = os.path.join(td, "krx_holidays.json")
        kc.save_holidays(p, {2026: {"2026-09-24": "추석"}})
        fetched = {"n": 0}

        def fetcher():
            fetched["n"] += 1
            if fetched["n"] == 2:
                raise RuntimeError("raw down")
            return {"holidays": {"2026": ["2026-09-24", "2026-10-05"]}}

        cache = kc.HolidayCache(path=p, fetcher=fetcher, refresh_s=3600)
        t0 = datetime.datetime(2026, 9, 24, 7, 0)
        assert cache.is_trading_day(t0) is False and fetched["n"] == 1        # 첫 판정에서 1회 갱신
        assert cache.is_trading_day(datetime.datetime(2026, 10, 5, 7, 0)) is False   # 갱신분 반영
        assert fetched["n"] == 1                                              # 같은 창 안 재호출 없음
        cache._next_refresh = 0                                               # 창 만료 시뮬레이션
        assert cache.is_trading_day(datetime.datetime(2026, 10, 5, 7, 0)) is False   # 실패 → 이전 값 유지
        assert fetched["n"] == 2
        assert cache.is_trading_day(datetime.datetime(2026, 9, 23, 7, 0)) is True

    print("ALL PASS (fetch_krx_holidays: api call/merge/keep-on-failure/main rc, HolidayCache refresh)")


if __name__ == "__main__":
    main()
