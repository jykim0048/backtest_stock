"""경제지표 캘린더 수집 -> public/econ_calendar.json

소스 2개를 병합한다:
  1. 네이버 증권 경제캘린더 API (인증 불필요) — 일정·중요도·실제값·이전값. 시각은 KST.
       upcoming:    /api/securityService/economic/indicator/nations/upcoming
       releaseDate: /api/securityService/economic/indicator/nations/releaseDate (페이징)
     importance: 1 낮음 / 2 보통 / 3 높음 / 4 매우높음 (네이버 프론트 "시장 영향력" 라벨)
  2. Forex Factory 주간 캘린더 JSON (무료) — 미국 핵심지표 컨센서스(forecast)·FOMC 이벤트.
     날짜는 미 동부 오프셋 포함 ISO — fromisoformat().astimezone(KST) 로만 변환(DST 자동).
     한국 지표 컨센서스는 없음 -> 한국은 이전값 대비만.

컨센서스 이월(carry-forward): FF 주간 파일은 일요일(미 동부) 경계라 월요일 아침에는
전주 금요일 발표분의 forecast 가 사라진다. 직전 커밋된 econ_calendar.json 의
forecast 를 (지표키, 발표일) 기준으로 이월해 병합한다.

실패 내성: 소스별 try/except. 네이버가 전멸하면 기존 파일을 보존하고 exit 0
(빈 파일로 덮어쓰지 않는다 — 대시보드가 직전 캘린더라도 계속 보여주도록).
"""
import os
import json
import datetime
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "public", "econ_calendar.json")

KST = datetime.timezone(datetime.timedelta(hours=9))

NAVER_BASE = "https://stock.naver.com/api/securityService/economic/indicator/nations"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_NEXT_URL = "https://nfs.faireconomy.media/ff_calendar_nextweek.json"

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
    "Referer": "https://stock.naver.com/",
}
TIMEOUT = 20

NATIONS = ["USA", "KOR"]
RELEASED_LOOKBACK_DAYS = 3   # 월요일 아침에 금·토·일 발표분 커버
UPCOMING_DAYS = 7

# -- 네이버 지표 <-> Forex Factory 컨센서스 매칭 테이블 -------------------------
# reuters 코드는 2026-07 API 실측치. 미확인 지표는 reuters=None + 한글명 부분일치.
# FF title 은 정확일치(같은 지표의 m/m·y/y 혼동 방지). 결합 조건에 KST 날짜 ±1일 포함.
US_INDICATOR_MAP = [
    {"reuters": "USCPNY=ECIX", "name": "소비자물가지수(CPI) 전년대비",      "ff": "CPI y/y"},
    {"reuters": "USCPI=ECIX",  "name": "소비자물가지수(CPI) 전월대비",      "ff": "CPI m/m"},
    {"reuters": "USCPFY=ECIX", "name": "근원 소비자물가지수(CPI) 전년대비", "ff": "Core CPI y/y"},
    {"reuters": "USCPF=ECIX",  "name": "근원 소비자물가지수(CPI) 전월대비", "ff": "Core CPI m/m"},
    {"reuters": "USPPFD=ECIX", "name": "생산자물가지수(PPI) 전월대비",      "ff": "PPI m/m"},
    {"reuters": "USPPIE=ECIX", "name": "근원 생산자물가지수(PPI) 전월대비", "ff": "Core PPI m/m"},
    {"reuters": "USRSL=ECIX",  "name": "소매판매 전월대비",                 "ff": "Retail Sales m/m"},
    {"reuters": "USRSLA=ECIX", "name": "근원 소매판매 전월대비",            "ff": "Core Retail Sales m/m"},
    {"reuters": "USNFAR=ECIX", "name": "비농업고용지수",                    "ff": "Non-Farm Employment Change"},
    {"reuters": "USUNR=ECIX",  "name": "실업률",                            "ff": "Unemployment Rate"},
    {"reuters": "USAVGE=ECIX", "name": "평균시간당소득 전월대비",           "ff": "Average Hourly Earnings m/m"},
    {"reuters": "USJOB=ECIX",  "name": "신규 실업수당청구건수",             "ff": "Unemployment Claims"},
    {"reuters": "USADP=ECIX",  "name": "ADP 비농업부문 고용 변화",          "ff": "ADP Non-Farm Employment Change"},
    {"reuters": "USJOLT=ECIX", "name": "JOLT",                              "ff": "JOLTS Job Openings"},
    {"reuters": "USPCEM=ECIX", "name": "근원 소비지출물가지수 전월대비",    "ff": "Core PCE Price Index m/m"},
    {"reuters": "USPMI=ECIX",  "name": "ISM 제조업 구매관리자지수",         "ff": "ISM Manufacturing PMI"},
    {"reuters": None,          "name": "ISM 비제조업",                      "ff": "ISM Services PMI"},
    {"reuters": None,          "name": "ISM 서비스",                        "ff": "ISM Services PMI"},
    {"reuters": "USCONC=ECIX", "name": "CB 소비자신뢰지수",                 "ff": "CB Consumer Confidence"},
    {"reuters": "USUMSP=ECIX", "name": "미시간대 소비자심리지수(잠정치)",   "ff": "Prelim UoM Consumer Sentiment"},
    {"reuters": "USDGN=ECIX",  "name": "내구재 수주 전월대비",              "ff": "Durable Goods Orders m/m"},
    {"reuters": "USDGXT=ECIX", "name": "근원 내구재수주 전월대비",          "ff": "Core Durable Goods Orders m/m"},
    # GDP 는 속보/잠정/확정에 따라 FF title 이 다르다 — 같은 날짜의 이벤트만 결합되므로 3개 모두 등록
    {"reuters": None, "name": "GDP", "ff": "Advance GDP q/q"},
    {"reuters": None, "name": "GDP", "ff": "Prelim GDP q/q"},
    {"reuters": None, "name": "GDP", "ff": "Final GDP q/q"},
    {"reuters": None, "name": "기준금리", "ff": "Federal Funds Rate"},
]

