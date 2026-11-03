"""트레이딩 히스토리(2026-11-03) — 판단 원문 팝업의 카드. 포지션 1회차의 기록을 한 줄 시간순으로 모은다.

흩어진 기록을 읽기만 한다(쓰기 없음): 원장 orders·orderLog·positions·trades, run/<날짜>.reviews, intraday/<날짜>(분할 체결).
회차 = 같은 진입 판단(decisionKey). 재판별 판단으로 열면 그 날짜가 들어 있는 회차를 찾는다.
일봉 = DB증권 CHARTDAY, 실패하면 회차의 판단 문서 차트(chart.py), 그것도 없으면 그래프의 주가 칸만 비운다.
분봉(2026-11-03) = Live Trader 가 저장한 15초 틱(intraday/<날짜>/ticks/<종목>)을 묶는다. 외부 호출 없음.
카드는 보유 일수에 따라 5~60분봉(봉 120개 이하), 회차 전 기간에 틱이 있을 때만. 날짜별 5분봉은 day 로 따로 조회.
"""
import time

from . import config, engine, tradedays
from .reflect import REASON

MODE = {"open": "시가", "vwap": "VWAP", "force": "전량", "stop": "손절", "stop_gap": "갭 손절", "livepm": "지시",
        "swA": "스위칭 A", "swB": "스위칭 B", "swC": "스위칭 C", "swD": "스위칭 D"}
STOP_REASONS = ("stop", "stop_gap")
STOP_SLIP = 0.03                                          # 손절 체결이 손절가보다 이만큼 불리하면 '계획과 다른 체결'
BAR_TTL = 600
_bars_cache = {}


def _db_bars(code, d1, d2):
    from . import universe as U
    return {_iso(r.get("Date")): {"o": U._num(r.get("Oprc")), "h": U._num(r.get("Hprc")), "l": U._num(r.get("Lprc")), "c": U._num(r.get("Prpr"))}
            for r in U.DbQuote().chart_day(code, d1, d2)}


bars_fn = _db_bars                                        # 데모 서버는 mock 시세로 바꾼다(server.main)

SESSION = ("09:00", "15:30")
STEPS = ((1, 5), (2, 10), (4, 15), (9, 30), (17, 60))     # 보유 일수 ≤ n → 분봉 간격. 그보다 길면 일봉만


def step_for(n):
    return next((s for k, s in STEPS if n <= k), None)


def _ticks(store, day, code):
    return store.get(f"intraday/{day}/ticks/{code}") or []


ticks_fn = _ticks


def live_date(store, asof):
    """정산 전 장중 기록 날짜(intraday 날짜 > 원장 asof) — 없으면 None."""
    day = max((k.split("/")[1] for k in store.keys("intraday/") if k.count("/") == 1), default=None)
    return day if day and (not asof or day > asof) else None


def live_prices(store, asof):
    """장중 현재가(2026-11-03) — 정산 전(intraday 날짜 > 원장 asof) Live Trader 기록의 종목별 마지막 시세 {code: {price, ts}}.
    15초 폴링값이라 장 마감 뒤 정산(16:30) 전까지는 15:2x 가격(종가 동시호가 전)이 남는다. 정산되면 원장 종가로 돌아간다."""
    day = live_date(store, asof)
    if not day:
        return {}
    return {c: {"price": v["price"], "ts": v.get("ts")} for c, v in ((store.get(f"intraday/{day}") or {}).get("vwap") or {}).items() if v.get("price")}


