"""한국 실적발표 캘린더 수집 -> public/earnings_calendar.json

소스 3개를 병합한다 (fetch_econ_calendar.py 와 동일 설계 철학):
  1. WiseReport 어닝스캘린더 AJAX (FnGuide 계열, 인증 불필요) — 일정의 SSOT.
       GET comp.wisereport.co.kr/wiseCalendar/GetCalendarAjax.aspx?call_typ=3&param1=YYYYMM
     발표 예정일 + 컨센서스(영업이익·순이익·YoY/QoQ) + 기업발표 잠정치 + 어닝서프라이즈
     괴리율 + 투자의견·목표주가. memo1~22 슬롯은 memoType(0=연결/분기, 1=개별/연간)에
     따라 위치가 달라지므로 반드시 라벨 정규식으로 파싱한다(슬롯 번호 의존 금지).
  2. FnGuide 실적속보 JSON — 발표 결과 보강 (매출액 + YoY + 컨상/컨하/턴어 태그).
       GET comp.fnguide.com/SVO2/common/sp_read_json_cache.asp?cmdText=menu_9_1&IN_gs_ym=...
     IN_gs_ym = 실적 귀속 분기의 말월(2Q=YYYY06). 최근 2개 분기를 조회.
  3. DART 공시목록 list.json — 잠정실적 공시 원문 링크 + 캘린더에 없는 발표 감지.
     제목 필터: 잠정실적/영업(잠정)실적/손익구조. (list 는 접수 '날짜'만 제공, 시각 없음)

금액 단위: WiseReport·FnGuide 모두 억원 (삼성전자 2Q 영업이익 894,000억 실측 일치).

실패 내성: 소스별 try/except 독립(부분 성공 허용). 주 소스(WiseReport)가 전멸하면
기존 파일의 upcoming 을 이월하고, 그마저 없으면 기존 파일을 보존한 채 exit 0.
"""
import os
import re
import json
import datetime

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "public", "earnings_calendar.json")

KST = datetime.timezone(datetime.timedelta(hours=9))

WISE_URL = "https://comp.wisereport.co.kr/wiseCalendar/GetCalendarAjax.aspx"
FNGUIDE_URL = "https://comp.fnguide.com/SVO2/common/sp_read_json_cache.asp"
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
}
TIMEOUT = 20

RELEASED_LOOKBACK_DAYS = int(os.environ.get("EARNINGS_LOOKBACK_DAYS", "7"))
UPCOMING_DAYS = int(os.environ.get("EARNINGS_UPCOMING_DAYS", "30"))

DART_KEY = os.environ.get("DART_API_KEY", "")
# 실적 공시 제목 키워드 (screener.py POSITIVE_KEYWORDS 실적 그룹과 동일 취지)
DART_EARNINGS_KEYWORDS = ("잠정실적", "영업(잠정)실적", "영업실적", "손익구조")
DART_EXCLUDE_KEYWORDS = ("정정", "첨부")


def _warn(msg):
    print(f"[earnings] {msg}")


# ── 파서 (순수 함수 — 픽스처 테스트 대상) ────────────────────────────────────

_NUM_RE = r"(-?[\d,]+(?:\.\d+)?)"


def _num(text):
    """'4,007' / '-39.8' / '' / None -> float | None"""
    if text is None:
        return None
    m = re.search(_NUM_RE, str(text).replace(" ", ""))
    return float(m.group(1).replace(",", "")) if m else None


