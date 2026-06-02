import json
import os
import sys


def get_ticker_map():
    try:
        root_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(root_dir, 'daily_market_report.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                stocks = json.load(f)
                return {
                    stock['code']: f"{stock['code']}{'.KS' if stock['market'] == 'KOSPI' else '.KQ'}"
                    for stock in stocks
                }
    except Exception as e:
        print(f"Error loading dynamic tickers: {e}", file=sys.stderr)

    return {
        "066570": "066570.KS",
        "011070": "011070.KS",
        "035420": "035420.KS",
        "090360": "090360.KQ",
    }