def _mins(hm):
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def minute_bars(ticks, step, day_bar=None):
    """15초 틱 → step 분봉 [{t, o, h, l, c, vw}]. 틱은 표본이라 고저가 실제보다 좁을 수 있고 동시호가가 빠진다 —
    그날 일봉(KRX 확정)이 있으면 첫 봉 시가·마지막 봉 종가를 그 값으로 덮는다. vw = 그 봉 끝의 당일 누적 VWAP."""
    t0, t1 = _mins(SESSION[0]), _mins(SESSION[1])
    out = {}
    for x in sorted(ticks, key=lambda x: x["ts"]):
        m, px = _mins(x["ts"][11:16]), x.get("price")
        if not px or not t0 <= m <= t1:
            continue
        k = t0 + (min(m, t1 - 1) - t0) // step * step
        b = out.get(k)
        if b is None:
            b = out[k] = {"t": f"{k // 60:02d}:{k % 60:02d}", "o": px, "h": px, "l": px, "c": px}
        else:
            b["h"], b["l"], b["c"] = max(b["h"], px), min(b["l"], px), px
        if x.get("value") and x.get("volume"):
            b["vw"] = round(x["value"] / x["volume"], 1)
    bars = [out[k] for k in sorted(out)]
    if bars and day_bar:
        bars[0]["o"], bars[-1]["c"] = day_bar["o"], day_bar["c"]
        for b in (bars[0], bars[-1]):
            b["h"], b["l"] = max(b["h"], b["o"], b["c"]), min(b["l"], b["o"], b["c"])
    return bars


def _iso(d):
    d = str(d or "").strip()
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 and d.isdigit() else d


def _chart_bars(doc):
    c = (doc or {}).get("chart") or {}
    return {_iso(d): {"o": o, "h": h, "l": l, "c": x} for d, o, h, l, x in zip(c.get("dates") or [], c.get("o") or [], c.get("h") or [], c.get("l") or [], c.get("c") or [])
            if None not in (o, h, l, x)}


def _bars(code, d1, d2, fallback_docs, log=print):
    k = (code, d1, d2)
    hit = _bars_cache.get(k)
    if hit and time.time() - hit[0] < BAR_TTL:
        return hit[1]
    try:
        out = bars_fn(code, d1, d2) or {}
    except Exception as ex:
        log(f"[swing] 히스토리 일봉 실패 {code}: {type(ex).__name__}: {str(ex)[:120]}")
        out = {}
    if not out:
        for doc in fallback_docs:
            out = {**out, **_chart_bars(doc)}
    else:
        _bars_cache[k] = (time.time(), out)
    return {d: b for d, b in out.items() if d1 <= d <= d2 and None not in b.values()}


def _parse_key(key):
    p = (key or "").split("/")
    if len(p) != 4 or p[0] != "decision":
        return None
    return p[1], p[2], "short" if p[3].endswith("-short") else "long"


def _rounds(L, code, side):
    """decisionKey → {orders, trades, position}."""
    out = {}

    def at(k):
        return out.setdefault(k, {"key": k, "orders": [], "trades": [], "position": None})
    for o in (L.get("orderLog") or []) + (L.get("orders") or []):
        if o["code"] == code and (o.get("side") or "long") == side and o.get("type", "entry") == "entry":
            at(o.get("decisionKey"))["orders"].append(o)
    for t in L.get("trades") or []:
        if t["code"] == code and (t.get("side") or "long") == side:
            at(t.get("decisionKey"))["trades"].append(t)
    for p in L.get("positions") or []:
        if p["code"] == code and (p.get("side") or "long") == side:
            at(p.get("decisionKey"))["position"] = p
    return out


def _span(r):
    fills = [o["validDate"] for o in r["orders"] if o.get("status") == "filled"]
    first = (r["position"] or {}).get("fillDate") or (r["trades"][0]["entryDate"] if r["trades"] else None) or (min(fills) if fills else None)
    last = None if r["position"] else (r["trades"][-1]["exitDate"] if r["trades"] else None)
    return first, last


def _locate(rounds, key, date):
    if key in rounds:
        return rounds[key]
    best = None
    for r in rounds.values():
        a, b = _span(r)
        if a and a <= date and (b is None or date <= b) and (best is None or a > _span(best)[0]):
            best = r
    return best


