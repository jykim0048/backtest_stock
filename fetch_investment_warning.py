"""KIND(kind.krx.co.kr) 투자주의·경고·위험종목 파싱 → public/data/investment_warning.json"""
import os
import sys
import json
import datetime
import requests
from bs4 import BeautifulSoup

BASE = "https://kind.krx.co.kr"
UA   = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

PATHS = {
    "caution": "/investinfo/investinfo03.do",  # 투자주의
    "warning": "/investinfo/investinfo04.do",  # 투자경고
    "danger":  "/investinfo/investinfo05.do",  # 투자위험
}


def _parse_table(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    tbody = table.find("tbody") or table
    rows = []
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        code = tds[0].get_text(strip=True)
        if not code or not code[:1].isdigit():
            continue
        rows.append({
            "code":   code.zfill(6),
            "name":   tds[1].get_text(strip=True) if len(tds) > 1 else "",
            "market": tds[2].get_text(strip=True) if len(tds) > 2 else "",
            "reason": tds[3].get_text(strip=True) if len(tds) > 3 else "",
            "date":   tds[4].get_text(strip=True) if len(tds) > 4 else "",
        })
    return rows


def _fetch(session: requests.Session, path: str) -> list:
    url     = BASE + path
    forward = path.split("/")[-1].replace(".do", "_sub")
    payload = {
        "method":           "searchInvestInfoSub",
        "forward":          forward,
        "pageIndex":        "1",
        "rowCountPerPage":  "1000",
    }
    headers = {
        "User-Agent":    UA,
        "Referer":       url,
        "Content-Type":  "application/x-www-form-urlencoded; charset=utf-8",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }
    r = session.post(url, data=payload, headers=headers, timeout=25)
    r.encoding = "utf-8"
    return _parse_table(r.text)


def main():
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now_str = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")

    session = requests.Session()
    try:
        session.get(BASE, headers={"User-Agent": UA}, timeout=10)
    except Exception as e:
        print(f"[warn] KIND 세션 초기화 실패: {e}", file=sys.stderr)

    result = {"updated": now_str, "caution": [], "warning": [], "danger": []}
    for key, path in PATHS.items():
        try:
            items = _fetch(session, path)
            result[key] = items
            print(f"{key}: {len(items)}건")
        except Exception as e:
            print(f"[error] {key} ({path}) 실패: {e}", file=sys.stderr)

    out_dir  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "public", "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "investment_warning.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"saved → {out_path}")


if __name__ == "__main__":
    main()