def parse_wisereport(payload: dict) -> list[dict]:
    """GetCalendarAjax(call_typ=3) 월간 응답 -> 종목 행 목록.

    memo 슬롯 위치가 memoType 에 따라 달라 라벨 텍스트를 순서대로 훑으며
    섹션([컨센서스]/[기업발표잠정치]/[어닝서프라이즈])을 추적해 파싱한다.
    """
    out = []
    for row in payload.get("jsonData", []):
        code = row.get("cmp_cd")
        if not code:
            continue                       # 종목 없는 달력 빈 칸
        item = {
            "date":    row.get("dt"),
            "code":    code,
            "name":    row.get("cmp_nm_kor"),
            "sector":  row.get("SEC_NM"),
            "fs":      None,               # 연결/개별
            "period":  None,               # "202606"
            "periodType": None,            # 분기/연간
            "opinion": None, "targetPrice": None,
            "consensus":   {"op": None, "np": None, "yoy": None, "qoq": None},
            "provisional": {"op": None, "np": None},
            "surprise":    {"opGap": None, "npGap": None},
        }
        desc = row.get("description") or ""
        m = re.search(r"\((연결|개별)\)", desc)
        if m:
            item["fs"] = m.group(1)

        section = None
        for i in range(1, 23):
            memo = row.get(f"memo{i}")
            if not memo:
                continue
            t = memo.strip()
            if t.startswith("[컨센서스]"):
                section = "consensus"; continue
            if t.startswith("[기업발표잠정치]"):
                section = "provisional"; continue
            if t.startswith("[어닝서프라이즈]"):
                section = "surprise"; continue

            if t.startswith("투자의견"):
                item["opinion"] = _num(t.partition(":")[2]); continue
            if t.startswith("목표주가"):
                item["targetPrice"] = _num(t.partition(":")[2]); continue
            if t.startswith("종목코드"):
                continue

            pm = re.search(r"(\d{6})\((분기|연간)\)", t)
            if pm and item["period"] is None:
                item["period"], item["periodType"] = pm.group(1), pm.group(2)

            gap = re.search(r"(영업이익|순이익)괴리율\((연결|개별)\)\s*:\s*(.*)", t)
            if gap:
                key = "opGap" if gap.group(1) == "영업이익" else "npGap"
                item["surprise"][key] = _num(gap.group(3))
                continue
            val = re.search(r"(영업이익|순이익)\((연결|개별)\)\s*:\s*(.*)", t)
            if val and section in ("consensus", "provisional"):
                key = "op" if val.group(1) == "영업이익" else "np"
                item[section][key] = _num(val.group(3))
                if item["fs"] is None:
                    item["fs"] = val.group(2)
                continue
            yq = re.search(r"YoY\s*:\s*([^/]*)/\s*QoQ\s*:\s*(.*)", t)
            if yq and section == "consensus":
                item["consensus"]["yoy"] = _num(yq.group(1))
                item["consensus"]["qoq"] = _num(yq.group(2))
        out.append(item)
    return out


def parse_fnguide(payload: dict) -> list[dict]:
    """실적속보(sp_read_json_cache, menu_9_1) 응답 -> 발표 결과 행 목록.

    SALES2/OPER2/NET2 는 YoY(%) 숫자 또는 '흑전/적전/적지' 텍스트 — 텍스트는 보존.
    DIS_DT 'YY/MM/DD' -> 'YYYY-MM-DD'.
    """
    def yoy(text):
        if text is None:
            return None
        t = str(text).strip()
        if not t:
            return None
        n = _num(t)
        return n if n is not None else t   # 흑전/적전/적지 등 텍스트 보존

    out = []
    for row in payload.get("data", []):
        gicode = row.get("GICODE") or ""
        code = gicode[1:] if gicode.startswith("A") else gicode
        if not code:
            continue
        dis = (row.get("DIS_DT") or "").strip()      # "26/07/07"
        date = None
        m = re.match(r"(\d{2})/(\d{2})/(\d{2})", dis)
        if m:
            date = f"20{m.group(1)}-{m.group(2)}-{m.group(3)}"
        out.append({
            "date":     date,
            "code":     code,
            "name":     row.get("ITEMNM"),
            "quarter":  row.get("GS_GB"),            # "2Q"
            "fs":       row.get("REP_GB"),           # 연결/별도
            "sales":    _num(row.get("SALES")),
            "op":       _num(row.get("OPER")),
            "np":       _num(row.get("NET")),
            "salesYoY": yoy(row.get("SALES2")),
            "opYoY":    yoy(row.get("OPER2")),
            "npYoY":    yoy(row.get("NET2")),
            "tag":      (row.get("ISSUE") or None),  # 컨상/컨하/턴어
            "kind":     row.get("DA_GB"),            # 잠정
        })
    return out


def parse_dart(rows: list[dict]) -> list[dict]:
    """DART list.json 행 -> 실적 공시만 필터한 {date, code, title, url}."""
    out = []
    for r in rows:
        title = (r.get("report_nm") or "").strip()
        code = (r.get("stock_code") or "").strip()
        if not code or not title:
            continue
        if not any(k in title for k in DART_EARNINGS_KEYWORDS):
            continue
        if any(k in title for k in DART_EXCLUDE_KEYWORDS):
            continue
        d = (r.get("rcept_dt") or "").strip()        # YYYYMMDD
        out.append({
            "date":  f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else None,
            "code":  code,
            "name":  (r.get("corp_name") or "").strip() or None,   # DART 단독 종목 이름 표시용
            "title": title,
            "url":   f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r.get('rcept_no')}",
        })
    return out