def _splits(fills):
    """분할 체결 요약 — 09:00~15:14 · VWAP 23회 57,600 · 손절 23주 @56,000."""
    if not fills:
        return None
    by = {}
    for f in fills:
        by.setdefault(f.get("mode") or "?", []).append(f)
    parts = []
    for m, xs in by.items():
        q = sum(x["qty"] for x in xs)
        avg = sum(x["qty"] * x["price"] for x in xs) / q if q else 0
        parts.append(f"{MODE.get(m, m)} {len(xs)}회 {q:,}주 @{avg:,.0f}")
    t = [f["ts"][11:16] for f in fills]
    return f"{t[0]}~{t[-1]} · " + " · ".join(parts)


def _avg(fills):
    q = sum(f["qty"] for f in fills)
    return q, (sum(f["qty"] * f["price"] for f in fills) / q if q else None)


def build(store, key, *, day=None, log=print):
    """판단 키 → 카드 데이터. 그 판단으로 주문·체결이 없으면 status none. day = 그날 5분봉·체결(dayView)도 붙인다."""
    parsed = _parse_key(key)
    if not parsed:
        return {"status": "none"}
    date, code, side = parsed
    L = store.get("ledger") or {}
    r = _locate(_rounds(L, code, side), key, date)
    if not r or not (r["orders"] or r["trades"] or r["position"]):
        return {"status": "none"}
    short, sign = side == "short", -1 if side == "short" else 1
    asof = L.get("asof")
    entry_doc = store.get(r["key"]) if r.get("key") else None
    orders = sorted(r["orders"], key=lambda o: (o.get("validDate") or "", o.get("created") or ""))
    plan = orders[0] if orders else {}
    days_doc, ev, fills = {}, [], {}

    def intra(d):
        if d not in days_doc:
            days_doc[d] = store.get(f"intraday/{d}") or {}
        return days_doc[d]

    def take(fs, sgn):                                    # 분할 체결 → 차트 점. q 는 보유 증감(+늘림 / −줄임)
        for f in fs or []:
            if f.get("ts") and f.get("qty"):
                fills[(f["ts"], f["price"], f["qty"], f.get("mode"))] = {"ts": f["ts"], "q": sgn * f["qty"], "px": f["price"], "mode": f.get("mode")}

    if orders:
        o = plan
        rating = (entry_doc or {}).get("rating") or o.get("rating") or "—"
        planned = (o.get("partial") or {}).get("planned") or o.get("qty") or 0
        ev.append({"date": o.get("created"), "rank": 0, "label": "판단", "cls": "plan", "qty": 0,
                   "text": f"PM {rating} · 계획 {planned:,}주({(o.get('weight') or 0) * 100:.1f}%)",
                   "sub": f"진입가 상한 {o.get('entry') or 0:,.0f} · 손절 {o.get('stop') or 0:,.0f}" + (f" · 목표 {o['target']:,.0f}" if o.get("target") else ""),
                   "decisionKey": r["key"]})
    buy_word = "공매도" if short else "매수"
    wait = []                                             # 이어진 비중 채우기 미체결(2026-11-03) — 한 줄로 묶는다

    def flush():
        if wait:
            a, b = wait[0], wait[-1]
            ev.append({"date": a.get("validDate"), "rank": 1, "label": "비중 채우기 대기", "cls": "plan", "qty": 0, "kind": "wait",
                       "text": f"{len(wait)}일 미체결" + (f" · {a['validDate'][5:]}~{b['validDate'][5:]}" if len(wait) > 1 else ""),
                       "sub": f"잔여 {b.get('qty') or 0:,}주 · 진입가 상한 {b.get('entry') or 0:,.0f} 너머 — 내려오면 체결"})
            wait.clear()
    for n, o in enumerate(orders):
        v, st = o.get("validDate"), o.get("status")
        rec = (intra(v).get("orders") or {}).get(o["id"]) or {}
        take(rec.get("fills"), 1)
        if st == "cancelled" and o.get("topupOf"):
            wait.append(o)
            continue
        flush()
        if st == "filled":
            px = o.get("fillPrice") or o.get("entry")
            over = sign * (px / o["entry"] - 1) if o.get("entry") else 0
            pt = o.get("partial") or {}
            warn = over > 0.0005
            sub = (f"계획보다 {over * 100:.1f}% 불리 · " if warn else "") + (f"잔여 {pt['remain']:,}주 다음 영업일로" if pt.get("remain") else (o.get("note") or ""))
            ev.append({"date": v, "rank": 1, "label": ("추가 " if o.get("topupOf") else "") + buy_word, "cls": "buy", "qty": o["qty"], "price": px,
                       "text": f"{o['qty']:,}주 체결" + (f" / 계획 {pt['planned']:,}주" if pt.get("planned") else ""), "sub": sub,
                       "warn": warn, "more": _splits(rec.get("fills"))})
        elif st in ("cancelled", "skipped"):
            ev.append({"date": v, "rank": 1, "label": "미체결", "cls": "plan", "qty": 0, "text": "미도달 취소" if st == "cancelled" else "스킵", "sub": o.get("note") or ""})
        elif rec.get("fills"):                            # 오늘 장중 체결(정산 전)
            q, px = _avg(rec["fills"])
            ev.append({"date": v, "rank": 1, "label": buy_word, "cls": "buy", "qty": q, "price": px, "text": f"{q:,}주 체결 중(정산 전)", "sub": rec.get("why") or "",
                       "more": _splits(rec["fills"]), "live": True})
        else:
            ev.append({"date": v, "rank": 1, "label": "주문 대기", "cls": "plan", "qty": 0, "text": f"{o.get('qty') or 0:,}주 대기", "sub": "다음 체결 시간대에 집행"})
    flush()

    start, end = _span(r)
    by_exit = {}
    for t in r["trades"]:
        by_exit.setdefault((t["exitDate"], t["reason"]), []).append(t)
    for (d, reason), ts in by_exit.items():
        q = sum(t["qty"] for t in ts)
        px = sum(t["qty"] * t["exitPrice"] for t in ts) / q
        pnl = sum(t.get("pnl") or 0 for t in ts)
        word = REASON.get(reason, reason)
        slip = sign * (plan.get("stop", 0) / px - 1) if reason in STOP_REASONS and plan.get("stop") and px else 0
        warn = slip > STOP_SLIP
        recs = [x for k, x in (intra(d).get("exits") or {}).items()
                if x.get("code") == code and x.get("side", "long") == side and k.startswith("s:") == (reason == "switch")]
        xf = [f for x in recs for f in x.get("fills") or []]
        take(xf, -1)
        tier = next((f["mode"][2:] for f in xf if (f.get("mode") or "").startswith("sw")), "")   # 스위칭 등급(A~D, 2026-11-03)
        ev.append({"date": d, "rank": 2, "label": ("스위칭 " + tier).strip() if reason == "switch" else word, "cls": "switch" if reason == "switch" else "stop" if reason in STOP_REASONS else "exit",
                   "qty": -q, "price": px, "text": f"{q:,}주 청산 · {pnl / 1e4:+,.0f}만 원", "pnl": pnl,
                   "sub": (f"손절가보다 {slip * 100:.1f}% 불리 · " if warn else "") + (ts[-1].get("note") or ""), "warn": warn, "more": _splits(xf)})
    last_day = max([d for d in (asof, end) if d] or [date])
    if r["position"] and not end:                         # 보유 중이면 오늘 장중(정산 전)까지 그래프에(2026-11-03 두산에너빌리티)
        last_day = max(last_day, live_date(store, asof) or last_day)
    for d in (tradedays.trading_days(start, end or asof or last_day) if start else []):
        for x in (store.get(f"run/{d}") or {}).get("reviews") or []:
            if x.get("code") == code and (x.get("side") or "long") == side:
                ev.append({"date": d, "rank": 3, "label": "재판별", "cls": "review", "qty": 0, "kind": "review",
                           "text": f"PM {x.get('rating') or '—'} · " + {"sell": "청산 예약", "sell_fallback": "실패 — 규칙대로 청산", "hold_updated": "보유 · 레벨 갱신"}.get(x.get("action"), "보유"),
                           "sub": " · ".join(s for s in (f"손절 {x['stop']:,.0f}" if x.get("stop") else "", f"목표 {x['target']:,.0f}" if x.get("target") else "") if s),
                           "decisionKey": x.get("decisionKey")})
    today = max((k.split("/")[1] for k in store.keys("intraday/") if k.count("/") == 1), default=None)
    if today and asof and today > asof:                   # 오늘 장중 청산(정산 전)
        for k, x in (intra(today).get("exits") or {}).items():
            if x.get("code") == code and x.get("side", "long") == side and x.get("fills") and r["position"]:
                q, px = _avg(x["fills"])
                take(x["fills"], -1)
                word = "스위칭" if k.startswith("s:") else REASON.get(x.get("kind"), x.get("kind"))
                pnl = sign * (px - r["position"]["entry"]) * q - (px * q * config.SELL_TAX if not short else 0)   # 정산 전 추정(Long 매도세 반영)
                ev.append({"date": today, "rank": 2, "label": word, "cls": "switch" if k.startswith("s:") else "stop" if x.get("kind") in STOP_REASONS else "exit", "qty": -q, "price": px,
                           "text": f"{q:,}주 청산(정산 전) · 약 {pnl / 1e4:+,.0f}만 원", "pnl": round(pnl), "sub": x.get("why") or "", "more": _splits(x["fills"]), "live": True})
                last_day = today
    ev.sort(key=lambda e: (e["date"] or "", e["rank"]))

    # 일별 보유 수량 → 비중(종가 평가액 / 그날 총평가액)
    p = r["position"]
    avg = p["entry"] if p else (r["trades"][-1]["entryPrice"] if r["trades"] else None)
    first = start or plan.get("validDate") or date
    span = tradedays.trading_days(first, max(end or last_day, first))      # 그래프 = 진입일 ~ 청산일(보유 중이면 최근일)
    docs = [store.get(x["decisionKey"]) for x in ev if x.get("kind") == "review" and x.get("decisionKey")][-1:] + [entry_doc]
    bars = _bars(code, span[0], span[-1], [x for x in docs if x], log=log) if span else {}
    eq = {e["date"]: e["equity"] for e in L.get("equity") or []}
    eq_last = (L.get("equity") or [{}])[-1].get("equity") or engine.equity_now(L) if "cash" in L else None
    delta = {}
    for e in ev:
        delta[e["date"]] = delta.get(e["date"], 0) + (e.get("qty") or 0)
    held, days, peak, peak_day = 0, [], 0.0, None
    fill_px = {e["date"]: e["price"] for e in ev if e.get("price")}
    for d in span:
        held += delta.get(d, 0)
        b = bars.get(d)
        px = (b or {}).get("c") or fill_px.get(d) or avg
        w = held * px / (eq.get(d) or eq_last) * 100 if held and px and (eq.get(d) or eq_last) else 0.0
        if w > peak:
            peak, peak_day = w, d
        days.append({"d": d, "w": round(w, 2), "held": held, **({"o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]} if b else {})})
    h2 = 0
    for e in ev:
        h2 += e.get("qty") or 0
        e["held"] = h2
        dd = next((x for x in days if x["d"] == e["date"]), None)
        e["weight"] = dd["w"] if dd else 0.0

    target = (plan.get("weight") or (p or {}).get("weight") or 0) * 100
    closes = [x["c"] for x in days if x.get("c")]
    qty_now = max(0, h2)
    live = live_prices(store, asof).get(code) if qty_now else None        # 보유 중이면 장중 현재가 우선
    last_px = (live or {}).get("price") or (p or {}).get("lastClose") or (closes[-1] if closes else None)
    realized = sum(t.get("pnl") or 0 for t in r["trades"])
    cost = sum(t["qty"] * t["entryPrice"] for t in r["trades"])
    # 오늘 장중 청산(정산 전, 2026-11-03 LG화학) — 추정 손익을 더하고 pending 으로 따로 알린다. 16:30 정산 뒤엔 원장 값으로 바뀜
    live_x = [e for e in ev if e.get("live") and (e.get("qty") or 0) < 0]
    pending = sum(e.get("pnl") or 0 for e in live_x)
    realized += pending
    cost += sum(-e["qty"] * (p or {}).get("entry", 0) for e in live_x)
    reached = next((x["d"] for x in days if target and x["w"] >= target * 0.95), None)
    first_exit = next((e for e in ev if (e.get("qty") or 0) < 0), None)
    switched = sum(-(e["qty"]) for e in ev if e["cls"] == "switch")
    bits = []
    if reached:
        bits.append(f"{reached[5:]} 목표 비중 도달")
    elif start:
        bits.append("목표 비중 미달")
    done = qty_now == 0 and first_exit
    if first_exit and (not done or first_exit["date"] != ev[-1]["date"]):
        bits.append(f"{first_exit['date'][5:]} {first_exit['label']} 시작")
    if done:
        bits.append(f"{ev[-1]['date'][5:]} " + (f"{first_exit['label']} " if first_exit["date"] == ev[-1]["date"] else "") + "전량 청산")
    line = " → ".join(bits)
    if start:
        line += " · " + (f"스위칭 {switched:,}주" if switched else "스위칭 없음")
    status = "none" if not start and not any(e["cls"] == "buy" for e in ev) else "open" if qty_now else "closed"

    # 분봉 — 회차 전 기간에 틱이 있어야 카드에 쓴다(10-19 이전·감시 밖 날이 섞이면 일봉 그대로)
    tick_days = [d for d in span if store.keys(f"intraday/{d}/ticks/{code}")]
    step = step_for(len(span)) if span and len(tick_days) == len(span) else None
    ibars = [{"d": d, **{k: v for k, v in b.items() if k != "vw"}} for d in span for b in minute_bars(ticks_fn(store, d, code), step, bars.get(d))] if step else []
    fl = sorted(fills.values(), key=lambda f: f["ts"])
    extra = {"step": step if ibars else None, "ibars": ibars, "fills": fl, "tickDays": tick_days}
    if day:
        extra["dayView"] = {"d": day, "step": 5, "bars": minute_bars(ticks_fn(store, day, code), 5, bars.get(day)) if day in tick_days else [],
                            "fills": [f for f in fl if f["ts"][:10] == day], "close": (bars.get(day) or {}).get("c")}
    return {**extra, "status": status, "code": code, "side": side, "name": plan.get("name") or (p or {}).get("name") or (r["trades"][-1]["name"] if r["trades"] else code),
            "entryDate": start, "exitDate": end, "avg": avg, "last": last_px, "lastTs": ((live or {}).get("ts") or "")[11:16] or None, "vsAvgPct": round(sign * (last_px / avg - 1) * 100, 2) if last_px and avg else None,
            "qty": qty_now, "targetWeight": round(target, 2), "peakWeight": round(peak, 2), "peakDay": peak_day,
            "nowWeight": days[-1]["w"] if days and qty_now else 0.0, "realized": round(realized), "realizedPct": round(realized / cost * 100, 2) if cost else None, "realizedPending": round(pending),
            "summary": line, "deviations": sum(1 for e in ev if e.get("warn")), "hasBars": bool(bars), "days": days, "events": ev}
