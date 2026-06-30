"""KIND 투자주의/경고/위험종목 파싱 -> public/data/investment_warning.json

출처: https://kind.krx.co.kr/investwarn/investattentwarnrisky.do

쿼리: startDate=endDate=오늘(기준일) → KIND 화면 기본값(현재 지정 종목).
      비우면 1472 오류, 과거 범위는 '지정→해제 이력'.
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


# -- KRX 마스터(SSOT) 로드 + 코드 보정 -----------------------------------------

def _load_krx_master():
    """public/assets/krx_companies.json -> (by_code, by_name) 딕셔너리.

    각 항목: {name, code, market, ticker}. 코드/시장/티커의 단일 진실 소스.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "public", "assets", "krx_companies.json")
    by_code, by_name = {}, {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for e in data:
            code = str(e.get("code", "")).zfill(6)
            if code:
                by_code[code] = e
            nm = e.get("name", "")
            if nm:
                by_name.setdefault(nm, e)
        print(f"[krx] 마스터 {len(by_code)}종목 로드", flush=True)
    except Exception as e:
        print(f"[warn] krx_companies.json 로드 실패: {e}", file=sys.stderr)
    return by_code, by_name


def _resolve_code(raw_code: str, code1: str, name: str,
                  by_code: dict, by_name: dict):
    """KIND 코드를 KRX 마스터로 교차검증해 (정확한 code, master_entry) 반환.

    1) 5자리+0 / 6자리 후보를 마스터 코드에서 확인.
    2) 실패 시 종목명으로 마스터 조회(우선주 등 끝자리 0 아닌 경우 보정).
    """
    cands = []
    if raw_code.isdigit():
        if len(raw_code) == 5:
            cands.append(raw_code + "0")        # 끝 0 복원
            # 우선주(끝 5/7) 가능성 — 마스터에 없으면 종목명으로 보정
        elif len(raw_code) == 6:
            cands.append(raw_code)
    if code1 and code1 not in cands:
        cands.append(code1)
    for c in cands:
        if c in by_code:
            return c, by_code[c]
    if name in by_name:
        e = by_name[name]
        return str(e.get("code", "")).zfill(6), e
    return (cands[0] if cands else code1), None


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

            # companysummary_open('XXXXX') — KIND 인자는 종목코드 앞5자리(끝 0 생략).
            # 5자리면 뒤에 0 복원, 6자리면 그대로. (main 에서 KRX 마스터로 재검증)
            onclick  = a_tag.get("onclick", "") if a_tag else ""
            m        = re.search(r"companysummary_open\('([^']+)'\)", onclick)
            raw_code = m.group(1) if m else ""
            if raw_code.isdigit() and len(raw_code) == 5:
                code = raw_code + "0"
            elif raw_code.isdigit() and len(raw_code) == 6:
                code = raw_code
            else:
                code = raw_code.zfill(6)

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
                "code":     code,
                "raw_code": raw_code,
                "name":     name.strip(),
                "market":   market,
                "index":    index_name,
                "reason":   reason,
                "date":     date,
                "release":  release,
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


# -- 지정해제 요건 계산 (시장감시규정 시행세칙 제3조의3·3조의4) -----------------
#
# 투자주의(제3조): 1매매거래일 단발 지정 → 다음 매매거래일 자동 해제(가격요건 없음).
# 투자경고(제3조의3⑤)·투자위험(제3조의4⑤): 지정일부터 10매매거래일 경과 후,
#   아래 '유지조건'이 깨지면 다음 매매거래일 해제.
#
# 유지조건(표준 1·2호 지정경로 가정 — KIND가 정확한 지정 호를 노출하지 않음):
#   당일 종가가 [최근 15일 중 최고가]  AND  (최근5일 상승률 ≥ 60%  OR  최근15일 상승률 ≥ 100%)
# → 해제기준가(release_price) = max( 15일최고가선,  min(5일상승선, 15일상승선) )
#   · 15일최고가선  = 직전 14거래일 종가의 최고값 (이하로 마감하면 '15일 최고가' 미갱신)
#   · 5일상승선     = 5거래일 전 종가 × 1.60   (이 미만이면 5일 상승률 < 60%)
#   · 15일상승선    = 15거래일 전 종가 × 2.00  (이 미만이면 15일 상승률 < 100%)
#   종가가 release_price '미만'이고 10매매거래일 경과 시 해제요건 충족.
# 거래일 계산은 주말만 제외(공휴일 미반영) → release_date 는 '추정'.


def _add_trading_days(start_str: str, n: int) -> str:
    """start_str(매매거래일) 로부터 n 매매거래일 뒤 날짜(ISO). 주말만 제외(추정)."""
    try:
        d = datetime.date.fromisoformat(start_str)
    except Exception:
        return ""
    added = 0
    while added < n:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:            # 월~금 (공휴일 미반영)
            added += 1
    return d.isoformat()


def _compute_release(rows: list, category: str, today: str) -> None:
    """rows 각 항목에 해제요건 필드를 추가(best-effort, 실패해도 JSON 출력 유지).

    추가 필드:
      release_type   "auto"(주의) | "cond"(경고·위험)
      release_date   해제(예정) 최소일 — 주의=익일, 경고·위험=지정일+10매매거래일(추정)
      release_passed 10매매거래일 경과 여부(경고·위험만 의미)
      release_price  해제기준가(경고·위험) — 종가가 이 값 미만이면 가격요건 충족
      release_high15 15일 최고가선(참고)
      release_rise   상승률선(참고, 5·15일 중 낮은 선)
    """
    # 투자주의: 1일 효력 → 익일 자동 해제. 가격요건 없음.
    if category == "caution":
        for r in rows:
            r["release_type"] = "auto"
            r["release_date"] = _add_trading_days(r.get("date", ""), 1)
            r["release_passed"] = True
            r["release_price"] = None
        return

    # 투자경고/위험: 종가 이력으로 해제기준가 + 10매매거래일 경과 판정.
    try:
        import yfinance as yf
        import pandas as pd
    except Exception as e:
        print(f"  [release] yfinance/pandas 미설치 — 건너뜀: {e}",
              file=sys.stderr)
        return

    for r in rows:
        r["release_type"] = "cond"
        r["release_date"] = _add_trading_days(r.get("date", ""), 10)
        r["release_passed"] = bool(r["release_date"]) and today >= r["release_date"]
        r["release_price"] = None
        ticker = r.get("ticker", "")
        if not ticker:
            continue
        try:
            hist = yf.download(ticker, period="3mo", progress=False,
                               auto_adjust=True)
            if hist is None or len(hist) < 16:
                print(f"  [release] {ticker} 데이터 부족({0 if hist is None else len(hist)}건)",
                      file=sys.stderr)
                continue
            if isinstance(hist.columns, pd.MultiIndex):
                hist.columns = hist.columns.get_level_values(0)
            closes = [float(c) for c in hist["Close"].tolist()]

            high15_line = max(closes[-15:-1])           # 직전 14거래일 최고 종가
            rise5_line  = closes[-6]  * 1.60            # 5거래일 전 × 1.60
            rise15_line = closes[-16] * 2.00            # 15거래일 전 × 2.00
            rise_line   = min(rise5_line, rise15_line)  # OR 조건 → 낮은 선
            release_price = max(high15_line, rise_line) # 둘 다 깨져야 해제 → 높은 선

            r["release_high15"] = int(round(high15_line))
            r["release_rise"]   = int(round(rise_line))
            r["release_price"]  = int(round(release_price))
        except Exception as e:
            print(f"  [release] {ticker} 계산 실패: {e}", file=sys.stderr)


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


# -- 단일 카테고리 fetch -------------------------------------------------------

def _fetch_category(session: requests.Session,
                    menu_idx: str, forward: str,
                    default_reason: str,
                    today: str) -> list:
    """POST -> HTML 파싱. 실패 시 Excel download fallback.

    startDate=endDate=오늘(기준일) 로 주면 KIND 화면 기본값과 동일하게
    '현재 지정 중인 종목'을 반환한다(probe 확인: today~today → 17건 전부 미해제).
    날짜를 비우면 1472 오류, 과거 범위를 주면 '지정→해제 이력'이 나온다.
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
            _save_debug(f"html_menu{menu_idx}", r.text)
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
            _save_debug("main_page", r.text, limit=30000)
    except Exception as e:
        print(f"[warn] 타겟 페이지 GET 실패: {e}", file=sys.stderr)

    by_code, by_name = _load_krx_master()

    result = {"caution": [], "warning": [], "danger": []}
    for menu_idx, forward, category, default_reason in MENU_MAP:
        try:
            rows = _fetch_category(session, menu_idx, forward,
                                   default_reason, today)
            total = len(rows)

            # ⓪ KRX 마스터로 코드/시장/티커 보정 (KIND 5자리 코드 → 정확한 6자리)
            for r in rows:
                code, e = _resolve_code(r.get("raw_code", ""), r.get("code", ""),
                                        r.get("name", ""), by_code, by_name)
                r["code"] = code
                if e:
                    r["market"] = e.get("market", r.get("market", ""))
                    r["ticker"] = e.get("ticker", "")
                else:
                    r["ticker"] = code + (".KQ" if r.get("market") == "코스닥"
                                          else ".KS")
                r.pop("raw_code", None)

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

            # ④ 지정해제 요건(해제기준가·해제가능최소일) 계산해 각 행에 추가
            _compute_release(rows, category, today)

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

    # 날짜별 아카이브 (대시보드에서 리포트 날짜로 과거 조회) + index.json
    root = os.path.dirname(os.path.abspath(__file__))
    archive_dir = os.path.join(root, "public", "reports", "invwarn")
    os.makedirs(archive_dir, exist_ok=True)
    arc_path = os.path.join(archive_dir, f"{today}.json")
    with open(arc_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    index_path = os.path.join(archive_dir, "index.json")
    dates = []
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                dates = json.load(f)
        except Exception:
            dates = []
    if today not in dates:
        dates.append(today)
    dates = sorted(set(dates), reverse=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(dates, f, ensure_ascii=False, indent=2)
    print(f"archived -> {arc_path} (index {len(dates)}일)", flush=True)


if __name__ == "__main__":
    main()
