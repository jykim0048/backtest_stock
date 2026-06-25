"""KIND(kind.krx.co.kr) 투자주의·경고·위험종목 파싱 → public/data/investment_warning.json

DEBUG=1 환경변수 설정 시 raw HTML 을 public/data/debug_kind_*.html 로 저장한다.
"""
import os
import re
import sys
import json
import datetime
import requests
from bs4 import BeautifulSoup

BASE = "https://kind.krx.co.kr"
UA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEBUG = os.environ.get("DEBUG_KIND") == "1"

PATHS = {
    "caution": "/investinfo/investinfo03.do",  # 투자주의
    "warning": "/investinfo/investinfo04.do",  # 투자경고
    "danger":  "/investinfo/investinfo05.do",  # 투자위험
}


def _parse_table(html: str) -> list:
    """테이블 어느 컬럼이든 6자리 숫자를 찾아 코드로 사용 (컬럼 순서 무관)."""
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for table in soup.find_all("table"):
        tbody = table.find("tbody") or table
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            # 6자리 숫자 컬럼 탐색
            code_idx = None
            for i, td in enumerate(tds):
                t = re.sub(r"\s+", "", td.get_text())
                if re.fullmatch(r"\d{6}", t):
                    code_idx = i
                    break
            if code_idx is None:
                continue
            def cell(i):
                return tds[i].get_text(strip=True) if len(tds) > i else ""
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


def _fetch(session: requests.Session, key: str, path: str) -> list:
    url     = BASE + path
    forward = path.split("/")[-1].replace(".do", "_sub")

    # 시도 1: POST (KIND 표준 폼 제출)
    payload = {
        "method":          "searchInvestInfoSub",
        "forward":         forward,
        "pageIndex":       "1",
        "rowCountPerPage": "1000",
    }
    headers = {
        "User-Agent":      UA,
        "Referer":         url,
        "Content-Type":    "application/x-www-form-urlencoded; charset=utf-8",
        "Accept":          "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    }
    r = session.post(url, data=payload, headers=headers, timeout=25)
    r.encoding = "utf-8"
    html = r.text

    print(f"[{key}] POST status={r.status_code} len={len(html)} "
          f"preview={html[:120].replace(chr(10),' ')!r}", flush=True)

    if DEBUG:
        _save_debug(key, html)

    items = _parse_table(html)
    if items:
        return items

    # 시도 2: GET (파라미터를 쿼리스트링으로)
    r2 = session.get(url, params=payload, headers=headers, timeout=25)
    r2.encoding = "utf-8"
    html2 = r2.text
    print(f"[{key}] GET  status={r2.status_code} len={len(html2)} "
          f"preview={html2[:120].replace(chr(10),' ')!r}", flush=True)
    if DEBUG:
        _save_debug(key + "_get", html2)
    return _parse_table(html2)


def _save_debug(key: str, html: str):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"debug_kind_{key}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html[:8000])  # 처음 8KB만
    print(f"  debug saved → {path}", flush=True)


def main():
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now_str = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")

    session = requests.Session()
    try:
        resp = session.get(BASE, headers={"User-Agent": UA}, timeout=10)
        print(f"[init] KIND 홈 status={resp.status_code}", flush=True)
    except Exception as e:
        print(f"[warn] KIND 세션 초기화 실패: {e}", file=sys.stderr)

    result = {"updated": now_str, "caution": [], "warning": [], "danger": []}
    for key, path in PATHS.items():
        try:
            items = _fetch(session, key, path)
            result[key] = items
            print(f"[{key}] → {len(items)}건", flush=True)
        except Exception as e:
            print(f"[error] {key} ({path}): {e}", file=sys.stderr)

    out_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "investment_warning.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"saved → {out_path}", flush=True)


if __name__ == "__main__":
    main()
