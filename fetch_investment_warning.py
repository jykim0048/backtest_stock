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

BASE  = "https://kind.krx.co.kr"
URL   = BASE + "/investwarn/investattentwarnrisky.do"
UA    = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DEBUG = os.environ.get("DEBUG_KIND") == "1"

LEVEL_MAP = {"주의": "caution", "경고": "warning", "위험": "danger"}

HDRS = {
    "User-Agent":      UA,
    "Referer":         URL + "?method=investattentwarnriskyMain",
    "Accept":          "text/html,application/xhtml+xml,*/*;q=0.9",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Content-Type":    "application/x-www-form-urlencoded; charset=utf-8",
}


# ── HTML 파싱 ──────────────────────────────────────────────────────────────────

def _parse(html: str) -> dict:
    """6자리 코드 행 추출 → {caution, warning, danger} 분류."""
    soup = BeautifulSoup(html, "html.parser")
    result = {"caution": [], "warning": [], "danger": []}

    for table in soup.find_all("table"):
        tbody = table.find("tbody") or table
        rows_found = False
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
            rows_found = True

            def cell(i):
                return tds[i].get_text(" ", strip=True) if len(tds) > i else ""

            # 단계(주의/경고/위험) 컬럼 탐색
            level_key = "caution"
            for td in tds:
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
            break
    return result


# ── 메인 페이지에서 hidden 필드 추출 ──────────────────────────────────────────

def _get_hidden(session: requests.Session) -> dict:
    r = session.get(URL, params={"method": "investattentwarnriskyMain"},
                    headers=HDRS, timeout=20)
    r.encoding = "utf-8"
    print(f"[main] GET status={r.status_code} len={len(r.text)}", flush=True)
    if DEBUG:
        _save_debug("main_page", r.text, limit=30000)

    soup = BeautifulSoup(r.text, "html.parser")
    hidden = {}
    for inp in soup.find_all("input", {"type": "hidden"}):
        name = inp.get("name") or inp.get("id", "")
        if name:
            hidden[name] = inp.get("value", "")
    print(f"[main] hidden fields: {list(hidden.keys())}", flush=True)
    return hidden


# ── 데이터 요청 시도 목록 ──────────────────────────────────────────────────────

def _fetch(session: requests.Session) -> dict:
    hidden = _get_hidden(session)

    attempts = [
        # 시도 A: hidden 필드 포함 POST (가장 정확)
        ("POST", {**hidden, "method": "investattentwarnriskySub",
                  "pageIndex": "1", "rowCountPerPage": "3000"}),
        # 시도 B: 최소 필드 POST
        ("POST", {"method": "investattentwarnriskySub",
                  "pageIndex": "1", "rowCountPerPage": "3000"}),
        # 시도 C: forward 포함 POST
        ("POST", {**hidden, "method": "investattentwarnriskySub",
                  "forward": "investattentwarnrisky_sub",
                  "pageIndex": "1", "rowCountPerPage": "3000"}),
        # 시도 D: GET sub
        ("GET",  {"method": "investattentwarnriskySub",
                  "pageIndex": "1", "rowCountPerPage": "3000"}),
    ]

    for label, payload in attempts:
        try:
            if label == "POST":
                r = session.post(URL, data=payload, headers=HDRS, timeout=25)
            else:
                r = session.get(URL, params=payload, headers=HDRS, timeout=25)
            r.encoding = "utf-8"
            txt = r.text
            print(f"[{label}] method={payload.get('method')} "
                  f"status={r.status_code} len={len(txt)} "
                  f"preview={txt.strip()[:80].replace(chr(10),' ')!r}", flush=True)
            if DEBUG:
                _save_debug(f"attempt_{label}_{payload.get('method')}", txt)
            parsed = _parse(txt)
            total = sum(len(v) for v in parsed.values())
            if total > 0:
                print(f"  → {total}건 확인", flush=True)
                return parsed
        except Exception as e:
            print(f"[{label}] 오류: {e}", file=sys.stderr)

    return {"caution": [], "warning": [], "danger": []}


# ── 디버그 저장 ────────────────────────────────────────────────────────────────

def _save_debug(key: str, html: str, limit: int = 8000):
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"debug_kind_{key}.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html[:limit])
    print(f"  debug → {path}", flush=True)


# ── 메인 ──────────────────────────────────────────────────────────────────────

def main():
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now_str = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")

    session = requests.Session()
    try:
        resp = session.get(BASE, headers={"User-Agent": UA}, timeout=10)
        print(f"[init] KIND 홈 status={resp.status_code}", flush=True)
    except Exception as e:
        print(f"[warn] KIND 홈 실패: {e}", file=sys.stderr)

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
