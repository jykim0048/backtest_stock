"""유니버스 선정 방식 분산효과 시뮬레이션(2026-10-13) — A/B/C 세 방식을 같은 과거 데이터로 돌려 비교한다.

  A 업종 라운드로빈 : 상방(하방) 업종을 1위 점수 순으로 돌며 업종당 1개씩, 자리가 남으면 2바퀴째(업종별 Top10 풀)
  B 통합 순위       : 전 업종 통합 롱/숏 모멘텀 순위 상위 N (현재 운용 규칙)
  C 통합 + 업종 상한: B 와 같되 업종당 최대 SECTOR_CAP(기본 2)
공통: 점수 ≥ UNI_CAND_MIN, 하루 Long 10 · Short 10, 보유·쿨다운(3영업일) 제외, 다음 영업일 시가 진입, 손절 ±5%(갭이면 시가),
보유 5영업일째 종가 청산, 종목당 5%(총노출 100% 초과 시 1/n), 매도세 0.2%, 숏 대차수수료 연 4%. trading_agent 판단은 생략
(기계 규칙) — 세 방식의 **상대 비교**가 목적이다.

과거 재구성(as-of D): 섹터 신호 = store signals/<D> → swing/data/wb_history/<D>.json → main 주간 아카이브(주 단위, 주 안에서는
선견 편향) · 구성종목·시총 = 현재 스냅샷(선견 편향) · 종목 일별 거래대금·투자자 = DB증권 DAYSTOCKTJJ 기간 조회 ·
공매도·대차 = KIS 허브 /flow rows=60(≈3개월, D 이전 행만) · 체결가 = DB증권 CHARTDAY 기간 조회.

실행(네트워크 → Railway): swing-web `GET /admin/sim?token=<SWING_ADMIN_TOKEN>&from=2026-08-31&to=2026-10-10` 가 백그라운드로 돌려
store `sim/universe_abc/<from>_<to>` 에 저장(진행 `sim/progress`). 결과는 `GET /api/swing/sim?key=…`.
오프라인: `python -m swing.tools.sim_universe --from 2026-09-09 --to 2026-09-26 --mock`.
"""
import argparse
import datetime
import json
import math
import os
import sys
import time
import traceback

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from swing import config, signals, store as store_mod, tradedays, universe as U  # noqa: E402

METHODS = ("A", "B", "C")
METHOD_DESC = {"A": "업종 라운드로빈(업종당 1개씩 순환)", "B": "전 업종 통합 모멘텀 순위 상위 N", "C": "통합 순위 + 업종당 최대 2",
               # 2026-10-14 선정 기준 비교 — 모두 업종 라운드로빈, 기준 점수만 다름
               "T1": "라운드로빈 · Top10 풀(모멘텀) 그대로, 종합점수 ≥ totalMin·종합순",
               "T2": "라운드로빈 · 업종 Top10 자체를 종합점수로 선정, 종합 ≥ totalMin·종합순"}
TOTAL_METHODS = ("A", "T1", "T2")   # --methods total
# 유동성 반영 방식 비교(2026-10-14): 모두 T2(업종 Top·후보·순서 = 종합순, 종합 ≥ totalMin), 종합 계산만 다름
METHOD_DESC.update({"T2W": "T2 · 종합 = 0.50주가+0.40수급+0.10유동성(게이트 없음)",
                    "T2S": "T2 · 유동성 < 30 이면 종합 × 유동성/30(부드러운 게이트)"})
LIQ_METHODS = ("T2", "T2W", "T2S")    # --methods liq
# 순노출 상한 비교(2026-10-15): 모두 T2 선정 + Short 총노출 ≤ 30%(운용과 같음), 순노출(Long−Short) 상한만 다름.
# 노출은 종목당 명목 비중(weight 5%) × 보유·신규 수로 계산 — 선정 시 Short 먼저 채운 뒤 Long 을 순노출 한도까지.
METHOD_DESC.update({"N0": "T2 · Short ≤ 30% · 순노출 상한 없음", "N30": "T2 · Short ≤ 30% · 순노출 ≤ 30%",
                    "N10": "T2 · Short ≤ 30% · 순노출 ≤ 10%"})
NET_METHODS = ("N0", "N30", "N10")     # --methods net
NET_CAP = {"N0": None, "N30": 0.30, "N10": 0.10}
SHORT_CAP_NET = 0.30
LIQ_VARIANTS = {"byTotal": {}, "byTotalW": {"liq_mode": "weight", "w_px": 0.50, "w_flow": 0.40, "w_liq": 0.10},
                "byTotalS": {"liq_mode": "soft"}}
WB_HISTORY = os.path.join(_ROOT, "swing", "data", "wb_history")


