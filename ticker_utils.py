import json
import os
import sys


def _read_stocks(path):
    """Read a [{code, market}, ...] list, returning [] on any error."""
    try:
        if os.path.exists(path):
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Error loading tickers from {path}: {e}", file=sys.stderr)
    return []


def get_ticker_map():
    """
    Build a {code: yahoo_ticker} map from the 장전 워치리스트 AND the 장중 관심종목.

    Both watchlist.json and intraday_watchlist.json live at the project root so
    they are reliably bundled with the Vercel serverless function (see
    includeFiles in vercel.json). The dashboard needs live prices for both lists
    (watchlist drives auto-trading; intraday is monitoring-only), so /api/prices
    serves the union. A missing/unreadable file is skipped. Codes are de-duped.
    """
    root_dir = os.path.dirname(os.path.abspath(__file__))
    ticker_map = {}
    for fname in ('watchlist.json', 'intraday_watchlist.json'):
        for stock in _read_stocks(os.path.join(root_dir, fname)):
            code = stock.get('code')
            if not code or code in ticker_map:
                continue
            suffix = '.KS' if stock.get('market') == 'KOSPI' else '.KQ'
            ticker_map[code] = f"{code}{suffix}"
    return ticker_map
