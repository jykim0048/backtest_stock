"""KIND 투자주의·경고·위험종목 파싱 → public/data/investment_warning.json

출처: https://kind.krx.co.kr/investwarn/investattentwarnrisky.do
"""
import os
import re
import sys
import json
import datetime
import requests
from bs4 import BeautifulSoup

BASE = "https://kind.krx.co.kr"
URL  = BASE + "/investwarn/investattentwarnrisky.do"
UA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEBUG = os.environ.get("DEBUG_KIND") == "1"

# 단계 컬럼 값 → 분류 키
LEVEL_MAP = {"주의": "caution", "경고": "warning", "위험": "danger"}


def _parse(html: str) -> dict:
    """HTML 에서 6자리 코드 행을 추출 → {caution, warning, danger} 분류."""
    soup = BeautifulSoup(html, "html.parser")
    result = {"caution": [], "warning": [], "danger": []}

    for table in soup.find_all("table"):
        tbody = table.find("tbody") or table
        rows_found = False
        for tr in tbody.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3:
                continue
            # 6자리 숫자 코드 컬럼 탐색
            code_idx = None
            for i, td in enumerate(tds):
                t = re.sub(r"\s+", "", td.get_text())
                if re.fullmatch(r"\d{6}", t):
                    code_idx = i
                    break
            if code_idx is None:
                continue

            rows_found = True
            def cell(i):
                return tds[i].get_text(" ", strip=True) if len(tds) > i else ""

            # 단계(주의/경고/위험) 컬럼 탐색 — 보통 code 이후 몇 번째 셀에 있음
            level_key = "caution"  # 기본값
            for i, td in enumerate(tds):
                txt = td.get_text(strip=True)
                for kw, key in LEVEL_MAP.items():
                    if kw in txt:
                        level_key = key
                        break

            result[level_key].append({
                "code":   cell(code_idx).zfill(6),
                "name":   cell(code_idx + 1),
                "market": cell(code_idx + 2),
                "reason": cell(code_idx + 3),
                "date":   cell(code_idx + 4),
            })

        if rows_found:
            break  # 데이터 테이블 찾으면 중단

    return result


def _fetch(session: requests.Session) -> dict:
    headers = {
        "User-Agent":      UA,
        "Referer":         URL,
        "Accept":          "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Content-Type":    "application/x-www-form-urlencoded; charset=utf-8",
    }

    # 시도 1: POST (서브 메서드)
    for method_name in ("investattentwarnriskySub", "searchInvestAttentwarnriskySub"):
        payload = {
            "method":          method_name,
            "forward":         "investattentwarnrisky_sub",
            "pageIndex":       "1",
            "rowCountPerPage": "3000",
        }
        r = session.post(URL, data=payload, headers=headers, timeout=25)
        r.encoding = "utf-8"
        print(f"POST {method_name} → status={r.status_code} len={len(r.text)}", flush=True)
        if DEBUG:
            _save_debug(f"post_{method_name}", r.text)
        parsed = _parse(r.text)
        total = sum(len(v) for v in parsed.values())
        if total > 0:
            return parsed

    # 시도 2: GET (메인 페이지 — 일부 KIND 페이지는 메인에 전체 데이터 포함)
    r2 = session.get(URL, params={"method": "investattentwarnriskyMain"},
                     headers=headers, timeout=25)
    r2.encoding = "utf-8"
    print(f"GET  main → status={r2.status_code} len={len(r2.text)}", flush=True)
    if DEBUG:
        _save_debug("get_main", r2.text)
    return _parse(r2.text)


def _save_debug(key: str, html: str):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"debug_kind_{key}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html[:8000])
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

    try:
        parsed = _fetch(session)
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
