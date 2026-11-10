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
  GET /admin/new-only?token=&date=[&dry=1]  생략된 신규 후보 판단·주문 보충(PLAN_NEW_ONLY.md, 백그라운드) · 결과 GET /api/swing/newonly
  GET /api/swing/config               운용 규칙(자본·보유일·세금·비중·신호/등급 조건)
  GET /api/swing/export               전체 문서 JSON(백업)
  GET /api/swing/intraday[?date=]     Live Trader(장중 집행기) — 분할 체결·장중 손절·종목별 VWAP(PLAN_LIVE_TRADER.md)
  GET /api/swing/reflections[?from=&to=]  매매 복기(reflection/<청산일>, swing/reflect.py) — 최근 청산일 순
  GET /api/swing/history?key=         트레이딩 히스토리(swing/history.py) — 그 판단이 속한 포지션 회차의 체결·재판별·청산·비중·일봉
  GET /api/swing/benchmark            평가액 추이 비교선 — 코스피 일별 종가(평가 기록 날짜)·기준 종가·장중 현재 지수
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
import traceback
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
    from swing import history
    equity, ret = (eq["equity"], eq["retPct"]) if eq else (round(L["cash"]), 0.0)
    live_ts = None
    lv = api_live(q)                                     # 장중(정산 전) — /api/swing/live 와 같은 평가액(2026-11-03·04)
    if lv.get("live") and L["positions"]:
        equity, ret, live_ts = lv["equity"], lv["retPct"], lv["liveTs"]
    return {"asof": L.get("asof"), "capital0": L["capital0"], "cash": round(L["cash"]),
            "equity": equity, "retPct": ret, "liveTs": live_ts, "liveDate": history.live_date(STORE, L.get("asof")) if live_ts else None,
            "closeEquity": eq["equity"] if eq else round(L["cash"]),
            "positions": len(L["positions"]), "openOrders": len(L["orders"]),
            "trades": {"n": len(tr), "wins": len(wins),
                       "winRate": round(len(wins) / len(tr) * 100, 1) if tr else None,
                       "avgRetPct": round(sum(t["retPct"] for t in tr) / len(tr), 2) if tr else None,
                       "pnl": sum(t["pnl"] for t in tr), "byReason": by_reason},
            "collateral": _collateral(L, equity),
            "lastRun": last}


def _collateral(L, equity):
    """Short 담보 현황(2026-10-18) — 대용가·요구 담보·여력·사용률. 대용 모드가 아니면 mode 만."""
    c = engine.collateral(L)
    base = L["cash"] + c["subst"]                       # 담보 총액(현금 + 대용가)
    return {"mode": config.SHORT_COLLATERAL, "substRate": config.SHORT_SUBST_RATE, "margin": config.SHORT_MARGIN,
            "cash": round(L["cash"]), "subst": round(c["subst"]), "req": round(c["req"]), "pledged": round(c["pledged"]),
            "room": round(c["room"]), "roomPct": round(c["room"] / equity * 100, 1) if equity else None,
            "usePct": round(c["req"] / base * 100, 1) if base > 0 else None}


def _today_kst():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).date().isoformat()


