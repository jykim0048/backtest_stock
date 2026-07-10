#!/usr/bin/env python3
"""Persistent dashboard + on-demand Deep Research server (Railway / always-on).

Unlike Vercel serverless (60s cap, stateless), a long-running process can do the
research SYNCHRONOUSLY and keep an in-memory cache + the DART corp map warm.

(2026-07 B' 마이그레이션) Vercel Hobby CPU 한도 초과를 계기로, 이 서버가
대시보드 전체를 병렬 서빙한다(Vercel 은 당분간 유지 — 비교 후 이전 결정):
  GET /                       -> public/index.html (대시보드)
  GET /<public 정적 경로>      -> 코드/에셋은 로컬, 데이터 JSON 은 GitHub raw
                                 프록시(60s TTL)로 항상 최신 ([skip railway]
                                 커밋으로 컨테이너 스냅샷이 얼어붙는 문제 대응)
  GET /api/prices[?codes=..]  -> 실시간 시세 (Vercel api/index.py 이식,
                                 4s TTL 캐시로 다중 탭 대응)
  GET /api/research?q=<name|code>
      -> {status:"done", code, name, market, result, generatedAt, cached}
      -> {status:"error", message, ...}
  GET /healthz   -> {status:"ok", rawProxy:{...}}

Reuses generate_analysis.analyze_stock — the same per-stock pipeline the CI batch
uses (peers + news + DART + community + LLM). No external store needed.

Env (Railway variables): GEMINI_API_KEY (or LLM_CHAIN + matching keys),
     DART_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, TAVILY_API_KEY.
     PORT is injected by Railway.
"""
import os
import sys
import json
import math
import time
import datetime
import threading
import mimetypes
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

import generate_analysis as ga          # heavy import once at boot — keeps the worker warm

KRX_MASTER = os.path.join(ROOT, "public", "assets", "krx_companies.json")
PEERS_PATH = os.path.join(ROOT, "analysis", "peers.json")

CACHE_TTL = int(os.environ.get("RESEARCH_CACHE_TTL", "21600"))   # cache a result 6h
RL_LIMIT  = int(os.environ.get("RESEARCH_RATELIMIT", "8"))       # requests / IP / minute
KST = datetime.timezone(datetime.timedelta(hours=9))

_CACHE = {}                 # code -> {"result", "generatedAt", "ts"}
_CACHE_LOCK = threading.Lock()
_INFLIGHT = {}              # code -> Lock (collapse duplicate concurrent requests)
_INFLIGHT_LOCK = threading.Lock()
_RL = {}                    # ip -> [recent request timestamps]
_RL_LOCK = threading.Lock()
_SECTOR_CACHE = {}          # {"result", "ts"} — one shared 6h cache for /api/sector
_SECTOR_LOCK = threading.Lock()

BRIEFING_PATH = os.path.join(ROOT, "public", "briefing", "latest.json")


# --- KRX master: resolve a free-text query (name or 6-digit code) to a stock ---
def _load_master():
    by_code, by_name = {}, {}
    try:
        with open(KRX_MASTER, encoding="utf-8") as f:
            for c in json.load(f):
                code = str(c.get("code", "")).zfill(6)
                name = (c.get("name") or "").strip()
                if not code or not name:
                    continue
                raw = c.get("market", "") or ""
                market = "KOSPI" if "유가" in raw else ("KOSDAQ" if "코스닥" in raw else "KOSPI")
                entry = {"code": code, "name": name, "market": market}
                by_code[code] = entry
                by_name.setdefault(name, entry)
    except Exception as ex:
        print(f"[research] master load failed: {ex}", file=sys.stderr)
    return by_code, by_name


_BY_CODE, _BY_NAME = _load_master()


def resolve(q):
    q = (q or "").strip()
    if not q:
        return None
    if q.isdigit():
        return _BY_CODE.get(q.zfill(6))
    if q in _BY_NAME:
        return _BY_NAME[q]
    ql = q.lower()                                    # fuzzy: shortest name containing q
    cands = [e for n, e in _BY_NAME.items() if ql in n.lower()]
    if cands:
        cands.sort(key=lambda e: len(e["name"]))
        return cands[0]
    return None


