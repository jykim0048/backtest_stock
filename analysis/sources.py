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
import json
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
# 2b) Naver Finance 종목토론방 (HTML scrape — not in the official Search API)
#     Encoding is auto-detected (now UTF-8; was EUC-KR historically). Datacenter/CI
#     IPs may be blocked, so this degrades to [] (cafearticle search is the fallback).
# ----------------------------------------------------------------------------
def naver_board(code, pages=1, limit=15):
    """종목토론방(finance.naver.com/item/board) 글을 파싱한다.

    Returns list of {title, url, date, views, agree, disagree}, 공감수 내림차순
    (노이즈/낚시 글을 가라앉히기 위함). 실패 시 [] 반환(배치 중단 방지).
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:
        _warn("beautifulsoup4 not installed — naver_board skipped")
        return []

    out = []
    for page in range(1, pages + 1):
        try:
            r = requests.get(
                "https://finance.naver.com/item/board.naver",
                params={"code": code, "page": page},
                headers={**UA, "Referer":
                         f"https://finance.naver.com/item/main.naver?code={code}"},
                timeout=15,
            )
            r.encoding = r.apparent_encoding or "utf-8"   # 자동 감지 (UTF-8/EUC-KR 모두 대응)
            r.raise_for_status()
        except Exception as e:
            _warn(f"naver_board({code}, p{page}) failed: {e}")
            break

        table = BeautifulSoup(r.text, "html.parser").select_one("table.type2")
        if table is None:
            break
        for tr in table.select("tr"):
            a = tr.select_one("td.title a")
            if not a:
                continue
            tds = tr.find_all("td")           # 날짜 | 제목 | 글쓴이 | 조회 | 공감 | 비공감

            def _num(i):
                try:
                    return int(tds[i].get_text(strip=True).replace(",", ""))
                except Exception:
                    return 0

            href = a.get("href", "")
            out.append({
                "title": (a.get("title") or a.get_text(strip=True)).strip(),
                "url": "https://finance.naver.com" + href if href.startswith("/") else href,
                "date": tds[0].get_text(strip=True) if tds else "",
                "views": _num(3),
                "agree": _num(4),
                "disagree": _num(5),
            })

    out.sort(key=lambda x: x.get("agree", 0), reverse=True)
    return out[:limit]


# ----------------------------------------------------------------------------
# 2c) Naver Finance 증권사 종목분석 리포트 (HTML scrape — 공식 API 없음)
#     목록: research/company_list.naver?searchType=itemCode&itemCode=<code>
#     상세: research/company_read.naver?nid=<nid> (목표주가·투자의견·요약 본문)
# ----------------------------------------------------------------------------
def naver_research(code, days=30, limit=6, detail_top=3):
    """증권사 종목분석 리포트 목록(+상위 detail_top 건 상세)을 파싱한다.

    Returns list of {title, broker, date, url, pdfUrl, targetPrice, opinion,
    summary} (최신순, days 일 이내). targetPrice/opinion/summary 는 상세를
    조회한 상위 건에만 채워진다. 실패 시 [] (배치 중단 방지).
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:
        _warn("beautifulsoup4 not installed — naver_research skipped")
        return []

    try:
        r = requests.get(
            "https://finance.naver.com/research/company_list.naver",
            params={"searchType": "itemCode", "itemCode": code},
            headers={**UA, "Referer": "https://finance.naver.com/research/"},
            timeout=15,
        )
        r.encoding = "euc-kr"
        r.raise_for_status()
    except Exception as e:
        _warn(f"naver_research({code}) list failed: {e}")
        return []

    kst = datetime.timezone(datetime.timedelta(hours=9))
    cutoff = datetime.datetime.now(kst).date() - datetime.timedelta(days=days)
    out = []
    for a in BeautifulSoup(r.text, "html.parser").select('a[href*="company_read.naver"]'):
        tr = a.find_parent("tr")
        if tr is None:
            continue
        tds = tr.find_all("td")      # 종목명 | 제목 | 증권사 | 첨부 | 날짜 | 조회수
        if len(tds) < 5:
            continue
        raw_date = tds[4].get_text(strip=True)     # "26.07.03"
        try:
            d = datetime.datetime.strptime(raw_date, "%y.%m.%d").date()
        except ValueError:
            continue
        if d < cutoff:
            continue                 # 목록은 최신순 — 계속 훑어도 무방하나 어차피 걸러짐
        pdf_a = tds[3].select_one("a[href]") if len(tds) > 3 else None
        href = a.get("href", "")
        out.append({
            "title": a.get_text(strip=True),
            "broker": tds[2].get_text(strip=True),
            "date": d.strftime("%Y-%m-%d"),
            "url": "https://finance.naver.com/research/" + href if not href.startswith("http") else href,
            "pdfUrl": pdf_a.get("href") if pdf_a else "",
            "targetPrice": None, "opinion": "", "summary": "",
        })
        if len(out) >= limit:
            break

    # 상위 detail_top 건만 상세 조회(요청 수 절약): 목표주가·투자의견·요약 본문
    for rpt in out[:detail_top]:
        try:
            rd = requests.get(rpt["url"], headers={**UA, "Referer":
                              "https://finance.naver.com/research/company_list.naver"},
                              timeout=15)
            rd.encoding = "euc-kr"
            rd.raise_for_status()
            soup = BeautifulSoup(rd.text, "html.parser")
            money = soup.select_one("em.money")
            if money:
                try:
                    rpt["targetPrice"] = int(money.get_text(strip=True).replace(",", ""))
                except ValueError:
                    pass
            coment = soup.select_one("em.coment")
            if coment:
                rpt["opinion"] = coment.get_text(strip=True)
            body = soup.select_one("td.view_cnt")
            if body:
                text = " ".join(body.get_text(" ", strip=True).split())
                rpt["summary"] = text[:500]
        except Exception as e:
            _warn(f"naver_research detail({rpt.get('url','')}) failed: {e}")
    return out


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