# ── 수집 ─────────────────────────────────────────────────────────────────────

def fetch_wisereport(months: list[str]) -> list[dict]:
    """당월+익월 캘린더. 실패한 달은 건너뜀(부분 성공 허용)."""
    rows = []
    headers = dict(UA, Referer="https://comp.wisereport.co.kr/wiseCalendar/EarningsReleaseMonthlyView.aspx")
    for ym in months:
        try:
            r = requests.get(WISE_URL, params={"call_typ": "3", "param1": ym},
                             headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            rows.extend(parse_wisereport(r.json()))
        except Exception as e:
            _warn(f"wisereport {ym} 실패: {e}")
    # 월 경계 중복(달력이 전후 주를 포함) 제거
    seen, uniq = set(), []
    for x in rows:
        k = (x["code"], x["date"], x["period"])
        if k in seen:
            continue
        seen.add(k); uniq.append(x)
    return uniq


def fetch_fnguide(gs_yms: list[str]) -> list[dict]:
    rows = []
    headers = dict(UA, Referer="https://comp.fnguide.com/SVO2/ASP/SVD_ProResultCorp.asp")
    for ym in gs_yms:
        try:
            r = requests.get(FNGUIDE_URL, params={
                "cmdText": "menu_9_1", "IN_gs_ym": ym, "IN_gs_gb": "N",
                "IN_report_gb": "X", "IN_gb": "D", "IN_SRC_GB": "SVO",
            }, headers=headers, timeout=TIMEOUT)
            r.raise_for_status()
            rows.extend(parse_fnguide(r.json()))
        except Exception as e:
            _warn(f"fnguide {ym} 실패: {e}")
    seen, uniq = set(), []
    for x in rows:
        k = (x["code"], x["date"], x["quarter"])
        if k in seen:
            continue
        seen.add(k); uniq.append(x)
    return uniq


def fetch_dart(bgn: str, end: str) -> list[dict]:
    """상장(Y)+코스닥(K) 최근 공시를 페이지네이션 스캔 후 실적 공시 필터."""
    if not DART_KEY:
        _warn("DART_API_KEY 미설정 — dart 소스 생략")
        return []
    raw = []
    for cls in ("Y", "K"):
        page = 1
        while page <= 10:
            try:
                r = requests.get(DART_LIST_URL, params={
                    "crtfc_key": DART_KEY, "bgn_de": bgn, "end_de": end,
                    "corp_cls": cls, "page_no": page, "page_count": 100,
                }, timeout=TIMEOUT)
                data = r.json()
                if data.get("status") != "000":
                    break
                raw.extend(data.get("list", []))
                if page >= int(data.get("total_page", 1)):
                    break
                page += 1
            except Exception as e:
                _warn(f"dart {cls} p{page} 실패: {e}")
                break
    return parse_dart(raw)


# ── 병합 ─────────────────────────────────────────────────────────────────────

def _recent_quarters(today: datetime.date) -> list[str]:
    """실적 귀속 분기 말월 후보 2개 (예: 7월 -> ['202606', '202603'])."""
    q_end_month = ((today.month - 1) // 3) * 3      # 직전 분기 말월 (0이면 전년 12월)
    y, m = today.year, q_end_month
    if m == 0:
        y, m = y - 1, 12
    prev_y, prev_m = (y, m - 3) if m > 3 else (y - 1, 12)
    return [f"{y}{m:02d}", f"{prev_y}{prev_m:02d}"]


def build(wise: list[dict], fng: list[dict], dart: list[dict],
          today: datetime.date) -> dict:
    """3소스 병합 -> {upcoming, released}. 순수 함수(테스트 대상)."""
    today_s = today.isoformat()
    lookback_s = (today - datetime.timedelta(days=RELEASED_LOOKBACK_DAYS)).isoformat()
    horizon_s = (today + datetime.timedelta(days=UPCOMING_DAYS)).isoformat()

    upcoming = sorted(
        (w for w in wise
         if w["date"] and today_s <= w["date"] <= horizon_s
         and w["provisional"]["op"] is None and w["provisional"]["np"] is None),
        key=lambda w: (w["date"], -(w["consensus"]["op"] or 0)))

    # released: code 기준 outer-join (발표일은 소스마다 같아야 정상 — 다르면 최신 우선)
    rel: dict[str, dict] = {}

    def slot(code):
        return rel.setdefault(code, {
            "date": None, "code": code, "name": None, "sector": None,
            "quarter": None, "period": None, "fs": None,
            "sales": None, "op": None, "np": None,
            "salesYoY": None, "opYoY": None, "npYoY": None,
            "tag": None, "consensus": {"op": None, "np": None},
            "surprise": {"opGap": None, "npGap": None},
            "dartUrl": None, "dartTitle": None, "sources": [],
        })

    for w in wise:
        if not w["date"] or not (lookback_s <= w["date"] <= today_s):
            continue
        if w["provisional"]["op"] is None and w["provisional"]["np"] is None:
            continue                                   # 잠정치 없으면 미발표
        s = slot(w["code"])
        s.update({"date": w["date"], "name": w["name"], "sector": w["sector"],
                  "period": w["period"], "fs": s["fs"] or w["fs"]})
        s["op"] = s["op"] if s["op"] is not None else w["provisional"]["op"]
        s["np"] = s["np"] if s["np"] is not None else w["provisional"]["np"]
        s["consensus"] = w["consensus"]
        s["surprise"] = w["surprise"]
        s["sources"].append("wisereport")

    for f in fng:
        if not f["date"] or not (lookback_s <= f["date"] <= today_s):
            continue
        s = slot(f["code"])
        s["date"] = max(s["date"] or f["date"], f["date"])
        s["name"] = s["name"] or f["name"]
        s["quarter"] = f["quarter"]
        s["fs"] = s["fs"] or f["fs"]
        for k in ("sales", "op", "np", "salesYoY", "opYoY", "npYoY"):
            if s.get(k) is None:
                s[k] = f[k]
        s["tag"] = f["tag"]
        s["sources"].append("fnguide")

    for d in dart:
        if not d["date"] or not (lookback_s <= d["date"] <= today_s):
            continue
        s = slot(d["code"])
        s["date"] = s["date"] or d["date"]
        s["name"] = s["name"] or d.get("name")   # DART 단독 종목도 이름 표시(240600 사례)
        s["dartUrl"], s["dartTitle"] = d["url"], d["title"]
        s["sources"].append("dart")

    released = sorted(rel.values(),
                      key=lambda x: (x["date"] or "", abs(x["surprise"]["opGap"] or 0)),
                      reverse=True)
    return {"upcoming": upcoming, "released": released}


# ── 메인 ─────────────────────────────────────────────────────────────────────

def _load_prev() -> dict:
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def main():
    now = datetime.datetime.now(KST)
    today = now.date()
    this_ym = today.strftime("%Y%m")
    nxt = (today.replace(day=1) + datetime.timedelta(days=32)).strftime("%Y%m")

    wise = fetch_wisereport([this_ym, nxt])
    fng = fetch_fnguide(_recent_quarters(today))
    bgn = (today - datetime.timedelta(days=RELEASED_LOOKBACK_DAYS)).strftime("%Y%m%d")
    dart = fetch_dart(bgn, today.strftime("%Y%m%d"))

    prev = _load_prev()
    if not wise:
        # 주 소스 전멸 — 직전 파일 upcoming 이월 (예정 일정이 사라지지 않게)
        carried = prev.get("upcoming", [])
        if not carried and not fng and not dart:
            _warn("전 소스 실패 — 기존 파일 보존")
            return
        _warn(f"wisereport 전멸 — 직전 upcoming {len(carried)}건 이월")

    merged = build(wise, fng, dart, today)
    if not wise and prev.get("upcoming"):
        merged["upcoming"] = prev["upcoming"]

    out = {
        "date": today.isoformat(),
        "asof": now.strftime("%Y-%m-%d %H:%M KST"),
        "sources": {"wisereport": bool(wise), "fnguide": bool(fng), "dart": bool(dart)},
        "upcoming": merged["upcoming"],
        "released": merged["released"],
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    _warn(f"저장: upcoming {len(out['upcoming'])}건 / released {len(out['released'])}건 "
          f"(wise={len(wise)} fng={len(fng)} dart={len(dart)})")


if __name__ == "__main__":
    main()