def _peer_cfg():
    try:
        with open(PEERS_PATH, encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not str(k).startswith("_")}
    except Exception:
        return {}


def rate_ok(ip):
    now = time.time()
    with _RL_LOCK:
        recent = [t for t in _RL.get(ip, []) if now - t < 60]
        recent.append(now)
        _RL[ip] = recent
        return len(recent) <= RL_LIMIT


def _cached(code):
    with _CACHE_LOCK:
        e = _CACHE.get(code)
        if e and time.time() - e["ts"] < CACHE_TTL:
            return e
    return None


def run_research(stock):
    """Return (entry, was_cached). Collapses duplicate concurrent calls per code."""
    code = stock["code"]
    hit = _cached(code)
    if hit:
        return hit, True
    with _INFLIGHT_LOCK:
        lock = _INFLIGHT.setdefault(code, threading.Lock())
    with lock:                                        # one compute per code at a time
        hit = _cached(code)
        if hit:
            return hit, True
        analysis = ga.analyze_stock(stock, _peer_cfg())
        entry = {"result": analysis,
                 "generatedAt": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
                 "ts": time.time()}
        with _CACHE_LOCK:
            _CACHE[code] = entry
        return entry, False


def run_sector():
    """Return (result, was_cached) for /api/sector. Single shared 6h cache;
    reuses railway_server.resolve so KR theme names map to KRX codes."""
    with _SECTOR_LOCK:
        e = _SECTOR_CACHE.get("v")
        if e and time.time() - e["ts"] < CACHE_TTL:
            return e["result"], True
    briefing = {}
    try:
        with open(BRIEFING_PATH, encoding="utf-8") as f:
            briefing = json.load(f)
    except Exception as ex:
        print(f"[sector] briefing load failed: {ex}", file=sys.stderr)
    from analysis import sector as sector_mod
    result = sector_mod.analyze_sectors(briefing, resolve_fn=resolve)
    with _SECTOR_LOCK:
        _SECTOR_CACHE["v"] = {"result": result, "ts": time.time()}
    return result, False


# ---------------------------------------------------------------------------
# 대시보드 정적 서빙 + 데이터 신선도 프록시 (Vercel 병렬 운영 — B' 마이그레이션)
#
# 이 서버가 Vercel 처럼 대시보드 전체(public/ + /api/prices)를 서빙한다.
# 함정: 리포트 커밋은 [skip railway] 라 컨테이너의 public/*.json 은 마지막
# '코드' 배포 시점 스냅샷으로 얼어붙는다. 그래서 파일을 두 부류로 나눈다.
#   - 코드/에셋(index.html, assets/*): 로컬 디스크 (코드 커밋은 재배포됨 → 최신)
#   - 데이터 JSON(리포트·브리핑 등): GitHub raw 프록시 + TTL 캐시 (레포가
#     private 라 토큰 필요 — GH_RAW_TOKEN 또는 GH_DISPATCH_TOKEN 에
#     Contents:read 권한. 실패 시 로컬 파일 폴백 = 다소 스테일하지만 동작)
# ---------------------------------------------------------------------------
PUBLIC_DIR = os.path.join(ROOT, "public")
_RAW_REPO  = os.environ.get("GH_REPO", "jykim0048/backtest_stock")
_RAW_REF   = os.environ.get("GH_REF", "main")
_RAW_TOKEN = os.environ.get("GH_RAW_TOKEN") or os.environ.get("GH_DISPATCH_TOKEN", "")
DATA_TTL   = int(os.environ.get("STATIC_DATA_TTL", "60"))   # 데이터 파일 raw 캐시(초)

_RAW_CACHE = {}          # relpath -> {"body": bytes|None, "ts": float}
_RAW_LOCK  = threading.Lock()
_RAW_STATE = {"ok": None, "note": "unprobed"}   # /healthz 진단용