def api_positions(q):
    """보유 — 현재가 = 장중이면 Live Trader 마지막 시세(15초 폴링, priceTs), 정산 뒤엔 원장 종가(2026-11-03)."""
    from swing import history
    L = _ledger()
    live = history.live_prices(STORE, L.get("asof"))
    day = history.live_date(STORE, L.get("asof"))
    doc = (STORE.get(f"intraday/{day}") or {}) if day else {}
    exits = doc.get("exits") or {}
    adds = _live_adds(L, doc)
    # 보유일(체결일=1)은 원장 정산(16:30) 때 +1 — 장중엔 정산 전이라 하루 덜 보였다(2026-11-05: 11/4 진입이 11/5 장중 1칸).
    # 장중 기록 날짜(live_date)가 원장 asof 보다 뒤면 그 사이 영업일 수만큼 더한다.
    # 2026-11-06: 장 시작 전(장중 기록이 아직 없을 때) 연 화면은 오늘을 빼고 셌고, 장중 10초 갱신(api_live)은 보유일을 다시 안 받아
    # 하루 덜 보인 채 남았다 → 기준 = max(장중 기록 날짜, 오늘 KST)까지의 영업일, api_live 에도 holdDay 를 싣는다.
    from swing import tradedays
    ref = max(x for x in (day, _today_kst()) if x)
    hold_add = len(tradedays.trading_days(tradedays.next_trading_day(L["asof"]), ref)) if L.get("asof") else 0
    out = []
    for p in L["positions"]:
        lv = live.get(p["code"])
        px = (lv or {}).get("price") or p.get("lastClose") or p["entry"]
        sgn = -1 if p.get("side") == "short" else 1
        # 오늘 장중 청산(정산 전, 2026-11-03 LG화학) — 원장은 16:30 정산 때 줄어드니 상태에 따로 표시
        own, sw = exits.get(p["id"]) or {}, exits.get("s:" + p["id"]) or {}
        trims = [x for k, x in exits.items() if k.startswith("i:") and x.get("positionId") == p["id"]]   # Live PM 부분 축소
        fills = sorted((own.get("fills") or []) + (sw.get("fills") or []) + [f for x in trims for f in x.get("fills") or []],
                       key=lambda f: f["ts"])
        # 오늘 장중 추가 체결(비중 채우기 topupOf, 2026-11-04) — 정산 때 평균단가로 합쳐지는 것과 같은 식으로 미리 반영
        af = adds.get(p["id"]) or []
        add_q = sum(f["qty"] for f in af)
        sold = min(sum(f["qty"] for f in fills), p["qty"] + add_q)
        qty = p["qty"] + add_q - sold
        entry = (p["entry"] * p["qty"] + sum(f["qty"] * f["price"] for f in af)) / (p["qty"] + add_q) if add_q else p["entry"]
        r = px / entry - 1
        # 오늘 청산분의 실현 손익(정산 전 추정, 2026-11-04 한화솔루션 — 손절 전량 환매로 +인데 평가손익이 현재가로 −로 보이던 문제)
        proceeds = sum(f["qty"] * f["price"] for f in fills)
        realized = sgn * (proceeds - sold * entry) - (proceeds * config.SELL_TAX if sgn > 0 else 0) if sold else None
        kind = own.get("kind") if own.get("fills") else "switch" if sw.get("fills") else (trims[0].get("kind") if trims else None)
        lx = {"kind": kind, "qty": sold, "done": qty <= 0, "at": fills[-1]["ts"][11:16]} if sold else None
        # 평가액 보정(api_live) — 원장 수량·평단 평가에 오늘 체결분 차이만 더한다: 추가 = (현재가 − 체결가), 청산 = (체결가 − 현재가)
        adj = sgn * (sum(f["qty"] * (px - f["price"]) for f in af) + sum(f["qty"] * (f["price"] - px) for f in fills))
        prev = (lv or {}).get("prevClose") or (p.get("lastClose") if lv else None)
        out.append({**p, "qty": qty, "entry": entry, "qtyLedger": p["qty"], "entryLedger": p["entry"],
                    "holdDay": (p.get("holdDay") or 0) + hold_add,
                    "price": px, "priceTs": ((lv or {}).get("ts") or "")[11:19] or None, "value": round(qty * px),
                    "chgPct": round((px / prev - 1) * 100, 2) if prev else None,
                    "unrealPct": round(sgn * r * 100, 2) if qty > 0 else None, "liveExit": lx,   # 숏은 하락이 이익 · 전량 청산이면 평가손익 없음
                    "liveRealized": round(realized) if realized is not None else None,
                    "liveRealizedPct": round(realized / (sold * entry) * 100, 2) if realized is not None else None,
                    "liveAdd": {"qty": add_q, "price": round(sum(f["qty"] * f["price"] for f in af) / add_q), "at": af[-1]["ts"][11:16]} if add_q else None,
                    "liveAdj": round(adj)})
    out += _live_entries(L, doc, live)
    return out


def _live_adds(L, doc):
    """오늘 장중 추가 체결(비중 채우기 주문 topupOf) → {포지션 id: [체결]} (2026-11-04)."""
    orders = {o["id"]: o for o in L.get("orders") or []}
    out = {}
    for oid, rec in (doc.get("orders") or {}).items():
        pid = rec.get("topupOf") or (orders.get(oid) or {}).get("topupOf")
        if pid and rec.get("fills"):
            out.setdefault(pid, []).extend(f for f in rec["fills"] if f.get("qty"))
    for v in out.values():
        v.sort(key=lambda f: f["ts"])
    return out


