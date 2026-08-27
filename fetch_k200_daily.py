# -*- coding: utf-8 -*-
"""KOSPI200 지수 일봉 OHLCV 수집 → data/k200_daily.csv

rwkv_ts_kospi200 논문 실험용 원천 데이터. Actions(k200_daily.yml)에서 실행.

1차: fchart.stock.naver.com XML (한 번에 전 기간, date|open|high|low|close|volume)
2차 폴백: m.stock.naver.com/api/index/KPI200/price 페이지네이션 JSON

volume 단위: 천주(fchart 기준). 값 자체보다 변화율로 쓰므로 단위는 스케일 무관.
"""
import csv
import os
import re
import sys

import requests

SYMBOL = "KPI200"
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "k200_daily.csv")
MIN_ROWS = 4000          # 약 16년치 미만이면 실패 처리
FETCH_COUNT = 9000       # fchart 요청 개수(1994 상장 이후 전 기간 커버)
UA = {"User-Agent": "Mozilla/5.0"}


def fetch_fchart():
    """fchart XML: <item data="20050103|110.11|112.03|109.85|111.63|123456"/>"""
    url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={SYMBOL}"
           f"&timeframe=day&count={FETCH_COUNT}&requestType=0")
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    rows = []
    for m in re.finditer(r'<item\s+data="([^"]+)"', r.text):
        parts = m.group(1).split("|")
        if len(parts) < 6:
            continue
        d, o, h, l, c, v = parts[:6]
        try:
            rows.append({
                "date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                "open": float(o), "high": float(h),
                "low": float(l), "close": float(c), "volume": float(v),
            })
        except ValueError:
            continue
    print(f"[fchart] parsed rows={len(rows)}", flush=True)
    return rows


def fetch_mstock():
    """폴백: 모바일 API 페이지네이션(최신→과거). 필드에 콤마 포함 문자열."""
    def _num(s):
        return float(str(s or "").replace(",", ""))
    rows, page = [], 1
    while page <= 150:
        url = (f"https://m.stock.naver.com/api/index/{SYMBOL}/price"
               f"?pageSize=100&page={page}")
        r = requests.get(url, headers=UA, timeout=15)
        r.raise_for_status()
        items = r.json() or []
        if not items:
            break
        for it in items:
            try:
                rows.append({
                    "date": str(it["localTradedAt"])[:10],
                    "open": _num(it["openPrice"]),
                    "high": _num(it["highPrice"]),
                    "low": _num(it["lowPrice"]),
                    "close": _num(it["closePrice"]),
                    "volume": _num(it.get("accumulatedTradingVolume", 0)),
                })
            except (KeyError, ValueError):
                continue
        page += 1
    rows.sort(key=lambda r: r["date"])
    print(f"[mstock] parsed rows={len(rows)}", flush=True)
    return rows


def validate(rows):
    assert len(rows) >= MIN_ROWS, f"too few rows: {len(rows)} < {MIN_ROWS}"
    dates = [r["date"] for r in rows]
    assert dates == sorted(dates), "dates not ascending"
    assert len(dates) == len(set(dates)), "duplicate dates"
    bad = [r for r in rows
           if not (r["high"] >= max(r["open"], r["close"]) - 1e-6
                   and r["low"] <= min(r["open"], r["close"]) + 1e-6
                   and r["close"] > 0)]
    # 아주 오래된 구간의 소수 이상치는 허용(0.1% 미만), 최근 구간은 무결점 요구
    assert len(bad) <= len(rows) // 1000, f"OHLC sanity failed rows={len(bad)}, e.g. {bad[:3]}"
    recent_bad = [r for r in bad if r["date"] >= "2005-01-01"]
    assert not recent_bad, f"OHLC sanity failed in recent data: {recent_bad[:3]}"


def main():
    try:
        rows = fetch_fchart()
    except Exception as e:
        print(f"[fchart] failed: {e}", flush=True)
        rows = []
    if len(rows) < MIN_ROWS:
        print("[main] falling back to m.stock API", flush=True)
        rows = fetch_mstock()

    validate(rows)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(rows)
    print(f"[result] rows={len(rows)}  range={rows[0]['date']}..{rows[-1]['date']}"
          f"  last_close={rows[-1]['close']}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
