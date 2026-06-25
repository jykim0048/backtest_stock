"""KIND 투자주의·경고·위험종목 파싱 → public/data/investment_warning.json

출처: https://kind.krx.co.kr/investwarn/investattentwarnrisky.do

JS 분석 결과 (fnSearch() 내 forward 매핑):
  menuIndex 1 → forward invstcautnisu_sub   (투자주의)
  menuIndex 2 → forward invstwarnisu_sub    (투자경고)
  menuIndex 3 → forward invstriskisu_sub    (투자위험)
  method = investattentwarnriskySub

Excel 다운로드 forward: invstcautnisu_down (fallback)
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

# menuIndex → (forward, 결과 dict key)
MENU_MAP = [
    ("1", "invstcautnisu_sub", "caution"),
    ("2", "invstwarnisu_sub",  "warning"),
    ("3", "invstriskisu_sub",  "danger"),
]

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


# ── HTML 파싱 ──────────────────────────────────────────────────────────────────

def _parse_html(html: str, default_category: str = "caution") -> list:
    """KIND 투자주의/경고/위험 테이블에서 종목 정보 추출."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            code_idx = next(
                (i for i, td in enumerate(tds)
                 if re.fullmatch(r"\d{6}", re.sub(r"\s+", "", td.get_text()))),
                None,
            )
            if code_idx is None:
                continue

            def cell(i):
                return tds[i].get_text(" ", strip=True) if len(tds) > i else ""

            rows.append({
                "code":   cell(code_idx).zfill(6),
                "name":   cell(code_idx + 1),
                "market": cell(code_idx + 2),
                "reason": cell(code_idx + 3),
                "date":   cell(code_idx + 4),
            })
        if rows:
            break
    return rows


# ── Excel 파싱 ────────────────────────────────────────────────────────────────

def _parse_excel(data: bytes, category: str) -> list:
    """Excel(xls/xlsx) 바이너리에서 종목 추출. openpyxl/xlrd 중 가능한 것 사용."""
    rows = []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True)
        ws = wb.active
        for row in ws.iter_rows(values_only=True):
            vals = [str(v or "").strip() for v in row]
            code_idx = next((i for i, v in enumerate(vals)
                             if re.fullmatch(r"\d{6}", v)), None)
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
        print(f"  [excel] openpyxl 실패: {e}", file=sys.stderr)
        try:
            import xlrd
            wb = xlrd.open_workbook(file_contents=data)
            ws = wb.sheet_by_index(0)
            for ridx in range(ws.nrows):
                vals = [str(ws.cell_value(ridx, c)).strip() for c in range(ws.ncols)]
                code_idx = next((i for i, v in enumerate(vals)
                                 if re.fullmatch(r"\d{6}", v)), None)
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
        except Exception as e2:
            print(f"  [excel] xlrd 실패: {e2}", file=sys.stderr)
    return rows


# ── 디버그 저장 ────────────────────────────────────────────────────────────────

def _save_debug(key: str, content, limit: int = 8000):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"debug_kind_{key}.txt")
    if isinstance(content, bytes):
        text = f"<binary {len(content)} bytes>\n{content[:200]!r}"
    else:
        text = str(content)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text[:limit])
    print(f"  debug → {path}", flush=True)


# ── 메인 fetch ────────────────────────────────────────────────────────────────

def _fetch_category(session: requests.Session, menu_idx: str,
                    forward: str, today: str, one_year_ago: str) -> list:
    """단일 카테고리(주의/경고/위험) POST → HTML 파싱. 실패 시 Excel fallback."""

    base_payload = {
        "method":          "investattentwarnriskySub",
        "forward":         forward,
        "menuIndex":       menu_idx,
        "currentPageSize": "1000",
        "pageIndex":       "1",
        "orderMode":       "4",
        "orderStat":       "D",
        "searchFromDate":  today,
        "startDate":       one_year_ago,
        "endDate":         today,
        "marketType":      "",
    }

    # ① HTML POST (올바른 forward + jQuery AJAX 헤더)
    try:
        r = session.post(URL, data=base_payload, headers=HDRS_AJAX, timeout=30)
        r.encoding = "utf-8"
        preview = r.text.strip()[:100].replace("\n", " ")
        print(f"  [POST menu={menu_idx}] status={r.status_code} "
              f"len={len(r.text)} preview={preview!r}", flush=True)
        if DEBUG:
            _save_debug(f"html_menu{menu_idx}", r.text)
        if len(r.text) > 2000:
            rows = _parse_html(r.text)
            print(f"  → HTML 파싱: {len(rows)}건", flush=True)
            if rows:
                return rows
    except Exception as e:
        print(f"  [POST menu={menu_idx}] 오류: {e}", file=sys.stderr)

    # ② Excel 다운로드 fallback — forward 에서 _sub → _down
    excel_forward = forward.replace("_sub", "_down")
    excel_payload = {**base_payload,
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
            rows = _parse_excel(r.content, excel_forward)
            print(f"  → Excel 파싱: {len(rows)}건", flush=True)
            if rows:
                return rows
    except Exception as e:
        print(f"  [Excel menu={menu_idx}] 오류: {e}", file=sys.stderr)

    return []


def _fetch(session: requests.Session, today: str, one_year_ago: str) -> dict:
    result = {"caution": [], "warning": [], "danger": []}
    for menu_idx, forward, category in MENU_MAP:
        rows = _fetch_category(session, menu_idx, forward, today, one_year_ago)
        result[category] = rows
    return result


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now  = datetime.datetime.now(kst)
    today        = now.strftime("%Y-%m-%d")          # KIND 날짜 형식 (폼 확인: 2026-06-25)
    one_year_ago = (now - datetime.timedelta(days=365)).strftime("%Y-%m-%d")
    now_str      = now.strftime("%Y-%m-%d %H:%M KST")

    session = requests.Session()

    # 홈 → 타겟 페이지 방문으로 세션/쿠키 확보
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

    try:
        parsed = _fetch(session, today, one_year_ago)
    except Exception as e:
        print(f"[error] fetch 실패: {e}", file=sys.stderr)
        parsed = {"caution": [], "warning": [], "danger": []}

    for k, v in parsed.items():
        print(f"{k}: {len(v)}건", flush=True)

    result = {"updated": now_str, **parsed}
    out_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "investment_warning.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"saved → {out_path}", flush=True)


if __name__ == "__main__":
    main()
