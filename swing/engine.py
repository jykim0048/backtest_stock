"""원장 엔진(2단계) — 체결·청산·현금부족·평가. 순수 함수(원장 dict in → 같은 dict 갱신),
네트워크·저장소 무관. 가격은 에이전트 가격, 일봉 고저는 '도달 여부' 판정에만 쓴다.

하루(D) 처리 순서 — settle_day(ledger, D, bars):
  [시가]  ① 전일 PM 매도 예약 → 시가 매도
          ② 보유 종목 갭하락(시가 ≤ 손절가) → 시가 손절
  [장중]  ③ D 가 유효일인 진입 주문: 저가 ≤ 진입가 → 진입가 체결. 현금 부족이면 평가손실
             보유 종목을 손실률 큰 순으로 시가 청산해 확보(합쳐도 부족하면 청산 없이 스킵).
             미도달·시세 없음 → 취소
          ④ 기존 보유: 저가 ≤ 손절 → 손절가, 고가 ≥ 목표 → 목표가, 둘 다 → 손절 우선
          ⑤ 당일 체결분: 저가 ≤ 손절 → 손절가(보수적). 목표가는 체결 전후 순서를 알 수 없어
             체결일엔 미적용(보수적)
  [종가]  ⑥ 보유일 == HOLD_DAYS(체결일=1일째) → 종가 청산
          ⑦ 평가(종가, 시세 없으면 직전 종가) → equity 행

비용: 매수 수수료 없음, 매도 대금 × SELL_TAX. side 는 현재 'long' 만(숏 확장 대비 필드).
"""
import math

from . import config


def new_ledger(capital=None, start=None):
    cap = float(capital if capital is not None else config.INITIAL_CAPITAL)
    return {"version": 1, "capital0": cap, "cash": cap, "asof": start,
            "positions": [], "orders": [], "trades": [], "orderLog": [],
            "equity": [], "seq": 0}


def _id(L, p):
    L["seq"] += 1
    return f"{p}{L['seq']}"


def equity_now(L):
    return L["cash"] + sum(p["qty"] * (p.get("lastClose") or p["entry"]) for p in L["positions"])


# ── 주문 생성(판단 직후, D 장 마감 후) ─────────────────────────────────────────
def place_entry(L, *, code, name, date, valid_date, entry, stop, target, weight,
                sector=None, signal=None, decision_key=None, side="long"):
    """다음 영업일(valid_date) 1일 유효 지정가 주문. 수량 = 비중 × D 종가 평가액 / 진입가."""
    qty = int(math.floor(weight * equity_now(L) / entry)) if entry > 0 else 0
    o = {"id": _id(L, "o"), "code": code, "name": name, "side": side, "type": "entry",
         "created": date, "validDate": valid_date, "entry": float(entry), "stop": float(stop),
         "target": float(target) if target else None, "weight": weight, "qty": qty,
         "sector": sector, "signal": signal, "decisionKey": decision_key, "status": "open"}
    if qty <= 0:
        o.update(status="skipped", note="수량 0(비중·가격)")
        L["orderLog"].append(o)
        return o
    L["orders"].append(o)
    return o


def schedule_sell(L, code, reason, decision_key=None, date=None):
    """보유 종목을 다음 영업일 시가에 매도 예약(PM 매도 판정)."""
    for p in L["positions"]:
        if p["code"] == code and not p.get("sellPending"):
            p["sellPending"] = {"reason": reason, "decisionKey": decision_key, "date": date}
            return True
    return False


# ── 체결 헬퍼 ────────────────────────────────────────────────────────────────
def _close_position(L, p, date, price, reason, note=None):
    gross = p["qty"] * price
    tax = gross * config.SELL_TAX
    cost = p["qty"] * p["entry"]
    pnl = gross - tax - cost
    L["cash"] += gross - tax
    L["positions"].remove(p)
    t = {"id": _id(L, "t"), "code": p["code"], "name": p["name"], "side": p["side"],
         "qty": p["qty"], "entryDate": p["fillDate"], "entryPrice": p["entry"],
         "exitDate": date, "exitPrice": float(price), "reason": reason,
         "holdDays": p["holdDay"], "tax": round(tax), "pnl": round(pnl),
         "retPct": round(pnl / cost * 100, 2) if cost else 0.0,
         "sector": p.get("sector"), "signal": p.get("signal"),
         "decisionKey": p.get("decisionKey"),
         "exitDecisionKey": (p.get("sellPending") or {}).get("decisionKey")}
    if note:
        t["note"] = note
    L["trades"].append(t)
    return t