def _raw_fetch(relpath):
    """GitHub raw 에서 레포 최신 파일을 TTL 캐시로 가져온다. 실패/부재 시 None
    (호출측이 로컬 파일로 폴백). 404 도 짧게 캐시해 반복 원격 조회를 막는다."""
    now = time.time()
    with _RAW_LOCK:
        e = _RAW_CACHE.get(relpath)
        if e and now - e["ts"] < DATA_TTL:
            return e["body"]
    url = f"https://raw.githubusercontent.com/{_RAW_REPO}/{_RAW_REF}/{relpath}"
    req = urllib.request.Request(url)
    if _RAW_TOKEN:
        req.add_header("Authorization", f"token {_RAW_TOKEN}")
    req.add_header("User-Agent", "railway-dashboard")
    body = None
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            body = r.read()
        _RAW_STATE.update(ok=True, note=relpath)
    except urllib.error.HTTPError as e:
        # private 레포에 권한 없는 토큰도 404 로 온다 — 부팅 프로브가 구분해 로그.
        if e.code != 404:
            _RAW_STATE.update(ok=False, note=f"{relpath}: HTTP {e.code}")
    except Exception as ex:
        _RAW_STATE.update(ok=False, note=f"{relpath}: {str(ex)[:120]}")
    with _RAW_LOCK:
        _RAW_CACHE[relpath] = {"body": body, "ts": now}
    return body


def _is_data_path(relpath):
    """항상 최신이어야 하는 데이터 파일 여부. assets/ 는 준정적(월/반기 갱신)이라
    로컬본으로 충분 — 리포트·브리핑 등 .json 만 raw 프록시 대상."""
    if relpath.startswith("public/assets/"):
        return False
    return relpath.endswith(".json")


def _raw_probe():
    """부팅 시 1회: raw 프록시가 실제로 동작하는지 확인해 로그로 남긴다.
    (레포가 private 라 토큰의 Contents:read 유무를 여기서 판정할 수 있다)"""
    body = _raw_fetch("watchlist.json")
    if body is not None:
        _RAW_STATE.update(ok=True, note="probe ok (watchlist.json)")
        print("[static] GitHub raw proxy OK — 데이터 JSON 은 항상 최신으로 서빙", flush=True)
    else:
        _RAW_STATE.update(ok=False, note="probe failed — 로컬 스냅샷 폴백 (토큰 Contents:read 필요)")
        print("[static] GitHub raw proxy FAILED — public/*.json 은 배포 시점 스냅샷으로"
              " 서빙됨. Railway 변수 GH_RAW_TOKEN(Contents:read) 추가 필요", file=sys.stderr, flush=True)


# --- /api/prices: 실시간 시세 (Vercel api/index.py 이식 + 인메모리 TTL 캐시) ---
# 상시 서버라 응답을 짧게 캐시할 수 있어(서버리스와 달리), 탭이 여러 개여도
# Yahoo 호출이 배수로 늘지 않는다. 워치리스트 코드 목록도 raw 우선으로 읽어
# [skip railway] 커밋으로 인한 스테일 워치리스트 시세를 방지한다.
PRICES_TTL = float(os.environ.get("PRICES_TTL", "4"))     # 대시보드 5초 폴링과 정합
_PRICES_CACHE = {}       # cache_key -> {"body": dict, "ts": float}
_PRICES_LOCK  = threading.Lock()

_WATCHLIST_FILES = ("watchlist.json", "intraday_watchlist.json",
                    "watchlist_down.json", "intraday_watchlist_down.json")


def _ticker_map_fresh():
    """{code: yahoo_ticker} — ticker_utils.get_ticker_map 과 동일 규칙이되,
    워치리스트 파일을 GitHub raw(최신) 우선, 로컬(스냅샷) 폴백으로 읽는다."""
    from ticker_utils import _krx_ticker_lookup
    krx = _krx_ticker_lookup()
    tmap = {}
    for fname in _WATCHLIST_FILES:
        body = _raw_fetch(fname)
        if body is None:
            try:
                with open(os.path.join(ROOT, fname), "rb") as f:
                    body = f.read()
            except OSError:
                continue
        try:
            stocks = json.loads(body.decode("utf-8"))
        except Exception:
            continue
        for s in (stocks if isinstance(stocks, list) else []):
            code = s.get("code") if isinstance(s, dict) else None
            if not code or code in tmap:
                continue
            fallback = f"{code}{'.KS' if s.get('market') == 'KOSPI' else '.KQ'}"
            tmap[code] = krx.get(str(code).zfill(6)) or fallback
    return tmap


