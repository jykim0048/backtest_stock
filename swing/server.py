"""swing-web(7단계) — 스윙 모의투자 대시보드 전용 서버(Railway 상시 서비스, 브랜치 swing_paper).

  GET /                               대시보드(static/index.html)
  GET /static/<file>                  JS·CSS
  GET /api/swing/summary              자본·현금·평가·수익률·거래 통계·최근 런
  GET /api/swing/positions            보유(진입·손절·목표·보유일·평가손익·매도 예약)
  GET /api/swing/orders[?date=]       그날 생성·유효 주문과 결과(기본: 최근 런 날짜)
  GET /api/swing/trades[?from=&to=]   청산 내역
  GET /api/swing/equity               일별 평가액
  GET /api/swing/runs[?date=]         런 날짜 목록 / 그날 런 로그
  GET /api/swing/decision?key=        trading_agent 판단 원문(decision/<date>/<code>/<purpose>)
  GET /api/swing/export               전체 문서 JSON(백업)
  GET /api/prices?codes=              장중 시세 — SWING_PRICES_PROXY(main 대시보드) 중계, 표시 전용
  GET /healthz

저장소는 store.from_env() — DATABASE_URL 있으면 Postgres(swing_docs), 없으면 파일.
"""
import json
import mimetypes
import os
import sys
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
from swing import engine, store as store_mod  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
PRICES_PROXY = os.environ.get("SWING_PRICES_PROXY", "").rstrip("/")
STORE = None


def _ledger():
    return STORE.get("ledger") or engine.new_ledger()


def _runs():
    return [k.split("/", 1)[1] for k in STORE.keys("run/")]


def api_summary(q):
    L = _ledger()
    tr = L["trades"]
    wins = [t for t in tr if t["pnl"] > 0]
    eq = L["equity"][-1] if L["equity"] else None
    runs = _runs()
    last = STORE.get(f"run/{runs[-1]}") if runs else None
    by_reason = {}
    for t in tr:
        by_reason[t["reason"]] = by_reason.get(t["reason"], 0) + 1
    return {"asof": L.get("asof"), "capital0": L["capital0"], "cash": round(L["cash"]),
            "equity": eq["equity"] if eq else round(L["cash"]),
            "retPct": eq["retPct"] if eq else 0.0,
            "positions": len(L["positions"]), "openOrders": len(L["orders"]),
            "trades": {"n": len(tr), "wins": len(wins),
                       "winRate": round(len(wins) / len(tr) * 100, 1) if tr else None,
                       "avgRetPct": round(sum(t["retPct"] for t in tr) / len(tr), 2) if tr else None,
                       "pnl": sum(t["pnl"] for t in tr), "byReason": by_reason},
            "lastRun": last}


def api_positions(q):
    L = _ledger()
    out = []
    for p in L["positions"]:
        px = p.get("lastClose") or p["entry"]
        out.append({**p, "value": round(p["qty"] * px),
                    "unrealPct": round((px / p["entry"] - 1) * 100, 2)})
    return out


def api_orders(q):
    L = _ledger()
    runs = _runs()
    date = q.get("date") or (runs[-1] if runs else L.get("asof"))
    created = [o for o in L["orders"] + L["orderLog"] if o.get("created") == date]
    executed = [o for o in L["orderLog"] if o.get("validDate") == date]
    return {"date": date, "dates": runs, "created": created, "executed": executed}


def api_trades(q):
    tr = _ledger()["trades"]
    f, t = q.get("from"), q.get("to")
    return [x for x in tr if (not f or x["exitDate"] >= f) and (not t or x["exitDate"] <= t)][::-1]


def api_runs(q):
    if q.get("date"):
        return STORE.get(f"run/{q['date']}")
    return _runs()


def api_decision(q):
    k = q.get("key") or ""
    if not k.startswith("decision/"):
        return None
    return STORE.get(k)


def api_export(q):
    return {k: STORE.get(k) for k in STORE.keys("")}


ROUTES = {"/api/swing/summary": api_summary, "/api/swing/positions": api_positions,
          "/api/swing/orders": api_orders, "/api/swing/trades": api_trades,
          "/api/swing/equity": lambda q: _ledger()["equity"],
          "/api/swing/runs": api_runs, "/api/swing/decision": api_decision,
          "/api/swing/export": api_export}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("SWING_ACCESS_LOG"):
            super().log_message(fmt, *args)

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/healthz":
                return self._json({"ok": True, "store": type(STORE).__name__})
            if u.path in ROUTES:
                r = ROUTES[u.path](q)
                return self._json(r) if r is not None else self._json({"error": "not found"}, 404)
            if u.path == "/api/prices":
                return self._prices(u.query)
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path.startswith("/static/"):
                return self._static(u.path[len("/static/"):])
            return self._json({"error": "not found"}, 404)
        except Exception as ex:
            return self._json({"error": f"{type(ex).__name__}: {ex}"}, 500)

    def _static(self, rel):
        full = os.path.normpath(os.path.join(STATIC, rel))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._json({"error": "not found"}, 404)
        with open(full, "rb") as f:
            body = f.read()
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, body, ctype, "no-cache")

    def _prices(self, query):
        if not PRICES_PROXY:
            return self._json({"error": "SWING_PRICES_PROXY 미설정"}, 404)
        try:
            with urllib.request.urlopen(f"{PRICES_PROXY}/api/prices?{query}", timeout=10) as r:
                return self._send(200, r.read())
        except Exception as ex:
            return self._json({"error": str(ex)[:200]}, 502)


def main():
    global STORE
    STORE = store_mod.from_env()
    port = int(os.environ.get("PORT", "8124"))
    print(f"[swing-web] :{port} store={type(STORE).__name__}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