# 네이버 캘린더에 대응 항목이 없어도 upcoming 에 추가할 FF 단독 이벤트 (한글 라벨)
FF_STANDALONE = {
    "FOMC Statement":         "FOMC 성명서",
    "FOMC Press Conference":  "FOMC 기자회견",
    "FOMC Meeting Minutes":   "FOMC 의사록",
    "Federal Funds Rate":     "미국 기준금리 결정(FOMC)",
}
FF_IMPACT_IMPORTANCE = {"High": 4, "Medium": 3, "Low": 2}


# -- 수집 ----------------------------------------------------------------------

def _get_json(url, params=None):
    r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _naver_params(extra):
    p = [("nationTypeList", n) for n in NATIONS]
    p.extend(extra)
    return p


def _naver_released(date_yyyymmdd):
    """특정일 발표 지표 전체 (totalCount 기준 페이징)."""
    items, page, page_size = [], 1, 60
    while True:
        data = _get_json(NAVER_BASE + "/releaseDate", _naver_params([
            ("releaseDate", date_yyyymmdd), ("page", page), ("pageSize", page_size)]))
        chunk = data.get("indicators") or []
        items.extend(chunk)
        if page * page_size >= int(data.get("totalCount") or 0) or not chunk:
            return items
        page += 1


def _naver_upcoming(limit=100):
    data = _get_json(NAVER_BASE + "/upcoming", _naver_params([
        ("gteImportance", 1), ("limit", limit)]))
    return data if isinstance(data, list) else []


def _fetch_ff(now_kst):
    """이번주(+주말엔 다음주) FF 이벤트. 실패 시 []."""
    events = []
    urls = [FF_URL]
    if now_kst.weekday() >= 5:      # 토·일이면 다음주 파일도 병합
        urls.append(FF_NEXT_URL)
    for url in urls:
        try:
            data = _get_json(url)
            if isinstance(data, list):
                events.extend(data)
        except Exception as e:
            print(f"[econ] FF fetch 실패 ({url}): {e}")
    return events


# -- 변환 ----------------------------------------------------------------------

def _naver_kst(item):
    """releaseDate('20260715') + releaseTime('213000') -> 'YYYY-MM-DD HH:MM' (KST)."""
    d = str(item.get("releaseDate") or "")
    if len(d) != 8 or not d.isdigit():
        return None
    t = str(item.get("releaseTime") or "")
    hh, mm = (t[0:2], t[2:4]) if len(t) >= 4 and t[:4].isdigit() else ("00", "00")
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]} {hh}:{mm}"


def _ff_kst(ev):
    """FF ISO 날짜(미 동부 오프셋 포함) -> KST datetime. 실패 시 None."""
    try:
        return datetime.datetime.fromisoformat(ev["date"]).astimezone(KST)
    except Exception:
        return None


