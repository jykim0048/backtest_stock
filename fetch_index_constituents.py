"""코스피200 + 코스닥150 정확한 구성종목 갱신 -> index_constituents.json

소스 우선순위:
  1. pykrx (data.krx.co.kr) — 정확한 지수 구성종목. 단, 해외 IP 차단 가능성.
  2. (실패 시) 갱신 건너뜀 — 기존 파일 유지.

지수 코드:
  1028 = 코스피200, 2203 = 코스닥150

출력: data/index_constituents.json 과 public/assets/index_constituents.json
      두 곳 모두 갱신(screener.py 는 data/, 대시보드/필터는 public/assets/ 사용).
"""
import os
import sys
import json
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


def main():
    date = _recent_business_day()
    kst = datetime.timezone(datetime.timedelta(hours=9))
    now_str = datetime.datetime.now(kst).strftime("%Y-%m-%d %H:%M KST")

    try:
        data = fetch_via_pykrx(date)
    except Exception as e:
        print(f"[error] pykrx 구성종목 수집 실패: {e}", file=sys.stderr)
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
        "_comment": "코스피200 + 코스닥150 정확한 구성종목 (pykrx, data.krx.co.kr). "
                    "반기 정기변경(6·12월) 후 자동 갱신.",
        "updated":   now_str,
        "source":    "pykrx get_index_portfolio_deposit_file",
        "KOSPI200":  data["KOSPI200"],
        "KOSDAQ150": data["KOSDAQ150"],
    }

    for path in OUT_PATHS:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"saved -> {path}", flush=True)


if __name__ == "__main__":
    main()
