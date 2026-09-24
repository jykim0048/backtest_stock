"""KRX 휴장일 캘린더 — 판정 헬퍼 + 특일정보 API 파서 + 파일 입출력.

배경(2026-09-24 추석 연휴): Railway 스케줄러(railway_server._scheduler)와 워크플로 가드가
요일·시간만 보고 휴장일을 몰라 장전·장중·마감 파이프라인이 전날 데이터를 재가공해
돌았다. 이 모듈이 단일 진실 소스 data/krx_holidays.json 을 읽고 영업일을 판정한다.

데이터 구성(fetch_krx_holidays.py 가 생성):
  - 정부 공휴일·대체공휴일·임시공휴일: 공공데이터포털 한국천문연구원 특일정보
    getRestDeInfo (parse_restde)
  - KRX 고유 휴장: 근로자의날(5/1), 연말휴장(12/31, 주말이면 직전 영업일) (krx_extra_holidays)
  - 천재지변 등 임시휴장: 파일 수동 편집

판정은 fail-open — 파일이 없거나 깨지면 빈 집합을 돌려 '평일 = 영업일'로 동작한다
(휴장 판정 실패로 정상 영업일 파이프라인이 멈추는 쪽이 더 나쁘다).
"""
import os
import json
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.join(ROOT, "data", "krx_holidays.json")


class RestDeError(Exception):
    """특일정보 API 가 정상 응답(resultCode 00)이 아닐 때 — 빈 공휴일로 오인하면 안 된다."""


def parse_restde(payload):
    """getRestDeInfo(_type=json) 응답 → {"YYYY-MM-DD": 이름} (isHoliday=Y 만).

    공공데이터포털 관례: item 이 여러 건이면 list, 1건이면 dict, 0건이면 items 가 "" 다."""
    resp = (payload or {}).get("response") or {}
    header = resp.get("header") or {}
    if str(header.get("resultCode")) != "00":
        raise RestDeError(f"resultCode={header.get('resultCode')} {header.get('resultMsg', '')}")
    items = (resp.get("body") or {}).get("items") or {}
    rows = items.get("item") if isinstance(items, dict) else []
    if isinstance(rows, dict):
        rows = [rows]
    out = {}
    for r in rows or []:
        if str(r.get("isHoliday", "")).upper() != "Y":
            continue
        s = str(r.get("locdate") or "")
        if len(s) != 8 or not s.isdigit():
            continue
        out[f"{s[:4]}-{s[4:6]}-{s[6:]}"] = str(r.get("dateName") or "").strip()
    return out


def krx_extra_holidays(year):
    """정부 공휴일이 아닌 KRX 고유 휴장일 — 근로자의날, 연말휴장(12/31이 주말이면 직전 영업일)."""
    last = datetime.date(year, 12, 31)
    while last.weekday() >= 5:
        last -= datetime.timedelta(days=1)
    return {f"{year}-05-01": "근로자의날", last.isoformat(): "연말휴장"}


def build_year(year, public_holidays):
    """그 해 휴장일 dict(날짜 → 이름, 정렬) — 공휴일 + KRX 규칙, 주말은 제외(판정에 불필요)."""
    merged = {**{d: n for d, n in (public_holidays or {}).items() if d.startswith(f"{year}-")},
              **krx_extra_holidays(year)}
    out = {}
    for d in sorted(merged):
        if datetime.date.fromisoformat(d).weekday() < 5:
            out[d] = merged[d]
    return out


def is_trading_day(d, holidays):
    """평일이고 휴장일 집합에 없으면 True. holidays 는 'YYYY-MM-DD' 집합(없으면 평일 = 영업일)."""
    return d.weekday() < 5 and d.isoformat() not in (holidays or set())


def save_holidays(path, years, source=""):
    """{year: {date: name}} → JSON 파일. holidays 는 연도별 정렬 날짜 목록, names 는 날짜 → 이름."""
    doc = {
        "source": source,
        "updatedAt": datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M KST"),
        "holidays": {str(y): sorted(years[y]) for y in sorted(years)},
        "names": {d: n for y in sorted(years) for d, n in sorted(years[y].items())},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _doc_to_set(doc):
    out = set()
    for days in ((doc or {}).get("holidays") or {}).values():
        out.update(str(d) for d in (days or []))
    return out


def load_holidays(path=None):
    """파일의 전 연도 휴장일을 'YYYY-MM-DD' 집합으로. 없거나 깨지면 빈 집합(fail-open)."""
    try:
        with open(path or DEFAULT_PATH, encoding="utf-8") as f:
            return _doc_to_set(json.load(f))
    except Exception:
        return set()


class HolidayCache:
    """상주 프로세스(railway_server)용 — 부팅 시 로컬 파일, 이후 refresh_s 마다 fetcher 로 갱신.

    fetcher 는 파일과 같은 형식의 dict(JSON) 를 돌려준다(예: GitHub raw 의 data/krx_holidays.json).
    갱신 실패는 이전 값을 유지하고 다음 창에 재시도한다 — 컨테이너 스냅샷이 낡아도 커밋된
    최신 캘린더를 따라가되, raw 장애로 판정이 비지 않게."""

    def __init__(self, path=None, fetcher=None, refresh_s=86400, log=None):
        self._path = path or DEFAULT_PATH
        self._fetcher = fetcher
        self._refresh_s = refresh_s
        self._log = log or (lambda m: None)
        self._holidays = load_holidays(self._path)
        self._next_refresh = 0.0

    def _maybe_refresh(self):
        import time
        now = time.time()
        if not self._fetcher or now < self._next_refresh:
            return
        self._next_refresh = now + self._refresh_s
        try:
            fresh = _doc_to_set(self._fetcher())
            if fresh:
                self._holidays = fresh
                self._log(f"휴장일 캘린더 갱신 {len(fresh)}일")
        except Exception as e:
            self._log(f"휴장일 캘린더 갱신 실패(이전 값 유지): {e}")

    @property
    def holidays(self):
        self._maybe_refresh()
        return self._holidays

    def is_trading_day(self, when):
        """datetime 또는 date → 영업일 여부(주말·휴장일 False)."""
        d = when.date() if isinstance(when, datetime.datetime) else when
        return is_trading_day(d, self.holidays)


def _cli(argv=None):
    """워크플로 가드용 CLI — `python krx_calendar.py --guard`: 영업일이면 0, 주말·휴장일이면 1.

    Actions 의 daily_report / intraday_screener / closing_briefing 첫 스텝에서 호출해 Railway
    스케줄러를 우회한 수동 실행·cron 지연 발화도 휴장일엔 멈춘다(force 입력 시 스텝 자체를 건너뜀)."""
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--guard", action="store_true", help="영업일 0 / 휴장·주말 1 로 종료")
    ap.add_argument("--date", default=None, help="판정 날짜 YYYY-MM-DD (기본: KST 오늘)")
    ap.add_argument("--path", default=None, help="휴장일 파일 경로 (기본 data/krx_holidays.json)")
    a = ap.parse_args(argv)
    kst = datetime.timezone(datetime.timedelta(hours=9))
    d = datetime.date.fromisoformat(a.date) if a.date else datetime.datetime.now(kst).date()
    hol = load_holidays(a.path)
    ok = is_trading_day(d, hol)
    why = ("주말" if d.weekday() >= 5 else "휴장일" if not ok else "")
    print(f"[krx-calendar] {d.isoformat()} ({'월화수목금토일'[d.weekday()]}) — "
          f"{'영업일' if ok else '휴장(' + why + ')'} · 캘린더 {len(hol)}일 로드")
    if a.guard:
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
