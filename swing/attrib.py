"""성과 기여도(2026-11-05) — Long·Short PF 탭의 섹터별·종목별 기여도. 읽기 전용(원장·장중 기록), 외부 호출 없음.

기여도(%p) = 종목 손익(원) ÷ 기간 시작 평가액. 종목 기여를 다 더하면 기간 수익률이 되고, 남는 차이(세금·대차료 반올림 등)는
'기타'(residual)로 따로 돌려준다.

  누적(all) — 기준 = 초기 자본. 손익 = 청산 실현손익(trades.pnl, 세금·대차료 반영) + 보유분 평가손익
              (현재가 − 평단 × 수량, 숏은 진입세·누적 대차료 차감) + 오늘 장중 청산 추정(정산 전 liveRealized).
  당일(day) — 기준 = 직전 영업일 종가 평가액.
     장중(정산 전): 원장 보유 = 원장 수량 × (현재가 − 전일 종가) + 오늘 체결 보정(liveAdj: 추가 = 현재가 − 체결가,
                    청산 = 체결가 − 현재가). 매도세는 정산 때 반영(장중 평가액과 같은 기준). 오늘 신규 진입 = (현재가 − 체결가) × 남은 수량 + 같은 날 청산 실현.
     정산 뒤: 보유 = (종가 − 전일 종가 prevClose) × 수량(오늘 진입이면 체결가 기준), 오늘 추가 매수분은 체결가 기준으로 보정,
              오늘 청산(trades exitDate = 기준일) = (청산가 − 전일 종가) × 수량 − 매도세, 같은 날 진입·청산은 실현손익 그대로.
              전일 종가 기록(engine prevClose, 2026-11-05 추가) 이 없는 행이 있으면 당일은 계산하지 않는다(available=False).

선정·트레이딩 분해(2026-11-06) — 종목 손익 = ① 선정 + ② 진입 + ③ 청산 + ④ 비용 (항등식, ③ = 손익 − ① − ② − ④).
  기준선 = '판단일 종가 B 에 계획 수량을 샀다고 치는' 가상 포지션.
  ① 선정 = 부호 × 계획 수량 × (끝 가격 − B)        끝 가격 = 청산일 종가(청산분) · 현재가(보유분)
  ② 진입 = 부호 × 체결 수량 × (B − 실제 평단) − 미체결 기회손익(부호 × 미체결 수량 × (끝 가격 − B))
  ③ 청산 = 실제 청산가 − 청산일 종가 몫 (나머지)
  ④ 비용 = 거래세(Long 매도·Short 진입) + 대차료 — 음수. 수수료·슬리피지는 엔진이 계산하지 않아 없음
  ①은 다시 시장 효과 + PM 선정으로 나눈다(β = 1): 시장 = 부호 × 계획 수량 × 기준가 × (끝 날짜 지수 ÷ 기준일 지수 − 1),
  지수 = 상장시장(코스피 0001·코스닥 1001, KIS 업종 일봉 — 허브 /index-chart), 장중 끝 지수 = 허브 /market 실시간 지수.
  mkt(code, 기준일, 끝 날짜|None=장중) → 지수 수익률|None. 없으면 그 종목은 시장 미분리(mkt=None, PM 선정 = 선정).
  당일 보기는 B 대신 직전 영업일 종가(판단 다음날 진입이면 B 와 같다). 미체결 기회손익은 진입 주문이 끝난 회차만
  (아직 열린 주문·장중 진행 중이면 빼고). 종가는 close(code, date) 로 받는다(서버가 일봉 캐시, 없으면 그 종목은 분해 없음).

노출(2026-11-06, 섹터별 노출 표와 합침) — 행마다 expNow(현재 보유 평가금액) · expCD(자본 × 일) · expDays(보유 영업일)
  → 노출 대비 수익률 = 손익 ÷ (expCD ÷ 보유 영업일) = 보유 기간 평균 노출 대비. 종목 1회차면 = 투입 금액 대비 수익률.
  섹터는 secDays[방향][섹터](그 섹터를 실제 들고 있던 영업일 합집합)로 나눈다 — 잠깐 든 종목이 부풀지 않게.
  당일 = 장 시작 노출(전일 종가 × 원장 수량) + 오늘 신규·추가 체결 금액, 하루.
  누적 = Σ(진입 금액 × 보유 영업일) — 진입가 기준 근사(매일 시가 평가 아님). 숏도 양수(금액)로 — 방향은 side.
"""
from __future__ import annotations

