"""
REST data sources for the daily Deep Research batch.

Each function returns RAW data (no interpretation). The LLM step
(generate_analysis.py) turns this raw data into the analysis schema.

All functions degrade gracefully: on any error they return an empty
result and log to stderr, so one failing API never kills the batch.

Env vars (GitHub Actions secrets):
  DART_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, TAVILY_API_KEY
"""
import io
import os
import sys
import zipfile
import datetime
import threading
import xml.etree.ElementTree as ET

import requests

# yfinance writes a cache; on read-only CI point it at a writable dir.
for _mod in ("appdirs", "platformdirs"):
    try:
        _m = __import__(_mod)
        _m.user_cache_dir = lambda *a, **k: "/tmp"
    except Exception:
        pass

import pandas as pd
import yfinance as yf

UA = {"User-Agent": "Mozilla/5.0 (quant-antigravity batch)"}


def _warn(msg):
    print(f"[sources] {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------
# 1) Peer quotes (yfinance) — deterministic, no key needed
# ----------------------------------------------------------------------------
def _ticker_frame(df, ticker):
    """Return a single-level OHLCV frame for `ticker`, handling yfinance MultiIndex."""
    if df is None:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        if ticker in df.columns.get_level_values(0):
            return df[ticker]                       # ticker-major (group_by='ticker')
        flat = df.copy()
        flat.columns = flat.columns.get_level_values(0)
        return flat                                  # field-major: flatten to fields
    return df


def get_peer_quotes(peers):
    """peers: list of {name, ticker, note}. Returns same list with price/changePct filled."""
    out = []
    tickers = [p["ticker"] for p in peers]
    try:
        df = yf.download(tickers, period="2d", group_by="ticker",
                         progress=False, threads=True, auto_adjust=True)
    except Exception as e:
        _warn(f"yfinance download failed: {e}")
        df = None

    for p in peers:
        item = {"name": p["name"], "ticker": p["ticker"],
                "price": "N/A", "changePct": 0, "note": p.get("note", "")}
        try:
            tdf = _ticker_frame(df, p["ticker"])
            closes = tdf["Close"].dropna() if (tdf is not None and "Close" in tdf.columns) else None
            if closes is not None and len(closes) >= 1:
                close = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else close
                item["price"] = round(close, 2)
                item["changePct"] = round(((close - prev) / prev) * 100, 2) if prev else 0
        except Exception as e:
            _warn(f"peer quote failed for {p['ticker']}: {e}")
        out.append(item)
    return out


# ----------------------------------------------------------------------------
# 2) Naver Search API (news, cafearticle)
# ----------------------------------------------------------------------------
def naver_search(kind, query, display=8):
    """kind: 'news' | 'cafearticle'. Returns list of {title, link, description, date}."""
    cid = os.environ.get("NAVER_CLIENT_ID")
    secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        _warn("NAVER_CLIENT_ID/SECRET missing")
        return []
    try:
        r = requests.get(
            f"https://openapi.naver.com/v1/search/{kind}.json",
            headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
            params={"query": query, "display": display, "sort": "date"},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        return [{
            "title": _strip_tags(it.get("title", "")),
            "link": it.get("originallink") or it.get("link", ""),
            "description": _strip_tags(it.get("description", "")),
            "date": it.get("pubDate", ""),
        } for it in items]
    except Exception as e:
        _warn(f"naver_search({kind}, {query}) failed: {e}")
        return []


def _strip_tags(s):
    return (s.replace("<b>", "").replace("</b>", "")
            .replace("&quot;", '"').replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))


# ----------------------------------------------------------------------------
# 3) Tavily Search API (overseas news, reddit)
# ----------------------------------------------------------------------------
def tavily_search(query, max_results=5, include_domains=None):
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        _warn("TAVILY_API_KEY missing")
        return []
    try:
        payload = {"api_key": key, "query": query, "max_results": max_results,
                   "search_depth": "basic"}
        if include_domains:
            payload["include_domains"] = include_domains
        r = requests.post("https://api.tavily.com/search", json=payload, timeout=25)
        r.raise_for_status()
        return [{
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "content": it.get("content", ""),
        } for it in r.json().get("results", [])]
    except Exception as e:
        _warn(f"tavily_search({query}) failed: {e}")
        return []


# ----------------------------------------------------------------------------
# 3b) Reddit public JSON search (keyless). Tavily is the PRIMARY source in the
#     caller — Reddit 403s from datacenter/CI IPs, so this is a residential-only
#     best-effort fallback.
# ----------------------------------------------------------------------------
REDDIT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_REDDIT_BLOCKED_WARNED = False


def reddit_search(query, max_results=5, period="year"):
    """Search Reddit posts via the public search.json endpoint. No key required.

    Returns list of {title, url, subreddit, score, num_comments, content}.
    Datacenter/CI IPs are blocked (403) by Reddit; on any failure this returns []
    so the caller falls back to Tavily. Block warnings are logged once per process.
    """
    try:
        r = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "sort": "relevance", "t": period,
                    "limit": max_results, "type": "link"},
            headers={"User-Agent": REDDIT_UA},
            timeout=20,
        )
        r.raise_for_status()
        children = r.json().get("data", {}).get("children", [])
        out = []
        for c in children:
            d = c.get("data", {})
            out.append({
                "title": d.get("title", ""),
                "url": "https://www.reddit.com" + d.get("permalink", ""),
                "subreddit": d.get("subreddit_name_prefixed") or ("r/" + d.get("subreddit", "")),
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "content": (d.get("selftext", "") or "")[:500],
            })
        return out
    except Exception as e:
        global _REDDIT_BLOCKED_WARNED
        if not _REDDIT_BLOCKED_WARNED:
            _warn(f"reddit_search blocked/failed ({e}); relying on Tavily for Reddit "
                  f"(further reddit warnings suppressed)")
            _REDDIT_BLOCKED_WARNED = True
        return []