def default_params(**kw):
    p = {"capLong": 10, "capShort": 10, "hold": 5, "stopPct": 0.05, "weight": 0.05, "sectorCapC": 2,
         "cooldown": config.UNI_COOLDOWN_DAYS, "poolPerSector": 40, "tax": config.SELL_TAX, "borrow": config.BORROW_RATE,
         "candMin": config.UNI_CAND_MIN, "topkTv": config.UNI_TOPK_TV, "topkCap": config.UNI_TOPK_CAP, "topN": config.UNI_TOPN,
         "minTvEok": config.UNI_MIN_TV_EOK, "totalMin": 50}
    p.update({k: v for k, v in kw.items() if v is not None})
    return p


# ─────────────────────────── 섹터 신호 as-of ───────────────────────────
class SignalSource:
    """날짜 → {norm 섹터명: {name, signal, chgPct…}} · 출처(store/history/weekly). 없으면 None."""

    def __init__(self, store=None, weekly_fetch=None, log=print):
        self.store, self.weekly_fetch, self.log = store, weekly_fetch, log
        self._weekly_index, self._weekly = None, {}

    def _rows(self, doc):
        rows = ((doc or {}).get("sectorFlow") or {}).get("rows") or []
        return {signals.norm_sec(r.get("name")): r for r in rows if r.get("name")} or None

    def get(self, date):
        if self.store is not None:
            d = self.store.get(f"signals/{date}")
            if d and self._rows(d):
                return self._rows(d), "store"
        p = os.path.join(WB_HISTORY, f"{date}.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                r = self._rows(json.load(f))
            if r:
                return r, "history"
        if self.weekly_fetch is not None:
            r = self._weekly_rows(date)
            if r:
                return r, "weekly"
        return None, None

    def _weekly_rows(self, date):
        if self._weekly_index is None:
            try:
                self._weekly_index = sorted(self.weekly_fetch("index") or [])
            except Exception as e:
                self.log(f"[sim] 주간 아카이브 index 실패: {e}")
                self._weekly_index = []
        keys = [k for k in self._weekly_index if k <= date]
        if not keys:
            return None
        k = keys[-1]
        if (datetime.date.fromisoformat(date) - datetime.date.fromisoformat(k)).days > 6:
            return None
        if k not in self._weekly:
            try:
                self._weekly[k] = self._rows(self.weekly_fetch(k))
            except Exception as e:
                self.log(f"[sim] 주간 아카이브 {k} 실패: {e}")
                self._weekly[k] = None
        return self._weekly[k]


def weekly_fetch_live(what):
    """main raw: index → 아카이브 날짜 목록, 날짜 → 그 주 weekly_briefing 문서."""
    import urllib.request
    base = f"{config.MAIN_RAW_BASE}/public/reports/weekly_briefing"
    url = f"{base}/index.json" if what == "index" else f"{base}/{what}.json"
    req = urllib.request.Request(url, headers={"User-Agent": "swing-sim"})
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))
    if what == "index":
        if isinstance(d, dict):
            d = d.get("dates") or d.get("items") or list(d.keys())
        return [x if isinstance(x, str) else (x.get("date") or x.get("week")) for x in d]
    return d