import datetime

from . import config


def _sgn(x):
    return -1 if (x.get("side") or "long") == "short" else 1


def _add(acc, key, x, **kv):
    code, side = key
    e = acc.get(key)
    if e is None:
        e = acc[key] = {"code": code, "side": side, "name": x.get("name"), "sector": x.get("sector") or "기타",
                        "decisionKey": x.get("decisionKey"), "pnl": 0.0, "realized": 0.0, "unreal": 0.0, "held": False}
    for k, v in kv.items():
        if k == "held":
            e["held"] = e["held"] or bool(v)
        else:
            e[k] += v or 0.0
            e["pnl"] += v or 0.0
    if not e.get("name") and x.get("name"):
        e["name"] = x["name"]
    if (e.get("sector") in (None, "기타")) and x.get("sector"):
        e["sector"] = x["sector"]
    return e


def _short_costs(p):
    return (p.get("entryTax") or 0.0) + (p.get("borrowFee") or 0.0) if _sgn(p) < 0 else 0.0


def cumulative(L, ps):
    acc = {}
    for t in L.get("trades") or []:
        _add(acc, (t["code"], t.get("side") or "long"), t, realized=t.get("pnl") or 0.0)
    for p in ps:
        key = (p["code"], p.get("side") or "long")
        unreal = _sgn(p) * ((p.get("price") or p["entry"]) - p["entry"]) * (p.get("qty") or 0) - _short_costs(p)
        _add(acc, key, p, realized=p.get("liveRealized") or 0.0, unreal=unreal, held=(p.get("qty") or 0) > 0)
    return acc


def day_live(ps):
    """정산 전(장중) 당일 손익 — api_positions 행(Live 현재가·오늘 체결 보정 포함)."""
    acc = {}
    for p in ps:
        key = (p["code"], p.get("side") or "long")
        s = _sgn(p)
        if str(p.get("id") or "").startswith("live:"):            # 오늘 신규 진입
            v = s * ((p.get("price") or p["entry"]) - p["entry"]) * (p.get("qty") or 0)
            _add(acc, key, p, unreal=v, realized=p.get("liveRealized") or 0.0, held=(p.get("qty") or 0) > 0)
            continue
        prev = p.get("lastClose") or p.get("entryLedger") or p["entry"]
        base = s * (p.get("qtyLedger") or 0) * ((p.get("price") or prev) - prev) + (p.get("liveAdj") or 0.0)
        tax = 0.0       # 장중 평가액(api_live)이 매도세를 빼지 않으니 합계를 맞추려 여기서도 빼지 않는다(정산 때 반영)
        sold = (p.get("liveExit") or {}).get("qty") or 0
        # 청산분과 보유분 나누기(표시용): 청산분 = 오늘 체결가 − 전일 종가 몫은 liveAdj 안에 섞여 있어 합계만 정확 — 보유 중이면 평가, 다 팔았으면 실현
        if (p.get("qty") or 0) > 0:
            _add(acc, key, p, unreal=base - tax, held=True)
        else:
            _add(acc, key, p, realized=base - tax)
        if sold and (p.get("qty") or 0) > 0:
            acc[key]["partial"] = True
    return acc