def _live_entries(L, doc, live):
    """오늘 장중 신규 진입(정산 전, 2026-11-04) — Live Trader 체결 기록으로 임시 보유 행을 만든다(id = live:<주문>).
    원장엔 16:30 정산 때 들어간다. 추가 매수(topupOf)는 기존 행에 합치지 않고 오늘 체결 수량만 liveAdd 로 붙인다."""
    orders = {o["id"]: o for o in L.get("orders") or []}
    held = {(p["code"], p.get("side", "long")) for p in L["positions"]}
    rows = []
    for oid, rec in (doc.get("orders") or {}).items():
        fills = rec.get("fills") or []
        q = sum(f["qty"] for f in fills)
        o = orders.get(oid) or {}
        side = rec.get("side") or o.get("side") or "long"
        if not q or rec.get("topupOf") or o.get("topupOf") or (rec.get("code"), side) in held:
            continue
        avg = sum(f["qty"] * f["price"] for f in fills) / q
        lv = live.get(rec.get("code")) or {}
        px = lv.get("price") or fills[-1]["price"]
        r = px / avg - 1
        # 같은 날 체결 직후 손절(exits['o:<주문>'], 2026-11-04 두산 3주 공매도 → 09:07 손절 전량 환매) — 남은 수량·실현 추정
        xf = ((doc.get("exits") or {}).get("o:" + oid) or {}).get("fills") or []
        out_q = min(q, sum(f["qty"] for f in xf))
        sgn = -1 if side == "short" else 1
        realized = sgn * (sum(f["qty"] * f["price"] for f in xf) / out_q - avg) * out_q if out_q else 0.0
        left = q - out_q
        rows.append({"id": "live:" + oid, "code": rec.get("code"), "name": rec.get("name") or o.get("name"), "side": side, "qty": left, "entry": avg,
                     "stop": rec.get("stop") or o.get("stop"), "target": o.get("target"), "sector": o.get("sector"), "signal": o.get("signal"),
                     "source": o.get("source"), "decisionKey": o.get("decisionKey"), "weight": o.get("weight") or rec.get("weight"),
                     "rating": o.get("rating") or rec.get("rating"), "fillDate": doc.get("date"), "holdDay": 1, "lastClose": None, "sellPending": None,
                     "dayPrev": lv.get("prevClose"),                         # 기여도 당일 분해 기준(전일 종가, 2026-11-06)
                     "price": px, "priceTs": (lv.get("ts") or fills[-1]["ts"])[11:19], "value": round(left * px), "liveRealized": round(realized),
                     "chgPct": round((px / lv["prevClose"] - 1) * 100, 2) if lv.get("prevClose") else None,
                     "unrealPct": round((-r if side == "short" else r) * 100, 2) if left > 0 else None,
                     "liveRealizedPct": round(realized / (out_q * avg) * 100, 2) if out_q else None,
                     "liveExit": {"kind": (doc.get("exits") or {})["o:" + oid].get("kind"), "qty": out_q, "done": left == 0, "at": xf[-1]["ts"][11:16]} if out_q else None,
                     "liveEntry": {"qty": q, "planned": rec.get("planned") or o.get("qty"), "done": bool(rec.get("done")), "at": fills[-1]["ts"][11:16]}})
    return rows


def api_live(q):
    """장중 실시간 갱신(2026-11-04) — 대시보드가 장중 10초마다 부른다. 평가액·보유 종목 현재가·평가금액·평가손익·장중 청산만."""
    from swing import history
    L = _ledger()
    ps = api_positions(q)
    ts = max(((p.get("priceTs") or "") for p in ps), default="")
    if not ts:
        return {"live": False}
    v = L["cash"] + sum(engine.position_value({**p, "qty": p["qtyLedger"], "entry": p["entryLedger"]}, p["price"]) + (p.get("liveAdj") or 0)
                        if not p["id"].startswith("live:")
                        else (-1 if p["side"] == "short" else 1) * (p["price"] - p["entry"]) * p["qty"] + (p.get("liveRealized") or 0) for p in ps)
    live = history.live_prices(STORE, L.get("asof"))
    return {"live": True, "liveTs": ts, "liveDate": history.live_date(STORE, L.get("asof")), "source": "bus" if any(x.get("bus") for x in live.values()) else "doc",
            "equity": round(v), "retPct": round((v / L["capital0"] - 1) * 100, 2) if L.get("capital0") else None, "cash": round(L["cash"]),
            "positions": [p if p["id"].startswith("live:") else {k: p.get(k) for k in ("id", "code", "side", "price", "priceTs", "chgPct", "value", "unrealPct", "liveExit",
                                                                                  "qty", "entry", "qtyLedger", "liveAdd", "liveRealized", "liveRealizedPct", "holdDay")}
                          for p in ps]}


def api_attribution(q):
    """성과 기여도(2026-11-05, Long·Short PF 탭) — 누적·당일 섹터별·종목별 손익 기여(swing/attrib.py)."""
    from swing import attrib, history
    L = _ledger()
    ps = api_positions(q)
    day = history.live_date(STORE, L.get("asof"))
    lv = api_live(q) if day else {}
    if lv.get("live") and L["positions"]:
        eq_now = lv["equity"]
    else:
        day = None
        eq_now = L["equity"][-1]["equity"] if L["equity"] else L["cash"]
    doc = STORE.get(f"intraday/{L.get('asof')}") if not day else None
    cl = _Closes(L.get("asof"))
    live_idx = _live_index() if day else {}
    mkts = set()

    def mkt(code, d1, d2):
        """상장시장 지수 수익률(β = 1) — 기준일 d1 종가 → d2 종가(None = 장중 현재 지수)."""
        ic = _index_of(code)
        mkts.add(ic)
        i1 = cl("IDX:" + ic, d1)
        i2 = (live_idx.get(ic) if d2 is None else cl("IDX:" + ic, d2))
        return i2 / i1 - 1 if i1 and i2 else None
    out = attrib.compute(L, ps, equity_now=eq_now, live_day=day, doc=doc, close=cl, mkt=mkt)
    out["closesPending"] = cl.kick()
    out["indexLive"] = {ic: live_idx.get(ic) for ic in sorted(mkts)} if day else None
    out["indexWait"] = sorted(ic for ic, t in _Closes._idx_fail.items() if t)
    return out


