"""코스피200 + 코스닥150 정확한 구성종목 갱신 -> index_constituents.json

소스 우선순위:
  1. pykrx (data.krx.co.kr) — 정확한 지수 구성종목. 단, 해외 IP 차단 가능성 +
     2026-10 기준 KRX 로그인(KRX_ID/KRX_PW) 없으면 빈 응답.
  2. 네이버/와이즈리포트 폴백 — 코스피200 = m.stock.naver.com 지수 편입종목 API,
     코스닥150 = KODEX 코스닥150(229200) ETF 구성종목(CU). 종목명→코드는 네이버
     자동완성(정확 일치)으로 확정 — krx_companies.json 은 사명변경이 늦게 반영됨.
  3. (둘 다 실패 시) 갱신 건너뜀 — 기존 파일 유지.

출력에 "names"(code → 현재 종목명)도 함께 저장 — press_news 종목 매칭용.

지수 코드:
  1028 = 코스피200, 2203 = 코스닥150

출력: data/index_constituents.json 과 public/assets/index_constituents.json
      두 곳 모두 갱신(screener.py 는 data/, 대시보드/필터는 public/assets/ 사용).
"""
import os
import sys
import json
import re
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATHS = [
    os.path.join(ROOT, "data", "index_constituents.json"),
    os.path.join(ROOT, "public", "assets", "index_constituents.json"),
]

INDEX_CODES = {
    "KOSPI200":  "1028",
    "KOSDAQ150": "2203",
}


def _recent_business_day() -> str:
    """오늘이 주말이면 직전 금요일로. KRX 영업일 근사 (YYYYMMDD)."""
    kst = datetime.timezone(datetime.timedelta(hours=9))
    d = datetime.datetime.now(kst)
    while d.weekday() >= 5:          # 5=토, 6=일
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def fetch_via_pykrx(date: str) -> dict:
    """pykrx 로 지수별 구성종목 코드 리스트 반환. 실패 시 예외."""
    from pykrx import stock

    result = {}
    for name, idx in INDEX_CODES.items():
        # 당일 데이터가 아직 없으면 직전 영업일들로 최대 5일 후퇴
        codes = []
        probe = datetime.datetime.strptime(date, "%Y%m%d")
        for _ in range(6):
            ymd = probe.strftime("%Y%m%d")
            try:
                codes = stock.get_index_portfolio_deposit_file(idx, ymd)
            except TypeError:
                # 일부 버전은 date 인자 미지원 -> 최신 호출
                codes = stock.get_index_portfolio_deposit_file(idx)
            if codes:
                print(f"[pykrx] {name}({idx}) {ymd}: {len(codes)}종목", flush=True)
                break
            probe -= datetime.timedelta(days=1)
            while probe.weekday() >= 5:
                probe -= datetime.timedelta(days=1)
        if not codes:
            raise RuntimeError(f"{name}({idx}) 구성종목 빈 응답")
        result[name] = sorted(str(c).zfill(6) for c in codes)
    return result


NAVER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
KQ150_ETF = "229200"          # KODEX 코스닥150 — 지수 완전복제 ETF


def _naver_code_by_name(name: str):
    """네이버 자동완성에서 종목명 정확 일치 코드. 없으면 None."""
    import requests
    r = requests.get("https://ac.stock.naver.com/ac",
                     params={"q": name, "target": "stock"},
                     headers={"User-Agent": NAVER_UA}, timeout=10)
    r.raise_for_status()
    for it in r.json().get("items", []):
        if it.get("name") == name and it.get("nationCode") == "KOR":
            return it.get("code")
    return None


def fetch_via_naver() -> tuple:
    """(codes_by_index, names) — 코스피200 은 네이버 지수 API, 코스닥150 은 ETF CU."""
    import requests
    names = {}

    k200 = []
    for page in range(1, 10):            # pageSize 상한 50 (100 은 HTTP 400)
        r = requests.get("https://m.stock.naver.com/api/index/KPI200/enrollStocks",
                         params={"page": page, "pageSize": 50},
                         headers={"User-Agent": NAVER_UA}, timeout=15)
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        for x in rows:
            k200.append(x["itemCode"])
            names[x["itemCode"]] = x["stockName"]
    print(f"[naver] KOSPI200: {len(k200)}종목", flush=True)

    r = requests.get("https://navercomp.wisereport.co.kr/v2/ETF/index.aspx",
                     params={"cmp_cd": KQ150_ETF},
                     headers={"User-Agent": NAVER_UA}, timeout=20)
    r.raise_for_status()
    m = re.search(r"CU_data\s*=\s*(\{.*?\});", r.text, re.S)
    if not m:
        raise RuntimeError("ETF CU_data 없음")
    rows = json.loads(m.group(1)).get("grid_data") or []
    kq150, unresolved = [], []
    for x in rows:
        nm = (x.get("STK_NM_KOR") or "").strip()
        if not nm or nm.endswith("현금") or not x.get("ETF_WEIGHT"):
            continue
        code = _naver_code_by_name(nm)
        if code:
            kq150.append(code)
            names[code] = nm
        else:
            unresolved.append(nm)
    print(f"[naver] KOSDAQ150(ETF {KQ150_ETF} CU {rows[0].get('TRD_DT') if rows else '?'}): "
          f"{len(kq150)}종목, 코드 미확정 {unresolved}", flush=True)
    if unresolved:
        raise RuntimeError(f"코스닥150 종목코드 미확정: {unresolved}")

    return ({"KOSPI200": sorted(set(k200)), "KOSDAQ150": sorted(set(kq150))}, names)


def main():
    date = _recent_business_day()
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now_str = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")

    names, source = {}, "pykrx get_index_portfolio_deposit_file"
    try:
        data = fetch_via_pykrx(date)
    except Exception as e:
        print(f"[warn] pykrx 구성종목 수집 실패: {e} — 네이버 폴백", file=sys.stderr)
        try:
            data, names = fetch_via_naver()
            source = f"naver KPI200 enrollStocks + KODEX코스닥150({KQ150_ETF}) CU"
        except Exception as e2:
            print(f"[error] 네이버 폴백도 실패: {e2}", file=sys.stderr)
            print("기존 index_constituents.json 유지 — 갱신 안 함.", file=sys.stderr)
            sys.exit(1)

    k = len(data.get("KOSPI200", []))
    q = len(data.get("KOSDAQ150", []))
    print(f"[result] KOSPI200={k}  KOSDAQ150={q}", flush=True)

    # sanity check: 정상 범위(코스피200≈200, 코스닥150≈150)
    if not (150 <= k <= 220) or not (120 <= q <= 170):
        print(f"[warn] 종목 수가 예상 범위를 벗어남 (K={k}, Q={q}) — "
              f"그래도 저장은 진행", flush=True)

    out = {
        "_comment": "코스피200 + 코스닥150 정확한 구성종목 (pykrx, 실패 시 네이버 폴백). "
                    "반기 정기변경(6·12월) 후 자동 갱신.",
        "updated":   now_str,
        "source":    source,
        "KOSPI200":  data["KOSPI200"],
        "KOSDAQ150": data["KOSDAQ150"],
    }
    if names:
        out["names"] = {c: names[c] for c in sorted(names)}

    for path in OUT_PATHS:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"saved -> {path}", flush=True)


if __name__ == "__main__":
    main()