def day_settled(L, doc=None):
    """정산 뒤 당일 손익 — 원장(기준일 asof) + 그날 장중 기록(추가 매수 체결). 전일 종가 기록이 없으면 (acc, False)."""
    d = L.get("asof")
    acc, ok = {}, True
    adds = {}
    for rec in ((doc or {}).get("orders") or {}).values():
        if rec.get("topupOf") and rec.get("fills"):
            adds.setdefault(rec["topupOf"], []).extend(f for f in rec["fills"] if f.get("qty"))
    for p in L.get("positions") or []:
        key = (p["code"], p.get("side") or "long")
        s, close = _sgn(p), p.get("lastClose") or p["entry"]
        if p.get("fillDate") == d:
            v = s * (close - p["entry"]) * p["qty"] - _short_costs(p)
        elif p.get("prevClose"):
            af = adds.get(p["id"]) or []
            v = s * (close - p["prevClose"]) * p["qty"] + s * sum(f["qty"] * (p["prevClose"] - f["price"]) for f in af)
        else:
            ok = False
            continue
        _add(acc, key, p, unreal=v, held=True)
    for t in L.get("trades") or []:
        if t.get("exitDate") != d:
            continue
        key = (t["code"], t.get("side") or "long")
        if t.get("entryDate") == d:
            v = t.get("pnl") or 0.0
        elif t.get("prevClose"):
            s = _sgn(t)
            v = s * (t["exitPrice"] - t["prevClose"]) * t["qty"] - (t.get("tax") or 0.0 if s > 0 else 0.0)
        else:
            ok = False
            continue
        _add(acc, key, t, realized=v)
    return acc, ok


# ── 선정·트레이딩 분해(2026-11-06) ─────────────────────────────────────────────────────────────────────────────

def _dk_date(k):
    p = (k or "").split("/")
    return p[1] if len(p) >= 4 and p[0] == "decision" else None


def prev_trading_day(d):
    from . import tradedays
    x = tradedays._d(d) - datetime.timedelta(days=1)
    while not tradedays.is_trading_day(x):
        x -= datetime.timedelta(days=1)
    return x.isoformat()


def _plans(L):
    """decisionKey → {planned: 첫 체결 주문의 계획 수량, open: 아직 열린 진입 주문(재주문·비중 채우기) 있음}."""
    g = {}
    for o in (L.get("orderLog") or []) + (L.get("orders") or []):
        dk = o.get("decisionKey")
        if o.get("type", "entry") != "entry" or not dk:
            continue
        e = g.setdefault(dk, {"planned": None, "open": False, "created": None})
        if o.get("status") == "open":
            e["open"] = True
        if o.get("topupOf") or o.get("status") != "filled":
            continue
        c = o.get("created") or ""
        if e["created"] is None or c < e["created"]:
            e["created"] = c
            e["planned"] = (o.get("partial") or {}).get("planned") or o.get("qty")
    return g


def _trade_cost(t):
    """청산 거래의 세금·대차료(양수) — Long 매도세, Short 진입세(수량 비율분) + 대차료."""
    return (t.get("tax") or 0.0) + (t.get("borrowFee") or 0.0)


def _held_cost(p):
    """보유 행 비용 — Short 진입세·누적 대차료 + 오늘 장중 Long 청산분 매도세(정산 전 추정 liveRealized 에 들어 있는 몫)."""
    c = _short_costs(p)
    sold = (p.get("liveExit") or {}).get("qty") or 0
    if sold and _sgn(p) > 0 and not str(p.get("id") or "").startswith("live:") and p.get("liveRealized") is not None:
        proceeds = (p["liveRealized"] + sold * p["entry"]) / (1 - config.SELL_TAX)
        c += proceeds * config.SELL_TAX
    return c


class _Split:
    """종목(code, side)별 sel·ent·miss 누적 + 회차(decisionKey)별 미체결 기회손익 계산용 상태."""

    def __init__(self, plans):
        self.acc, self.rounds, self.plans = {}, {}, plans

    def at(self, key):
        return self.acc.setdefault(key, {"sel": 0.0, "ent": 0.0, "miss": 0.0, "cost": 0.0, "mkt": 0.0, "mok": True, "ok": True})

    def bad(self, key):
        self.at(key)["ok"] = False

    def piece(self, key, s, q, end, ref, avg, dk=None, end_d="", planned=None, open_=False, cost=0.0, mret=None):
        """체결 q 주(평단 avg)가 기준가 ref → 끝 가격 end, cost = 세금·대차료(양수), mret = 같은 구간 지수 수익률.
        dk 를 주면 회차 미체결 계산에 쓴다."""
        e = self.at(key)
        e["cost"] -= cost or 0.0
        e["sel"] += s * q * (end - ref)
        e["ent"] += s * q * (ref - avg)
        if mret is None:
            e["mok"] = False
        else:
            e["mkt"] += s * q * ref * mret
        if dk is None:
            return
        r = self.rounds.setdefault(dk, {"key": key, "s": s, "ref": ref, "filled": 0, "end": end, "end_d": end_d,
                                        "planned": planned, "open": open_, "mret": mret})
        r["filled"] += q
        if end_d >= r["end_d"]:
            r["end"], r["end_d"], r["mret"] = end, end_d, mret
        r["planned"] = r["planned"] or planned
        r["open"] = r["open"] or open_

    def finish(self):
        for dk, r in self.rounds.items():
            pl = self.plans.get(dk) or {}
            planned = r["planned"] or pl.get("planned")
            if not planned or r["open"] or pl.get("open"):
                continue
            m = planned - r["filled"]
            if m > 0:
                v = r["s"] * m * (r["end"] - r["ref"])
                e = self.at(r["key"])
                e["sel"] += v
                e["ent"] -= v
                e["miss"] += v
                if r["mret"] is None:
                    e["mok"] = False
                else:
                    e["mkt"] += r["s"] * m * r["ref"] * r["mret"]
        return self.acc