def _naver_indices():
    """코스피/코스닥 지수를 네이버에서 조회 — {kospi:{price,rate}, kosdaq:{...}}.

    yfinance(^KS11/^KQ11)는 Railway 데이터센터 IP 에서 빈 응답을 주는 사례가
    있어(KRX 계열 차단과 유사), 네이버 모바일 API 를 지수 1차 소스로 쓴다.
    closePrice 는 장전(PREOPEN)엔 전일 종가, 장중·마감 후엔 당일 종가를 주므로
    사용자 요구(장전=전일종가 / 마감후=당일종가)에 그대로 부합한다.
    실패 시 {} (호출부에서 yfinance 폴백)."""
    def _num(s):
        try:
            return float(str(s or "").replace(",", "").replace("+", ""))
        except (ValueError, AttributeError):
            return None
    out = {}
    for key, sym in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        try:
            req = urllib.request.Request(
                f"https://m.stock.naver.com/api/index/{sym}/basic",
                headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                d = json.loads(r.read().decode("utf-8"))
            price = _num(d.get("closePrice"))
            rate = _num(d.get("fluctuationsRatio"))
            if price is not None:
                out[key] = {"price": price, "rate": rate if rate is not None else 0.0}
        except Exception as ex:
            print(f"[prices] naver index {sym}: {ex}", file=sys.stderr, flush=True)
    return out


def _build_prices(codes_param):
    """api/index.py 의 시세 응답 생성 로직 이식(종목+지수 단일 yf.download)."""
    import yfinance as yf                     # ga 경유로 이미 로드돼 있어 비용 없음

    is_codes = bool(codes_param)
    if is_codes:
        ticker_map = {}
        for tk in codes_param.split(","):
            tk = tk.strip()
            if not tk:
                continue
            code = tk.split(".")[0].zfill(6)
            ticker_map[code] = tk if "." in tk else tk + ".KS"
    else:
        ticker_map = _ticker_map_fresh()
    tickers_list = list(ticker_map.values())

    # 지수는 네이버 1차(yfinance 는 Railway IP 에서 지수 결측). ?codes= 요청엔 생략.
    indices = {} if is_codes else _naver_indices()
    idx_map = ({} if is_codes or indices
               else {"kospi": "^KS11", "kosdaq": "^KQ11"})   # 네이버 실패 시에만 yf 폴백
    combined = tickers_list + [t for t in idx_map.values() if t not in tickers_list]
    df = (yf.download(combined, period="2d", group_by="ticker",
                      progress=False, threads=True) if combined else None)

    formatted = {}
    for code, ticker in ticker_map.items():
        try:
            try:
                d = df[ticker]
            except (KeyError, TypeError):
                d = df
            if d.empty or len(d) < 1:
                continue
            price_val, open_val = d["Close"].iloc[-1], d["Open"].iloc[-1]
            if math.isnan(price_val) or math.isnan(open_val):
                continue
            price, open_price = float(price_val), float(open_val)
            vol = d["Volume"].iloc[-1] if "Volume" in d else 0
            volume = int(vol) if not math.isnan(vol) else 0
            prev_close = price
            if len(d) >= 2:
                pv = d["Close"].iloc[-2]
                prev_close = float(pv) if not math.isnan(pv) else price
            rate = ((price - prev_close) / prev_close) * 100 if prev_close > 0 else 0.0
            formatted[code] = {"price": price, "rate": rate, "volume": volume,
                               "open": open_price, "prevClose": prev_close}
        except Exception as ex:
            print(f"[prices] parse {ticker}: {ex}", file=sys.stderr, flush=True)

    now = datetime.datetime.now(KST)
    market_state = ("REGULAR" if now.weekday() < 5
                    and 900 <= now.hour * 100 + now.minute <= 1530 else "CLOSED")

    for key, tk in idx_map.items():          # 네이버 실패 시 yfinance 폴백만 채운다
        try:
            try:
                d = df[tk]
            except (KeyError, TypeError):
                d = df
            if d.empty:
                continue
            cur = float(d["Close"].iloc[-1])
            if math.isnan(cur):
                continue
            prev = float(d["Close"].iloc[-2]) if len(d) >= 2 else cur
            if math.isnan(prev) or prev <= 0:
                prev = cur
            indices[key] = {"price": cur,
                            "rate": ((cur - prev) / prev) * 100 if prev > 0 else 0.0}
        except Exception as ex:
            print(f"[prices] index {tk}: {ex}", file=sys.stderr, flush=True)

    return {"status": "success", "marketState": market_state,
            "stocks": formatted, "indices": indices}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._send(200, {"status": "ok", "rawProxy": _RAW_STATE})
        if u.path == "/api/prices":
            return self._prices(u)
        if u.path == "/api/krxtest":
            return self._krxtest()
        if u.path == "/api/srctest":
            return self._srctest()
        if u.path == "/api/sector":
            try:
                result, was_cached = run_sector()
                return self._send(200, {"status": "done", "cached": was_cached, **result})
            except Exception as ex:
                return self._send(500, {"status": "error", "message": str(ex)[:300]})
        if u.path == "/api/research":
            return self._research(u)
        if u.path.startswith("/api/"):
            return self._send(404, {"status": "error", "message": "not found"})
        # API 외 경로는 대시보드 정적 서빙 ("/" → public/index.html)
        return self._serve_static(u.path)

    def _research(self, u):
        try:
            params = parse_qs(u.query)
            q = (params.get("q") or params.get("code") or [""])[0]
            stock = resolve(q)
            if not stock:
                return self._send(404, {"status": "error", "message": f"종목을 찾을 수 없습니다: {q}"})

            ip = (self.headers.get("x-forwarded-for", "") or self.client_address[0]
                  or "unknown").split(",")[0].strip()
            if not rate_ok(ip):
                return self._send(429, {"status": "error", **stock,
                                        "message": "요청이 많습니다. 잠시 후 다시 시도하세요."})
            if not ga.llm.configured():
                return self._send(500, {"status": "error", **stock,
                                        "message": "LLM 미설정: GEMINI_API_KEY 환경변수를 추가하세요."})

            entry, was_cached = run_research(stock)
            return self._send(200, {"status": "done", **stock, "cached": was_cached,
                                    "generatedAt": entry["generatedAt"], "result": entry["result"]})
        except Exception as ex:
            return self._send(500, {"status": "error", "message": str(ex)[:300]})

    def _prices(self, u):
        """실시간 시세 — Vercel /api/prices 와 동일 응답. TTL 캐시로 다중 탭 대응."""
        try:
            codes_param = (parse_qs(u.query).get("codes") or [""])[0].strip()
            cache_key = codes_param or "__watchlists__"
            now = time.time()
            with _PRICES_LOCK:
                e = _PRICES_CACHE.get(cache_key)
                if e and now - e["ts"] < PRICES_TTL:
                    return self._send(200, e["body"])
            payload = _build_prices(codes_param)
            with _PRICES_LOCK:
                _PRICES_CACHE[cache_key] = {"body": payload, "ts": time.time()}
            return self._send(200, payload)
        except Exception as ex:
            return self._send(500, {"status": "error", "message": str(ex)[:300]})

    def _serve_static(self, path):
        """public/ 정적 서빙. 데이터 JSON 은 GitHub raw 프록시(최신) 우선."""
        rel = unquote(path).lstrip("/")
        if not rel:
            rel = "index.html"
        base = os.path.normpath(PUBLIC_DIR)
        full = os.path.normpath(os.path.join(base, rel))
        if full != base and not full.startswith(base + os.sep):   # 디렉토리 탈출 차단
            return self._send(404, {"status": "error", "message": "not found"})
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
            rel = rel.rstrip("/") + "/index.html" if rel else "index.html"

        relpath = "public/" + rel.replace("\\", "/")
        body = _raw_fetch(relpath) if _is_data_path(relpath) else None
        if body is None:
            try:
                with open(full, "rb") as f:
                    body = f.read()
            except OSError:
                return self._send(404, {"status": "error", "message": "not found"})

        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/json":
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Access-Control-Allow-Origin", "*")
        # 데이터/HTML 은 항상 재검증(대시보드가 ?t= 캐시버스팅도 함), 에셋은 짧은 캐시
        no_store = ((relpath.endswith(".json") or relpath.endswith(".html"))
                    and not relpath.startswith("public/assets/"))
        self.send_header("Cache-Control", "no-store" if no_store else "public, max-age=300")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _krxtest(self):
        """진단 전용: Railway IP 에서 pykrx(KRX 직접 스크래핑)가 실제로 데이터를 받는지 확인.
        차단이면 빈 응답(rows=0)/에러, 정상이면 행 다수. 결과 보고 후 본 엔드포인트는 제거한다."""
        result = {"region_hint": os.environ.get("RAILWAY_REPLICA_REGION", "unknown"), "checks": {}}
        end = datetime.datetime.now(KST)
        start = end - datetime.timedelta(days=12)
        s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        try:
            from pykrx import stock as pk
            try:
                df = pk.get_market_ohlcv(s, e, "005930")
                ok = df is not None and not df.empty
                result["checks"]["ohlcv_005930"] = {
                    "ok": bool(ok), "rows": int(len(df)) if ok else 0,
                    "last_close": float(df["종가"].iloc[-1]) if ok else None}
            except Exception as ex:
                result["checks"]["ohlcv_005930"] = {"ok": False, "error": str(ex)[:300]}
            try:
                lst = pk.get_market_ticker_list(e, market="KOSPI")
                result["checks"]["kospi_tickers"] = {"ok": len(lst) > 0, "count": len(lst)}
            except Exception as ex:
                result["checks"]["kospi_tickers"] = {"ok": False, "error": str(ex)[:300]}
        except Exception as ex:
            result["checks"]["import"] = {"ok": False, "error": str(ex)[:300]}
        verdict = any(c.get("ok") for c in result["checks"].values())
        result["verdict"] = "KRX reachable from Railway ✅" if verdict else "KRX blocked/empty ❌"
        return self._send(200, result)

    def _srctest(self):
        """진단 전용: DART 공시·재무 + Reddit(Tavily/직접) 소스가 실제로 데이터를 받는지 확인.
        삼성전자(005930) + 해외 peer 예시로 각 소스를 호출해 성공/빈/에러를 반환한다."""
        src = ga.sources
        out = {"env": {k: bool(os.environ.get(k)) for k in
                       ("DART_API_KEY", "TAVILY_API_KEY", "BRAVE_API_KEY",
                        "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET")},
               "checks": {}}
        code = "005930"
        # 1) DART corp_code 매핑 (정적맵 우선)
        try:
            corp = src.dart_corp_code(code)
            out["checks"]["dart_corp_code"] = {"ok": bool(corp), "corp_code": corp}
        except Exception as ex:
            out["checks"]["dart_corp_code"] = {"ok": False, "error": str(ex)[:300]}
            corp = None
        # 2) DART 공시
        if corp:
            try:
                disc = src.dart_disclosures(corp, days=120)
                out["checks"]["dart_disclosures"] = {
                    "ok": len(disc) > 0, "count": len(disc),
                    "latest": (disc[0].get("date"), disc[0].get("title")) if disc else None}
            except Exception as ex:
                out["checks"]["dart_disclosures"] = {"ok": False, "error": str(ex)[:300]}
            try:
                fin = src.dart_financials(corp)
                out["checks"]["dart_financials"] = {
                    "ok": bool(fin), "keys": list(fin.keys())[:6] if isinstance(fin, dict) else None}
            except Exception as ex:
                out["checks"]["dart_financials"] = {"ok": False, "error": str(ex)[:300]}
        # 2b) 네이버 밸류에이션 (섹터분석 PER/PBR/동일업종PER 보완 소스)
        try:
            nv = src.naver_valuation(code)
            out["checks"]["naver_valuation"] = {
                "ok": bool(nv.get("per") or nv.get("pbr")),
                "per": nv.get("per"), "pbr": nv.get("pbr"),
                "industryPer": nv.get("industryPer"),
                "targetPrice": nv.get("targetPrice"), "estPer": nv.get("estPer")}
        except Exception as ex:
            out["checks"]["naver_valuation"] = {"ok": False, "error": str(ex)[:300]}
        # 3) Tavily 상세 — 키 유효성(일반검색) vs include_domains 필터(reddit) 구분
        tkey = os.environ.get("TAVILY_API_KEY", "")
        for label, dom in (("tavily_plain", None), ("tavily_reddit_domain", ["reddit.com"])):
            try:
                payload = {"api_key": tkey, "query": "Eli Lilly stock discussion",
                           "max_results": 5, "search_depth": "basic"}
                if dom:
                    payload["include_domains"] = dom
                req = urllib.request.Request(
                    "https://api.tavily.com/search",
                    data=json.dumps(payload).encode("utf-8"), method="POST",
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=25) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    out["checks"][label] = {"http": resp.status,
                                            "results": len(body.get("results", []))}
            except urllib.error.HTTPError as ex:
                out["checks"][label] = {"http": ex.code,
                                        "error": ex.read()[:200].decode("utf-8", "replace")}
            except Exception as ex:
                out["checks"][label] = {"error": str(ex)[:300]}
        # 4) 직접 Reddit (datacenter IP 에서 403 예상)
        try:
            rd = src.reddit_search("Eli Lilly stock", max_results=3)
            out["checks"]["reddit_direct"] = {"ok": len(rd) > 0, "count": len(rd)}
        except Exception as ex:
            out["checks"]["reddit_direct"] = {"ok": False, "error": str(ex)[:300]}
        # 5) Brave 폴백 (Tavily 432 시 대체)
        try:
            bv = src.brave_search("Eli Lilly stock discussion", max_results=3,
                                  include_domains=["reddit.com"])
            out["checks"]["brave_reddit"] = {"ok": len(bv) > 0, "count": len(bv)}
        except Exception as ex:
            out["checks"]["brave_reddit"] = {"ok": False, "error": str(ex)[:300]}
        try:
            bn = src.brave_search("Samsung Electronics stock news", max_results=3)
            out["checks"]["brave_news"] = {"ok": len(bn) > 0, "count": len(bn)}
        except Exception as ex:
            out["checks"]["brave_news"] = {"ok": False, "error": str(ex)[:300]}
        return self._send(200, out)

    def log_message(self, fmt, *args):                # compact request logging
        sys.stderr.write("[research] " + (fmt % args) + "\n")


# ---------------------------------------------------------------------------
# Scheduled GitHub Actions trigger.
# GitHub's own `schedule:` is best-effort — it delays by 1~3h and sometimes drops
# entirely. This always-on server fires workflow_dispatch at EXACT KST times, so
# the schedule: blocks in those workflows can be removed (workflow_dispatch stays
# for manual runs). Needs GH_DISPATCH_TOKEN (fine-grained PAT, Actions: write).
# ---------------------------------------------------------------------------
GH_REPO  = os.environ.get("GH_REPO", "jykim0048/backtest_stock")
GH_REF   = os.environ.get("GH_REF", "main")
GH_TOKEN = os.environ.get("GH_DISPATCH_TOKEN", "")

DAILY_WF     = "daily_report.yml"
INTRADAY_WF  = "intraday_screener.yml"
CLOSING_WF   = "closing_briefing.yml"
INVWARN_WF   = "investment_warning.yml"
CORPMAP_WF   = "build_corp_map.yml"
INDEXCON_WF  = "index_constituents.yml"
THEMEMAP_WF  = "theme_map.yml"
INTRADAY_MIN = {7, 37}            # KST 09:07~14:37, 30분 간격 (장 마감 전후 회차 제외)


def _dispatch(workflow_file):
    if not GH_TOKEN:
        print("[sched] GH_DISPATCH_TOKEN 미설정 — 트리거 생략", file=sys.stderr, flush=True)
        return False
    url = f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{workflow_file}/dispatches"
    data = json.dumps({"ref": GH_REF}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "railway-scheduler")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(f"[sched] dispatched {workflow_file} -> HTTP {r.status}", flush=True)
            return True
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "replace")
        print(f"[sched] {workflow_file} dispatch 실패: HTTP {e.code} {body}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[sched] {workflow_file} dispatch 오류: {e}", file=sys.stderr, flush=True)
    return False


