"""KIND 투자주의/경고/위험종목 파싱 -> public/data/investment_warning.json

출처: https://kind.krx.co.kr/investwarn/investattentwarnrisky.do

쿼리: startDate/endDate 를 비워 KIND 화면 기본값(현재 지정 종목)을 받는다.
      날짜를 주면 '지정→해제 이력'이 나오므로 비운다.
필터:
  - 지수 뱃지: K200/Q150/X300/V100 중 하나라도 달린 종목만.
  - 투자주의: 1일 효력 → 최근 지정일(=당일) 종목만.
  - 투자경고/위험: 해제일 공란/"-"(현재 지정 중) 종목만.
중복 제거: 같은 종목코드가 여러 행이면 지정일(date) 최신 항목 하나만 유지.

JS 분석 결과 (fnSearch() forward 매핑):
  menuIndex 1 -> invstcautnisu_sub  (투자주의)
  menuIndex 2 -> invstwarnisu_sub   (투자경고)
  menuIndex 3 -> invstriskisu_sub   (투자위험)
  method = investattentwarnriskySub

컬럼 구조:
  menu1: 번호 | 종목명(img+a) | 유형 | 공시일 | 지정일
  menu2/3: 번호 | 종목명(img+a) | 공시일 | 지정일 | 해제일

종목코드: onclick="companysummary_open('XXXXX')" 에서 추출,
          5자리 숫자면 앞에 0 패딩해 6자리로 변환.
"""
import io
import os
import re
import sys
import json
import datetime
import requests
from bs4 import BeautifulSoup

BASE  = "https://kind.krx.co.kr"
URL   = BASE + "/investwarn/investattentwarnrisky.do"
UA    = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
         "AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/125.0.0.0 Safari/537.36")
DEBUG = os.environ.get("DEBUG_KIND") == "1"

# menuIndex -> (forward, result key, 기본 사유 레이블)
MENU_MAP = [
    ("1", "invstcautnisu_sub", "caution", "투자주의"),
    ("2", "invstwarnisu_sub",  "warning", "투자경고"),
    ("3", "invstriskisu_sub",  "danger",  "투자위험"),
]

# KIND 지수 뱃지 alt -> 화면 약어. 이 뱃지 중 하나라도 달린 종목만 통과.
#   KOSPI200=K200, KOSDAQ150=Q150, KRX300=X300, V100(=KRX100/대형주지수)
INDEX_BADGE_ABBR = {
    "KOSPI200":  "K200",
    "KOSDAQ150": "Q150",
    "KRX300":    "X300",
    "V100":      "V100",
}

# jQuery AJAX POST 헤더
HDRS_AJAX = {
    "User-Agent":       UA,
    "Referer":          URL + "?method=investattentwarnriskyMain",
    "Accept":           "text/html, */*; q=0.01",
    "Accept-Language":  "ko-KR,ko;q=0.9,en;q=0.8",
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin":           BASE,
    "Connection":       "keep-alive",
}

# 일반 브라우저 GET 헤더
HDRS_GET = {
    "User-Agent":       UA,
    "Accept":           "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":  "ko-KR,ko;q=0.9,en;q=0.8",
    "Connection":       "keep-alive",
}


# -- HTML 파싱 -----------------------------------------------------------------

