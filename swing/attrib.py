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
"""
from __future__ import annotations

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


def _pack(acc, base, total, side=None):
    rows = [e for e in acc.values() if abs(e["pnl"]) >= 0.5]
    for e in rows:
        e["contribPct"] = round(e["pnl"] / base * 100, 4) if base else None
        for k in ("pnl", "realized", "unreal"):
            e[k] = round(e[k])
    rows.sort(key=lambda e: -e["pnl"])
    sec = {}
    for e in rows:
        g = sec.setdefault(e["sector"], {"sector": e["sector"], "long": 0.0, "short": 0.0, "pnl": 0.0, "n": 0, "codes": set()})
        g["long" if e["side"] == "long" else "short"] += e["pnl"]
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
    return {"base": round(base), "total": round(total), "totalPct": round(total / base * 100, 4) if base else None,
            "sum": round(s), "residual": round(total - s), "residualPct": round((total - s) / base * 100, 4) if base else None,
            "byCode": rows, "bySector": secs}


def compute(L, ps, *, equity_now, live_day=None, doc=None):
    """L = 원장, ps = api_positions 행(장중이면 Live 반영), equity_now = 현재 평가액(장중 Live 포함),
    live_day = 정산 전 장중 기록 날짜(없으면 정산 뒤), doc = 당일 장중 기록(intraday/<날짜>)."""
    cap = L.get("capital0") or 0.0
    eq = [r for r in L.get("equity") or [] if r.get("equity")]
    out = {"asof": L.get("asof"), "live": bool(live_day), "date": live_day or L.get("asof")}
    out["all"] = _pack(cumulative(L, ps), cap, equity_now - cap)
    if live_day:
        base = eq[-1]["equity"] if eq else cap
        out["day"] = {**_pack(day_live(ps), base, equity_now - base), "available": True}
    else:
        base = eq[-2]["equity"] if len(eq) >= 2 else cap
        acc, ok = day_settled(L, doc)
        out["day"] = {**_pack(acc, base, equity_now - base), "available": ok}
        if not ok:
            out["day"]["note"] = "정산 뒤 당일 기여도는 종목별 전일 종가 기록(2026-11-05 추가) 이후 정산분부터"
    return out
