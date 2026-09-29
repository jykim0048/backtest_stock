"""원장 엔진(2단계) — 체결·청산·현금부족·평가. 순수 함수(원장 dict in → 같은 dict 갱신),
네트워크·저장소 무관. 가격은 에이전트 가격, 일봉 고저는 '도달 여부' 판정에만 쓴다.

하루(D) 처리 순서 — settle_day(ledger, D, bars):
  [시가]  ① 전일 청산 예약(PM 매도·목표/만기 재판별 매도·최대 보유) → 시가 매도(숏은 환매)
          ② 보유 종목 갭 손절: Long 시가 ≤ 손절가 / Short 시가 ≥ 손절가 → 시가 청산
  [장중]  ③ D 가 유효일인 진입 주문: Long 저가 ≤ 진입가 / Short 고가 ≥ 진입가 → 진입가 체결.
             현금 부족이면 평가손실 보유 종목을 손실률 큰 순으로 시가 청산해 확보(합쳐도 부족하면
             청산 없이 스킵). 미도달·시세 없음 → 취소
          ④ 기존 보유: 손절 도달(Long 저가 ≤ / Short 고가 ≥) → 손절가 즉시 청산. 목표 도달(Long 고가 ≥ /
             Short 저가 ≤) → **청산하지 않고 재판별 표시**(reviewFlag="target", 2026-09-29) — 둘 다면 손절 우선
          ⑤ 당일 체결분: 손절 도달 → 손절가(보수적). 목표가는 체결 전후 순서를 알 수 없어
             체결일엔 미적용(보수적)
  [종가]  ⑥ 보유일 ≥ HOLD_DAYS(체결일=1일째) → **청산하지 않고 재판별 표시**(reviewFlag="expiry").
             보유일 ≥ MAX_HOLD_DAYS → 판별 없이 다음 영업일 시가 청산 예약(max_hold)
          ⑦ Short 대차수수료 차감(진입금액 × BORROW_RATE × 일수/365) · 평가(종가, 시세 없으면 직전 종가)
재판별(daily.run_day, 장 마감 후): Long PM Sell/Underweight · Short PM Buy/Overweight → schedule_sell
(다음 날 시가), 그 외 → apply_hold(목표 도달이면 새 목표가·본전 손절, 이후 매일 재판별 = extended).

비용·자금(2026-09-30 Short 추가):
  Long  : 매수 수수료 없음, 매도 대금 × SELL_TAX.
  Short : 공매도 진입(매도) 대금 × SELL_TAX, 환매(매수) 비용 없음, 대차수수료 일할.
          체결 시 진입금액 100% 를 담보로 현금에서 묶고 매도대금은 현금에 넣지 않는다(레버리지 없음).
          평가 = 담보 + (진입가 − 현재가) × 수량. Short 총노출(수량 × 현재가)은 평가액 × SHORT_MAX_GROSS 이내.
"""
import datetime
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


def is_short(x):
    return (x or {}).get("side") == "short"


def _px(p):
    return p.get("lastClose") or p["entry"]


def position_value(p, px=None):
    """평가액 기여분. Long = 수량 × 현재가, Short = 담보(수량 × 진입가) + (진입가 − 현재가) × 수량."""
    px = _px(p) if px is None else px
    if is_short(p):
        return p["qty"] * (2 * p["entry"] - px)
    return p["qty"] * px


def short_exposure(L):
    """Short 총노출 = 보유 숏 수량 × 현재가 + 대기 숏 주문 수량 × 진입가."""
    return (sum(p["qty"] * _px(p) for p in L["positions"] if is_short(p))
            + sum(o["qty"] * o["entry"] for o in L["orders"] if is_short(o)))


def equity_now(L):
    return L["cash"] + sum(position_value(p) for p in L["positions"])


# ── 주문 생성(판단 직후, D 장 마감 후) ─────────────────────────────────────────
def place_entry(L, *, code, name, date, valid_date, entry, stop, target, weight,
                sector=None, signal=None, decision_key=None, side="long"):
    """다음 영업일(valid_date) 1일 유효 지정가 주문(Long 매수·Short 공매도).
    수량 = 비중 × D 종가 평가액 / 진입가. Short 는 총노출 상한을 넘으면 스킵."""
    eq = equity_now(L)
    qty = int(math.floor(weight * eq / entry)) if entry > 0 else 0
    o = {"id": _id(L, "o"), "code": code, "name": name, "side": side, "type": "entry",
         "created": date, "validDate": valid_date, "entry": float(entry), "stop": float(stop),
         "target": float(target) if target else None, "weight": weight, "qty": qty,
         "sector": sector, "signal": signal, "decisionKey": decision_key, "status": "open"}
    note = None
    if qty <= 0:
        note = "수량 0(비중·가격)"
    elif side == "short" and short_exposure(L) + qty * entry > config.SHORT_MAX_GROSS * eq:
        note = f"Short 총노출 상한 {config.SHORT_MAX_GROSS * 100:.0f}% 초과"
    if note:
        o.update(status="skipped", note=note)
        L["orderLog"].append(o)
        return o
    L["orders"].append(o)
    return o