def brave_search(query, max_results=5, include_domains=None):
    """Brave Web Search (Tavily 한도초과 432 시 폴백). 반환 형태는 tavily_search 와 동일.

    무료 티어: 2,000 쿼리/월, 1 req/s. include_domains 는 쿼리에 site: 로 반영한다.
    """
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        _warn("BRAVE_API_KEY missing")
        return []
    q = f"{query} site:{include_domains[0]}" if include_domains else query
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": q, "count": min(max_results, 20)},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            timeout=25,
        )
        r.raise_for_status()
        results = ((r.json().get("web") or {}).get("results")) or []
        return [{
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "content": it.get("description", ""),
        } for it in results[:max_results]]
    except Exception as e:
        _warn(f"brave_search({query}) failed: {e}")
        return []


def web_search(query, max_results=5, include_domains=None):
    """웹 검색 통합 진입점: Tavily 우선, 빈 결과/한도초과(432)·오류 시 Brave 폴백."""
    res = tavily_search(query, max_results, include_domains)
    if res:
        return res
    return brave_search(query, max_results, include_domains)


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

# Bundled static stock_code -> corp_code map (built by tools/build_dart_corp_map.py).
# Serverless (Vercel /api/research) loads this to skip the ~tens-of-MB corpCode.xml
# download on every cold start. Absent → fall back to the live DART download below.
_STATIC_CORP_MAP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "public", "assets", "dart_corp_map.json")


def _dart_key():
    return os.environ.get("DART_API_KEY")


def _load_static_corp_map():
    """Load the bundled stock_code->corp_code map, or None if missing/empty."""
    try:
        with open(_STATIC_CORP_MAP_PATH, encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and m:
            return {str(k).zfill(6): str(v) for k, v in m.items()}
    except FileNotFoundError:
        return None
    except Exception as e:
        _warn(f"static corp map load failed: {e}")
    return None


def _load_corp_map():
    global _CORP_MAP
    if _CORP_MAP is not None:
        return _CORP_MAP
    with _CORP_LOCK:
        if _CORP_MAP is not None:          # another thread populated it while we waited
            return _CORP_MAP
        # Fast path: bundled static map (no network, no DART key needed) — serverless.
        static = _load_static_corp_map()
        if static is not None:
            _warn(f"corp map: static file ({len(static)} listed codes)")
            _CORP_MAP = static
            return _CORP_MAP
        # Fallback: download the full corpCode.xml from DART (needs DART_API_KEY).
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
