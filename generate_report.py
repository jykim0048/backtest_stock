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

import pandas as pd
import yfinance as yf

ROOT           = os.path.dirname(os.path.abspath(__file__))

# Input/output are env-overridable so the same generator serves the
# 장전 워치리스트 (defaults), 장중 관심종목, 하락 관찰 pipelines:
#   REPORT_WATCHLIST=intraday_watchlist.json
#   REPORT_OUT=public/intraday_report.json
#   REPORT_ARCHIVE=0   # 장중은 최신본만 — 날짜별 아카이브/index 드롭다운에 섞지 않음
#   REPORT_SIDE=down   # 하락 관찰(참고용) — 매수 안내 대신 관찰용 시나리오 문구
WATCHLIST_PATH = os.environ.get('REPORT_WATCHLIST') or os.path.join(ROOT, 'watchlist.json')
DAILY_PATH     = os.environ.get('REPORT_OUT')       or os.path.join(ROOT, 'public', 'daily_market_report.json')
REPORTS_DIR    = os.environ.get('REPORT_ARCHIVE_DIR') or os.path.join(ROOT, 'public', 'reports')
ARCHIVE        = os.environ.get('REPORT_ARCHIVE', '1') != '0'   # 날짜별 아카이브 + index.json 갱신 여부
SIDE           = os.environ.get('REPORT_SIDE', 'up')            # 'up' | 'down'(하락 관찰)
# 장중 증분 분석: 직전 리포트의 deep-research(analysis/catalyst)를 코드별로 이월해
# generate_analysis 가 신규/갱신 대상만 다시 분석하게 한다(반복 재분석 LLM 비용 절감).
CARRY_ANALYSIS = os.environ.get('REPORT_CARRY_ANALYSIS') == '1'

if not os.path.isabs(WATCHLIST_PATH):
    WATCHLIST_PATH = os.path.join(ROOT, WATCHLIST_PATH)
if not os.path.isabs(DAILY_PATH):
    DAILY_PATH = os.path.join(ROOT, DAILY_PATH)
if not os.path.isabs(REPORTS_DIR):
    REPORTS_DIR = os.path.join(ROOT, REPORTS_DIR)


def load_watchlist():
    # 하락 관찰 리스트는 LLM 이 못 돌았거나 도입 전이면 파일이 없을 수 있다 — 빈 리포트로.
    if not os.path.exists(WATCHLIST_PATH):
        print(f"  [watchlist] {WATCHLIST_PATH} 없음 — 빈 리스트로 진행", file=sys.stderr)
        return []
    with open(WATCHLIST_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)


def round100(v):
    return int(round(v / 100) * 100)


def fetch_stock_data(code, market):
    suffix = '.KS' if market == 'KOSPI' else '.KQ'
    hist = yf.download(f"{code}{suffix}", period='10d', progress=False, auto_adjust=True)

    if hist is None or len(hist) < 2:
        return None

    # yfinance 1.x returns MultiIndex columns even for single tickers
    if isinstance(hist.columns, pd.MultiIndex):
        hist.columns = hist.columns.get_level_values(0)

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
    # Use KST (Asia/Seoul), not the runner's UTC clock. The CI cron fires at
    # 23:00 UTC, which is already 08:00 of the next day in Korea — using UTC
    # here would date the report one day behind the Korean trading day and the
    # KST "today" file would never be produced. (archive_report.yml does the same.)
    kst = datetime.timezone(datetime.timedelta(hours=9))
    today = datetime.datetime.now(kst).strftime('%Y-%m-%d')

    print(f"=== Generating daily market report ({today}) ===")

    # 직전 리포트에서 deep-research(analysis/catalyst)를 코드별로 로드해 이월(carry-over).
    prev_by_code = {}
    if CARRY_ANALYSIS and os.path.exists(DAILY_PATH):
        try:
            with open(DAILY_PATH, encoding='utf-8') as f:
                for e in json.load(f):
                    if isinstance(e, dict) and e.get('code') and e.get('analysis'):
                        prev_by_code[e['code']] = e
            print(f"  Carry-over    : 직전 analysis {len(prev_by_code)}종목 로드")
        except Exception as ex:
            print(f"  [carry] 직전 리포트 로드 실패: {ex}", file=sys.stderr)

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

        entry = {
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
                f"<b>[하락 관찰 종목]</b><br>"
                f"하방 재료가 관찰된 참고용 종목입니다 — 자동매매(매수) 대상이 아닙니다. "
                f"전일 종가 {data['basePrice']:,}원. 반등 전환 시 관찰 레벨: {data['entry']:,}원."
            ) if SIDE == 'down' else (
                f"<b>[자동 생성 시나리오]</b><br>"
                f"전일 종가 {data['basePrice']:,}원 기준으로 산출된 기술적 레벨입니다. "
                f"진입 상한가 {data['entry']:,}원 이하에서 분할 매수하고, "
                f"목표 익절가 {data['target']:,}원, 손절선 {data['stop']:,}원으로 리스크를 관리하세요."
            ),
        }
        # 워치리스트가 자체 촉매를 갖고 있으면(하락 관찰 — 스크리너의 하방 사유) 우선 사용.
        if stock.get('catalyst'):
            entry['catalyst'] = stock['catalyst']
        if stock.get('reason'):
            entry['reason'] = stock['reason']
        # 이월된 deep-research 가 있으면 붙인다(신규/갱신은 generate_analysis 가 덮어씀).
        prev = prev_by_code.get(code)
        if prev:
            entry['analysis'] = prev['analysis']
            if prev.get('catalyst'):
                entry['catalyst'] = prev['catalyst']   # 이월된 실제 촉매 유지
            # direction(하방 판정)도 이월 — 누락 시 재분석 안 된 회차마다 판정이
            # 리셋되어 bearish 종목이 상승 뷰로 되돌아가는 사고(2026-07-07 LG엔솔).
            d = prev.get('direction') or (prev.get('analysis') or {}).get('direction')
            if d in ('bullish', 'neutral', 'bearish'):
                entry['direction'] = d
        report.append(entry)

    os.makedirs(os.path.dirname(DAILY_PATH), exist_ok=True)
    with open(DAILY_PATH, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"  Updated       : {DAILY_PATH}")

    # 장중 모드(REPORT_ARCHIVE=0)는 최신본만 갱신하고, 날짜별 아카이브와
    # index.json(날짜 드롭다운)에는 끼우지 않는다. 장전 워치리스트만 아카이브한다.
    if ARCHIVE:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        dated_path = os.path.join(REPORTS_DIR, f'{today}.json')
        with open(dated_path, 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"  Saved archive : {dated_path}")

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