def api_benchmark(q):
    """평가액 추이 비교선(2026-11-10) — 코스피 일별 종가를 평가액 기록 날짜에 맞춰 준다.
    기준 = 첫 평가 기록 직전 영업일 종가(운용 시작 = 초기 자본). 화면은 '초기 자본을 코스피에 넣었다면'(초기 자본 × 종가 ÷ 기준)으로 그린다.
    종가는 성과 기여도와 같은 캐시(attrib/closes 의 IDX:0001, 허브 /index-chart) — 없는 날짜는 백그라운드로 채우고 pending 으로 알린다.
    정산 전 장중이면 live(허브 /market 현재 지수)를 liveDate 점으로 붙인다."""
    from swing import history, tradedays
    ic = "0001"
    L = _ledger()
    eq = L.get("equity") or []
    out = {"code": ic, "name": INDEX_NAME[ic], "baseDate": None, "base": None, "closes": {}, "live": None, "liveDate": None,
           "pending": 0, "wait": False}
    if not eq:
        return out
    first = eq[0]["date"]
    prev = tradedays.trading_days((datetime.date.fromisoformat(first) - datetime.timedelta(days=14)).isoformat(), first)
    base_day = prev[-2] if len(prev) >= 2 and prev[-1] == first else (prev[-1] if prev and prev[-1] < first else None)
    cl = _Closes(L.get("asof"))
    out["baseDate"], out["base"] = base_day, (cl("IDX:" + ic, base_day) if base_day else None)
    for r in eq:
        v = cl("IDX:" + ic, r["date"])
        if v:
            out["closes"][r["date"]] = v
    out["pending"] = cl.kick()
    out["wait"] = bool(_Closes._idx_fail.get(ic))
    day = history.live_date(STORE, L.get("asof"))
    if day:
        out["liveDate"], out["live"] = day, _live_index().get(ic)
    return out


INDEX_NAME = {"0001": "코스피", "1001": "코스닥"}
_mkt_of = None


def _index_of(code):
    """종목 → 상장시장 지수 코드(KIS 업종: 0001 코스피 · 1001 코스닥). public/assets/krx_companies.json(ticker .KQ)."""
    global _mkt_of
    if _mkt_of is None:
        from swing import prices
        _mkt_of = prices.YFinancePrices()
    return "1001" if _mkt_of._market(str(code).zfill(6)) == "KQ" else "0001"


_live_idx_cache = [0.0, {}]


def _live_index():
    """장중 지수(허브 /market — kis:market:state.index {코드: {price, ctrt}}, 2026-11-06 허브 1단계) → {코드: 현재 지수}. 20초 캐시."""
    import time as _t
    if _t.time() - _live_idx_cache[0] < 20:
        return _live_idx_cache[1]
    out = {}
    try:
        with urllib.request.urlopen(config.FLOW_API_BASE.rstrip("/") + "/market?n=1", timeout=5) as r:
            st = (json.loads(r.read().decode("utf-8")) or {}).get("state") or {}
        for ic, v in (st.get("index") or {}).items():
            if (v or {}).get("price"):
                out[ic] = float(v["price"])
    except Exception as ex:
        print(f"[swing] 장중 지수(/market) 실패: {type(ex).__name__}: {str(ex)[:120]}", flush=True)
    _live_idx_cache[0], _live_idx_cache[1] = _t.time(), out
    return out