def schedule_sell(L, code, reason, decision_key=None, date=None, kind="pm_sell"):
    """보유 종목을 다음 영업일 시가에 청산 예약(Long 매도·Short 환매). kind = 청산 사유 코드
    (pm_sell 신호 트리거 PM 판정 · review_target 목표 도달 후 PM 판정 · review_expiry 만기/연장 후 PM 판정 ·
    max_hold 최대 보유 도달)."""
    for p in L["positions"]:
        if p["code"] == code and not p.get("sellPending"):
            p["sellPending"] = {"reason": reason, "decisionKey": decision_key, "date": date, "kind": kind}
            return True
    return False


def apply_hold(L, code, *, target=None, stop=None, decision_key=None, date=None):
    """재판별 결과 '계속 보유'(2026-09-29 사용자 합의, Short 는 방향만 반대).
    - 목표 도달(reviewFlag=target): 목표가 = 새 목표가(Long 현재가 초과·Short 현재가 미만일 때만, 아니면 없음),
      손절가 = 본전 손절 — Long max(새 손절, 기존, 매수가) / Short min(새 손절, 기존, 공매도가).
    - 만기·연장(expiry/extended): 새 목표가·손절가가 유효하면 교체, 아니면 유지.
    이후 매일 재판별(extended=True). 반환: 갱신된 포지션 또는 None."""
    for p in L["positions"]:
        if p["code"] != code:
            continue
        px = _px(p)
        flag = p.get("reviewFlag")
        short = is_short(p)
        ok_t = bool(target) and (target < px if short else target > px)
        ok_s = bool(stop) and (stop > px if short else stop < px)
        if flag == "target":
            p["target"] = float(target) if ok_t else None
            be = min if short else max
            p["stop"] = float(be([p["stop"], p["entry"]] + ([stop] if ok_s else [])))
        else:
            if ok_t:
                p["target"] = float(target)
            if ok_s:
                p["stop"] = float(stop)
        p["extended"] = True
        p.setdefault("holdLog", []).append({"date": date, "flag": flag or "extended", "decisionKey": decision_key,
                                            "target": p["target"], "stop": p["stop"]})
        p["reviewFlag"] = None
        return p
    return None


# ── 체결 헬퍼 ────────────────────────────────────────────────────────────────
def _close_position(L, p, date, price, reason, note=None):
    cost = p["qty"] * p["entry"]
    if is_short(p):
        # 환매: 담보 반환 + (공매도가 − 환매가) × 수량. 세금은 진입 때 이미 냄
        gain = p["qty"] * (p["entry"] - price)
        L["cash"] += cost + gain
        tax = p.get("entryTax", 0.0)
        fee = p.get("borrowFee", 0.0)
        pnl = gain - tax - fee
    else:
        gross = p["qty"] * price
        tax = gross * config.SELL_TAX
        fee = 0.0
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
    if fee:
        t["borrowFee"] = round(fee)
    if note:
        t["note"] = note
    L["trades"].append(t)
    return t


def _loss_rate(p, px):
    """방향 반영 손익률 — 음수가 평가손실(숏은 가격 상승이 손실)."""
    r = px / p["entry"] - 1
    return -r if is_short(p) else r


def _raise_cash(L, need, date, bars, exclude):
    """평가손실 보유 종목을 손실률 큰 순으로 시가 청산해 need 확보. 합쳐도 부족하면 아무것도
    팔지 않고 False(이익 종목은 건드리지 않음)."""
    losers = []
    for p in L["positions"]:
        b = bars.get(p["code"])
        if p["id"] in exclude or not b or not b.get("open"):
            continue
        r = _loss_rate(p, b["open"])
        if r < 0:
            losers.append((r, p, b["open"]))
    losers.sort(key=lambda x: x[0])
    avail = sum(position_value(p, px) if is_short(p) else p["qty"] * px * (1 - config.SELL_TAX)
                for _, p, px in losers)
    if L["cash"] + avail < need:
        return False, []
    sold = []
    for r, p, px in losers:
        if L["cash"] >= need:
            break
        sold.append(_close_position(L, p, date, px, "cash",
                                    note=f"현금 확보(시가 손실률 {r * 100:.2f}%)"))
    return True, sold