def _raise_cash(L, need, date, bars, exclude):
    """평가손실 보유 종목을 손실률 큰 순으로 시가 청산해 need 확보. 합쳐도 부족하면 아무것도
    팔지 않고 False(이익 종목은 건드리지 않음)."""
    losers = []
    for p in L["positions"]:
        b = bars.get(p["code"])
        if p["id"] in exclude or not b or not b.get("open"):
            continue
        r = b["open"] / p["entry"] - 1
        if r < 0:
            losers.append((r, p, b["open"]))
    losers.sort(key=lambda x: x[0])
    avail = sum(p["qty"] * px * (1 - config.SELL_TAX) for _, p, px in losers)
    if L["cash"] + avail < need:
        return False, []
    sold = []
    for r, p, px in losers:
        if L["cash"] >= need:
            break
        sold.append(_close_position(L, p, date, px, "cash",
                                    note=f"현금 확보(시가 손실률 {r * 100:.2f}%)"))
    return True, sold


# ── 하루 정산 ────────────────────────────────────────────────────────────────
def settle_day(L, date, bars):
    """date 영업일 처리. bars = {code: {open, high, low, close}}. 이미 처리한 날은 무시.
    반환: 그날 이벤트 {filled, cancelled, skipped, exits}."""
    if L.get("asof") and date <= L["asof"]:
        return {"skip": f"이미 정산됨(asof {L['asof']})"}
    ev = {"filled": [], "cancelled": [], "skipped": [], "exits": []}

    for p in L["positions"]:
        p["holdDay"] += 1

    # ① PM 매도 예약 → 시가
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if p.get("sellPending") and b and b.get("open"):
            ev["exits"].append(_close_position(L, p, date, b["open"], "pm_sell",
                                               note=p["sellPending"].get("reason")))
    # ② 갭하락 손절 → 시가
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if b and b.get("open") and b["open"] <= p["stop"]:
            ev["exits"].append(_close_position(L, p, date, b["open"], "stop_gap"))

    # ③ 진입 주문
    todays = [o for o in L["orders"] if o["validDate"] == date]
    stale = [o for o in L["orders"] if o["validDate"] < date]
    L["orders"] = [o for o in L["orders"] if o["validDate"] > date]
    for o in stale:
        o.update(status="cancelled", note="유효일 경과")
        L["orderLog"].append(o)
        ev["cancelled"].append(o)
    new_ids = set()
    for o in todays:
        b = bars.get(o["code"])
        if not b or b.get("low") is None:
            o.update(status="cancelled", note="시세 없음")
        elif any(p["code"] == o["code"] for p in L["positions"]):
            o.update(status="skipped", note="이미 보유")
        elif b["low"] > o["entry"]:
            o.update(status="cancelled",
                     note=f"미도달(저가 {b['low']:,.0f} > 진입 {o['entry']:,.0f})")
        else:
            cost = o["qty"] * o["entry"]
            if L["cash"] < cost:
                ok, sold = _raise_cash(L, cost, date, bars, new_ids)
                ev["exits"].extend(sold)
                if not ok:
                    o.update(status="skipped", note="현금 부족(손실 종목 청산으로도 부족)")
            if o["status"] == "open":
                L["cash"] -= cost
                p = {"id": _id(L, "p"), "code": o["code"], "name": o["name"], "side": o["side"],
                     "qty": o["qty"], "entry": o["entry"], "stop": o["stop"],
                     "target": o["target"], "fillDate": date, "holdDay": 1,
                     "sector": o.get("sector"), "signal": o.get("signal"),
                     "decisionKey": o.get("decisionKey"), "orderId": o["id"],
                     "lastClose": None, "sellPending": None}
                L["positions"].append(p)
                new_ids.add(p["id"])
                o.update(status="filled", fillPrice=o["entry"], positionId=p["id"])
        L["orderLog"].append(o)
        ev[{"filled": "filled", "skipped": "skipped"}.get(o["status"], "cancelled")].append(o)

    # ④⑤ 장중 손절·목표
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if not b or b.get("low") is None:
            continue
        hit_stop = b["low"] <= p["stop"]
        if p["id"] in new_ids:
            if hit_stop:
                ev["exits"].append(_close_position(L, p, date, p["stop"], "stop",
                                                   note="체결일 손절(보수적)"))
            continue
        hit_tgt = bool(p.get("target")) and b.get("high") is not None and b["high"] >= p["target"]
        if hit_stop:
            ev["exits"].append(_close_position(
                L, p, date, p["stop"], "stop",
                note="목표·손절 동시 도달 → 손절 우선" if hit_tgt else None))
        elif hit_tgt:
            ev["exits"].append(_close_position(L, p, date, p["target"], "target"))

    # ⑥ 만기 종가 청산 · ⑦ 평가
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if b and b.get("close"):
            p["lastClose"] = float(b["close"])
            if p["holdDay"] >= config.HOLD_DAYS:
                ev["exits"].append(_close_position(L, p, date, b["close"], "expiry"))
    value = sum(p["qty"] * (p.get("lastClose") or p["entry"]) for p in L["positions"])
    eq = L["cash"] + value
    L["equity"].append({"date": date, "cash": round(L["cash"]), "value": round(value),
                        "equity": round(eq),
                        "retPct": round((eq / L["capital0"] - 1) * 100, 3),
                        "positions": len(L["positions"])})
    L["asof"] = date
    return ev