def _hub_index_closes(ic, d1, d2):
    """지수 일봉 종가 {YYYY-MM-DD: 종가} — 허브 /index-chart(KIS FHKUP03500100). 실패면 예외."""
    url = (config.FLOW_API_BASE.rstrip("/") + "/index-chart?" +
           urllib.parse.urlencode({"code": ic, "from": d1.replace("-", ""), "to": d2.replace("-", "")}))
    with urllib.request.urlopen(url, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    if d.get("status") != "success":
        raise RuntimeError(d.get("error") or "status != success")
    return {k: float(v) for k, v in (d.get("closes") or {}).items()}


index_closes_fn = _hub_index_closes                         # 데모 서버는 mock 지수로 바꾼다(main)


class _Closes:
    """선정·트레이딩 분해용 종가 조회(2026-11-06) — 문서 attrib/closes {code: {date: 종가}} 캐시.
    없는 (종목, 날짜)는 모아 두었다가 kick() 이 백그라운드로 일봉(history._bars, DB증권 chart_day)을 받아 채운다.
    정산된 날짜(≤ 원장 asof)만 캐시한다. 받았는데 그날 봉이 없으면 0 을 적어 다시 받지 않는다.
    지수는 code 'IDX:<0001|1001>' — 허브 /index-chart 로 받고, 실패하면(허브 배포 전 등) 5분 뒤 다시 시도한다(대기 중엔 pending 에 안 셈)."""
    KEY = "attrib/closes"
    _lock = __import__("threading").Lock()
    _running = [False]
    _idx_fail = {}                                           # 지수 코드 → 마지막 실패 시각(성공하면 0)
    IDX_RETRY = 300

    def __init__(self, asof):
        self.asof = asof
        self.cache = STORE.get(self.KEY) or {}
        self.missing = {}

    def __call__(self, code, d):
        if not d or (self.asof and d > self.asof):
            return None
        v = (self.cache.get(code) or {}).get(d)
        if v is None:
            import time as _t
            if not (code.startswith("IDX:") and _t.time() - (self._idx_fail.get(code[4:]) or 0) < self.IDX_RETRY):
                self.missing.setdefault(code, set()).add(d)
        return v or None

    def kick(self):
        if not self.missing:
            return 0
        n = sum(len(v) for v in self.missing.values())
        with self._lock:
            if self._running[0]:
                return n
            self._running[0] = True
        import threading
        want = {c: sorted(v) for c, v in self.missing.items()}

        def job():
            from swing import history
            try:
                cache = STORE.get(self.KEY) or {}
                import time as _t
                for i, (code, ds) in enumerate(want.items()):
                    if code.startswith("IDX:"):
                        try:
                            got = index_closes_fn(code[4:], ds[0], ds[-1])
                        except Exception as ex:
                            _Closes._idx_fail[code[4:]] = _t.time()
                            print(f"[swing] 기여도 지수 {code[4:]} 실패(5분 뒤 재시도): {type(ex).__name__}: {str(ex)[:120]}", flush=True)
                            continue
                        _Closes._idx_fail[code[4:]] = 0
                        row = cache.setdefault(code, {})
                        for d in ds:
                            row[d] = float(got.get(d) or 0)
                        continue
                    bars = history._bars(code, ds[0], ds[-1], [])
                    row = cache.setdefault(code, {})
                    for d in ds:
                        row[d] = float((bars.get(d) or {}).get("c") or 0)
                    if i % 10 == 9:
                        STORE.put(self.KEY, cache)
                STORE.put(self.KEY, cache)
                print(f"[swing] 기여도 분해 종가 {sum(len(v) for v in want.values())}건 / {len(want)}종목 채움", flush=True)
            except Exception as ex:
                print(f"[swing] 기여도 분해 종가 실패: {type(ex).__name__}: {str(ex)[:160]}", flush=True)
            finally:
                _Closes._running[0] = False
        threading.Thread(target=job, daemon=True, name="swing-attrib-closes").start()
        return n


def api_queue(q):
    """진입 우선순위(2026-11-04) — 다음(또는 오늘 장중) 세션 진입 주문을 실행기와 같은 순서(engine.entry_priority:
    PM 등급 → 높은 비중)로 줄 세우고, 누적 필요 금액을 Long 가용 현금·Short 담보 여력과 견준다. 장중이면 체결 진행도."""
    from swing import history
    L = _ledger()
    day = history.live_date(STORE, L.get("asof"))
    doc = (STORE.get(f"intraday/{day}") or {}) if day else {}
    orders = [o for o in L.get("orders") or [] if o.get("type", "entry") == "entry" and o.get("status") == "open"]
    session = day or min((o.get("validDate") for o in orders), default=None)
    orders = [o for o in orders if o.get("validDate") == session]
    col = engine.collateral(L)
    subst = config.SHORT_COLLATERAL == "subst"
    budget = {"long": max(0.0, col["free"]), "short": max(0.0, col["room"] if subst else L["cash"])}
    out = {"session": session, "live": bool(day), "asof": doc.get("asof"), "complete": bool(doc.get("complete")), "collateral": config.SHORT_COLLATERAL,
           "margin": config.SHORT_MARGIN, "ratings": {"long": list(config.BUY_RATINGS), "short": list(config.SHORT_RATINGS)}}
    for side in ("long", "short"):
        cum, rows = 0.0, []
        for i, o in enumerate(sorted((o for o in orders if (o.get("side") or "long") == side), key=engine.entry_priority)):
            notional = (o.get("qty") or 0) * (o.get("entry") or 0)
            need = notional * (config.SHORT_MARGIN if side == "short" and subst else 1.0)
            cum += need
            rec = (doc.get("orders") or {}).get(o["id"]) or {}
            filled = sum(f["qty"] for f in rec.get("fills") or [])
            wait = _wait_reason(o, rec, (doc.get("vwap") or {}).get(o["code"]) or {}, side, filled, cum <= budget[side] + 1,
                                bool(doc.get("complete"))) if day else None
            rows.append({"rank": i + 1, "id": o["id"], "code": o["code"], "name": o.get("name"), "rating": o.get("rating"),
                         "weight": o.get("weight"), "qty": o.get("qty"), "entry": o.get("entry"), "stop": o.get("stop"),
                         "need": round(need), "cum": round(cum), "fits": cum <= budget[side] + 1,
                         "topup": o.get("topup") if o.get("topupOf") else None, "decisionKey": o.get("decisionKey"),
                         "filled": filled, "done": bool(rec.get("done")), "why": rec.get("why") or None,
                         "wait": wait[0] if wait else None, "waitTip": wait[1] if wait else None})
        out[side] = {"budget": round(budget[side]), "need": round(cum), "items": rows}
    return out


def _wait_reason(o, rec, lv, side, filled, fits, complete):
    """장중 진입 주문이 왜 (더) 체결되지 않는지 — 실행기 조건(executor._advance)과 같은 순서로 짧게. (라벨, 설명) 또는 None."""
    qty = o.get("qty") or 0
    if qty and filled >= qty:
        return None
    why = rec.get("why") or ""
    if rec.get("done") and "손절" in why:            # 본 포지션 손절·체결 후 손절가 도달 → 남은 분할 취소
        return "손절로 중단", why
    if rec.get("done") and not complete:
        return ("중단", why) if why else None
    px, vw = lv.get("price"), lv.get("vwap")
    cap, stop = rec.get("cap") or o.get("entry"), rec.get("stop") or o.get("stop")
    long_ = side == "long"
    word = "마감 시 " if complete else "현재가 "
    cond = None
    if not px:
        cond = None if complete else ("시세 대기", "아직 이 종목 시세를 받지 못함")
    elif stop and (px <= stop if long_ else px >= stop):
        cond = ("손절가 밖", f"{word}{px:,.0f}가 손절가 {stop:,.0f} {'아래' if long_ else '위'} — 진입하지 않음")
    elif cap and (px > cap if long_ else px < cap):
        g = (px / cap - 1) * 100
        cond = (f"리밋 이탈 {g:+.1f}%",
                f"{word}{px:,.0f} {'>' if long_ else '<'} 진입가 리밋 {cap:,.0f}" + ("" if complete else f" — {'내려와야' if long_ else '올라와야'} 체결"))
    elif vw and (px > vw if long_ else px < vw):
        cond = (f"VWAP {'위' if long_ else '아래'}", f"{word}{px:,.0f} {'>' if long_ else '<'} VWAP {vw:,.0f} — VWAP보다 유리할 때만 분할 체결")
    if complete:                                       # 마감 후(2026-11-04) — 마지막 시세 기준으로 왜 못 채웠는지
        rest = " · 남은 수량은 잔여분 추가 주문(비중 채우기)으로 이어짐"
        if cond:
            return cond[0], cond[1] + rest
        return "분할 미완", ("마감 때는 조건이 맞았지만, VWAP보다 유리한 구간에서만 5분 간격으로 나눠 체결해 "
                           "15:19 안에 다 못 채움" + rest)
    if cond:
        return cond
    if not fits and long_:                 # Short 는 장중에 담보로 막지 않는다 — 정산 때 담보 여력만큼 우선순위대로 반영
        return "현금 대기", "조건은 맞지만 앞 순위가 현금을 먼저 씀 — 부족분은 적시 스위칭으로 마련"
    return "분할 간격", "조건 충족 — 다음 VWAP 분할(5분 간격)을 기다리는 중"


def api_orders(q):
    L = _ledger()
    runs = _runs()
    date = q.get("date") or (runs[-1] if runs else L.get("asof"))
    created = [o for o in L["orders"] + L["orderLog"] if o.get("created") == date]
    executed = [o for o in L["orderLog"] if o.get("validDate") == date]
    return {"date": date, "dates": runs, "created": created, "executed": executed}


def api_fills(q):
    """진입 주문 처리 기록 전체(orderLog — 체결·미도달 취소·스킵) — 트레이딩 리포트 '진입 내역'(2026-10-16)."""
    L = _ledger()
    keys = ("id", "code", "name", "side", "created", "validDate", "entry", "stop", "target", "qty", "weight", "status", "note",
            "retry", "source", "sector", "signal", "decisionKey", "partial", "topup", "topupOf")
    return [{k: o.get(k) for k in keys} for o in L.get("orderLog") or [] if o.get("type", "entry") == "entry"][::-1]


def api_trades(q):
    tr = _ledger()["trades"]
    f, t = q.get("from"), q.get("to")
    return [x for x in tr if (not f or x["exitDate"] >= f) and (not t or x["exitDate"] <= t)][::-1]


def api_runs(q):
    if q.get("date"):
        return STORE.get(f"run/{q['date']}")
    return _runs()


def api_decision(q):
    """판단 원문. 진입 판단이면 같은 회차의 마지막 재판별 요약(latestReview)을 붙인다(2026-11-03 — PM 요약은 최신 판단 기준)."""
    k = q.get("key") or ""
    if not k.startswith("decision/"):
        return None
    d = STORE.get(k)
    if d is not None:
        lr = _latest_review(k)
        if lr:
            d = {**d, "latestReview": lr}
    return d


def _latest_review(key):
    p = key.split("/")
    if len(p) != 4 or not p[3].startswith("entry"):
        return None
    from swing import history
    date, code, short = p[1], p[2], p[3].endswith("-short")
    r = history._rounds(_ledger(), code, "short" if short else "long").get(key)
    if not r:
        return None
    end = history._span(r)[1] or "9999-12-31"                # 회차 청산일까지(보유 중이면 오늘까지) — 다른 회차 재판별 제외
    want = "review-short" if short else "review"
    ks = [x for x in STORE.keys("decision/") if x.endswith(f"/{code}/{want}") and date < x.split("/")[1] <= end]
    if not ks:
        return None
    lk = max(ks, key=lambda x: x.split("/")[1])
    doc = STORE.get(lk) or {}
    # 손절·목표는 재판별 뒤 실제 적용된 값(run 기록 — 폭 상한·본전 손절 반영), 없으면 판단 문서 값
    rec = next((x for x in (STORE.get(f"run/{lk.split('/')[1]}") or {}).get("reviews") or [] if x.get("decisionKey") == lk), {})
    return {"key": lk, "date": lk.split("/")[1], "rating": doc.get("rating"), "summary": doc.get("summary"), "error": doc.get("error"), "count": len(ks),
            "trader": doc.get("action"), "reviewAction": rec.get("action"),
            "stop": rec.get("stop") if rec.get("stop") is not None else doc.get("stop"),
            "target": rec.get("target") if rec else doc.get("target")}


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


def admin_newonly(q):
    """생략된 신규 후보 판단·주문 보충(토큰 필요, PLAN_NEW_ONLY.md) — ?date=YYYY-MM-DD[&dry=1]. 백그라운드 스레드,
    결과·로그는 GET /api/swing/newonly(newonly/progress). 가드는 newonly.run_new_only 가 본다(중복 실행·장중 등).
    swing-web 에 swing-cron 과 같은 변수(GEMINI_API_KEY·FLOW_API_BASE·DART·NAVER·SWING_AGENT·SWING_PRICES)가 있어야
    실제 판단이 된다(2026-11-09 변수 참조로 추가). Postgres 가 내부망 전용이라 railway run 으로는 못 돌려 여기서 돈다."""
    if not _admin_ok(q):
        return {"error": "token 불일치 또는 미설정"}
    import threading
    from swing import newonly, run_daily, universe as U
    date = q.get("date") or datetime.date.today().isoformat()
    dry = (q.get("dry") or "0") not in ("0", "", "false")
    cur = STORE.get("newonly/progress") or {}
    if cur.get("status") == "running":
        return {"error": "이미 실행 중", "progress": cur}

    def put(**kw):
        STORE.put("newonly/progress", dict(kw, date=date, dry=dry, at=datetime.datetime.now().isoformat(timespec="seconds")))

    def job():
        logs = []

        def log(msg):
            logs.append(str(msg)[:300])
            print(msg, flush=True)
        try:
            put(status="running", step="주간 브리핑 로드")
            wb = run_daily.load_wb(date)
            agent = run_daily.make_agent(os.environ.get("SWING_AGENT", "trading_agent"), STORE)
            prices = run_daily.make_prices(os.environ.get("SWING_PRICES", "yfinance"))
            uni = U.UniverseRunner(STORE, log=log) if config.UNIVERSE_ENABLED else None
            put(status="running", step="후보 수집·판단", startedAt=datetime.datetime.now().isoformat(timespec="seconds"))
            res = newonly.run_new_only(date, STORE, agent, prices, wb, uni=uni, dry_run=dry, log=log)
            put(status="done", result=res, logs=logs[-120:])
        except Exception as e:
            traceback.print_exc()
            put(status="failed", error=f"{type(e).__name__}: {e}"[:300], logs=logs[-120:])
    threading.Thread(target=job, daemon=True, name="swing-new-only").start()
    return {"started": date, "dry": dry, "progress": "/api/swing/newonly"}


def api_newonly(q):
    return STORE.get("newonly/progress") or {"status": "none"}


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


def admin_catrep(q):
    """하방 촉매 리플레이(2026-10-15) — ?token&from&to[&min=4][&drop=20] → 백그라운드, 결과 /api/swing/sim?key=sim/catalyst_replay/<from>_<to>."""
    if not _admin_ok(q):
        return {"error": "token"}
    from swing.tools import replay_catalyst
    kw = {}
    if q.get("min"):
        kw["min_strength"] = int(q["min"])
    if q.get("drop"):
        kw["max_drop"] = float(q["drop"])
    key = replay_catalyst.start_background(STORE, q.get("from") or "2026-09-01", q.get("to") or STORE.get("ledger", {}).get("asof"), **kw)
    return {"started": key, "progress": "/api/swing/sim?key=sim/catalyst_replay/progress"}


def api_progress(q):
    """실행 진행률(daily.Progress) — 없으면 None."""
    return STORE.get("progress")


def _top_cap_codes():
    """코스피 시총 상위 2종목(종목당 상한 10% 대상) — 최신 유니버스 문서의 시가총액으로 판정, 5분 캐시. 못 구하면 기본값."""
    import time as _t
    from swing import exposure
    if _t.time() - _top_cap_cache[0] < 300 and _top_cap_cache[1]:
        return _top_cap_cache[1]
    top = list(exposure.DEFAULT_TOP)
    try:
        ds = _universe_dates()
        if ds:
            top = exposure.top_cap_codes(STORE.get(f"universe/{ds[-1]}"), lambda c: _index_of(c) == "0001")
    except Exception as ex:
        print(f"[swing] 시총 상위 판정 실패(기본값 사용): {type(ex).__name__}: {str(ex)[:120]}", flush=True)
    _top_cap_cache[0], _top_cap_cache[1] = _t.time(), top
    return top


_top_cap_cache = [0.0, None]


def api_config(q):
    from swing import exposure
    return {"exposure": exposure.limits(_top_cap_codes()),
            "initialCapital": config.INITIAL_CAPITAL, "sellTax": config.SELL_TAX,
            "holdDays": config.HOLD_DAYS, "maxHoldDays": config.MAX_HOLD_DAYS, "maxWeight": config.MAX_WEIGHT,
            "defaultWeight": config.DEFAULT_WEIGHT, "maxNewPerDay": config.MAX_NEW_PER_DAY,
            "buySignals": config.BUY_SIGNALS, "sellSignals": config.SELL_SIGNALS,
            "buyRatings": config.BUY_RATINGS, "sellRatings": config.SELL_RATINGS,
            "shortEnabled": config.SHORT_ENABLED, "shortSignals": config.SHORT_SIGNALS,
            "maxNewShortPerDay": config.MAX_NEW_SHORT_PER_DAY, "shortMaxGross": config.SHORT_MAX_GROSS,
            "shortFutOnly": config.UNI_SHORT_FUT_ONLY,
            "borrowRate": config.BORROW_RATE,
            "shortCollateral": config.SHORT_COLLATERAL, "shortSubstRate": config.SHORT_SUBST_RATE, "shortMargin": config.SHORT_MARGIN,
            "universe": config.UNIVERSE_ENABLED, "uniNewPerDay": config.UNI_NEW_PER_DAY,
            "uniNewShortPerDay": config.UNI_NEW_SHORT_PER_DAY, "uniCandMin": config.UNI_CAND_MIN, "uniRankBasis": config.UNI_RANK_BASIS,
            "uniTotalMin": config.UNI_TOTAL_MIN,
            "uniCooldownDays": config.UNI_COOLDOWN_DAYS, "uniPickMode": config.UNI_PICK_MODE, "uniSectorCap": config.UNI_SECTOR_CAP, "reflectMax": config.REFLECT_MAX,
            "adminSimConfigured": bool((os.environ.get("SWING_ADMIN_TOKEN") or "").strip())}


def api_export(q):
    return {k: STORE.get(k) for k in STORE.keys("") if "/ticks/" not in k}   # VWAP 15초 틱(재생용)은 백업에서 제외


def api_intraday(q):
    """Live Trader(장중 집행기) 기록(PLAN_LIVE_TRADER.md) — 분할 체결·장중 손절·종목별 VWAP. date 없으면 최근 날짜."""
    dates = sorted({k.split("/")[1] for k in STORE.keys("intraday/") if k.count("/") == 1})
    date = q.get("date") or (dates[-1] if dates else None)
    return {"dates": dates, "date": date, "doc": STORE.get(f"intraday/{date}") if date else None}


def api_history(q):
    from swing import history
    return history.build(STORE, q.get("key") or "", day=q.get("day"))


def api_reflections(q):
    """매매 복기 — 끝난 포지션별 복기(swing/reflect.py). from/to = 청산일."""
    f, t = q.get("from"), q.get("to")
    out = []
    for k in sorted(STORE.keys("reflection/"), reverse=True):
        d = k.split("/", 1)[1]
        if (not f or d >= f) and (not t or d <= t):
            out.extend((STORE.get(k) or {}).get("items") or [])
    return out


ROUTES = {"/api/swing/summary": api_summary, "/api/swing/progress": api_progress, "/api/swing/positions": api_positions,
          "/api/swing/orders": api_orders, "/api/swing/queue": api_queue, "/api/swing/attribution": api_attribution, "/api/swing/trades": api_trades, "/api/swing/fills": api_fills,
          "/api/swing/equity": lambda q: _ledger()["equity"],
          "/api/swing/runs": api_runs, "/api/swing/decision": api_decision,
          "/api/swing/signals": api_signals, "/api/swing/universe": api_universe, "/api/swing/sim": api_sim,
          "/admin/sim": admin_sim, "/admin/catrep": admin_catrep, "/admin/dbq": admin_dbq, "/admin/universe": admin_universe,
          "/api/swing/universe/progress": api_universe_progress, "/api/swing/config": api_config,
          "/admin/new-only": admin_newonly, "/api/swing/newonly": api_newonly,
          "/api/swing/export": api_export, "/api/swing/intraday": api_intraday,
          "/api/swing/reflections": api_reflections, "/api/swing/history": api_history, "/api/swing/live": api_live,
          "/api/swing/benchmark": api_benchmark}


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
    if a.demo or a.rebuild_demo:                             # 데모는 DB증권 대신 mock 일봉(데모 원장과 같은 시세)
        from swing import history, prices, tradedays
        mock = prices.MockPrices()

        def bars(code, d1, d2):
            out = {}
            for d in tradedays.trading_days(d1, d2):
                b = mock.daily_bars([code], d)[code]
                out[d] = {"o": b["open"], "h": b["high"], "l": b["low"], "c": b["close"]}
            return out
        history.bars_fn = bars

        def idx(ic, d1, d2):                                 # 데모 지수 — 날짜별 결정적 등락(코스닥이 더 출렁)
            import hashlib
            out, v = {}, 2500.0 if ic == "0001" else 800.0
            for d in tradedays.trading_days("2026-01-02", d2):
                h = int(hashlib.md5((ic + d).encode()).hexdigest()[:6], 16) / 0xFFFFFF - 0.5
                v *= 1 + h * (0.02 if ic == "0001" else 0.03)
                if d >= d1:
                    out[d] = round(v, 2)
            return out
        global index_closes_fn
        index_closes_fn = idx
    port = int(os.environ.get("PORT", "8124"))
    print(f"[swing-web] :{port} store={type(STORE).__name__}" + (" (demo)" if a.demo or a.rebuild_demo else ""), flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
