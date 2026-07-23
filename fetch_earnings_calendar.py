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

개별 종목 보강 (2026-07-23 — 위 캘린더 소스만으론 수치·컨센서스 결손이 잦음):
  4. 네이버 모바일 finance/quarter JSON — 분기 실적 5개+컨센서스(isConsensus 플래그,
     기간 키 명시). 결손 실적·컨센서스·전년동기(YoY 계산 기반)를 종목 단위로 채움.
  5. FnGuide wcomp getCnsPerforTrend JSON — 컨센서스·전년동기대비·컨센서스대비(%)
     행 제공. 네이버가 못 채운 필드의 2차 폴백.
  회차당 종목 조회 상한 EARNINGS_ENRICH_MAX(기본 10).

금액 단위: WiseReport·FnGuide·네이버 모두 억원 (삼성전자 2Q 영업이익 894,000억 실측 일치).

실패 내성: 소스별 try/except 독립(부분 성공 허용). 주 소스(WiseReport)가 전멸하면
기존 파일의 upcoming 을 이월하고, 그마저 없으면 기존 파일을 보존한 채 exit 0.
"""
import os
import re
import json
import time
import datetime

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "public", "earnings_calendar.json")

KST = datetime.timezone(datetime.timedelta(hours=9))

WISE_URL = "https://comp.wisereport.co.kr/wiseCalendar/GetCalendarAjax.aspx"
FNGUIDE_URL = "https://comp.fnguide.com/SVO2/common/sp_read_json_cache.asp"
DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
NAVER_FIN_URL = "https://m.stock.naver.com/api/stock/{code}/finance/quarter"
WCOMP_CNS_URL = "https://wcomp.fnguide.com/CompanyInfo/getCnsPerforTrend"

UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/125.0.0.0 Safari/537.36"),
}
TIMEOUT = 20

RELEASED_LOOKBACK_DAYS = int(os.environ.get("EARNINGS_LOOKBACK_DAYS", "7"))
UPCOMING_DAYS = int(os.environ.get("EARNINGS_UPCOMING_DAYS", "30"))
ENRICH_MAX = int(os.environ.get("EARNINGS_ENRICH_MAX", "40"))   # 회차당 개별 종목 조회 상한(오늘자 우선, 마감일 다발표 대비)

DART_KEY = os.environ.get("DART_API_KEY", "")
# 실적 공시 제목 키워드 (screener.py POSITIVE_KEYWORDS 실적 그룹과 동일 취지)
DART_EARNINGS_KEYWORDS = ("잠정실적", "영업(잠정)실적", "영업실적", "손익구조")
DART_EXCLUDE_KEYWORDS = ("정정", "첨부")

# DART 공시 원문(공정공시 뷰어) — 집계 사이트 미반영 종목의 '실제' 잠정실적을 직접 파싱.
# viewer 는 인증키 불필요·EUC-KR. dcmNo 는 main.do 의 viewDoc("rcpNo","dcmNo",...) 에서 추출.
DART_VIEW_MAIN = "https://dart.fss.or.kr/dsaf001/main.do"
DART_VIEW_DOC = "https://dart.fss.or.kr/report/viewer.do"
DART_DOC_ON = os.environ.get("EARNINGS_DART_DOC", "1") != "0"
# 표 단위 → 억원 환산계수
_DART_UNIT = {"조원": 10000.0, "십억원": 10.0, "억원": 1.0, "백만원": 0.01,
              "천원": 1e-5, "백원": 1e-6, "원": 1e-8}
# 전년동기대비 흑자적자전환 표기 정규화 (frontend _earnBadge 태그와 정합)
_DART_FLAG = {"흑자전환": "흑전", "적자전환": "적전", "적자지속": "적지",
              "흑전": "흑전", "적전": "적전", "적지": "적지"}


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


def _dart_num(text):
    """공시 표 셀 -> float. '△1,691'/'(1,691)'/'-1,691' = 음수, '-'/'' = 미기재(None)."""
    if text is None:
        return None
    s = str(text).replace(" ", "").replace("\xa0", "")
    if s in ("", "-", "–", "—"):
        return None
    neg = s[:1] in ("△", "▲", "-") or (s[:1] == "(" and s[-1:] == ")")
    m = re.search(r"[\d,]+(?:\.\d+)?", s)
    if not m:
        return None
    v = float(m.group(0).replace(",", ""))
    return -v if neg else v


def parse_dart_document(html: str) -> dict:
    """영업(잠정)실적 공정공시 뷰어 HTML -> 실제 실적(억원) + 전년동기대비.

    표 구조: 지표(매출액/영업이익/당기순이익) 행마다 '당해실적' 칸 뒤로
    [당기실적, 전기실적, 전기대비%, 전기흑적, 전년동기실적, 전년동기대비%, 전년동기흑적].
    단위는 '단위 : 백만원' 등에서 읽어 억원으로 환산. 회사가 미공시한 지표는 '-'(제외)."""
    from bs4 import BeautifulSoup     # 지연 import — 미설치 환경서도 모듈 로드는 유지
    m = re.search(r"단위\s*[:：]\s*([가-힣]+원)", html)
    factor = _DART_UNIT.get(m.group(1) if m else "", 0.01)   # 기본 백만원
    soup = BeautifulSoup(html, "html.parser")
    want = {"매출액": ("sales", "salesYoY"), "영업이익": ("op", "opYoY"),
            "당기순이익": ("np", "npYoY")}
    out = {}
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True).replace("\xa0", " ").strip()
                 for c in tr.find_all(["td", "th"])]
        if not cells:
            continue
        keys = want.get(cells[0].replace(" ", ""))
        if not keys or keys[0] in out or "당해실적" not in cells:
            continue
        vk, yk = keys
        i = cells.index("당해실적")
        cur = _dart_num(cells[i + 1]) if i + 1 < len(cells) else None
        if cur is None:
            continue                                    # 회사 미공시 지표
        out[vk] = round(cur * factor, 1)
        yoy_pct = _dart_num(cells[i + 6]) if i + 6 < len(cells) else None
        yoy_flag = cells[i + 7].replace(" ", "") if i + 7 < len(cells) else ""
        if yoy_pct is not None:
            out[yk] = yoy_pct
        elif yoy_flag in _DART_FLAG:
            out[yk] = _DART_FLAG[yoy_flag]
    return out


def _yoy_calc(cur, prev):
    """전년동기 대비 증감 — 적자 구간은 관례 텍스트(흑전/적전/적지)."""
    if cur is None or prev is None:
        return None
    if prev < 0:
        return "흑전" if cur >= 0 else "적지"
    if cur < 0:
        return "적전"
    if prev == 0:
        return None
    return round((cur / prev - 1) * 100, 1)


def parse_naver_finance(payload: dict) -> dict:
    """m.stock.naver.com /api/stock/{code}/finance/quarter 응답 ->
    {"periods": {"202606": True(컨센서스)}, "metrics": {"op": {"202606": 1640.0}}} (억원)."""
    fi = (payload or {}).get("financeInfo") or {}
    periods = {t["key"]: t.get("isConsensus") == "Y"
               for t in fi.get("trTitleList") or [] if t.get("key")}
    name_map = {"매출액": "sales", "영업이익": "op", "당기순이익": "np"}
    metrics = {v: {} for v in name_map.values()}
    for row in fi.get("rowList") or []:
        key = name_map.get((row.get("title") or "").strip())
        if not key:
            continue
        for per, cell in (row.get("columns") or {}).items():
            v = _num((cell or {}).get("value"))
            if v is not None:
                metrics[key][per] = v
    return {"periods": periods, "metrics": metrics, "yoy": {}, "consGap": {}}


def parse_wcomp_cns(payload: dict) -> dict:
    """wcomp.fnguide.com getCnsPerforTrend(freq_typ=Q) 응답 -> parse_naver_finance 와
    같은 형태 + 소스 제공 yoy(전년동기대비 행)·consGap(컨센서스대비 행, %)."""
    ds = (payload or {}).get("dataset") or {}
    header = {h["CD"]: (h["YYMM"].replace("/", ""), (h.get("EP_CHK") or "").strip() == "E")
              for h in ds.get("header") or []}
    ac_map = {"121000": "sales", "121450": "op", "122700": "np"}
    out = {"periods": {per: is_e for per, is_e in header.values()},
           "metrics": {}, "yoy": {}, "consGap": {}}
    for row in ds.get("data") or []:
        key = ac_map.get(row.get("AC_CODE") or "")
        if not key:
            continue
        name = (row.get("NAME") or "").strip()
        vals = {per: row.get(cd) for cd, (per, _) in header.items()}
        if row.get("LVL") == 1:
            out["metrics"][key] = {p: _num(v) for p, v in vals.items() if _num(v) is not None}
        elif name == "전년동기대비":       # 숫자(%) 또는 '적자지속' 등 텍스트
            out["yoy"][key] = {p: (_num(v) if _num(v) is not None else str(v).strip())
                               for p, v in vals.items() if v not in (None, "")}
        elif name == "컨센서스대비":
            out["consGap"][key] = {p: _num(v) for p, v in vals.items() if _num(v) is not None}
    return out


def _target_period(row, today: datetime.date) -> str:
    """행의 실적 귀속 분기 말월(YYYYMM) — period 명시가 없으면 발표일 기준 직전 분기."""
    if row.get("period"):
        return row["period"]
    d = row.get("date") or today.isoformat()
    y, m = int(d[:4]), int(d[5:7])
    qm = ((m - 1) // 3) * 3
    if qm == 0:
        y, qm = y - 1, 12
    return f"{y}{qm:02d}"


def _enrich_released(r, per, src, tag_src):
    """released 행의 결손 필드를 소스 하나로 채움. 채운 게 있으면 True."""
    prev = f"{int(per[:4]) - 1}{per[4:]}"
    is_cons = src["periods"].get(per)      # True=컨센서스, False=발표 실적, None=기간 없음
    filled = False
    for k in ("sales", "op", "np"):
        v = (src["metrics"].get(k) or {}).get(per)
        if v is None:
            continue
        if is_cons:                        # 아직 추정치 — 컨센서스 슬롯만
            if k != "sales" and r["consensus"].get(k) is None:
                r["consensus"][k] = v
                filled = True
            continue
        if r.get(k) is None:               # 발표 실적
            r[k] = v
            filled = True
        yk = {"sales": "salesYoY", "op": "opYoY", "np": "npYoY"}[k]
        if r.get(yk) is None:
            yv = (src["yoy"].get(k) or {}).get(per)
            if yv is None:
                yv = _yoy_calc(v, (src["metrics"].get(k) or {}).get(prev))
            if yv is not None:
                r[yk] = yv
                filled = True
    # 컨센서스대비(%) — 소스 제공 우선, 없으면 실적·컨센서스로 계산
    if r["surprise"]["opGap"] is None:
        gap = (src["consGap"].get("op") or {}).get(per)
        if gap is None and r.get("op") is not None and r["consensus"].get("op"):
            cons = r["consensus"]["op"]
            gap = round((r["op"] - cons) / abs(cons) * 100, 1)
        if gap is not None:
            r["surprise"]["opGap"] = gap
            filled = True
    # 배지 태그 — 적자 관련은 YoY 텍스트로 보완 (컨상/컨하/부합은 프론트가 opGap 으로 판정)
    if r.get("tag") is None and isinstance(r.get("opYoY"), str):
        r["tag"] = {"흑전": "턴어", "적전": "적전", "적지": "적지"}.get(r["opYoY"])
    if filled and tag_src not in r["sources"]:
        r["sources"].append(tag_src)
    return filled


def _enrich_upcoming(u, per, src, tag_src):
    """upcoming 행의 컨센서스 결손만 채움 (실적은 아직 없음)."""
    if not src["periods"].get(per):        # 해당 분기가 컨센서스 상태일 때만
        return False
    prev = f"{int(per[:4]) - 1}{per[4:]}"
    filled = False
    for k in ("op", "np"):
        v = (src["metrics"].get(k) or {}).get(per)
        if v is not None and u["consensus"].get(k) is None:
            u["consensus"][k] = v
            filled = True
    if u["consensus"].get("yoy") is None and u["consensus"].get("op") is not None:
        yv = _yoy_calc(u["consensus"]["op"], (src["metrics"].get("op") or {}).get(prev))
        if isinstance(yv, float):
            u["consensus"]["yoy"] = yv
            filled = True
    return filled


def enrich_calendar(released, upcoming, today: datetime.date,
                    fetch_naver=None, fetch_wcomp=None, fetch_doc=None) -> int:
    """개별 종목으로 결손 보강. 보강 행 수 반환.

    released 실적 미집계(op None)는 DART 공시 원문에서 실제 실적을 직접 파싱하고
    (집계 사이트 지연 무관), 이어 네이버 -> FnGuide 로 컨센서스를 보강한다.
    fetch_* 는 테스트 픽스처 주입용. 조회 종목 수는 ENRICH_MAX 로 제한."""
    fetch_naver = fetch_naver or _naver_finance
    fetch_wcomp = fetch_wcomp or _wcomp_cns
    fetch_doc = fetch_doc or _dart_document
    today_s = today.isoformat()

    def _rel_needs(r):
        return r["op"] is None or r["sales"] is None or r["consensus"]["op"] is None

    # 대시보드는 '오늘자' 행만 표시(_load_earnings_events) → 오늘자를 우선 보강한다.
    # (과거 released 를 먼저 보강하면 ENRICH_MAX 예산이 표시 안 되는 행에 소진돼
    #  오늘 예정 종목이 조회되지 못했던 회귀: 두산밥캣 2026-07-23.)
    targets = [("rel", r) for r in released if r.get("date") == today_s and _rel_needs(r)]
    targets += [("up", u) for u in upcoming if u["date"] == today_s
                and u["consensus"]["op"] is None]

    fetched, enriched = 0, 0
    for kind, row in targets:                 # build() 상 종목코드는 타깃마다 유일
        if fetched >= ENRICH_MAX:
            _warn(f"개별 종목 보강: ENRICH_MAX({ENRICH_MAX}) 도달 — 잔여 {len(targets) - fetched}행 생략")
            break
        fetched += 1
        per = _target_period(row, today)
        hit = False
        # (A) released 실적 미집계 → DART 원문에서 '실제' 잠정실적 직접 파싱.
        #     집계 사이트(네이버·FnGuide)가 아직 컨센서스만 들고 있어도 발표 수치를 즉시 반영.
        #     op 뿐 아니라 매출·순이익 중 하나라도 결손이면 시도 — 금융지주처럼 집계 사이트가
        #     op 만 주고 매출액을 비우는 경우(신한지주 2026-07-23)도 원문에서 채운다.
        need_actual = (row.get("op") is None or row.get("sales") is None
                       or row.get("np") is None)
        if DART_DOC_ON and kind == "rel" and need_actual:
            rc = re.search(r"rcpNo=(\d+)", row.get("dartUrl") or "")
            if rc:
                try:
                    act = fetch_doc(rc.group(1)) or {}
                    for k in ("sales", "op", "np", "salesYoY", "opYoY", "npYoY"):
                        if row.get(k) is None and act.get(k) is not None:
                            row[k] = act[k]
                            hit = True
                    if hit and "dart-doc" not in row["sources"]:
                        row["sources"].append("dart-doc")
                except Exception as e:
                    _warn(f"enrich dart-doc {row['code']} 실패: {e}")
        # (B) 네이버 우선 → 행이 채워지면 FnGuide 생략(호출 절감). 컨센서스·서프라이즈 갭 보강.
        for fn, tag, parse in ((fetch_naver, "naver", parse_naver_finance),
                               (fetch_wcomp, "fnguide-cns", parse_wcomp_cns)):
            try:
                src = parse(fn(row["code"]))
            except Exception as e:
                _warn(f"enrich {tag} {row['code']} 실패: {e}")
                continue
            enr = _enrich_released if kind == "rel" else _enrich_upcoming
            hit = enr(row, per, src, tag) or hit
            if _row_complete(kind, row):
                break
        enriched += 1 if hit else 0
    if fetched:
        _warn(f"개별 종목 보강: {fetched}종목 조회, {enriched}행 채움")
    return enriched


def _row_complete(kind, row):
    """추가 소스 조회가 불필요할 만큼 채워졌는지 — 채워졌으면 폴백 fetch 생략."""
    if kind == "up":
        return row["consensus"]["op"] is not None
    return (row["op"] is not None and row["sales"] is not None
            and row["consensus"]["op"] is not None)


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


def _naver_finance(code: str) -> dict:
    r = requests.get(NAVER_FIN_URL.format(code=code), headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _dart_get(url, params, headers=None):
    """DART 호스트(opendart.fss.or.kr 목록·dart.fss.or.kr 뷰어)는 해외(Actions) IP 에서
    ConnectTimeout·SSL EOF 로 간헐 드롭 — 전송 오류만 백오프 재시도(최대 3회).
    4xx 등 HTTP 오류는 즉시 전파."""
    last = None
    for i in range(3):
        try:
            r = requests.get(url, params=params, headers=headers or UA, timeout=TIMEOUT)
            r.raise_for_status()
            return r
        except (requests.exceptions.SSLError, requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def _dart_document(rcept_no: str) -> dict:
    """rcept_no -> DART 공정공시 원문에서 실제 잠정실적(억원). 2요청(main→dcmNo, viewer)."""
    m = _dart_get(DART_VIEW_MAIN, {"rcpNo": rcept_no})
    dm = re.search(r'viewDoc\(\s*"%s"\s*,\s*"(\d+)"' % re.escape(str(rcept_no)), m.text)
    if not dm:
        return {}
    v = _dart_get(DART_VIEW_DOC, {
        "rcpNo": rcept_no, "dcmNo": dm.group(1), "eleId": "0",
        "offset": "0", "length": "0", "dtd": "HTML",
    })
    v.encoding = "euc-kr"
    return parse_dart_document(v.text)


def _wcomp_cns(code: str) -> dict:
    r = requests.get(WCOMP_CNS_URL, params={
        "cmp_cd": code, "consol_typ": "C", "freq_typ": "Q", "data_typ": "2",
    }, headers=dict(UA, Referer="https://wcomp.fnguide.com/CompanyInfo/Consensus"),
        timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


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
                r = _dart_get(DART_LIST_URL, {
                    "crtfc_key": DART_KEY, "bgn_de": bgn, "end_de": end,
                    "corp_cls": cls, "page_no": page, "page_count": 100,
                })
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
        if "dart" not in s["sources"]:           # 한 종목이 연결·별도 등 복수 공시여도 1회만
            s["sources"].append("dart")

    released = sorted(rel.values(),
                      key=lambda x: (x["date"] or "", abs(x["surprise"]["opGap"] or 0)),
                      reverse=True)
    # 이미 발표된 종목이 같은 날짜의 '예정'에도 남아 있으면 제거 (WiseReport 반영 지연 —
    # DART 가 먼저 감지한 발표가 예정·결과 양쪽에 중복 표시되던 문제)
    released_by_date = {(x["code"], x["date"]) for x in released}
    upcoming = [u for u in upcoming if (u["code"], u["date"]) not in released_by_date]
    return {"upcoming": upcoming, "released": released}


# ── 메인 ─────────────────────────────────────────────────────────────────────

def stamp_captured_date(released, prev_released, today_s):
    """released 로 '처음' 잡힌 날짜(capturedDate)를 이월/스탬프.

    전일 장 마감 후 늦게 발표된 실적은 익일 첫 수집에서 처음 released 로 잡히므로
    capturedDate 가 익일이 된다 → 대시보드가 그날 하루 노출(_load_earnings_events 가
    date==today OR capturedDate==today 로 표시). 이미 잡혀 있던 종목은 기존 값 유지."""
    prev_cap = {r.get("code"): r.get("capturedDate") for r in (prev_released or [])
                if r.get("code") and r.get("capturedDate")}
    for r in released:
        r["capturedDate"] = prev_cap.get(r.get("code")) or today_s


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

    try:
        enrich_calendar(merged["released"], merged["upcoming"], today)
    except Exception as e:
        _warn(f"개별 종목 보강 실패(무시): {e}")

    stamp_captured_date(merged["released"], prev.get("released") or [], today.isoformat())

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
