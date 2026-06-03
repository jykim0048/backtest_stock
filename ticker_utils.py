import json
import os
import sys


def get_ticker_map():
    """
    Build a {code: yahoo_ticker} map from watchlist.json (project root).

    watchlist.json lives at the project root so it is reliably bundled with
    the Vercel serverless function (see includeFiles in vercel.json). If it
    cannot be read, fall back to a small hardcoded default set.
    """
    try:
        root_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(root_dir, 'watchlist.json')
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                stocks = json.load(f)
                return {
                    stock['code']: f"{stock['code']}{'.KS' if stock['market'] == 'KOSPI' else '.KQ'}"
                    for stock in stocks
                }
    except Exception as e:
        print(f"Error loading watchlist tickers: {e}", file=sys.stderr)

    return {}
