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
  GET /api/swing/signals?date=        그날 주간 브리핑 신호(섹터 신호·종목 관찰 4칸 축약본)
  GET /api/swing/universe[?date=]     업종별 종목 수급 모멘텀 유니버스(universe/<date>, 없으면 최신) + 날짜 목록
  GET /api/swing/sim[?key=]           유니버스 선정 방식 A/B/C 시뮬 결과(sim/…) — key 없으면 목록+진행
  GET /admin/sim?token=&from=&to=     시뮬 백그라운드 실행(SWING_ADMIN_TOKEN 필요, Railway 에서 과거 재구성 — tools/sim_universe)
  GET /admin/dbq?token=&tr=&code=     DB증권 TR 원본 응답 진단(industry|components|investor|chart|fcodes) — 필드명·구조 확인용
  GET /admin/universe?token=&date=    그 날짜 유니버스 재산출(덮어쓰기, 백그라운드) · 진행 GET /api/swing/universe/progress
  GET /api/swing/config               운용 규칙(자본·보유일·세금·비중·신호/등급 조건)
  GET /api/swing/export               전체 문서 JSON(백업)
  GET /api/prices?codes=              장중 시세 — SWING_PRICES_PROXY(main 대시보드) 중계, 표시 전용
  GET /healthz

저장소는 store.from_env() — DATABASE_URL 있으면 Postgres(swing_docs), 없으면 파일.
로컬 확인: `python swing/server.py --demo` → 데모 데이터(swing/data/demo) 자동 생성 후 서빙.
"""
import datetime
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
from swing import config, engine, store as store_mod  # noqa: E402

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
        r = px / p["entry"] - 1
        out.append({**p, "value": round(p["qty"] * px),
                    "unrealPct": round((-r if p.get("side") == "short" else r) * 100, 2)})   # 숏은 하락이 이익
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


def api_signals(q):
    d = q.get("date")
    if not d:
        ks = STORE.keys("signals/")
        d = ks[-1].split("/", 1)[1] if ks else None
    return {"date": d, "signals": STORE.get(f"signals/{d}") if d else None}


def _universe_dates():
    return [k.split("/", 1)[1] for k in STORE.keys("universe/") if len(k) == len("universe/2026-01-01")]


def api_universe(q):
    """{date, dates, universe} — date 없거나 그날 문서가 없으면 그 날짜 이하 최신본."""
    ds = _universe_dates()
    d = q.get("date")
    if d not in ds:
        older = [x for x in ds if not d or x <= d]
        d = older[-1] if older else (ds[-1] if ds else None)
    return {"date": d, "dates": ds, "universe": STORE.get(f"universe/{d}") if d else None}


def api_sim(q):
    k = q.get("key")
    if k:
        return STORE.get(k) if k.startswith("sim/") else None
    return {"keys": [x for x in STORE.keys("sim/") if x != "sim/progress"], "progress": STORE.get("sim/progress")}


def _admin_ok(q):
    tok = (os.environ.get("SWING_ADMIN_TOKEN") or "").strip().strip('"').strip("'")
    return bool(tok) and (q.get("token") or "").strip() == tok


def admin_dbq(q):
    """DB증권 TR 원본 응답 진단(토큰 필요) — tr=industry|components|investor|chart|fcodes[&code=][&d1=&d2=][&mkt=].
    응답 본문은 행 3개·필드명·rsp 만(길이 제한)."""
    if not _admin_ok(q):
        return {"error": "token 불일치 또는 미설정"}
    from swing import universe as U
    dbq = U.DbQuote()
    tr, code = q.get("tr") or "industry", q.get("code") or "005930"
    d2 = q.get("d2") or datetime.date.today().isoformat()
    d1 = q.get("d1") or (datetime.date.fromisoformat(d2) - datetime.timedelta(days=14)).isoformat()
    if tr == "sectors":                                         # 구성종목 겹침 해석 결과(캐시 무시)
        sm = U.load_sector_map(STORE, datetime.date.today().isoformat())
        res = U.resolve_db_sectors(dbq, sm, codes=[str(c) for c in range(int(q.get("c1") or 1001), int(q.get("c2") or 1061))])
        return {"sectorMap": len(sm), "resolved": res, "calls": dbq.calls}
    spec = {"industry": (dbq.P_IND, {"InputCondMrktDivCode": q.get("mkt") or "K", "InputMrktClsCode": q.get("cls") if q.get("cls") is not None else "U"}),
            "components": (dbq.P_COMP, {"InputBstpIscd": code if len(code) == 4 else "1005", "InputRankSortClsCode1": "42", "InputCondMrktDivCode": "UJ"}),
            "investor": (dbq.P_INV, {"InputCondMrktDivCode": "J", "InputIscd1": code, "InputDate1": d1.replace("-", ""), "InputDate2": d2.replace("-", "")}),
            "chart": (dbq.P_CHART, {"InputOrgAdjPrc": "0", "InputCondMrktDivCode": "J", "InputIscd1": code, "InputDate1": d1.replace("-", ""), "InputDate2": d2.replace("-", "")}),
            "fcodes": (dbq.P_FUT, {"InputCondMrktDivCode": "JF"})}
    if tr not in spec:
        return {"error": f"tr 는 {sorted(spec)} 중 하나"}
    path, body = spec[tr]
    try:
        r = dbq.raw(path, body)
    except Exception as ex:
        return {"error": f"{type(ex).__name__}: {ex}"[:300], "path": path, "in": body}
    data = r["body"] if isinstance(r["body"], dict) else {"_": r["body"]}
    rows = U.DbQuote._rows_of(data) if isinstance(data, dict) else []
    return {"path": path, "in": body, "http": r["status"], "cont_yn": r["cont_yn"],
            "rsp_cd": data.get("rsp_cd"), "rsp_msg": data.get("rsp_msg"), "topKeys": list(data.keys())[:10],
            "outType": type(data.get("Out")).__name__, "rows": len(rows), "fieldNames": list(rows[0].keys()) if rows and isinstance(rows[0], dict) else None,
            "sample": rows[:3], "parsedIndustries": [dict(zip(("code", "name"), U._pick_code_name(x))) for x in rows[:40]] if tr == "industry" else None}


def admin_universe(q):
    """특정 날짜 유니버스 재산출(토큰 필요) — ?date=YYYY-MM-DD[&force=1]. 백그라운드, 진행은 universe/progress.
    주간 브리핑은 main raw 최신본(그 날짜 asof 가 아니면 doc.wbAsof 로 표시). 후보·주문은 다시 돌리지 않는다(표시용 문서만)."""
    if not _admin_ok(q):
        return {"error": "token 불일치 또는 미설정"}
    import threading
    from swing import run_daily, universe as U
    date = q.get("date") or datetime.date.today().isoformat()
    force = q.get("force", "1") != "0"
    key = f"universe/{date}"

    def put(**kw):
        STORE.put("universe/progress", dict(kw, key=key, at=datetime.datetime.now().isoformat(timespec="seconds")))

    def job():
        try:
            put(status="running", step="주간 브리핑 로드")
            wb = run_daily.load_wb(date)
            put(status="running", step="산출")
            doc = U.UniverseRunner(STORE, force=force).build_or_load(date, wb, STORE.get("ledger"))
            put(status="done", summary=doc["summary"], secs=doc["diag"]["secs"], wbAsof=doc.get("wbAsof"))
        except Exception as e:
            put(status="failed", error=f"{type(e).__name__}: {e}"[:300])
    threading.Thread(target=job, daemon=True, name="swing-universe-rebuild").start()
    return {"started": key, "force": force, "progress": "/api/swing/universe/progress"}


def api_universe_progress(q):
    return STORE.get("universe/progress") or {"status": "none"}


def admin_sim(q):
    """토큰 미설정/불일치는 404 가 아니라 이유를 돌려준다(값은 노출하지 않음) — 2026-10-13 진단."""
    tok = (os.environ.get("SWING_ADMIN_TOKEN") or "").strip().strip('"').strip("'")
    if not tok:
        return {"error": "SWING_ADMIN_TOKEN 미설정 — swing-web 서비스 Variables 에 추가 후 Deploy 필요"}
    if (q.get("token") or "").strip() != tok:
        return {"error": "token 불일치", "hint": f"서버 토큰 길이 {len(tok)} · 받은 길이 {len((q.get('token') or '').strip())}"}
    from swing.tools import sim_universe
    kw = {}
    if q.get("cap"):
        kw["capLong"] = kw["capShort"] = int(q["cap"])
    key = sim_universe.start_background(STORE, q.get("from") or "2026-08-31", q.get("to") or STORE.get("ledger", {}).get("asof"), **kw)
    return {"started": key, "progress": "/api/swing/sim"}


def api_progress(q):
    """실행 진행률(daily.Progress) — 없으면 None."""
    return STORE.get("progress")


def api_config(q):
    return {"initialCapital": config.INITIAL_CAPITAL, "sellTax": config.SELL_TAX,
            "holdDays": config.HOLD_DAYS, "maxHoldDays": config.MAX_HOLD_DAYS, "maxWeight": config.MAX_WEIGHT,
            "defaultWeight": config.DEFAULT_WEIGHT, "maxNewPerDay": config.MAX_NEW_PER_DAY,
            "buySignals": config.BUY_SIGNALS, "sellSignals": config.SELL_SIGNALS,
            "buyRatings": config.BUY_RATINGS, "sellRatings": config.SELL_RATINGS,
            "shortEnabled": config.SHORT_ENABLED, "shortSignals": config.SHORT_SIGNALS,
            "maxNewShortPerDay": config.MAX_NEW_SHORT_PER_DAY, "shortMaxGross": config.SHORT_MAX_GROSS,
            "borrowRate": config.BORROW_RATE,
            "universe": config.UNIVERSE_ENABLED, "uniNewPerDay": config.UNI_NEW_PER_DAY,
            "uniNewShortPerDay": config.UNI_NEW_SHORT_PER_DAY, "uniCandMin": config.UNI_CAND_MIN, "uniRankBasis": config.UNI_RANK_BASIS,
            "uniTotalMin": config.UNI_TOTAL_MIN,
            "uniCooldownDays": config.UNI_COOLDOWN_DAYS, "uniPickMode": config.UNI_PICK_MODE, "uniSectorCap": config.UNI_SECTOR_CAP,
            "adminSimConfigured": bool((os.environ.get("SWING_ADMIN_TOKEN") or "").strip())}


def api_export(q):
    return {k: STORE.get(k) for k in STORE.keys("")}


ROUTES = {"/api/swing/summary": api_summary, "/api/swing/progress": api_progress, "/api/swing/positions": api_positions,
          "/api/swing/orders": api_orders, "/api/swing/trades": api_trades,
          "/api/swing/equity": lambda q: _ledger()["equity"],
          "/api/swing/runs": api_runs, "/api/swing/decision": api_decision,
          "/api/swing/signals": api_signals, "/api/swing/universe": api_universe, "/api/swing/sim": api_sim,
          "/admin/sim": admin_sim, "/admin/dbq": admin_dbq, "/admin/universe": admin_universe,
          "/api/swing/universe/progress": api_universe_progress, "/api/swing/config": api_config,
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
        if self.path.startswith("/api/"):
            self.send_header("Access-Control-Allow-Origin", "*")   # 2026-10-14 주 대시보드(backteststock)가 유니버스를 직접 fetch
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


def main(argv=None):
    """--demo: 데모 데이터(swing/data/demo)가 없으면 자동 생성하고 그 데이터로 서빙(로컬 확인용,
    DATABASE_URL 무시). --rebuild-demo: 데모 데이터를 지우고 다시 생성. 옵션 없으면 store.from_env()."""
    import argparse
    global STORE
    for _s in (sys.stdout, sys.stderr):                  # Windows 콘솔(cp949)에서 한글 로그 깨짐 방지
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="데모 데이터로 서빙(없으면 자동 생성)")
    ap.add_argument("--rebuild-demo", action="store_true", help="데모 데이터 재생성 후 서빙")
    a = ap.parse_args(argv)
    if a.demo or a.rebuild_demo:
        from swing.tools import demo_data
        STORE = store_mod.FileStore(demo_data.build(rebuild=a.rebuild_demo))
    else:
        STORE = store_mod.from_env()
    port = int(os.environ.get("PORT", "8124"))
    print(f"[swing-web] :{port} store={type(STORE).__name__}" + (" (demo)" if a.demo or a.rebuild_demo else ""), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