def _num(s):
    """FF 문자열 수치 파싱 -> (float, 접미사, 숫자 원문).
    '3.8%'->(3.8,'%','3.8'), '216K'->(216.0,'K','216'), '0.0%'->(0.0,'%','0.0').
    숫자 원문(text)은 표시용 — float 변환 시 '0.0'->'0' 처럼 정밀도가 떨어지는 것을 방지."""
    if s is None:
        return None, "", ""
    s = str(s).strip().replace(",", "")
    if not s:
        return None, "", ""
    suffix = ""
    if s.endswith("%"):
        suffix, s = "%", s[:-1]
    elif s and s[-1].upper() in ("K", "M", "B", "T"):
        suffix, s = s[-1].upper(), s[:-1]
    try:
        return float(s), suffix, s
    except ValueError:
        return None, "", ""


def _naver_suffix(row):
    """정규화된 row 기준 단위 접미사 ('%'/'K'/'M'/'B'/'')."""
    if (row.get("unit") or "") == "%":
        return "%"
    return (row.get("unitScale") or "").upper()


def _norm_naver(item):
    """네이버 응답 -> 출력 스키마 공통부."""
    released = bool(item.get("isRelease"))
    return {
        "nation": item.get("nationType"),
        "name": (item.get("name") or "").strip(),
        "reutersCode": item.get("reutersCode"),
        "importance": int(item.get("importance") or 0),
        "period": item.get("period"),
        "category": item.get("category"),
        "releaseAtKST": _naver_kst(item),
        "actual": item.get("actualValue") if released else None,
        "previous": item.get("previousValue"),
        "unit": item.get("indicatorUnit") or "",
        "unitScale": item.get("unitScale") or "",
        "forecast": None,
        "forecastSource": None,
        "source": "naver",
    }


# -- 컨센서스 매칭 --------------------------------------------------------------

def _map_entry(row):
    """네이버 항목에 해당하는 매핑 엔트리들(0..n)."""
    out = []
    for m in US_INDICATOR_MAP:
        if m["reuters"] and m["reuters"] == row.get("reutersCode"):
            out.append(m)
        elif m["reuters"] is None and m["name"] in (row.get("name") or ""):
            out.append(m)
    return out


def _match_forecast(row, ff_events):
    """미국 지표에 FF 컨센서스 결합 -> (float, 숫자 원문) 또는 None.
    단위 접미사 불일치 시 포기(틀린 서프라이즈 방지)."""
    if row.get("nation") != "USA" or not row.get("releaseAtKST"):
        return None
    row_date = datetime.date.fromisoformat(row["releaseAtKST"][:10])
    for m in _map_entry(row):
        for ev in ff_events:
            if ev.get("country") != "USD" or ev.get("title") != m["ff"]:
                continue
            dt = _ff_kst(ev)
            if dt is None or abs((dt.date() - row_date).days) > 1:
                continue
            val, suffix, text = _num(ev.get("forecast"))
            if val is None:
                continue
            if suffix != _naver_suffix(row):
                print(f"[econ] 단위 불일치로 컨센서스 제외: {row['name']} "
                      f"(FF '{ev.get('forecast')}' vs naver '{_naver_suffix(row)}')")
                continue
            return val, text
    return None


def _load_prev_forecasts():
    """직전 econ_calendar.json 의 forecast 이월맵 {(지표키, 발표일): (forecast, 원문)}."""
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        return {}
    out = {}
    for row in (prev.get("released") or []) + (prev.get("upcoming") or []):
        fc = row.get("forecast")
        at = row.get("releaseAtKST") or ""
        if fc is None or not at:
            continue
        key = (row.get("reutersCode") or row.get("name"), at[:10])
        out[key] = (fc, row.get("forecastText") or "")
    return out


def _surprise(row):
    """actual vs forecast(있으면) / previous. verdict: above|inline|below."""
    actual = row.get("actual")
    if actual is None:
        return None
    if row.get("forecast") is not None:
        base, vs = row["forecast"], "forecast"
    elif row.get("previous") is not None:
        base, vs = row["previous"], "previous"
    else:
        return None
    try:
        diff = round(float(actual) - float(base), 4)
    except (TypeError, ValueError):
        return None
    verdict = "inline" if diff == 0 else ("above" if diff > 0 else "below")
    return {"vs": vs, "diff": diff, "verdict": verdict}


# -- 조립 ----------------------------------------------------------------------