def _parse_html(html: str, menu_idx: str = "1",
                default_reason: str = "") -> list:
    """KIND 투자주의/경고/위험 테이블에서 종목 정보 추출.

    종목코드는 onclick 내 companysummary_open() 인자에서 추출.
    종목명 td 안의 <img alt='KOSPI200'/'KOSDAQ150'> 뱃지로 지수 편입 판단.
    경고/위험(menu2/3)은 해제일 컬럼(release)을 파싱 — 공란이면 현재 지정 중.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows = []

    for table in soup.find_all("table", class_=re.compile(r"\blist\b")):
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue

            # 종목명 td: <a id="companysum"> 포함
            name_td = next(
                (td for td in tds if td.find("a", id="companysum")), None
            )
            if name_td is None:
                continue

            a_tag = name_td.find("a")
            imgs  = name_td.find_all("img")

            # companysummary_open('XXXXX') -> 6자리 패딩
            onclick  = a_tag.get("onclick", "") if a_tag else ""
            m        = re.search(r"companysummary_open\('([^']+)'\)", onclick)
            raw_code = m.group(1) if m else ""
            code     = raw_code.zfill(6) if raw_code.isdigit() else raw_code

            name = (a_tag.get("title") or
                    a_tag.get_text(strip=True)) if a_tag else ""

            # 첫 img alt = 시장(유가증권/코스닥), 나머지 = 지수/구분 뱃지
            alts   = [img.get("alt", "") for img in imgs]
            market = alts[0] if alts else ""
            # 지수 뱃지(K200/Q150/X300/V100) 수집 — 중복 제거, 순서 유지
            matched = [INDEX_BADGE_ABBR[a] for a in alts if a in INDEX_BADGE_ABBR]
            index_name = " ".join(dict.fromkeys(matched))   # "" 면 미편입

            ni = tds.index(name_td)

            def cell(offset: int) -> str:
                idx = ni + offset
                return tds[idx].get_text(" ", strip=True) if idx < len(tds) else ""

            if menu_idx == "1":
                # 번호 | 종목명 | 유형 | 공시일 | 지정일
                reason  = cell(1) or default_reason   # 유형
                date    = cell(3)                     # 지정일
                release = ""                          # 투자주의는 해제일 없음
            else:
                # 번호 | 종목명 | 공시일 | 지정일 | 해제일
                reason  = default_reason              # 유형 컬럼 없음
                date    = cell(2)                     # 지정일
                release = cell(3)                     # 해제일
                # "-" / 빈칸 = 미해제(현재 지정 중) → "" 로 정규화
                if release in ("", "-"):
                    release = ""

            rows.append({
                "code":    code,
                "name":    name.strip(),
                "market":  market,
                "index":   index_name,
                "reason":  reason,
                "date":    date,
                "release": release,
            })

        if rows:
            break

    return rows


# -- 중복 제거 -----------------------------------------------------------------

def _dedup(rows: list) -> list:
    """같은 code 가 여러 행이면 date 최신 항목 하나만 유지."""
    best: dict = {}
    for row in rows:
        code = row["code"]
        if code not in best or row["date"] > best[code]["date"]:
            best[code] = row
    return list(best.values())


# -- Excel 파싱 (fallback) ----------------------------------------------------

def _parse_excel(data: bytes) -> list:
    """Excel(xls/xlsx) 바이너리에서 종목 추출 (fallback)."""
    rows = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            vals = [str(v or "").strip() for v in row]
            code_idx = next(
                (i for i, v in enumerate(vals) if re.fullmatch(r"\d{6}", v)),
                None,
            )
            if code_idx is None:
                continue
            def cell(i):
                return vals[i] if i < len(vals) else ""
            rows.append({
                "code":   cell(code_idx),
                "name":   cell(code_idx + 1),
                "market": cell(code_idx + 2),
                "reason": cell(code_idx + 3),
                "date":   cell(code_idx + 4),
            })
    except Exception as e:
        print(f"  [excel] 파싱 실패: {e}", file=sys.stderr)
    return rows


# -- 디버그 저장 ---------------------------------------------------------------

def _save_debug(key: str, content, limit: int = 8000):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"debug_kind_{key}.txt")
    if isinstance(content, bytes):
        text = f"<binary {len(content)} bytes>\n{content[:200]!r}"
    else:
        text = str(content)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text[:limit])
    print(f"  debug -> {path}", flush=True)


# -- 진단: 날짜 파라미터 변형 테스트 (DEBUG 시 1회) ----------------------------

def _probe_date_variants(session: requests.Session, today: str):
    """투자경고(menu2)로 날짜 조합을 바꿔가며 '현재 지정(미해제)'을 주는 조합 탐색.

    화면 기본(현재 지정 17건)과 일치하는 startDate/endDate 조합을 찾기 위함.
    각 변형의 (행수, 미해제 수)를 로그로 출력.
    """
    one_year   = (datetime.datetime.strptime(today, "%Y-%m-%d")
                  - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    far_future = "2027-12-31"
    base = {
        "method": "investattentwarnriskySub", "forward": "invstwarnisu_sub",
        "menuIndex": "2", "currentPageSize": "1000", "pageIndex": "1",
        "orderMode": "4", "orderStat": "D", "searchFromDate": today,
        "marketType": "",
    }
    variants = [
        ("empty",        "",        ""),
        ("today_today",  today,     today),
        ("year_today",   one_year,  today),
        ("today_future", today,     far_future),
        ("year_future",  one_year,  far_future),
        ("future_only",  "",        far_future),
    ]
    print("=== [probe] 날짜 변형 테스트 (menu2/투자경고) ===", flush=True)
    for name, sd, ed in variants:
        p = {**base, "startDate": sd, "endDate": ed}
        try:
            r = session.post(URL, data=p, headers=HDRS_AJAX, timeout=30)
            r.encoding = "utf-8"
            txt = r.text
            if len(txt) < 2000:
                print(f"  [probe {name:12}] sd={sd!r:12} ed={ed!r:12} "
                      f"ERROR len={len(txt)}", flush=True)
                continue
            rows = _parse_html(txt, "2", "투자경고")
            open_cnt = sum(1 for x in rows if not x.get("release"))
            idx_open = sum(1 for x in rows if not x.get("release") and x.get("index"))
            print(f"  [probe {name:12}] sd={sd!r:12} ed={ed!r:12} "
                  f"rows={len(rows)} 미해제={open_cnt} 지수+미해제={idx_open}",
                  flush=True)
        except Exception as e:
            print(f"  [probe {name:12}] EXC {e}", flush=True)
    print("=== [probe] 끝 ===", flush=True)


# -- 단일 카테고리 fetch -------------------------------------------------------

def _fetch_category(session: requests.Session,
                    menu_idx: str, forward: str,
                    default_reason: str,
                    today: str) -> list:
    """POST -> HTML 파싱. 실패 시 Excel download fallback.

    날짜 범위(startDate/endDate)를 비우면 KIND 화면 기본값과 동일하게
    '현재 지정 중인 종목'(투자주의는 당일)을 반환한다. 날짜를 주면
    그 기간의 '지정→해제 이력'이 나오므로 비워 둔다.
    """
    payload = {
        "method":          "investattentwarnriskySub",
        "forward":         forward,
        "menuIndex":       menu_idx,
        "currentPageSize": "1000",
        "pageIndex":       "1",
        "orderMode":       "4",
        "orderStat":       "D",
        "searchFromDate":  today,
        "startDate":       today,    # 오늘 기준일 → 현재 지정 현황(가설)
        "endDate":         today,
        "marketType":      "",
    }

    # 1. HTML POST
    try:
        r = session.post(URL, data=payload, headers=HDRS_AJAX, timeout=30)
        r.encoding = "utf-8"
        preview = r.text.strip()[:120].replace("\n", " ")
        print(f"  [POST menu={menu_idx}] status={r.status_code} "
              f"len={len(r.text)} preview={preview!r}", flush=True)
        if DEBUG:
            _save_debug(f"html_menu{menu_idx}", r.text, limit=600000)
        if len(r.text) > 2000:
            rows = _parse_html(r.text, menu_idx, default_reason)
            print(f"  -> HTML 파싱: {len(rows)}건", flush=True)
            if rows:
                return rows
            print(f"  [warn] HTML 수신됐으나 파싱 0건 (menu={menu_idx})",
                  flush=True)
    except Exception as e:
        print(f"  [POST menu={menu_idx}] 오류: {e}", file=sys.stderr)

    # 2. Excel fallback
    excel_forward = forward.replace("_sub", "_down")
    excel_payload = {**payload,
                     "method":  "investattentwarnriskyExcel",
                     "forward": excel_forward}
    try:
        r = session.post(URL, data=excel_payload, headers=HDRS_AJAX, timeout=30)
        ctype = r.headers.get("Content-Type", "")
        print(f"  [Excel menu={menu_idx}] status={r.status_code} "
              f"len={len(r.content)} ctype={ctype!r}", flush=True)
        if DEBUG:
            _save_debug(f"excel_menu{menu_idx}", r.content)
        if "excel" in ctype or "spreadsheet" in ctype or "octet" in ctype:
            rows = _parse_excel(r.content)
            print(f"  -> Excel 파싱: {len(rows)}건", flush=True)
            if rows:
                return rows
    except Exception as e:
        print(f"  [Excel menu={menu_idx}] 오류: {e}", file=sys.stderr)

    return []


# -- 메인 ---------------------------------------------------------------------

def main():
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now  = datetime.datetime.now(kst)
    today   = now.strftime("%Y-%m-%d")
    now_str = now.strftime("%Y-%m-%d %H:%M KST")

    session = requests.Session()

    # 홈 -> 타겟 페이지 방문으로 세션/쿠키 확보
    try:
        r = session.get(BASE, headers=HDRS_GET, timeout=10)
        print(f"[init] KIND 홈 status={r.status_code}", flush=True)
    except Exception as e:
        print(f"[warn] KIND 홈 실패: {e}", file=sys.stderr)

    try:
        r = session.get(URL, params={"method": "investattentwarnriskyMain"},
                        headers=HDRS_GET, timeout=20)
        r.encoding = "utf-8"
        print(f"[main] GET status={r.status_code} len={len(r.text)}", flush=True)
        if DEBUG:
            _save_debug("main_page", r.text, limit=90000)
    except Exception as e:
        print(f"[warn] 타겟 페이지 GET 실패: {e}", file=sys.stderr)

    # 진단: 어떤 날짜 조합이 현재 지정 종목을 주는지 1회 탐색
    if DEBUG:
        try:
            _probe_date_variants(session, today)
        except Exception as e:
            print(f"[warn] probe 실패: {e}", file=sys.stderr)

    result = {"caution": [], "warning": [], "danger": []}
    for menu_idx, forward, category, default_reason in MENU_MAP:
        try:
            rows = _fetch_category(session, menu_idx, forward,
                                   default_reason, today)
            total = len(rows)

            # ① 지수 뱃지 필터 — K200/Q150/X300/V100 중 하나라도 달린 종목만
            rows = [r for r in rows if r.get("index")]
            after_idx = len(rows)

            if category == "caution":
                # ② 투자주의: 1일 효력 → 최근 지정일(=당일)만 남김
                rows = _dedup(rows)
                if rows:
                    latest = max(r["date"] for r in rows if r["date"])
                    rows = [r for r in rows if r["date"] == latest]
            else:
                # ③ 경고/위험: 해제일 공란(현재 지정 중)만 남김
                rows = [r for r in rows if not r.get("release")]
                rows = _dedup(rows)

            print(f"  -> [{category}] 전체 {total} -> 지수뱃지 {after_idx} "
                  f"-> 최종 {len(rows)}건", flush=True)

            result[category] = rows
        except Exception as e:
            print(f"[error] menu={menu_idx} 실패: {e}", file=sys.stderr)

    for k, v in result.items():
        print(f"{k}: {len(v)}건", flush=True)

    out = {"updated": now_str, **result}
    out_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "investment_warning.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"saved -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