# ─────────────────────────── 데이터 수집(한 번) ───────────────────────────
def gather(p, dates, dbq, hub, sig, *, log=print, progress=None, store=None, sector_map=None):
    """구성종목(현재 스냅샷) · 종목 일별(DAYSTOCKTJJ 전 구간) · 공매도/대차(/flow) · 선물 상장 → dict."""
    sig_by_date, src_by_date = {}, {}
    for d in dates:
        rows, src = sig.get(d)
        sig_by_date[d], src_by_date[d] = rows, src
    wanted = set()
    for rows in sig_by_date.values():
        for k, r in (rows or {}).items():
            if U.direction_of(r.get("signal"), r.get("name")):
                wanted.add(k)
    diag = {"industries": [], "wanted": sorted(wanted), "unmatched": [], "errors": []}
    try:
        inds, how = U.sector_codes(dbq, store, dates[-1], sector_map=sector_map, log=log)
    except Exception as e:
        inds, how = [], "error"
        diag["errors"].append(f"업종 해석: {type(e).__name__}: {str(e)[:120]}")
    diag["sectorSource"] = how
    diag["industries"] = [f"{i['code']} {i['name']}" for i in inds]
    comps, names = {}, {}
    for ind in inds:
        k = signals.norm_sec(ind["name"])
        if k not in wanted:
            diag["unmatched"].append(ind["name"])
            continue
        try:
            rows = dbq.industry_components(ind["code"], "2")
        except Exception as e:
            log(f"[sim] {ind['name']} 구성종목 실패: {e}")
            diag["errors"].append(f"{ind['name']} USTOCKCONDLIST: {type(e).__name__}: {str(e)[:120]}")
            continue
        if rows and "componentsSample" not in diag:
            diag["componentsSample"] = rows[0]
            diag["componentsKeys"] = list(rows[0].keys()) if isinstance(rows[0], dict) else None
        xs = []
        for r in rows:
            code = str(r.get("Iscd") or "").strip().zfill(6)
            name = str(r.get("KorIsnm") or "").strip()
            if U.is_excluded(code, name):
                continue
            xs.append({"code": code, "name": name, "cap": (U._num(r.get("Avls"), 0.0) or 0.0) / config.UNI_CAP_DIV})
        xs.sort(key=lambda x: -x["cap"])
        comps[k] = xs[:p["poolPerSector"]]
        names[k] = ind["name"]
    codes = sorted({x["code"] for xs in comps.values() for x in xs})
    log(f"[sim] 업종 {len(comps)} · 풀 {len(codes)}종목 · 신호 출처 {dict((s, list(src_by_date.values()).count(s)) for s in set(src_by_date.values()))}")
    d1 = (datetime.date.fromisoformat(dates[0]) - datetime.timedelta(days=45)).isoformat()
    inv = {}
    for i, c in enumerate(codes):
        try:
            inv[c] = dbq.daily_investor(c, d1, dates[-1])
            if inv[c] and "investorSample" not in diag:
                diag["investorSample"] = inv[c][0]
        except Exception as e:
            log(f"[sim] {c} DAYSTOCKTJJ 실패: {e}")
            if len(diag["errors"]) < 10:
                diag["errors"].append(f"{c} DAYSTOCKTJJ: {type(e).__name__}: {str(e)[:120]}")
        if progress and i % 50 == 0:
            progress(f"일별 투자자 {i}/{len(codes)}")
    flows, errs = hub.many(codes)
    if errs:
        log(f"[sim] /flow 실패 {len(errs)}: {errs[:3]}")
        diag["errors"].extend(f"/flow {e}" for e in errs[:5])
    if flows and "flowSample" not in diag:
        fv = next(iter(flows.values()))
        diag["flowSample"] = {"shorts": (fv.get("shorts") or [])[:1], "loans": (fv.get("loans") or [])[:1]}
    try:
        fut = dbq.stock_future_underlyings()
    except Exception as e:
        fut = set()
        log(f"[sim] FCODES 실패: {e}")
    # 종가 이력(멀티기간 수익률용) — 풀 전 종목 CHARTDAY 1콜(≈15개월)
    px = {}
    px_from = (datetime.date.fromisoformat(dates[0]) - datetime.timedelta(days=int(config.UNI_PX_KEEP * 1.6))).isoformat()
    for i, c in enumerate(codes):
        try:
            rows = dbq.chart_day(c, px_from, dates[-1])
        except Exception as e:
            continue
        m = {}
        for r in rows:
            d, cl = U._iso(r.get("Date")), U._num(r.get("Prpr"))
            if len(d) == 10 and cl:
                m[d] = cl
        ds = sorted(m)
        px[c] = (ds, [m[d] for d in ds])
        if progress and i % 100 == 0:
            progress(f"종가 이력 {i}/{len(codes)}")
    diag["calls"] = {"db": getattr(dbq, "calls", None), "hub": getattr(hub, "calls", None)}
    return {"sig": sig_by_date, "src": src_by_date, "comps": comps, "names": names, "inv": inv, "flows": flows, "fut": fut, "diag": diag, "px": px}


def _ymd(s):
    return str(s or "").replace("-", "")[:8]


def _asof_flow(flow, date):
    """/flow 행(최신순) 중 date 이전(T+1 공표 가정) 행만, 최신순 유지. 허브 날짜는 YYYYMMDD(2026-10-14 실측) — ISO 와 섞이지 않게 정규화."""
    if not flow:
        return None
    d = _ymd(date)
    return {"shorts": [x for x in flow.get("shorts") or [] if _ymd(x.get("date")) < d],
            "loans": [x for x in flow.get("loans") or [] if _ymd(x.get("date")) < d]}