def _nomkt(code, d1, d2):
    return None


def split_all(L, ps, close, mkt=_nomkt, live=False):
    """누적 분해 — cumulative() 와 같은 원장·보유 행. close(code, date) → 종가|None, live = 보유 행 가격이 장중 현재가."""
    sp = _Split(_plans(L))
    held_end = None if live else L.get("asof")
    for t in L.get("trades") or []:
        key, s, dk = (t["code"], t.get("side") or "long"), _sgn(t), t.get("decisionKey")
        ref = close(t["code"], _dk_date(dk)) if _dk_date(dk) else None
        cx = close(t["code"], t["exitDate"])
        if not (ref and cx):
            sp.bad(key)
            continue
        sp.piece(key, s, t["qty"], cx, ref, t["entryPrice"], dk=dk, end_d=t["exitDate"], cost=_trade_cost(t),
                 mret=mkt(t["code"], _dk_date(dk), t["exitDate"]))
    for p in ps:
        key, s, dk = (p["code"], p.get("side") or "long"), _sgn(p), p.get("decisionKey")
        q = (p.get("qty") or 0) + ((p.get("liveExit") or {}).get("qty") or 0)      # 오늘 장중 청산분은 현재가를 끝 가격으로
        ref = close(p["code"], _dk_date(dk)) if _dk_date(dk) else None
        if not ref:
            sp.bad(key)
            continue
        le = p.get("liveEntry") or {}
        sp.piece(key, s, q, p.get("price") or p["entry"], ref, p["entry"], dk=dk, end_d="9999",
                 planned=le.get("planned"), open_=bool(le) and not le.get("done"), cost=_held_cost(p),
                 mret=mkt(p["code"], _dk_date(dk), held_end))
    return sp.finish()


def split_day_live(ps, close, day, mkt=_nomkt):
    """장중 당일 분해 — day_live() 와 같은 행. 기준 = 직전 영업일 종가."""
    sp = _Split({})
    pdd = prev_trading_day(day)
    for p in ps:
        key, s = (p["code"], p.get("side") or "long"), _sgn(p)
        px = p.get("price") or p["entry"]
        if str(p.get("id") or "").startswith("live:"):
            prev = p.get("dayPrev") or close(p["code"], prev_trading_day(day))
            if not prev:
                sp.bad(key)
                continue
            le = p.get("liveEntry") or {}
            q = (p.get("qty") or 0) + ((p.get("liveExit") or {}).get("qty") or 0)
            sp.piece(key, s, q, px, prev, p["entry"], dk="live:" + str(p["id"]), planned=le.get("planned"),
                     open_=not le.get("done"), mret=mkt(p["code"], pdd, None))
            continue
        prev = p.get("lastClose") or p.get("entryLedger") or p["entry"]
        la = p.get("liveAdd") or {}
        mr = mkt(p["code"], pdd, None)
        sp.piece(key, s, p.get("qtyLedger") or 0, px, prev, prev, mret=mr)            # 원장 보유분 = 선정(보유 판단)
        if la.get("qty"):
            sp.piece(key, s, la["qty"], px, prev, la["price"], mret=mr)               # 오늘 추가 매수 체결가 = 진입
    return sp.finish()


