# -*- coding: utf-8 -*-
"""미국 주가지수 일별 종가 스냅샷 수집 — rwkv_ts_kospi200 논문 Set D(외생시장정보)용.

Actions(us_index_daily.yml)에서만 실행한다(로컬 PC 수집 금지 규칙). 산출물은 레포에
커밋하지 않고 workflow artifact 로만 업로드한다(지수 데이터 재배포 제한 — 공개 레포).

대상(가격지수, 현지 세션 종가):
  - S&P 500 가격지수        Yahoo ^GSPC   / FRED SP500 (교차검증, FRED는 최근 10년만 제공)
  - Nasdaq-100 가격지수     Yahoo ^NDX    / FRED NASDAQ100 (교차검증)
  (Nasdaq Composite ^IXIC 아님. ETF·총수익지수 아님.)

1차 공급자는 Yahoo chart API(v8) JSON 원문을 그대로 저장한다(원문 SHA256 보존).
chart API 실패 시에만 yfinance 로 재시도하고 method 필드에 기록한다.
FRED fredgraph.csv 는 API 키 없이 공개 CSV 를 받는다(교차검증 전용).

출력: out/us_index_daily/<snapshot_id>/
  raw/<provider>_<series>.<json|csv>   원문 바이트
  parsed/<provider>_<series>.csv       session_date,close[,open,high,low,adjclose,volume]
  manifest.json                        URL·수집시각(UTC)·HTTP 상태·SHA256·행수·기간·환경
"""
import datetime as dt
import hashlib
import json
import os
import platform
import sys
import time
import traceback
from zoneinfo import ZoneInfo

import requests

START = dt.datetime(2004, 1, 1, tzinfo=dt.timezone.utc)   # 2006 학습 시작 전 look-back 여유
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
NY = ZoneInfo("America/New_York")
BASE = os.path.dirname(os.path.abspath(__file__))

YAHOO = {"sp500": "^GSPC", "nasdaq100": "^NDX"}
FRED = {"sp500": "SP500", "nasdaq100": "NASDAQ100"}


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def now_utc():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def write_csv(path, header, rows):
    lines = [",".join(header)] + [",".join("" if v is None else str(v) for v in r) for r in rows]
    write(path, ("\n".join(lines) + "\n").encode("utf-8"))


def yahoo_chart(symbol):
    p1 = int(START.timestamp())
    p2 = int(time.time()) + 86400
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(symbol)}"
           f"?period1={p1}&period2={p2}&interval=1d&events=history&includeAdjustedClose=true")
    last = None
    for attempt in range(4):
        r = requests.get(url, headers=UA, timeout=60)
        last = r
        if r.status_code == 200:
            break
        time.sleep(5 * (attempt + 1))
    js = last.json() if last.status_code == 200 else None
    rows = []
    if js:
        res = js["chart"]["result"][0]
        q = res["indicators"]["quote"][0]
        adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
        for i, ts in enumerate(res["timestamp"]):
            d = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).astimezone(NY).date()
            rows.append([d.isoformat(), q["close"][i], q["open"][i], q["high"][i], q["low"][i],
                         adj[i] if adj else None, q["volume"][i]])
    return url, last.status_code, last.content, rows


def yahoo_yfinance(symbol):
    import yfinance as yf
    df = yf.Ticker(symbol).history(start=START.date().isoformat(), interval="1d",
                                   auto_adjust=False, actions=False)
    rows = [[idx.date().isoformat(), r["Close"], r["Open"], r["High"], r["Low"],
             r.get("Adj Close"), r["Volume"]] for idx, r in df.iterrows()]
    raw = df.to_csv().encode("utf-8")
    return f"yfinance {yf.__version__} Ticker({symbol}).history", 200, raw, rows


def fred_csv(series):
    """FRED_API_KEY 가 있으면 공식 API(JSON), 없으면 fredgraph.csv. 기록 URL에서 키는 가린다."""
    key = os.environ.get("FRED_API_KEY")
    rows = []
    if key:
        url = ("https://api.stlouisfed.org/fred/series/observations"
               f"?series_id={series}&file_type=json&observation_start=2004-01-01&api_key=")
        r = requests.get(url + key, headers=UA, timeout=120)
        if r.status_code == 200:
            for o in r.json()["observations"]:
                if o["value"] not in (".", ""):
                    rows.append([o["date"], float(o["value"])])
        return url + "<REDACTED>", r.status_code, r.content, rows
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"
    r = requests.get(url, headers=UA, timeout=120)
    if r.status_code == 200:
        for line in r.text.strip().splitlines()[1:]:
            d, v = line.split(",")[:2]
            if v not in (".", ""):
                rows.append([d, float(v)])
    return url, r.status_code, r.content, rows


NAVER = {"sp500": "SPI@SPX", "nasdaq100": "NAS@NDX"}