def build(now_kst):
    today = now_kst.date()
    sources = {"naver": False, "forexfactory": False}

    naver_released = []
    for back in range(RELEASED_LOOKBACK_DAYS, -1, -1):
        d = today - datetime.timedelta(days=back)
        try:
            naver_released.extend(_naver_released(d.strftime("%Y%m%d")))
            sources["naver"] = True
        except Exception as e:
            print(f"[econ] naver releaseDate {d} 실패: {e}")

    naver_upcoming = []
    try:
        naver_upcoming = _naver_upcoming()
        if naver_upcoming:
            sources["naver"] = True
    except Exception as e:
        print(f"[econ] naver upcoming 실패: {e}")

    ff_events = _fetch_ff(now_kst)
    sources["forexfactory"] = bool(ff_events)
    prev_forecasts = _load_prev_forecasts()

    def attach_forecast(row):
        m = _match_forecast(row, ff_events)
        if m is not None:
            row["forecast"], row["forecastText"] = m
            row["forecastSource"] = "forexfactory"
            return
        key = (row.get("reutersCode") or row.get("name"),
               (row.get("releaseAtKST") or "")[:10])
        if key in prev_forecasts:
            row["forecast"], row["forecastText"] = prev_forecasts[key]
            row["forecastSource"] = "carryover"

    released, seen = [], set()
    for item in naver_released:
        if not item.get("isRelease"):
            continue
        row = _norm_naver(item)
        key = (row["reutersCode"] or row["name"], row["releaseAtKST"])
        if not row["releaseAtKST"] or key in seen:
            continue
        seen.add(key)
        attach_forecast(row)
        row["surprise"] = _surprise(row)
        released.append(row)

    horizon = f"{today + datetime.timedelta(days=UPCOMING_DAYS)} 23:59"
    today_str = str(today)
    upcoming, up_seen = [], set()
    for item in naver_upcoming:
        if item.get("isRelease") or item.get("nationType") not in NATIONS:
            continue
        row = _norm_naver(item)
        at = row["releaseAtKST"]
        key = (row["reutersCode"] or row["name"], at)
        if not at or not (today_str <= at[:10] <= horizon[:10]) or key in up_seen:
            continue
        up_seen.add(key)
        attach_forecast(row)
        row.pop("actual", None)
        upcoming.append(row)

    # FF 단독 이벤트(FOMC 등) — 같은 날짜에 네이버 기준금리 항목이 이미 있으면 중복 제외
    naver_days = {(r["name"], r["releaseAtKST"][:10]) for r in upcoming}
    matched_ff_titles = set()
    for r in upcoming:
        if r.get("forecastSource") == "forexfactory":
            for m in _map_entry(r):
                matched_ff_titles.add((m["ff"], r["releaseAtKST"][:10]))
    for ev in ff_events:
        title = ev.get("title")
        if ev.get("country") != "USD" or title not in FF_STANDALONE:
            continue
        dt = _ff_kst(ev)
        if dt is None or not (today_str <= str(dt.date()) <= horizon[:10]):
            continue
        if (title, str(dt.date())) in matched_ff_titles:
            continue
        label = FF_STANDALONE[title]
        if (label, str(dt.date())) in naver_days:
            continue
        val, suffix, text = _num(ev.get("forecast"))
        pval, _, _ = _num(ev.get("previous"))
        upcoming.append({
            "nation": "USA", "name": label, "reutersCode": None,
            "importance": FF_IMPACT_IMPORTANCE.get(ev.get("impact"), 3),
            "period": None, "category": "FOMC",
            "releaseAtKST": dt.strftime("%Y-%m-%d %H:%M"),
            "previous": pval, "unit": suffix if suffix == "%" else "",
            "unitScale": "" if suffix == "%" else suffix,
            "forecast": val, "forecastText": text,
            "forecastSource": "forexfactory" if val is not None else None,
            "source": "forexfactory",
        })

    released.sort(key=lambda r: (r["releaseAtKST"], -r["importance"]))
    upcoming.sort(key=lambda r: (r["releaseAtKST"], -r["importance"]))

    return {
        "date": today_str,
        "asof": now_kst.strftime("%Y-%m-%d %H:%M KST"),
        "sources": sources,
        "released": released,
        "upcoming": upcoming,
    }


def main():
    now_kst = datetime.datetime.now(KST)
    payload = build(now_kst)

    if not payload["sources"]["naver"]:
        # 캘린더 본체(네이버)가 전멸 — 기존 파일을 보존하고 파이프라인은 계속 진행
        print("[econ] naver 소스 전멸 — 기존 econ_calendar.json 보존")
        return

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    n_fc = sum(1 for r in payload["released"] + payload["upcoming"]
               if r.get("forecast") is not None)
    print(f"[econ] released {len(payload['released'])}건 / upcoming {len(payload['upcoming'])}건 "
          f"/ 컨센서스 {n_fc}건 -> {os.path.relpath(OUT_PATH, ROOT)}")


if __name__ == "__main__":
    main()