def split_day_settled(L, doc, close, mkt=_nomkt):
    """정산 뒤 당일 분해 — day_settled() 와 같은 원장 행."""
    d = L.get("asof")
    pd = prev_trading_day(d) if d else None
    plans = _plans(L)
    sp = _Split(plans)
    adds = {}
    for rec in ((doc or {}).get("orders") or {}).values():
        if rec.get("topupOf") and rec.get("fills"):
            adds.setdefault(rec["topupOf"], []).extend(f for f in rec["fills"] if f.get("qty"))
    for p in L.get("positions") or []:
        key, s = (p["code"], p.get("side") or "long"), _sgn(p)
        cl = p.get("lastClose") or p["entry"]
        if p.get("fillDate") == d:
            ref = close(p["code"], pd)
            if not ref:
                sp.bad(key)
                continue
            sp.piece(key, s, p["qty"], cl, ref, p["entry"], dk=p.get("decisionKey"), end_d=d, cost=_short_costs(p),
                     mret=mkt(p["code"], pd, d))
        elif p.get("prevClose"):
            af = adds.get(p["id"]) or []
            qa = sum(f["qty"] for f in af)
            mr = mkt(p["code"], pd, d)
            sp.piece(key, s, p["qty"] - qa, cl, p["prevClose"], p["prevClose"], mret=mr)
            for f in af:
                sp.piece(key, s, f["qty"], cl, p["prevClose"], f["price"], mret=mr)
        else:
            sp.bad(key)
    for t in L.get("trades") or []:
        if t.get("exitDate") != d:
            continue
        key, s = (t["code"], t.get("side") or "long"), _sgn(t)
        cd = close(t["code"], d)
        if t.get("entryDate") == d:
            ref = close(t["code"], pd)
            if not (ref and cd):
                sp.bad(key)
                continue
            sp.piece(key, s, t["qty"], cd, ref, t["entryPrice"], dk=t.get("decisionKey"), end_d=d, cost=_trade_cost(t),
                     mret=mkt(t["code"], pd, d))
        elif t.get("prevClose") and cd:
            sp.piece(key, s, t["qty"], cd, t["prevClose"], t["prevClose"], cost=(t.get("tax") or 0.0) if s > 0 else 0.0,
                     mret=mkt(t["code"], pd, d))
        else:
            sp.bad(key)
    # 당일 보기의 미체결 기회손익은 오늘 처음 체결된 회차만(이전부터 이어진 회차는 누적에서)
    first = {}
    for o in (L.get("orderLog") or []) + (L.get("orders") or []):
        if o.get("type", "entry") == "entry" and o.get("status") == "filled" and not o.get("topupOf") and o.get("decisionKey"):
            first[o["decisionKey"]] = min(first.get(o["decisionKey"]) or "9999", o.get("validDate") or o.get("created") or "")
    for dk in list(sp.rounds):
        if first.get(dk) != d:
            sp.rounds.pop(dk)
    return sp.finish()


def _apply_split(acc, sp):
    for key, e in acc.items():
        x = (sp or {}).get(key)
        if not x or not x["ok"]:
            e["split"] = None
            continue
        e["split"] = {"sel": x["sel"], "ent": x["ent"], "ext": e["pnl"] - x["sel"] - x["ent"] - x["cost"], "cost": x["cost"],
                      "miss": x["miss"], "mkt": x["mkt"] if x["mok"] else None}


def _split_zero():
    return {"sel": 0, "ent": 0, "ext": 0, "cost": 0, "miss": 0, "mkt": 0, "pure": 0, "mktNoneSel": 0, "nMktNone": 0,
            "none": 0, "nNone": 0}


def _split_add(dst, e):
    """행 분해를 합계에 더한다 — 분해 없음(종가 미확보)은 none, 시장 미분리(지수 없음)는 mktNoneSel(그 종목 선정)으로."""
    x = e.get("split")
    if not x:
        dst["none"] += e["pnl"]
        dst["nNone"] += 1
        return
    for k in ("sel", "ent", "ext", "cost", "miss"):
        dst[k] += x[k]
    if x.get("mkt") is None:
        dst["mktNoneSel"] += x["sel"]
        dst["nMktNone"] += 1
    else:
        dst["mkt"] += x["mkt"]
        dst["pure"] += x["pure"]


