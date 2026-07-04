#!/usr/bin/env python3
"""EOD closing-price settlement file generator.

매매일지의 15:15 EOD 청산은 브라우저(클라이언트)에서 실행되는데, 그 시각에 탭이
열려 있지 않으면 청산 기회를 놓친다. 이 스크립트는 장 마감 후 CI(Actions)에서
당일 워치리스트 종목들의 '확정 종가'를 수집해 정적 JSON 으로 발행하고, 대시보드는
다음 접속 시 이 파일의 종가로 놓친 청산을 소급 정산해 해당 일자 매매일지에 기록한다.

Outputs:
  public/reports/eod/YYYY-MM-DD.json   {date, asof, prices: {code: {name, close}}, source}
  public/reports/eod/index.json        사용 가능한 날짜 목록 (신규순)

Env knobs:
  EOD_DATE   정산 일자 override (기본: KST 오늘)
  EOD_OUT    출력 디렉터리    (기본: public/reports/eod)
"""
import os
import sys
import json
import datetime

import pandas as pd
import yfinance as yf

ROOT = os.path.dirname(os.path.abspath(__file__))
KST = datetime.timezone(datetime.timedelta(hours=9))

OUT_DIR = os.environ.get("EOD_OUT") or os.path.join(ROOT, "public", "reports", "eod")
# 장전 워치리스트 + 장중 관심종목 합집합 — 클라이언트 보유 포지션은 이 중 하나에서 나온다.
WATCHLIST_FILES = ["watchlist.json", "intraday_watchlist.json"]


def load_codes():
    seen, out = set(), []
    for fn in WATCHLIST_FILES:
        path = os.path.join(ROOT, fn)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for s in json.load(f):
                    code = str(s.get("code", "")).zfill(6)
                    if code and code not in seen:
                        seen.add(code)
                        out.append({"code": code, "name": s.get("name", ""),
                                    "market": s.get("market", "KOSPI")})
        except Exception as ex:
            print(f"[eod] {fn} load failed: {ex}", file=sys.stderr)
    return out


def fetch_close(code, market, date_str):
    """당일 일봉 종가. 최신 봉의 날짜가 정산일과 다르면(휴장/데이터 지연) None."""
    suffix = ".KS" if market == "KOSPI" else ".KQ"
    try:
        hist = yf.download(f"{code}{suffix}", period="5d", progress=False, auto_adjust=True)
        if hist is None or len(hist) == 0:
            return None
        if isinstance(hist.columns, pd.MultiIndex):
            hist.columns = hist.columns.get_level_values(0)
        bar_date = hist.index[-1].strftime("%Y-%m-%d")
        if bar_date != date_str:
            print(f"[eod] {code}: 최신 봉 {bar_date} != 정산일 {date_str} — 제외", file=sys.stderr)
            return None
        return int(round(float(hist["Close"].iloc[-1])))
    except Exception as ex:
        print(f"[eod] {code} fetch failed: {ex}", file=sys.stderr)
        return None


def main():
    date_str = os.environ.get("EOD_DATE") or datetime.datetime.now(KST).strftime("%Y-%m-%d")
    stocks = load_codes()
    print(f"=== EOD close settlement ({date_str}, {len(stocks)} codes) ===")

    prices = {}
    for s in stocks:
        close = fetch_close(s["code"], s["market"], date_str)
        if close is not None:
            prices[s["code"]] = {"name": s["name"], "close": close}
            print(f"  {s['name']} ({s['code']}): {close:,}")

    if not prices:
        # 휴장일/데이터 미도달 — 파일을 만들지 않는다 (클라이언트는 없으면 폴백).
        print("  No closes collected — skip writing (holiday or data not ready).")
        return

    out = {
        "date": date_str,
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "prices": prices,
        "source": "Yahoo Finance daily close",
    }
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{date_str}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  Saved: {path}")

    index_path = os.path.join(OUT_DIR, "index.json")
    index = []
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except Exception:
            index = []
    if date_str not in index:
        index.append(date_str)
    index.sort(reverse=True)
    index = index[:90]   # 매매일지 보관 기간(JOURNAL_MAX_DAYS)과 동일
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"  Updated index: {index_path} ({len(index)} entries)")
    print("=== Done ===")


if __name__ == "__main__":
    main()