def _scheduler():
    print(f"[sched] started (repo={GH_REPO} ref={GH_REF} "
          f"token={'set' if GH_TOKEN else 'MISSING'})", flush=True)
    fired = set()                                  # (date, key) — 같은 시각 중복 트리거 방지
    while True:
        try:
            now = datetime.datetime.now(KST)
            today = now.strftime("%Y-%m-%d")
            fired = {(d, k) for (d, k) in fired if d == today}   # 날짜 바뀌면 정리
            if now.weekday() <= 4:                               # 월~금
                if now.hour == 7 and now.minute == 43:           # 장전 리포트 07:43 KST
                    key = (today, "daily")
                    if key not in fired and _dispatch(DAILY_WF):
                        fired.add(key)
                if now.hour == 7 and now.minute == 50:           # 투자주의/경고 07:50 KST
                    key = (today, "invwarn")
                    if key not in fired and _dispatch(INVWARN_WF):
                        fired.add(key)
                if 9 <= now.hour <= 14 and now.minute in INTRADAY_MIN:   # 장중 09:07~14:37
                    key = (today, f"intraday-{now.hour:02d}{now.minute:02d}")
                    if key not in fired and _dispatch(INTRADAY_WF):
                        fired.add(key)
                if now.hour == 15 and now.minute == 40:          # 마감 시황 15:40 KST
                    key = (today, "closing")
                    if key not in fired and _dispatch(CLOSING_WF):
                        fired.add(key)
                if now.weekday() == 0 and now.hour == 6 and now.minute == 30:   # 테마맵 주1회 월 06:30 KST
                    key = (today, "thememap")
                    if key not in fired and _dispatch(THEMEMAP_WF):
                        fired.add(key)
            # 평일 무관(시각 민감도 낮음) — 월 1회 / 반기
            if now.day == 2 and now.hour == 3 and now.minute == 13:       # 매월 2일 03:13 corp map
                key = (today, "corpmap")
                if key not in fired and _dispatch(CORPMAP_WF):
                    fired.add(key)
            if now.month in (6, 12) and now.day == 16 and \
               now.hour == 7 and now.minute == 0:                          # 6·12월 16일 07:00 반기 지수구성
                key = (today, "indexcon")
                if key not in fired and _dispatch(INDEXCON_WF):
                    fired.add(key)
        except Exception as e:
            print(f"[sched] loop error: {e}", file=sys.stderr, flush=True)
        time.sleep(20)


def main():
    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[research] listening on :{port}  (master={len(_BY_CODE)} stocks)", flush=True)

    # Warm the DART corp map in background so the server starts accepting requests
    # immediately (corpCode.xml download can take minutes on cold start).
    def _warmup():
        try:
            ga.sources._load_corp_map()
            print("[research] corp map ready", flush=True)
        except Exception as ex:
            print(f"[research] corp map warmup skipped: {ex}", file=sys.stderr, flush=True)

    threading.Thread(target=_warmup, daemon=True).start()
    threading.Thread(target=_scheduler, daemon=True).start()
    threading.Thread(target=_raw_probe, daemon=True).start()   # 데이터 프록시 진단 로그
    srv.serve_forever()


if __name__ == "__main__":
    main()