def build_day(p, date, data):
    """as-of date 유니버스: {sectors:[{name, signal, dir, top:[…]}], rankLong, rankShort} (universe.build_universe 와 같은 규칙)."""
    sig = data["sig"].get(date)
    if not sig:
        return None
    ymd = date.replace("-", "")
    secs = []
    for k, xs in data["comps"].items():
        r = sig.get(k)
        if not r:
            continue
        name = r.get("name") or data["names"][k]
        d = U.direction_of(r.get("signal"), name)
        if not d:
            continue
        rows_day = []
        for x in xs:
            rows = data["inv"].get(x["code"]) or []
            day = next((q for q in rows if str(q.get("Date") or "").strip() == ymd), None)
            if not day:
                continue
            rows_day.append({**x, "tvDay": U.day_tv(day) / config.UNI_TV_DIV, "_rows": rows})
        by_tv = sorted(rows_day, key=lambda x: -x["tvDay"])[:p["topkTv"]]
        by_cap = sorted(rows_day, key=lambda x: -x["cap"])[:p["topkCap"]]
        cut, seen = [], set()
        for x in by_tv + by_cap:
            if x["code"] not in seen:
                seen.add(x["code"])
                cut.append(x)
        scored = []
        for x in cut:
            m = U.weekly_metrics(x["_rows"], date, x["cap"], tv_div=config.UNI_TV_DIV, flow_div=config.UNI_FLOW_DIV)
            if not m or (m["tv20d"] or 0) < p["minTvEok"]:
                continue
            m.update(U.short_loan_metrics(_asof_flow(data["flows"].get(x["code"]), date), x["cap"]))
            pd_, pc_ = (data.get("px") or {}).get(x["code"]) or ([], [])
            m.update(U.period_returns(pd_, pc_, date))
            sc, hi, why = U.score_stock(m, d)
            scored.append({"code": x["code"], "name": x["name"], "cap": x["cap"], **m, "score": sc, "shortHigh": hi, "reason": why,
                           "hasStockFuture": x["code"] in data["fut"]})
        scored.sort(key=lambda x: (-x["score"], -(x["tv1w"] or 0)))
        top = scored[:p["topN"]]
        for i, x in enumerate(top):
            x["rank"] = i + 1
        secs.append({"name": name, "signal": r.get("signal"), "dir": d, "top": top, "_all": scored})
    # 종합점수 기준(T2 계열): 업종의 1차 컷 통과 전 종목을 같은 방향 풀 백분위로 채점 → 종합순 Top N.
    # 변형마다 종합 계산(유동성 반영 방식)만 다르다 — 시뮬 비교라 운용 노브(UNI_LIQ_MODE)와 무관하게 명시
    out = {}
    for vk, kw in LIQ_VARIANTS.items():
        full = [{"name": x["name"], "signal": x["signal"], "dir": x["dir"],
                 "top": [dict(y) for y in x["_all"]]} for x in secs]
        U.annotate_composite(full, **dict({"liq_mode": "gate", "w_px": 0.55, "w_flow": 0.45}, **kw))
        for x in full:
            x["top"].sort(key=lambda y: (y.get("totalScore") is None, -(y.get("totalScore") or 0), -(y.get("tv1w") or 0)))
            x["top"] = x["top"][:p["topN"]]
            for i, y in enumerate(x["top"]):
                y["rank"] = i + 1
        out[vk] = {"sectors": full}
    for x in secs:
        x.pop("_all", None)
    U.annotate_composite(secs, liq_mode="gate", w_px=0.55, w_flow=0.45)
    return dict({"sectors": secs, "rankLong": U.integrated_rank(secs, "long", 200),
                 "rankShort": U.integrated_rank(secs, "short", 200)}, **out)


# ─────────────────────────── 선정 방식 ───────────────────────────
def _ok(x, p, held, cool):
    return x.get("score") is not None and x["score"] >= p["candMin"] and x["code"] not in held and x["code"] not in cool


def _sel(mode):
    def f(day, side, p, held, cool):
        cap = p["capLong"] if side == "long" else p["capShort"]
        return U.pick(day, side, mode, lambda x: _ok(x, p, held, cool), cap=cap, sector_cap=p["sectorCapC"])
    return f


select_A, select_B, select_C = _sel("roundrobin"), _sel("rank"), _sel("sectorcap")


def _rr_total(use_total_pool, pool_key="byTotal"):
    """종합점수 라운드로빈 — 업종 순서 = 첫 적격 종목 종합점수, 업종 안 종합순, 업종당 1개씩 돌고 남으면 2바퀴째."""
    def f(day, side, p, held, cool):
        cap = p["capLong"] if side == "long" else p["capShort"]
        secs = (day.get(pool_key) or {}).get("sectors") if use_total_pool else day.get("sectors")
        lists = []
        for sct in secs or []:
            if sct.get("dir") != side:
                continue
            xs = [dict(x, sector=sct.get("name"), signal=sct.get("signal")) for x in (sct.get("top") or [])
                  if x.get("totalScore") is not None and x["totalScore"] >= p["totalMin"]
                  and x["code"] not in held and x["code"] not in cool]
            xs.sort(key=lambda x: -x["totalScore"])
            if xs:
                lists.append(xs)
        lists.sort(key=lambda xs: -xs[0]["totalScore"])
        out, i = [], 0
        while any(len(xs) > i for xs in lists) and len(out) < cap:
            for xs in lists:
                if len(xs) > i and len(out) < cap:
                    out.append(xs[i])
            i += 1
        return out
    return f


