#!/usr/bin/env python3
"""
Daily market report generator.
Reads watchlist.json, fetches price data via yfinance, and writes:
  - reports/YYYY-MM-DD.json  (cumulative archive)
  - reports/index.json        (list of available dates, newest first)
  - daily_market_report.json  (today's report, read by the dashboard)
"""
import json
import os
import sys
import datetime

import yfinance as yf

ROOT           = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_PATH = os.path.join(ROOT, 'watchlist.json')
REPORTS_DIR    = os.path.join(ROOT, 'reports')
DAILY_PATH     = os.path.join(ROOT, 'daily_market_report.json')


def load_watchlist():
    with open(WATCHLIST_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def round100(v):
    return int(round(v / 100) * 100)


def fetch_stock_data(code, market):
    suffix = '.KS' if market == 'KOSPI' else '.KQ'
    hist = yf.download(f"{code}{suffix}", period='10d', progress=False, auto_adjust=True)

    if hist is None or len(hist) < 2:
        return None

    close      = float(hist['Close'].iloc[-1])
    high       = float(hist['High'].iloc[-1])
    low        = float(hist['Low'].iloc[-1])
    prev_close = float(hist['Close'].iloc[-2])

    change_pct = ((close - prev_close) / prev_close) * 100
    atr        = high - low

    return {
        'basePrice': int(close),
        'entry':     round100(close * 1.005),
        'target':    round100(close + atr * 2.0),
        'stop':      round100(close - atr * 1.5),
        'changePct': round(change_pct, 2),
    }


def stars_for(change_pct):
    a = abs(change_pct)
    if a >= 7: return '⭐⭐⭐⭐⭐'
    if a >= 4: return '⭐⭐⭐⭐'
    return '⭐⭐⭐'


def generate_report():
    watchlist = load_watchlist()
    today = datetime.date.today().strftime('%Y-%m-%d')

    print(f"=== Generating daily market report ({today}) ===")

    report = []
    for stock in watchlist:
        code, name, market = stock['code'], stock['name'], stock['market']
        print(f"  Fetching {name} ({code})...")

        data = fetch_stock_data(code, market)
        if not data:
            print(f"  Skipped {name}: no data returned", file=sys.stderr)
            continue

        chg       = data['changePct']
        sign      = '+' if chg >= 0 else ''
        direction = '상승' if chg >= 0 else '하락'

        report.append({
            'code':      code,
            'name':      name,
            'market':    market,
            'basePrice': data['basePrice'],
            'entry':     data['entry'],
            'target':    data['target'],
            'stop':      data['stop'],
            'stars':     stars_for(chg),
            'catalyst': (
                f"전일 종가 기준 {direction} {sign}{chg:.2f}% 마감. "
                f"자동 생성 리포트 — 세부 촉매 분석은 수동 업데이트가 필요합니다."
            ),
            'scenario': (
                f"<b>[자동 생성 시나리오]</b><br>"
                f"전일 종가 {data['basePrice']:,}원 기준으로 산출된 기술적 레벨입니다. "
                f"진입 상한가 {data['entry']:,}원 이하에서 분할 매수하고, "
                f"목표 익절가 {data['target']:,}원, 손절선 {data['stop']:,}원으로 리스크를 관리하세요."
            ),
        })

    os.makedirs(REPORTS_DIR, exist_ok=True)

    dated_path = os.path.join(REPORTS_DIR, f'{today}.json')
    with open(dated_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  Saved archive : {dated_path}")

    with open(DAILY_PATH, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  Updated       : {DAILY_PATH}")

    index_path = os.path.join(REPORTS_DIR, 'index.json')
    index = []
    if os.path.exists(index_path):
        with open(index_path, 'r', encoding='utf-8') as f:
            index = json.load(f)

    if today not in index:
        index.append(today)
        index.sort(reverse=True)

    with open(index_path, 'w', encoding='utf-8') as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"  Updated index : {index_path} ({len(index)} entries)")
    print("=== Done ===")


if __name__ == '__main__':
    generate_report()