# ----------------------------------------------------------------------------
# 4) DART OpenAPI (corp_code, financials, disclosures, major holders)
# ----------------------------------------------------------------------------
_CORP_MAP = None  # stock_code(6) -> corp_code(8), cached per process
_CORP_LOCK = threading.Lock()  # guard the one-time (large) corpCode.xml download


def _dart_key():
    return os.environ.get("DART_API_KEY")


def _load_corp_map():
    global _CORP_MAP
    if _CORP_MAP is not None:
        return _CORP_MAP
    with _CORP_LOCK:
        if _CORP_MAP is not None:          # another thread populated it while we waited
            return _CORP_MAP
        m = {}
        key = _dart_key()
        if not key:
            _warn("DART_API_KEY missing")
            _CORP_MAP = m
            return _CORP_MAP
        try:
            r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                             params={"crtfc_key": key}, timeout=30)
            r.raise_for_status()
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            xml = zf.read(zf.namelist()[0])
            root = ET.fromstring(xml)
            for el in root.iter("list"):
                stock = (el.findtext("stock_code") or "").strip()
                corp = (el.findtext("corp_code") or "").strip()
                if stock and corp:
                    m[stock] = corp
        except Exception as e:
            _warn(f"DART corpCode load failed: {e}")
        _CORP_MAP = m
        return _CORP_MAP


def dart_corp_code(stock_code):
    return _load_corp_map().get(stock_code)


def dart_financials(corp_code, year=None):
    """Annual single-company key accounts for the most recent available year."""
    key = _dart_key()
    if not (key and corp_code):
        return {}
    year = year or (datetime.date.today().year - 1)
    for y in (year, year - 1):  # fall back one year if not yet filed
        try:
            r = requests.get(
                "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                params={"crtfc_key": key, "corp_code": corp_code,
                        "bsns_year": str(y), "reprt_code": "11011", "fs_div": "CFS"},
                timeout=20,
            )
            data = r.json()
            if data.get("status") == "000" and data.get("list"):
                wanted = {"매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"}
                rows = {}
                for it in data["list"]:
                    nm = it.get("account_nm")
                    if nm in wanted and nm not in rows:
                        rows[nm] = {
                            "thstrm": it.get("thstrm_amount"),
                            "frmtrm": it.get("frmtrm_amount"),
                        }
                if rows:
                    return {"year": y, "accounts": rows}
        except Exception as e:
            _warn(f"dart_financials({corp_code}, {y}) failed: {e}")
    return {}


def dart_disclosures(corp_code, days=120, page_count=15):
    key = _dart_key()
    if not (key and corp_code):
        return []
    end = datetime.date.today()
    bgn = end - datetime.timedelta(days=days)
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={"crtfc_key": key, "corp_code": corp_code,
                    "bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
                    "page_count": page_count},
            timeout=20,
        )
        data = r.json()
        if data.get("status") != "000":
            return []
        out = []
        for it in data.get("list", []):       # DART returns newest first
            rno = it.get("rcept_no", "")
            out.append({
                "date": _fmt_date(it.get("rcept_dt", "")),
                "title": it.get("report_nm", ""),
                "filer": it.get("flr_nm", ""),
                "rcept_no": rno,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}" if rno else "",
            })
        return out
    except Exception as e:
        _warn(f"dart_disclosures({corp_code}) failed: {e}")
        return []


def dart_market_disclosures(bgn_de, end_de, corp_cls, max_pages=20):
    """corp_code 없이 시장 전체 공시를 날짜·시장구분으로 스캔한다 (per-stock fan-out 회피).

    corp_cls: 'Y'(유가증권/KOSPI) | 'K'(코스닥) | 'N'(코넥스) | 'E'(기타).
    Returns list of {date, stock_code, corp_name, title, rcept_no, url}, 최신순.
    """
    key = _dart_key()
    if not key:
        _warn("DART_API_KEY missing")
        return []
    out = []
    for page in range(1, max_pages + 1):
        try:
            r = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": key, "bgn_de": bgn_de, "end_de": end_de,
                        "corp_cls": corp_cls, "page_count": 100, "page_no": page},
                timeout=20,
            )
            data = r.json()
        except Exception as e:
            _warn(f"dart_market_disclosures({corp_cls}, p{page}) failed: {e}")
            break
        if data.get("status") != "000":
            break
        for it in data.get("list", []):
            rno = it.get("rcept_no", "")
            out.append({
                "date": _fmt_date(it.get("rcept_dt", "")),
                "stock_code": (it.get("stock_code") or "").strip(),
                "corp_name": it.get("corp_name", ""),
                "title": it.get("report_nm", ""),
                "rcept_no": rno,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}" if rno else "",
            })
        if page >= int(data.get("total_page", 1) or 1):
            break
    return out


def dart_major_holders(corp_code):
    """대량보유 상황(5% rule) — recent ownership changes."""
    key = _dart_key()
    if not (key and corp_code):
        return []
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/majorstock.json",
            params={"crtfc_key": key, "corp_code": corp_code},
            timeout=20,
        )
        data = r.json()
        if data.get("status") != "000":
            return []
        out = []
        for it in data.get("list", [])[:8]:
            out.append({
                "date": _fmt_date(it.get("rcept_dt", "")),
                "holder": it.get("repror", ""),
                "ratio": it.get("stkrt", ""),
                "reason": it.get("report_resn", ""),
            })
        return out
    except Exception as e:
        _warn(f"dart_major_holders({corp_code}) failed: {e}")
        return []


def _fmt_date(yyyymmdd):
    s = (yyyymmdd or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s