SELECT = {"A": select_A, "B": select_B, "C": select_C,   # = universe.pick 의 roundrobin / rank / sectorcap
          "T1": _rr_total(False), "T2": _rr_total(True),
          "T2W": _rr_total(True, "byTotalW"), "T2S": _rr_total(True, "byTotalS"),
          "N0": _rr_total(True), "N30": _rr_total(True), "N10": _rr_total(True)}


# ─────────────────────────── 포트폴리오 시뮬 ───────────────────────────
def _bar(prices, code, date):
    return (prices.get(code) or {}).get(date)


def simulate(p, dates, days, prices, method, *, nxt_of, log=print):
    """하루 단위: 전일 선정 → 오늘 시가 진입 → 손절/만기 → 일별 수익. prices[code][date] = {o,h,l,c}."""
    positions, pending, cool, trades = [], [], {}, []
    eq, series, hold_n = 1.0, [], p["hold"]
    long_cum, short_cum = 0.0, 0.0
    all_days = list(dates)
    # 만기 청산용으로 to 이후 영업일 5개 추가
    extra = []
    d = dates[-1]
    for _ in range(hold_n + 1):
        d = nxt_of(d)
        extra.append(d)
    for t in all_days + extra:
        day_ret, contrib = 0.0, {"long": 0.0, "short": 0.0}
        # ① 전일 선정분 진입(시가)
        new_pos = []
        for sel in pending:
            b = _bar(prices, sel["code"], t)
            if not b or not b.get("o"):
                continue
            dirn = 1 if sel["side"] == "long" else -1
            new_pos.append({"code": sel["code"], "name": sel["name"], "sector": sel["sector"], "side": sel["side"],
                            "dir": dirn, "entry": b["o"], "stop": b["o"] * (1 - p["stopPct"]) if dirn > 0 else b["o"] * (1 + p["stopPct"]),
                            "entryDate": t, "holdDay": 0, "prev": b["o"], "score": sel.get("score")})
        pending = []
        positions.extend(new_pos)
        n_open = len(positions)
        w = min(p["weight"], 1.0 / n_open) if n_open else 0.0
        # ② 손절·만기·일별 수익
        keep = []
        for ps in positions:
            b = _bar(prices, ps["code"], t)
            if not b:
                keep.append(ps)
                continue
            ps["holdDay"] += 1
            exit_px, reason = None, None
            if ps["dir"] > 0:
                if b["o"] <= ps["stop"]:
                    exit_px, reason = b["o"], "stop_gap"
                elif b["l"] <= ps["stop"]:
                    exit_px, reason = ps["stop"], "stop"
            else:
                if b["o"] >= ps["stop"]:
                    exit_px, reason = b["o"], "stop_gap"
                elif b["h"] >= ps["stop"]:
                    exit_px, reason = ps["stop"], "stop"
            if exit_px is None and ps["holdDay"] >= hold_n:
                exit_px, reason = b["c"], "expiry"
            end = exit_px if exit_px is not None else b["c"]
            r = (end / ps["prev"] - 1) * ps["dir"]
            cost = 0.0
            if ps["side"] == "short":
                cost += p["borrow"] / 365
                if ps["holdDay"] == 1:
                    cost += p["tax"]
            elif exit_px is not None:
                cost += p["tax"]
            day_ret += w * (r - cost)
            contrib[ps["side"]] += w * (r - cost)
            ps["prev"] = end
            if exit_px is not None:
                ret = (exit_px / ps["entry"] - 1) * ps["dir"] - p["tax"] - (p["borrow"] / 365 * ps["holdDay"] if ps["side"] == "short" else 0)
                trades.append({"code": ps["code"], "name": ps["name"], "sector": ps["sector"], "side": ps["side"],
                               "entryDate": ps["entryDate"], "exitDate": t, "entry": ps["entry"], "exit": exit_px,
                               "reason": reason, "holdDays": ps["holdDay"], "retPct": round(ret * 100, 2), "score": ps.get("score")})
            else:
                keep.append(ps)
        positions = keep
        eq *= 1 + day_ret
        long_cum += contrib["long"]
        short_cum += contrib["short"]
        # ③ 오늘 선정(to 안에서만) → 내일 진입
        sel_today = []
        if t in days and days[t]:
            held = {ps["code"] for ps in positions} | {s["code"] for s in pending}
            cool_set = {c for c, until in cool.items() if until >= t}
            gated = method in NET_CAP
            w0 = p["weight"]
            lexp = w0 * sum(1 for ps in positions if ps["side"] == "long")
            sexp = w0 * sum(1 for ps in positions if ps["side"] == "short")
            for side in (("short", "long") if gated else ("long", "short")):      # 게이트면 Short 먼저 → Long 여유 확보
                xs = SELECT[method](days[t], side, p, held, cool_set)
                for x in xs:
                    if gated and side == "short":
                        if sexp + w0 > SHORT_CAP_NET + 1e-9:
                            continue
                        sexp += w0
                    elif gated and NET_CAP[method] is not None:
                        if (lexp + w0) - sexp > NET_CAP[method] + 1e-9:
                            continue
                        lexp += w0
                    elif gated:
                        lexp += w0
                    sel_today.append({"code": x["code"], "name": x["name"], "sector": x.get("sector"), "side": side, "score": x["score"]})
                    held.add(x["code"])
            pending = sel_today
            cd = t
            for _ in range(p["cooldown"]):
                cd = nxt_of(cd)
            for s in sel_today:
                cool[s["code"]] = cd
        # 분산 지표
        secs = {}
        for ps in positions:
            secs[ps["sector"]] = secs.get(ps["sector"], 0) + 1
        n = len(positions)
        hhi = sum((v / n) ** 2 for v in secs.values()) if n else None
        series.append({"date": t, "equity": round(eq, 6), "ret": round(day_ret, 6), "open": n, "sectors": len(secs),
                       "hhi": None if hhi is None else round(hhi, 4), "gross": round(w * n, 3),
                       "long": sum(1 for ps in positions if ps["side"] == "long"), "short": n - sum(1 for ps in positions if ps["side"] == "long"),
                       "selected": len(sel_today), "longCum": round(long_cum, 6), "shortCum": round(short_cum, 6),
                       "longExp": round(w * sum(1 for ps in positions if ps["side"] == "long"), 3),
                       "shortExp": round(w * sum(1 for ps in positions if ps["side"] == "short"), 3),
                       "netExp": round(w * (2 * sum(1 for ps in positions if ps["side"] == "long") - n), 3)})
    return series, trades