def _exp(acc, key, x, now=0.0, base=0.0, days=()):
    """base = 자본 × 일(당일 보기는 그날 노출 × 1), days = 그 노출이 있던 영업일들."""
    e = _add(acc, key, x)
    e["expNow"] = e.get("expNow", 0.0) + (now or 0.0)
    e["expCD"] = e.get("expCD", 0.0) + (base or 0.0)
    e.setdefault("_days", set()).update(days)


def _value(p):
    return p.get("value") if p.get("value") is not None else (p.get("qty") or 0) * (p.get("price") or p.get("lastClose") or p["entry"])


def _tdays(a, b):
    from . import tradedays
    return tradedays.trading_days(a, b) if a and b and a <= b else ([a] if a else [])


def exposure_all(acc, L, ps, live_day):
    """누적 — 자본 × 보유 영업일(진입 금액 기준), 보유 영업일 집합."""
    end = live_day or L.get("asof")
    for t in L.get("trades") or []:
        ds = _tdays(t["entryDate"], t["exitDate"])
        _exp(acc, (t["code"], t.get("side") or "long"), t, base=t["qty"] * t["entryPrice"] * len(ds), days=ds)
    for p in ps:
        q = (p.get("qty") or 0) + ((p.get("liveExit") or {}).get("qty") or 0)
        ds = _tdays(p.get("fillDate") or end, end)
        _exp(acc, (p["code"], p.get("side") or "long"), p, now=_value(p), base=q * p["entry"] * len(ds), days=ds)


def exposure_day_live(acc, ps, day):
    for p in ps:
        key = (p["code"], p.get("side") or "long")
        if str(p.get("id") or "").startswith("live:"):
            q = (p.get("qty") or 0) + ((p.get("liveExit") or {}).get("qty") or 0)
            _exp(acc, key, p, now=_value(p), base=q * p["entry"], days=[day])
            continue
        prev = p.get("lastClose") or p.get("entryLedger") or p["entry"]
        la = p.get("liveAdd") or {}
        _exp(acc, key, p, now=_value(p), base=(p.get("qtyLedger") or 0) * prev + (la.get("qty") or 0) * (la.get("price") or 0), days=[day])


def exposure_day_settled(acc, L, doc):
    d = L.get("asof")
    adds = {}
    for rec in ((doc or {}).get("orders") or {}).values():
        if rec.get("topupOf") and rec.get("fills"):
            adds.setdefault(rec["topupOf"], []).extend(f for f in rec["fills"] if f.get("qty"))
    for p in L.get("positions") or []:
        key = (p["code"], p.get("side") or "long")
        now = p["qty"] * (p.get("lastClose") or p["entry"])
        if p.get("fillDate") == d:
            base = p["qty"] * p["entry"]
        else:
            af = adds.get(p["id"]) or []
            qa = sum(f["qty"] for f in af)
            base = (p["qty"] - qa) * (p.get("prevClose") or p.get("lastClose") or p["entry"]) + sum(f["qty"] * f["price"] for f in af)
        _exp(acc, key, p, now=now, base=base, days=[d])
    for t in L.get("trades") or []:
        if t.get("exitDate") == d:
            ref = t["entryPrice"] if t.get("entryDate") == d else (t.get("prevClose") or t["entryPrice"])
            _exp(acc, (t["code"], t.get("side") or "long"), t, base=t["qty"] * ref, days=[d])


