#!/usr/bin/env python3
"""Persistent on-demand Deep Research server (Railway / any always-on host).

Unlike Vercel serverless (60s cap, stateless), a long-running process can do the
research SYNCHRONOUSLY and keep an in-memory cache + the DART corp map warm. The
Vercel-hosted dashboard's search box calls this service's /api/research directly
(set RESEARCH_API_BASE in public/index.html to this service's public URL).

  GET /api/research?q=<name|code>
      -> {status:"done", code, name, market, result, generatedAt, cached}
      -> {status:"error", message, ...}
  GET /healthz   -> {status:"ok"}

Reuses generate_analysis.analyze_stock — the same per-stock pipeline the CI batch
uses (peers + news + DART + community + LLM). No external store needed.

Env (Railway variables): GEMINI_API_KEY (or LLM_CHAIN + matching keys),
     DART_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, TAVILY_API_KEY.
     PORT is injected by Railway.
"""
import os
import sys
import json
import time
import datetime
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

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
        if u.path in ("/", "/healthz"):
            return self._send(200, {"status": "ok"})
        if u.path != "/api/research":
            return self._send(404, {"status": "error", "message": "not found"})
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

    def log_message(self, fmt, *args):                # compact request logging
        sys.stderr.write("[research] " + (fmt % args) + "\n")


def main():
    # Warm the DART corp map (static file if bundled, else one-time download) so the
    # first request isn't slow.
    try:
        ga.sources._load_corp_map()
    except Exception as ex:
        print(f"[research] corp map warmup skipped: {ex}", file=sys.stderr)

    port = int(os.environ.get("PORT", "8080"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[research] listening on :{port}  (master={len(_BY_CODE)} stocks)")
    srv.serve_forever()


if __name__ == "__main__":
    main()