def pairwise_corr(series_sel, prices, trades, dates):
    """보유 종목 간 일별 수익률 상관(평균) — 전 거래의 종목 집합 기준 간단 근사: 종목별 일별 종가 수익률을 창 전체에서 구해
    같은 날 겹쳐 보유한 종목 쌍의 상관 평균."""
    rets = {}
    for code in {t["code"] for t in trades}:
        px = prices.get(code) or {}
        ds = [d for d in dates if d in px and px[d].get("c")]
        rets[code] = {ds[i]: px[ds[i]]["c"] / px[ds[i - 1]]["c"] - 1 for i in range(1, len(ds)) if px[ds[i - 1]]["c"]}
    pairs, seen = [], set()
    ts = sorted(trades, key=lambda t: t["entryDate"])
    for i, a in enumerate(ts):
        for b_ in ts[i + 1:]:
            if b_["entryDate"] > a["exitDate"] or a["code"] == b_["code"]:
                continue
            key = tuple(sorted((a["code"], b_["code"])))
            if key in seen:
                continue
            seen.add(key)
            ra, rb = rets.get(a["code"], {}), rets.get(b_["code"], {})
            common = [d for d in ra if d in rb]
            if len(common) < 10:
                continue
            xa, xb = [ra[d] for d in common], [rb[d] for d in common]
            ma, mb = sum(xa) / len(xa), sum(xb) / len(xb)
            cov = sum((x - ma) * (y - mb) for x, y in zip(xa, xb))
            va, vb = sum((x - ma) ** 2 for x in xa), sum((y - mb) ** 2 for y in xb)
            if va > 0 and vb > 0:
                pairs.append(cov / math.sqrt(va * vb))
    return round(sum(pairs) / len(pairs), 3) if pairs else None, len(pairs)


def metrics(series, trades, corr):
    rets = [s["ret"] for s in series]
    n = len(rets)
    eqs = [s["equity"] for s in series]
    peak, mdd = 1.0, 0.0
    for e in eqs:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    mean = sum(rets) / n if n else 0.0
    sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (n - 1)) if n > 1 else 0.0
    wins = [t for t in trades if t["retPct"] > 0]
    active = [s for s in series if s["open"]]

    def avg(key):
        xs = [s[key] for s in active if s.get(key) is not None]
        return round(sum(xs) / len(xs), 3) if xs else None
    return {"days": n, "cumRetPct": round((eqs[-1] - 1) * 100, 2) if eqs else 0.0,
            "annVolPct": round(sd * math.sqrt(252) * 100, 2), "sharpe": round(mean / sd * math.sqrt(252), 2) if sd else None,
            "mddPct": round(mdd * 100, 2), "trades": len(trades), "winRatePct": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "avgTradeRetPct": round(sum(t["retPct"] for t in trades) / len(trades), 2) if trades else None,
            "longCumPct": round(series[-1]["longCum"] * 100, 2) if series else 0.0,
            "shortCumPct": round(series[-1]["shortCum"] * 100, 2) if series else 0.0,
            "avgOpen": avg("open"), "avgSectors": avg("sectors"), "avgHHI": avg("hhi"), "avgGross": avg("gross"),
            "avgLongExp": avg("longExp"), "avgShortExp": avg("shortExp"), "avgNetExp": avg("netExp"),
            "pairCorr": corr[0], "pairs": corr[1],
            "byReason": {r: sum(1 for t in trades if t["reason"] == r) for r in ("expiry", "stop", "stop_gap")},
            "bySector": _by_sector(trades)}