def naver_world(symbol, max_pages=800):
    """네이버 해외지수 일별 시세(10행/페이지, 최신→과거). 교차검증 전용."""
    base = f"https://finance.naver.com/world/worldDayListJson.naver?symbol={symbol}&fdtc=0&page="
    pages, rows, status = [], {}, None
    for p in range(1, max_pages + 1):
        r = requests.get(base + str(p), headers=UA, timeout=30)
        status = r.status_code
        if r.status_code != 200:
            break
        pages.append(r.text)
        items = r.json() or []
        if not items:
            break
        for it in items:
            d = str(it["xymd"])
            rows[f"{d[:4]}-{d[4:6]}-{d[6:8]}"] = float(it["clos"])
        if min(rows) < "2004-01-01":
            break
        time.sleep(0.05)
    raw = ("[" + ",".join(pages) + "]").encode("utf-8")
    return base + "<1..N>", status, raw, [[d, rows[d]] for d in sorted(rows)]


def main():
    snap = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = os.path.join(BASE, "out", "us_index_daily", snap)
    manifest = {"snapshot_id": snap, "created_utc": now_utc(),
                "purpose": "rwkv_ts_kospi200 Set D (A + sp500_ret + nasdaq100_ret)",
                "github": {k: os.environ.get(k) for k in
                           ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_REF", "GITHUB_RUN_ID",
                            "GITHUB_WORKFLOW")},
                "python": sys.version.split()[0], "platform": platform.platform(),
                "requests": requests.__version__, "files": [], "errors": []}
    ok_primary = True

    for name, sym in YAHOO.items():
        entry = {"provider": "yahoo", "series": name, "symbol": sym, "role": "primary"}
        try:
            url, status, raw, rows = yahoo_chart(sym)
            entry["method"] = "chart_v8_json"
            if status != 200 or len(rows) < 4000:
                manifest["errors"].append(f"yahoo chart {sym}: status={status} rows={len(rows)}")
                url, status, raw, rows = yahoo_yfinance(sym)
                entry["method"] = "yfinance_fallback"
            ext = "json" if entry["method"] == "chart_v8_json" else "csv"
            raw_path = f"raw/yahoo_{name}.{ext}"
            write(os.path.join(out, raw_path), raw)
            write_csv(os.path.join(out, f"parsed/yahoo_{name}.csv"),
                      ["session_date", "close", "open", "high", "low", "adjclose", "volume"], rows)
            entry.update(url=url, http_status=status, downloaded_at_utc=now_utc(),
                         raw_file=raw_path, raw_sha256=sha256(raw), raw_bytes=len(raw),
                         parsed_file=f"parsed/yahoo_{name}.csv", rows=len(rows),
                         first_date=rows[0][0] if rows else None,
                         last_date=rows[-1][0] if rows else None)
            if len(rows) < 4000:
                ok_primary = False
        except Exception:
            ok_primary = False
            manifest["errors"].append(f"yahoo {sym}:\n{traceback.format_exc()}")
        manifest["files"].append(entry)

    for name, sid in FRED.items():
        entry = {"provider": "fred", "series": name, "symbol": sid, "role": "cross_check"}
        try:
            url, status, raw, rows = fred_csv(sid)
            write(os.path.join(out, f"raw/fred_{name}.csv"), raw)
            write_csv(os.path.join(out, f"parsed/fred_{name}.csv"), ["session_date", "close"], rows)
            entry.update(method="fredgraph_csv", url=url, http_status=status,
                         downloaded_at_utc=now_utc(), raw_file=f"raw/fred_{name}.csv",
                         raw_sha256=sha256(raw), raw_bytes=len(raw),
                         parsed_file=f"parsed/fred_{name}.csv", rows=len(rows),
                         first_date=rows[0][0] if rows else None,
                         last_date=rows[-1][0] if rows else None)
        except Exception:
            manifest["errors"].append(f"fred {sid}:\n{traceback.format_exc()}")
        manifest["files"].append(entry)

    for name, sym in NAVER.items():
        entry = {"provider": "naver", "series": name, "symbol": sym, "role": "cross_check"}
        try:
            url, status, raw, rows = naver_world(sym)
            write(os.path.join(out, f"raw/naver_{name}.json"), raw)
            write_csv(os.path.join(out, f"parsed/naver_{name}.csv"), ["session_date", "close"], rows)
            entry.update(method="worldDayListJson", url=url, http_status=status,
                         downloaded_at_utc=now_utc(), raw_file=f"raw/naver_{name}.json",
                         raw_sha256=sha256(raw), raw_bytes=len(raw),
                         parsed_file=f"parsed/naver_{name}.csv", rows=len(rows),
                         first_date=rows[0][0] if rows else None,
                         last_date=rows[-1][0] if rows else None,
                         raw_head=raw[:300].decode("utf-8", "replace"))
        except Exception:
            manifest["errors"].append(f"naver {sym}:\n{traceback.format_exc()}")
        manifest["files"].append(entry)

    for e in manifest["files"]:
        if e.get("parsed_file"):
            with open(os.path.join(out, e["parsed_file"]), "rb") as f:
                e["parsed_sha256"] = sha256(f.read())
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(json.dumps({k: v for k, v in manifest.items() if k != "files"}, indent=2))
    for e in manifest["files"]:
        print(e.get("provider"), e.get("symbol"), e.get("method"), e.get("http_status"),
              e.get("rows"), e.get("first_date"), e.get("last_date"))
    sys.exit(0 if ok_primary else 1)


if __name__ == "__main__":
    main()
