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
import urllib.request
import urllib.error
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
INVWARN_WF   = "investment_warning.yml"
CORPMAP_WF   = "build_corp_map.yml"
INDEXCON_WF  = "index_constituents.yml"
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
    srv.serve_forever()


if __name__ == "__main__":
    main()