def _by_sector(trades):
    out = {}
    for t in trades:
        o = out.setdefault(t["sector"] or "-", {"n": 0, "retSum": 0.0})
        o["n"] += 1
        o["retSum"] += t["retPct"]
    return {k: {"n": v["n"], "avgRetPct": round(v["retSum"] / v["n"], 2)} for k, v in sorted(out.items(), key=lambda kv: -kv[1]["n"])}


# ─────────────────────────── 실행 ───────────────────────────
def run(p, date_from, date_to, *, dbq, hub, sig, prices_fetch, store=None, log=print, progress=None, key=None, sector_map=None,
        methods=METHODS):
    t0 = time.time()
    prog = progress or (lambda msg: None)
    dates = tradedays.trading_days(date_from, date_to)
    if not dates:
        raise ValueError("영업일 없음")
    prog("수집")
    data = gather(p, dates, dbq, hub, sig, log=log, progress=prog, store=store, sector_map=sector_map)
    days = {}
    for i, d in enumerate(dates):
        days[d] = build_day(p, d, data)
        if i % 10 == 0:
            prog(f"유니버스 {i}/{len(dates)}")
    nxt_of = tradedays.next_trading_day
    # 선정(가격 없이) → 필요한 종목만 시세
    needed = set()
    for d in dates:
        if not days[d]:
            continue
        for m in methods:
            for side in ("long", "short"):
                for x in SELECT[m](days[d], side, p, set(), set())[: (p["capLong"] + p["capShort"]) * 2]:
                    needed.add(x["code"])
        for x in days[d]["rankLong"][:60] + days[d]["rankShort"][:60]:
            needed.add(x["code"])
    prog(f"시세 {len(needed)}종목")
    px_from = (datetime.date.fromisoformat(date_from) - datetime.timedelta(days=35)).isoformat()
    px_to = date_to
    for _ in range(p["hold"] + 1):
        px_to = nxt_of(px_to)
    prices = prices_fetch(sorted(needed), px_from, px_to)
    results = {}
    for m in methods:
        prog(f"시뮬 {m}")
        series, trades = simulate(p, dates, days, prices, m, nxt_of=nxt_of, log=log)
        corr = pairwise_corr(series, prices, trades, tradedays.trading_days(px_from, px_to))
        results[m] = {"desc": METHOD_DESC[m], "metrics": metrics(series, trades, corr), "series": series, "trades": trades}
    doc = {"key": key, "from": date_from, "to": date_to, "params": p, "asof": datetime.datetime.now().isoformat(timespec="seconds"),
           "secs": round(time.time() - t0, 1), "signalSource": {d: data["src"][d] for d in dates},
           "coverage": {"days": len(dates), "daysWithUniverse": sum(1 for d in dates if days[d]),
                        "pool": len(data["inv"]), "sectors": len(data["comps"]), "priced": len(prices)},
           "diag": data.get("diag"),
           "note": ("구성종목·시총은 현재 스냅샷, 주간 아카이브 신호는 주 단위(선견 편향 가능), 체결은 기계 규칙(에이전트 생략) — "
                    "세 방식의 상대 비교용"),
           "methods": results}
    if store is not None and key:
        store.put(key, doc)
    log(summary_text(doc))
    return doc