def _pack(acc, base, total, side=None):
    rows = [e for e in acc.values() if abs(e["pnl"]) >= 0.5 or (e.get("expNow") or 0) >= 1]
    sec_days = {"all": {}, "long": {}, "short": {}}
    for e in rows:
        ds = e.pop("_days", None) or set()
        for k in ("all", e["side"]):
            sec_days[k].setdefault(e["sector"], set()).update(ds)
        e["expNow"] = round(e.get("expNow") or 0)
        e["expCD"] = round(e.get("expCD") or 0)
        e["expDays"] = len(ds) or 1
        e["expBase"] = round(e["expCD"] / e["expDays"])                       # 보유 기간 평균 노출
        e["expRet"] = round(e["pnl"] / e["expBase"] * 100, 3) if e["expBase"] else None
        e["contribPct"] = round(e["pnl"] / base * 100, 4) if base else None
        for k in ("pnl", "realized", "unreal"):
            e[k] = round(e[k])
        if e.get("split"):
            x = {k: (round(v) if v is not None else None) for k, v in e["split"].items()}
            x["ext"] = e["pnl"] - x["sel"] - x["ent"] - x["cost"]     # 반올림 뒤에도 합 = 손익
            x["pure"] = x["sel"] - x["mkt"] if x["mkt"] is not None else None
            e["split"] = x
    rows.sort(key=lambda e: -e["pnl"])
    sec = {}
    for e in rows:
        g = sec.setdefault(e["sector"], {"sector": e["sector"], "long": 0.0, "short": 0.0, "pnl": 0.0, "n": 0, "codes": set(),
                                         "split": _split_zero()})
        g["long" if e["side"] == "long" else "short"] += e["pnl"]
        _split_add(g["split"], e)
        g["pnl"] += e["pnl"]
        g["codes"].add(e["code"])
    secs = []
    for g in sec.values():
        g["n"] = len(g.pop("codes"))
        for k in ("long", "short", "pnl"):
            g[k] = round(g[k])
        g["longPct"] = round(g["long"] / base * 100, 4) if base else None
        g["shortPct"] = round(g["short"] / base * 100, 4) if base else None
        g["contribPct"] = round(g["pnl"] / base * 100, 4) if base else None
        secs.append(g)
    secs.sort(key=lambda g: -g["pnl"])
    s = sum(e["pnl"] for e in rows)
    tot = _split_zero()
    for e in rows:
        _split_add(tot, e)
    return {"secDays": {k: {sec: len(v) or 1 for sec, v in g.items()} for k, g in sec_days.items()}, "split": tot, "base": round(base), "total": round(total), "totalPct": round(total / base * 100, 4) if base else None,
            "sum": round(s), "residual": round(total - s), "residualPct": round((total - s) / base * 100, 4) if base else None,
            "byCode": rows, "bySector": secs}


def compute(L, ps, *, equity_now, live_day=None, doc=None, close=None, mkt=None):
    """L = 원장, ps = api_positions 행(장중이면 Live 반영), equity_now = 현재 평가액(장중 Live 포함),
    live_day = 정산 전 장중 기록 날짜(없으면 정산 뒤), doc = 당일 장중 기록(intraday/<날짜>),
    close(code, date) = 그날 종가(선정·트레이딩 분해용, 없으면 None → 그 종목 분해 생략),
    mkt(code, 기준일, 끝 날짜|None) = 그 종목 상장시장 지수 수익률(시장 효과, 없으면 None → 시장 미분리)."""
    cap = L.get("capital0") or 0.0
    eq = [r for r in L.get("equity") or [] if r.get("equity")]
    out = {"asof": L.get("asof"), "live": bool(live_day), "date": live_day or L.get("asof")}
    close = close or (lambda code, d: None)
    mkt = mkt or _nomkt
    out["equityNow"] = round(equity_now)
    acc = cumulative(L, ps)
    exposure_all(acc, L, ps, live_day)
    _apply_split(acc, split_all(L, ps, close, mkt, live=bool(live_day)))
    out["all"] = _pack(acc, cap, equity_now - cap)
    if live_day:
        base = eq[-1]["equity"] if eq else cap
        acc = day_live(ps)
        exposure_day_live(acc, ps, live_day)
        _apply_split(acc, split_day_live(ps, close, live_day, mkt))
        out["day"] = {**_pack(acc, base, equity_now - base), "available": True}
    else:
        base = eq[-2]["equity"] if len(eq) >= 2 else cap
        acc, ok = day_settled(L, doc)
        exposure_day_settled(acc, L, doc)
        _apply_split(acc, split_day_settled(L, doc, close, mkt))
        out["day"] = {**_pack(acc, base, equity_now - base), "available": ok}
        if not ok:
            out["day"]["note"] = "정산 뒤 당일 기여도는 종목별 전일 종가 기록(2026-11-05 추가) 이후 정산분부터"
    return out
