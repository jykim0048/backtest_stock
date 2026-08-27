# -*- coding: utf-8 -*-
"""KOSPI200 지수 일봉 OHLCV 수집 → data/k200_daily.csv

rwkv_ts_kospi200 논문 실험용 원천 데이터. Actions(k200_daily.yml)에서 실행.

소스(순서대로 시도, 성공 기준 MIN_ROWS 이상):
  1. fchart siseJson.naver (JSON 유사 배열, 전 기간 한 번에)
  2. fchart sise.nhn (구형 XML)
  3. m.stock.naver.com /api/index/KPI200/price 페이지네이션

실패 시 진단을 data/k200_fetch_debug.txt 로 남긴다(Actions 로그 열람 불가 환경 대응
— 워크플로가 실패해도 이 파일은 커밋되므로 로컬에서 git pull 로 원인 확인).

volume 단위: 천주(fchart 기준). 변화율로만 쓰므로 스케일 무관.
"""
import csv
import datetime
import os
import re
import sys
import traceback

import requests

SYMBOL = "KPI200"
BASE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE, "data", "k200_daily.csv")
DEBUG_PATH = os.path.join(BASE, "data", "k200_fetch_debug.txt")
MIN_ROWS = 4000
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

DIAG = []


def diag(msg):
    print(msg, flush=True)
    DIAG.append(str(msg))


def _get(url, timeout=30):
    r = requests.get(url, headers=UA, timeout=timeout)
    diag(f"GET {url} -> {r.status_code} len={len(r.text)} head={r.text[:200]!r}")
    r.raise_for_status()
    return r


def fetch_sisejson():
    """[['날짜','시가','고가','저가','종가','거래량','외국인소진율'], ['20050103',...], ...]"""
    end = datetime.date.today().strftime("%Y%m%d")
    url = (f"https://fchart.stock.naver.com/siseJson.naver?symbol={SYMBOL}"
           f"&requestType=1&startTime=19940601&endTime={end}&timeframe=day")
    text = _get(url).text
    rows = []
    for m in re.finditer(r'\[\s*"(\d{8})"\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)', text):
        d, o, h, l, c, v = m.groups()
        rows.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                     "open": float(o), "high": float(h),
                     "low": float(l), "close": float(c), "volume": float(v)})
    diag(f"[sisejson] parsed rows={len(rows)}")
    return rows


def fetch_fchart_xml():
    """<item data="20050103|110.11|112.03|109.85|111.63|123456"/>"""
    url = (f"https://fchart.stock.naver.com/sise.nhn?symbol={SYMBOL}"
           f"&timeframe=day&count=9000&requestType=0")
    text = _get(url).text
    rows = []
    for m in re.finditer(r'<item\s+data="([^"]+)"', text):
        parts = m.group(1).split("|")
        if len(parts) < 6:
            continue
        d, o, h, l, c, v = parts[:6]
        try:
            rows.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:8]}",
                         "open": float(o), "high": float(h),
                         "low": float(l), "close": float(c), "volume": float(v)})
        except ValueError:
            continue
    diag(f"[fchart_xml] parsed rows={len(rows)}")
    return rows


def fetch_mstock():
    """모바일 API 페이지네이션(최신→과거). 숫자 필드에 콤마 포함."""
    def _num(s):
        return float(str(s or "0").replace(",", ""))
    rows, page, fails = [], 1, 0
    while page <= 250 and fails < 3:
        url = (f"https://m.stock.naver.com/api/index/{SYMBOL}/price"
               f"?pageSize=60&page={page}")
        try:
            items = _get(url, timeout=15).json() or []
        except Exception as e:
            diag(f"[mstock] page={page} error: {e}")
            fails += 1
            page += 1
            continue
        if not items:
            break
        for it in items:
            try:
                rows.append({"date": str(it["localTradedAt"])[:10],
                             "open": _num(it["openPrice"]),
                             "high": _num(it["highPrice"]),
                             "low": _num(it["lowPrice"]),
                             "close": _num(it["closePrice"]),
                             "volume": _num(it.get("accumulatedTradingVolume"))})
            except (KeyError, ValueError) as e:
                diag(f"[mstock] row skip: {e} item_keys={list(it)[:8]}")
        page += 1
    dedup = {r["date"]: r for r in rows}
    rows = sorted(dedup.values(), key=lambda r: r["date"])
    diag(f"[mstock] parsed rows={len(rows)}")
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
    # 아주 오래된 구간의 소수 이상치는 허용(0.1% 미만), 2005년 이후는 무결점 요구
    assert len(bad) <= len(rows) // 1000, f"OHLC sanity failed rows={len(bad)}, e.g. {bad[:3]}"
    recent_bad = [r for r in bad if r["date"] >= "2005-01-01"]
    assert not recent_bad, f"OHLC sanity failed in recent data: {recent_bad[:3]}"


def main():
    rows = []
    for fetch in (fetch_sisejson, fetch_fchart_xml, fetch_mstock):
        try:
            rows = fetch()
        except Exception:
            diag(f"[{fetch.__name__}] EXCEPTION:\n{traceback.format_exc()}")
            rows = []
        if len(rows) >= MIN_ROWS:
            break

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    try:
        validate(rows)
    except AssertionError:
        diag(f"[validate] FAILED:\n{traceback.format_exc()}")
        with open(DEBUG_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(DIAG))
        return 1

    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "open", "high", "low", "close", "volume"])
        w.writeheader()
        w.writerows(rows)
    if os.path.exists(DEBUG_PATH):
        os.remove(DEBUG_PATH)
    diag(f"[result] rows={len(rows)}  range={rows[0]['date']}..{rows[-1]['date']}"
         f"  last_close={rows[-1]['close']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