def summary_text(doc):
    rows = [("누적수익률 %", "cumRetPct"), ("연변동성 %", "annVolPct"), ("Sharpe", "sharpe"), ("MDD %", "mddPct"),
            ("거래 수", "trades"), ("승률 %", "winRatePct"), ("평균 거래수익 %", "avgTradeRetPct"), ("Long 기여 %", "longCumPct"),
            ("Short 기여 %", "shortCumPct"), ("평균 보유 종목", "avgOpen"), ("평균 보유 업종", "avgSectors"),
            ("업종 집중도 HHI", "avgHHI"), ("평균 총노출", "avgGross"), ("평균 Long 노출", "avgLongExp"),
            ("평균 Short 노출", "avgShortExp"), ("평균 순노출", "avgNetExp"), ("보유 종목간 상관", "pairCorr")]
    ms = doc["methods"]
    mk = list(ms)
    out = [f"[sim] {doc['from']}~{doc['to']} 영업일 {doc['coverage']['days']}(유니버스 {doc['coverage']['daysWithUniverse']}) · "
           f"풀 {doc['coverage']['pool']} · {doc['secs']}s", "{:<16}".format("지표") + "".join("{:>14}".format(m) for m in mk)]
    for lbl, k in rows:
        out.append("{:<16}".format(lbl) + "".join("{:>14}".format("-" if ms[m]["metrics"][k] is None else ms[m]["metrics"][k]) for m in mk))
    return "\n".join(out)


def chart_prices_live(dbq):
    def fetch(codes, d1, d2):
        out = {}
        for c in codes:
            try:
                rows = dbq.chart_day(c, d1, d2)
            except Exception as e:
                dbq.log(f"[sim] {c} CHARTDAY 실패: {e}")
                continue
            px = {}
            for r in rows:
                d = str(r.get("Date") or "").strip()
                if len(d) == 8:
                    o, h, l, cl = (U._num(r.get(k)) for k in ("Oprc", "Hprc", "Lprc", "Prpr"))
                    if cl:
                        px[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = {"o": o or cl, "h": h or cl, "l": l or cl, "c": cl}
            out[c] = px
        return out
    return fetch


def start_background(store, date_from, date_to, **kw):
    """swing-web /admin/sim — 스레드로 실행, 진행은 sim/progress."""
    import threading
    key = f"sim/universe_abc/{date_from}_{date_to}"

    def prog(msg):
        store.put("sim/progress", {"key": key, "status": "running", "step": msg, "at": datetime.datetime.now().isoformat(timespec="seconds")})

    def job():
        try:
            dbq = U.DbQuote()
            hub = U.HubFlow(rows=60)
            run(default_params(**kw), date_from, date_to, dbq=dbq, hub=hub, sig=SignalSource(store, weekly_fetch_live),
                prices_fetch=chart_prices_live(dbq), store=store, progress=prog, key=key)
            store.put("sim/progress", {"key": key, "status": "done", "at": datetime.datetime.now().isoformat(timespec="seconds")})
        except Exception as e:
            traceback.print_exc()
            store.put("sim/progress", {"key": key, "status": "failed", "error": f"{type(e).__name__}: {e}"[:300],
                                       "at": datetime.datetime.now().isoformat(timespec="seconds")})
    threading.Thread(target=job, daemon=True, name="swing-sim").start()
    return key


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start", required=True)
    ap.add_argument("--to", dest="end", required=True)
    ap.add_argument("--mock", action="store_true", help="오프라인 mock(DB증권·허브·시세 가짜)")
    ap.add_argument("--store", default="none", help="none | env | file:<dir>")
    ap.add_argument("--out", help="결과 JSON 파일")
    ap.add_argument("--cap", type=int, default=10)
    ap.add_argument("--methods", default="abc", help="abc = A/B/C 선정 방식 · total = A vs 종합점수 T1/T2 · liq = 유동성 반영 T2/T2W/T2S"
                                                        " · net = 순노출 상한 N0/N30/N10")
    ap.add_argument("--total-min", type=float, default=50)
    a = ap.parse_args(argv)
    methods = {"total": TOTAL_METHODS, "liq": LIQ_METHODS, "net": NET_METHODS}.get(a.methods, METHODS)
    kp = {"total": "sim/universe_total", "liq": "sim/universe_liq", "net": "sim/universe_net"}.get(a.methods, "sim/universe_abc")
    st = None if a.store == "none" else (store_mod.from_env() if a.store == "env" else store_mod.FileStore(a.store[5:]))
    p = default_params(capLong=a.cap, capShort=a.cap, totalMin=a.total_min)
    if a.mock:
        from swing import universe_mock as M
        dbq, hub = M.MockDbQuote(), M.MockHubFlow(d0=a.end, n=90)
        doc = run(p, a.start, a.end, dbq=dbq, hub=hub, sig=SignalSource(st, None), prices_fetch=M.mock_chart_prices(dbq),
                  store=st, key=f"{kp}/{a.start}_{a.end}", sector_map=M.mock_sector_map(), methods=methods)
    else:
        dbq, hub = U.DbQuote(), U.HubFlow(rows=60)
        doc = run(p, a.start, a.end, dbq=dbq, hub=hub, sig=SignalSource(st, weekly_fetch_live), prices_fetch=chart_prices_live(dbq),
                  store=st, key=f"{kp}/{a.start}_{a.end}", methods=methods)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