def _days_between(a, b):
    try:
        return max((datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days, 1)
    except (TypeError, ValueError):
        return 1


# ── 하루 정산 ────────────────────────────────────────────────────────────────
def settle_day(L, date, bars):
    """date 영업일 처리. bars = {code: {open, high, low, close}}. 이미 처리한 날은 무시.
    반환: 그날 이벤트 {filled, cancelled, skipped, exits}."""
    if L.get("asof") and date <= L["asof"]:
        return {"skip": f"이미 정산됨(asof {L['asof']})"}
    prev = L.get("asof")
    ev = {"filled": [], "cancelled": [], "skipped": [], "exits": []}

    for p in L["positions"]:
        p["holdDay"] += 1

    # ① 청산 예약 → 시가
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if p.get("sellPending") and b and b.get("open"):
            sp = p["sellPending"]
            ev["exits"].append(_close_position(L, p, date, b["open"], sp.get("kind") or "pm_sell",
                                               note=sp.get("reason")))
    # ② 갭 손절 → 시가
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if b and b.get("open") and (b["open"] >= p["stop"] if is_short(p) else b["open"] <= p["stop"]):
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
        short = is_short(o)
        if not b or b.get("low") is None or (short and b.get("high") is None):
            o.update(status="cancelled", note="시세 없음")
        elif any(p["code"] == o["code"] for p in L["positions"]):
            o.update(status="skipped", note="이미 보유")
        elif short and b["high"] < o["entry"]:
            o.update(status="cancelled",
                     note=f"미도달(고가 {b['high']:,.0f} < 진입 {o['entry']:,.0f})")
        elif not short and b["low"] > o["entry"]:
            o.update(status="cancelled",
                     note=f"미도달(저가 {b['low']:,.0f} > 진입 {o['entry']:,.0f})")
        else:
            notional = o["qty"] * o["entry"]
            tax = notional * config.SELL_TAX if short else 0.0
            cost = notional + tax                         # 숏: 담보 + 공매도 매도세
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
                if short:
                    p.update(entryTax=tax, borrowFee=0.0)
                L["positions"].append(p)
                new_ids.add(p["id"])
                o.update(status="filled", fillPrice=o["entry"], positionId=p["id"])
        L["orderLog"].append(o)
        ev[{"filled": "filled", "skipped": "skipped"}.get(o["status"], "cancelled")].append(o)

    # ④⑤ 장중 손절·목표
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if not b or b.get("low") is None or (is_short(p) and b.get("high") is None):
            continue
        if is_short(p):
            hit_stop = b["high"] >= p["stop"]
            hit_tgt = bool(p.get("target")) and b["low"] <= p["target"]
            extreme = b["low"]
        else:
            hit_stop = b["low"] <= p["stop"]
            hit_tgt = bool(p.get("target")) and b.get("high") is not None and b["high"] >= p["target"]
            extreme = b.get("high")
        if p["id"] in new_ids:
            if hit_stop:
                ev["exits"].append(_close_position(L, p, date, p["stop"], "stop",
                                                   note="체결일 손절(보수적)"))
            continue
        if hit_stop:
            ev["exits"].append(_close_position(
                L, p, date, p["stop"], "stop",
                note="목표·손절 동시 도달 → 손절 우선" if hit_tgt else None))
        elif hit_tgt and not p.get("sellPending"):
            p["reviewFlag"] = "target"                      # 청산하지 않음 — 장 마감 후 재판별
            p["targetHit"] = {"date": date, "target": p["target"], "high" if not is_short(p) else "low": extreme}

    # ⑥ 만기 → 재판별 표시 / 최대 보유 → 다음 날 시가 청산 예약 · ⑦ 대차수수료·평가
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if b and b.get("close"):
            p["lastClose"] = float(b["close"])
        if is_short(p):
            days = 1 if p["id"] in new_ids else _days_between(prev, date)
            fee = p["qty"] * p["entry"] * config.BORROW_RATE * days / 365
            p["borrowFee"] = p.get("borrowFee", 0.0) + fee
            L["cash"] -= fee
        if p.get("sellPending"):
            continue
        if p["holdDay"] >= config.MAX_HOLD_DAYS:
            schedule_sell(L, p["code"], f"최대 보유 {config.MAX_HOLD_DAYS}영업일 도달", date=date, kind="max_hold")
            p["reviewFlag"] = None
            ev.setdefault("maxHold", []).append({"id": p["id"], "code": p["code"]})
        elif p["holdDay"] >= config.HOLD_DAYS and not p.get("reviewFlag"):
            p["reviewFlag"] = "expiry"
    value = sum(position_value(p) for p in L["positions"])
    eq = L["cash"] + value
    L["equity"].append({"date": date, "cash": round(L["cash"]), "value": round(value),
                        "equity": round(eq),
                        "retPct": round((eq / L["capital0"] - 1) * 100, 3),
                        "positions": len(L["positions"])})
    L["asof"] = date
    return ev
