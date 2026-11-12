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


_KRX_TICKERS = None  # code(6) -> exact yahoo ticker, cached per process


def _krx_ticker_lookup():
    """code -> exact Yahoo ticker from public/assets/krx_companies.json (SSOT).

    The watchlist's own `market` field can be wrong (index_constituents.json
    misclassifies some names), which flips the .KS/.KQ suffix and makes yfinance
    return nothing for that stock. The KRX master file carries the authoritative
    ticker (e.g. "326030.KS"), so we prefer it and fall back to the watchlist's
    market only for codes missing from the master. Cached for the process.
    """
    global _KRX_TICKERS
    if _KRX_TICKERS is not None:
        return _KRX_TICKERS
    _KRX_TICKERS = {}
    try:
        root_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(root_dir, 'public', 'assets', 'krx_companies.json')
        with open(path, 'r', encoding='utf-8') as f:
            for c in json.load(f):
                code = str(c.get('code', '')).zfill(6)
                tk = c.get('ticker')
                if code and tk:
                    _KRX_TICKERS[code] = tk
    except Exception as e:
        print(f"Error loading KRX master tickers: {e}", file=sys.stderr)
    return _KRX_TICKERS


def codes_to_tickers(codes_param, krx=None):
    """/api/prices?codes= 파라미터 → {code6: yahoo_ticker}.

    접미사(.KS/.KQ)가 있으면 그대로, 없으면 KRX 마스터(krx_companies.json)의 정확한 티커,
    마스터에도 없으면 .KS. 2026-11-12 이전에는 접미사 없는 코드에 무조건 .KS 를 붙여
    KOSDAQ 종목이 누락되거나 엉뚱한 값이 왔다(036930·089030 누락, 240810 → 79,700 0.00%)."""
    krx = _krx_ticker_lookup() if krx is None else krx
    out = {}
    for tk in str(codes_param or "").split(","):
        tk = tk.strip()
        if not tk:
            continue
        code = tk.split(".")[0].zfill(6)
        out[code] = tk if "." in tk else (krx.get(code) or code + ".KS")
    return out


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
    krx = _krx_ticker_lookup()
    ticker_map = {}
    # *_down.json: 하락 관찰(참고용) — 대시보드 하락 뷰의 실시간 시세용
    for fname in ('watchlist.json', 'intraday_watchlist.json',
                  'watchlist_down.json', 'intraday_watchlist_down.json'):
        for stock in _read_stocks(os.path.join(root_dir, fname)):
            code = stock.get('code')
            if not code or code in ticker_map:
                continue
            # KRX 마스터의 정확한 ticker 우선; 없는 코드만 watchlist market 기반 fallback
            fallback = f"{code}{'.KS' if stock.get('market') == 'KOSPI' else '.KQ'}"
            ticker_map[code] = krx.get(str(code).zfill(6)) or fallback
    return ticker_map
