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

비용·자금(2026-09-29 Short 추가):
  Long  : 매수 수수료 없음, 매도 대금 × SELL_TAX.
  Short : 공매도 진입(매도) 대금 × SELL_TAX, 환매(매수) 비용 없음, 대차수수료 일할.
          담보(2026-10-18, SHORT_COLLATERAL):
            subst(기본) = 주식 대용 — 담보 여력 = 현금 + 보유 Long 평가액 × SHORT_SUBST_RATE(70%) − 요구 담보
                          (대용 Short 평가액 × SHORT_MARGIN). 진입 때 현금은 매도세만 나가고 매도대금은 현금에 넣지 않는다.
                          평가 = (진입가 − 현재가) × 수량. 요구 담보가 대용가로 모자라는 만큼 현금이 묶여(pledged) Long 매수에
                          못 쓴다. 장 마감 담보 부족이면 손실 큰 Short 부터 다음 영업일 시가 환매 예약(반대매매, kind=margin).
            cash = 종전 — 진입금액 100% 를 현금에서 묶음, 평가 = 담보 + (진입가 − 현재가) × 수량.
          포지션마다 진입 당시 방식(p["collateral"])으로 정산. Short 총노출(수량 × 현재가)은 SHORT_MAX_GROSS > 0 이면
          평가액 × SHORT_MAX_GROSS 이내(기본 0 = 상한 없음).
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


def is_subst(p):
    """주식 대용 담보 Short(2026-10-18) — 진입 시 현금을 묶지 않은 포지션."""
    return is_short(p) and p.get("collateral") == "subst"


def position_value(p, px=None):
    """평가액 기여분. Long = 수량 × 현재가, Short(현금 담보) = 담보(수량 × 진입가) + (진입가 − 현재가) × 수량,
    Short(대용 담보) = (진입가 − 현재가) × 수량."""
    px = _px(p) if px is None else px
    if is_subst(p):
        return p["qty"] * (p["entry"] - px)
    if is_short(p):
        return p["qty"] * (2 * p["entry"] - px)
    return p["qty"] * px


def collateral(L, px=None):
    """대용 담보 현황 → {subst: Long 대용가 합, req: 요구 담보, pledged: 대용가로 모자라 묶인 현금, room: 추가 Short 여력,
    free: Long 매수에 쓸 수 있는 현금}. px = {code: 가격}(없으면 직전 종가)."""
    px = px or {}
    pv = lambda p: px.get(p["code"]) or _px(p)
    subst = sum(p["qty"] * pv(p) for p in L["positions"] if not is_short(p)) * config.SHORT_SUBST_RATE
    req = sum(p["qty"] * pv(p) for p in L["positions"] if is_subst(p)) * config.SHORT_MARGIN
    pledged = max(0.0, req - subst)
    return {"subst": subst, "req": req, "pledged": pledged,
            "room": L["cash"] + subst - req, "free": L["cash"] - pledged}


def short_exposure(L):
    """Short 총노출 = 보유 숏 수량 × 현재가 + 대기 숏 주문 수량 × 진입가."""
    return (sum(p["qty"] * _px(p) for p in L["positions"] if is_short(p))
            + sum(o["qty"] * o["entry"] for o in L["orders"] if is_short(o)))


def equity_now(L):
    return L["cash"] + sum(position_value(p) for p in L["positions"])


# ── 주문 생성(판단 직후, D 장 마감 후) ─────────────────────────────────────────
def place_entry(L, *, code, name, date, valid_date, entry, stop, target, weight,
                sector=None, signal=None, decision_key=None, side="long", source=None, qty=None, extra=None):
    """다음 영업일(valid_date) 1일 유효 지정가 주문(Long 매수·Short 공매도).
    수량 = 비중 × D 종가 평가액 / 진입가(qty 를 주면 그 수량 — 부분 진입 잔여분 추가 주문). Short 는 총노출 상한을 넘으면 스킵.
    extra = 주문에 덧붙일 필드(topupOf·topup 등)."""
    eq = equity_now(L)
    if qty is None:
        qty = int(math.floor(weight * eq / entry)) if entry > 0 else 0
    o = {"id": _id(L, "o"), "code": code, "name": name, "side": side, "type": "entry",
         "created": date, "validDate": valid_date, "entry": float(entry), "stop": float(stop),
         "target": float(target) if target else None, "weight": weight, "qty": qty,
         "sector": sector, "signal": signal, "decisionKey": decision_key, "status": "open",
         "source": source or "matrix"}
    if extra:
        o.update(extra)
    note = None
    if qty <= 0:
        note = "수량 0(비중·가격)"
    elif side == "short" and config.SHORT_MAX_GROSS > 0 and short_exposure(L) + qty * entry > config.SHORT_MAX_GROSS * eq:
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


def _stop_suspended(p, date):
    """체결일 손절가 도달 후 재판별 전(체결 다음 영업일) — 그날 갭·장중 손절을 적용하지 않는다."""
    fs = p.get("fillStop")
    return bool(fs) and not fs.get("reviewed") and fs.get("date") and fs["date"] < date


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
        if flag != "fill_stop":
            p["extended"] = True                             # 체결일 손절 재판별은 연장 보유가 아님
        p.setdefault("holdLog", []).append({"date": date, "flag": flag or "extended", "decisionKey": decision_key,
                                            "target": p["target"], "stop": p["stop"]})
        p["reviewFlag"] = None
        return p
    return None


# ── 체결 헬퍼 ────────────────────────────────────────────────────────────────
def close_position(L, p, date, price, reason, note=None):
    """외부(daily) 정리용 공개 청산 — 그날 지정 가격으로 즉시 청산(예: 선물 미상장 Short 종가 환매)."""
    return _close_position(L, p, date, price, reason, note=note)


def _close_position(L, p, date, price, reason, note=None, qty=None):
    """qty 를 주면 그 수량만 청산(분할 청산, 2026-10-18) — 숏 진입세·대차수수료는 수량 비율로 나눠 반영하고 포지션은 남는다."""
    q = p["qty"] if qty is None else min(int(qty), p["qty"])
    part = q < p["qty"]
    f = q / p["qty"] if p["qty"] else 1.0
    cost = q * p["entry"]
    if is_short(p):
        # 환매: 담보 반환 + (공매도가 − 환매가) × 수량. 세금은 진입 때 이미 냄
        gain = q * (p["entry"] - price)
        L["cash"] += gain if is_subst(p) else cost + gain
        tax = p.get("entryTax", 0.0) * f
        fee = p.get("borrowFee", 0.0) * f
        pnl = gain - tax - fee
        if part:
            p["entryTax"] = p.get("entryTax", 0.0) - tax
            p["borrowFee"] = p.get("borrowFee", 0.0) - fee
    else:
        gross = q * price
        tax = gross * config.SELL_TAX
        fee = 0.0
        pnl = gross - tax - cost
        L["cash"] += gross - tax
    if part:
        p["qty"] -= q
    else:
        L["positions"].remove(p)
    t = {"id": _id(L, "t"), "code": p["code"], "name": p["name"], "side": p["side"],
         "qty": q, "entryDate": p["fillDate"], "entryPrice": p["entry"],
         "exitDate": date, "exitPrice": float(price), "reason": reason,
         "holdDays": p["holdDay"], "tax": round(tax), "pnl": round(pnl),
         "retPct": round(pnl / cost * 100, 2) if cost else 0.0,
         "sector": p.get("sector"), "signal": p.get("signal"), "source": p.get("source"),
         "decisionKey": p.get("decisionKey"),
         "exitDecisionKey": (p.get("sellPending") or {}).get("decisionKey")}
    if fee:
        t["borrowFee"] = round(fee)
    if note:
        t["note"] = note
    if part:
        t["partial"] = True
    L["trades"].append(t)
    return t


def _loss_rate(p, px):
    """방향 반영 손익률 — 음수가 평가손실(숏은 가격 상승이 손실)."""
    r = px / p["entry"] - 1
    return -r if is_short(p) else r


def _loser_avail(L, bars, exclude):
    """평가손실 보유 종목(손실률 큰 순)과 시가 청산 시 확보 가능한 현금 합 → (avail, losers)."""
    losers = []
    for p in L["positions"]:
        b = bars.get(p["code"])
        if p["id"] in exclude or not b or not b.get("open") or is_subst(p):
            continue                                       # 대용 Short 환매는 현금을 만들지 않는다(손실만 확정)
        r = _loss_rate(p, b["open"])
        if r < 0:
            losers.append((r, p, b["open"]))
    losers.sort(key=lambda x: x[0])
    avail = sum(position_value(p, px) if is_short(p) else p["qty"] * px * (1 - config.SELL_TAX)
                for _, p, px in losers)
    return avail, losers


def _raise_cash(L, need, date, bars, exclude):
    """평가손실 보유 종목을 손실률 큰 순으로 시가 청산해 need 확보. 합쳐도 부족하면 아무것도
    팔지 않고 False(이익 종목은 건드리지 않음). 대용 담보로 묶인 현금(pledged)은 쓸 수 없는 현금으로 본다
    (Long 을 팔면 대용가도 줄어 묶임이 늘 수 있어 청산 중 매번 다시 계산)."""
    avail, losers = _loser_avail(L, bars, exclude)
    if _free_cash(L) + avail < need:
        return False, []
    sold = []
    for r, p, px in losers:
        if _free_cash(L) >= need:
            break
        sold.append(_close_position(L, p, date, px, "cash",
                                    note=f"현금 확보(시가 손실률 {r * 100:.2f}%)"))
    return True, sold


def _free_cash(L):
    return collateral(L)["free"] if any(is_subst(p) for p in L["positions"]) else L["cash"]


def _days_between(a, b):
    try:
        return max((datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days, 1)
    except (TypeError, ValueError):
        return 1


# ── 하루 정산 ────────────────────────────────────────────────────────────────
def _intraday(doc, date):
    """Live Trader(장중 집행기) 기록(swing/vwap, PLAN_LIVE_TRADER.md) → (doc, 감시 주문 id, 감시 포지션 id, 세션 완주). 없으면 None."""
    if not doc or doc.get("date") != date or not doc.get("asof"):
        # 기록이 없거나 시세를 한 번도 못 받은 날(asof 없음 — 2026-10-19 /prices 전부 시간 초과) → 종전 일봉 규칙
        return None, set(), set(), False
    w = doc.get("watched") or {}
    return doc, set(w.get("orders") or []), set(w.get("positions") or []), bool(doc.get("complete"))


def _hm(ts):
    return (ts or "")[11:16]


def _apply_exit(L, p, rec, date, ev, reason, carry=True):
    """장중 실행기 청산 슬라이스 기록(시가 10%·VWAP 분할·±5% 전량·손절 첫 분할) → 체결 합 수량을 평균가로 청산.
    carry=True 면 남은 수량은 다음 영업일 계속(sellPending — 실행기가 없으면 엔진 ① 시가 청산).
    carry=False(Live PM trim) 면 못 채운 trim 수량은 버리고 포지션은 그대로 둔다. 반환 True = 기록 반영."""
    fills = (rec or {}).get("fills") or []
    q = min(sum(f["qty"] for f in fills), p["qty"])
    if q <= 0:
        return False
    avg = sum(f["qty"] * f["price"] for f in fills) / sum(f["qty"] for f in fills)
    sp = p.get("sellPending") or {}
    modes = "·".join(dict.fromkeys(f.get("mode") or "?" for f in fills))
    note = f"장중 분할 {len(fills)}회({modes}) {_hm(fills[0]['ts'])}~{_hm(fills[-1]['ts'])}"
    if sp.get("reason"):
        note = f"{sp['reason']} · {note}"
    ev["exits"].append(_close_position(L, p, date, avg, reason, note=note, qty=q))
    if carry and p in L["positions"]:
        p["sellPending"] = {"reason": f"분할 청산 잔여 {p['qty']:,}주 — 다음 영업일 계속", "decisionKey": sp.get("decisionKey"),
                            "date": date, "kind": reason}
    return True


def settle_day(L, date, bars, intraday=None):
    """date 영업일 처리. bars = {code: {open, high, low, close}}. 이미 처리한 날은 무시.
    intraday = 그날 Live Trader(장중 집행기) 문서(intraday/<date>) — 감시한 주문은 분할 체결 기록으로 체결, 감시한 포지션은
    장중 손절 기록으로 청산(세션 완주면 일봉 손절 판정 생략). 없거나 감시 밖이면 종전 일봉 규칙(2026-10-18).
    반환: 그날 이벤트 {filled, cancelled, skipped, exits}."""
    if L.get("asof") and date <= L["asof"]:
        return {"skip": f"이미 정산됨(asof {L['asof']})"}
    prev = L.get("asof")
    ev = {"filled": [], "cancelled": [], "skipped": [], "exits": []}
    I, w_ord, w_pos, complete = _intraday(intraday, date)
    # 장중 기록으로 처리할 주문 = 분할 체결이 있었거나, 세션을 완주해 '조건 미충족 미체결'이 확정된 것. 미완주·무체결은 일봉 규칙(2026-10-19)
    w_ord = {oid for oid in w_ord if complete or ((I or {}).get("orders") or {}).get(oid, {}).get("fills")}
    i_exits = (I or {}).get("exits") or {}
    from_intraday = {}                                       # 새 포지션 id → 실행기 주문 기록

    for p in L["positions"]:
        p["holdDay"] += 1

    # ① 청산 예약 → 시가
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if p.get("sellPending") and p["id"] in i_exits:
            _apply_exit(L, p, i_exits[p["id"]], date, ev, p["sellPending"].get("kind") or "pm_sell")   # 시가 10% + VWAP 분할
            continue
        if p.get("sellPending") and complete and p["id"] in w_pos:
            continue                                         # 실행기가 하루 감시했지만 체결 없음(거래정지 등) — 다음 날 계속
        if p.get("sellPending") and b and b.get("open"):
            sp = p["sellPending"]
            ev["exits"].append(_close_position(L, p, date, b["open"], sp.get("kind") or "pm_sell",
                                               note=sp.get("reason")))
    # ② 갭 손절 → 시가 (체결일 손절 재판별 대기 포지션은 그날 손절 유예 — 2026-10-16)
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        if _stop_suspended(p, date):
            continue
        rec = i_exits.get(p["id"])
        if rec and rec.get("trigger") == "stop" and rec.get("kind") == "stop_gap":
            _apply_exit(L, p, rec, date, ev, "stop_gap")
            continue
        if rec or (complete and p["id"] in w_pos):
            continue                                         # 장중 실행기가 판정(④에서 손절가 청산 또는 미도달)
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
        elif o.get("topupOf") and not any(p["id"] == o["topupOf"] for p in L["positions"]):
            o.update(status="cancelled", note="본 포지션 청산됨 — 추가 진입 취소")
        elif not o.get("topupOf") and any(p["code"] == o["code"] for p in L["positions"]):
            o.update(status="skipped", note="이미 보유")
        elif o["id"] in w_ord and not sum(f["qty"] for f in ((I["orders"].get(o["id"]) or {}).get("fills") or [])):
            o.update(status="cancelled", note="미도달(VWAP 분할 — 진입가 상한·VWAP 조건 미충족)")
        elif o["id"] not in w_ord and short and b["high"] < o["entry"]:
            o.update(status="cancelled",
                     note=f"미도달(고가 {b['high']:,.0f} < 진입 {o['entry']:,.0f})")
        elif o["id"] not in w_ord and not short and b["low"] > o["entry"]:
            o.update(status="cancelled",
                     note=f"미도달(저가 {b['low']:,.0f} > 진입 {o['entry']:,.0f})")
        else:
            irec = I["orders"].get(o["id"]) if o["id"] in w_ord else None
            if irec:
                # 장중 VWAP 분할 체결(2026-10-18) — 수량 = 체결 합, 가격 = 체결 평균. 진입가(o["entry"])는 상한·다음 날 잔여분 레벨로 유지
                fq = sum(f["qty"] for f in irec["fills"])
                px = sum(f["qty"] * f["price"] for f in irec["fills"]) / fq
                o["vwapFills"] = irec["fills"]
                if fq < o["qty"]:
                    planned = o["qty"]
                    o["partial"] = {"planned": planned, "filled": fq, "remain": planned - fq}
                    o["note"] = f"장중 분할 {len(irec['fills'])}회 — {fq:,}/{planned:,}주, 잔여 {planned - fq:,}주 다음 영업일 추가 주문"
                o["qty"] = fq
            else:
                px = o["entry"]
            if short and (o.get("collateral") or config.SHORT_COLLATERAL) == "subst":
                # 주식 대용 담보(2026-10-18): 주당 요구 담보 = 진입가 × SHORT_MARGIN, 현금은 매도세만 나감
                o["collateral"] = "subst"
                unit = px * config.SHORT_MARGIN
                room = collateral(L)["room"] - o["qty"] * px * config.SELL_TAX
                if room < o["qty"] * unit:
                    q2 = min(o["qty"], int(math.floor(max(room, 0) / (px * (config.SHORT_MARGIN + config.SELL_TAX)))))
                    if config.PARTIAL_ENTRY and q2 > 0 and q2 >= math.ceil(o["qty"] * config.PARTIAL_ENTRY_MIN):
                        planned = (o.get("partial") or {}).get("planned") or o["qty"]     # VWAP 분할 잔여가 있으면 원래 계획 기준
                        o["partial"] = {"planned": planned, "filled": q2, "remain": planned - q2}
                        o["qty"] = q2
                        o["note"] = f"담보 부족 — {q2:,}/{planned:,}주 부분 진입, 잔여 {planned - q2:,}주 다음 영업일 추가 주문"
                    else:
                        o.update(status="skipped", note=f"담보 부족(대용 포함 가능 {q2:,}/{o['qty']:,}주"
                                                        + (f" < 최소 {config.PARTIAL_ENTRY_MIN * 100:.0f}%)" if q2 else ")"))
                unit = px * config.SELL_TAX                                  # 현금 유출 = 매도세뿐
                cost = o["qty"] * unit
            else:
                unit = px * (1 + config.SELL_TAX) if short else px   # 주당 필요 현금(숏: 담보 + 매도세)
                cost = o["qty"] * unit
                if _free_cash(L) < cost:
                    ok, sold = _raise_cash(L, cost, date, bars, new_ids)
                    ev["exits"].extend(sold)
                    if not ok:
                        # 부분 진입(2026-10-16): 현금 + 손실 종목 청산 가능액으로 살 수 있는 수량이 계획의 PARTIAL_ENTRY_MIN 이상이면 그만큼만
                        q2 = int(math.floor((_free_cash(L) + _loser_avail(L, bars, new_ids)[0]) / unit)) if unit > 0 else 0
                        q2 = min(q2, o["qty"])
                        if config.PARTIAL_ENTRY and q2 > 0 and q2 >= math.ceil(o["qty"] * config.PARTIAL_ENTRY_MIN):
                            ok2, sold2 = _raise_cash(L, q2 * unit, date, bars, new_ids)
                            ev["exits"].extend(sold2)
                            if ok2:
                                planned = (o.get("partial") or {}).get("planned") or o["qty"]
                                o["partial"] = {"planned": planned, "filled": q2, "remain": planned - q2}
                                o["qty"], cost = q2, q2 * unit
                                o["note"] = f"현금 부족 — {q2:,}/{planned:,}주 부분 진입, 잔여 {planned - q2:,}주 다음 영업일 추가 주문"
                            else:
                                o.update(status="skipped", note="현금 부족(손실 종목 청산으로도 부족)")
                        else:
                            o.update(status="skipped", note=f"현금 부족(가능 {q2:,}/{o['qty']:,}주 < 최소 {config.PARTIAL_ENTRY_MIN * 100:.0f}%)")
            if o["status"] == "open" and o.get("topupOf"):
                # 추가 진입(잔여분) — 기존 포지션에 합쳐 평균단가
                notional = o["qty"] * px
                tax = notional * config.SELL_TAX if short else 0.0
                L["cash"] -= o["qty"] * unit
                p = next(x for x in L["positions"] if x["id"] == o["topupOf"])
                newq = p["qty"] + o["qty"]
                p["entry"] = (p["entry"] * p["qty"] + px * o["qty"]) / newq
                p["qty"] = newq
                if short:
                    p["entryTax"] = (p.get("entryTax") or 0.0) + tax
                p.setdefault("topups", []).append({"date": date, "qty": o["qty"], "price": px, "orderId": o["id"]})
                o.update(status="filled", fillPrice=px, positionId=p["id"])
                if o.get("partial"):
                    o["partial"]["positionId"] = p["id"]
                    ev.setdefault("partials", []).append(o)
            elif o["status"] == "open":
                notional = o["qty"] * px
                tax = notional * config.SELL_TAX if short else 0.0
                L["cash"] -= cost
                p = {"id": _id(L, "p"), "code": o["code"], "name": o["name"], "side": o["side"],
                     "qty": o["qty"], "entry": px, "stop": o["stop"],
                     "target": o["target"], "fillDate": date, "holdDay": 1,
                     "sector": o.get("sector"), "signal": o.get("signal"), "source": o.get("source"),
                     "decisionKey": o.get("decisionKey"), "orderId": o["id"],
                     "lastClose": None, "sellPending": None}
                p.update({k: o[k] for k in ("thesis", "invalidation", "horizon") if o.get(k)})   # Live PM 근거(PLAN_PM §5)
                if short:
                    p.update(entryTax=tax, borrowFee=0.0, collateral=o.get("collateral") or "cash")
                L["positions"].append(p)
                new_ids.add(p["id"])
                if irec:
                    from_intraday[p["id"]] = dict(irec, _oid=o["id"])
                o.update(status="filled", fillPrice=px, positionId=p["id"])
                if o.get("partial"):
                    o["partial"]["positionId"] = p["id"]
                    ev.setdefault("partials", []).append(o)
        L["orderLog"].append(o)
        ev[{"filled": "filled", "skipped": "skipped"}.get(o["status"], "cancelled")].append(o)

    # ④-0 Live PM trim(2026-10-19) — 먼저 반영(그 뒤 손절·지시 청산 기록은 남은 수량 기준)
    for key, rec in i_exits.items():
        if key.startswith("i:"):
            p = next((x for x in L["positions"] if x["id"] == rec.get("positionId")), None)
            if p is not None:
                _apply_exit(L, p, rec, date, ev, rec.get("kind") or "livepm_trim", carry=False)

    # ④⑤ 장중 손절·목표
    for p in list(L["positions"]):
        b = bars.get(p["code"])
        own = i_exits.get(p["id"]) or {}
        if own.get("trigger") == "livepm":                   # Live PM exit 지시 — 시세 유무와 관계없이 기록대로
            _apply_exit(L, p, own, date, ev, own.get("kind") or "livepm_exit")
            continue
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
        oid = (from_intraday.get(p["id"]) or {}).get("_oid")
        rec = i_exits.get(p["id"]) if (i_exits.get(p["id"]) or {}).get("trigger") == "stop" else (i_exits.get("o:" + oid) if oid else None)
        if rec:
            # 장중 실행기 손절(체결 후 손절 포함) — 손절 도달 10% + VWAP 분할·±5% 전량 기록으로 청산, 체결일 손절 유예 규칙보다 우선
            _apply_exit(L, p, rec, date, ev, rec.get("kind") or "stop")
            continue
        if p.get("sellPending") and p["id"] in i_exits:
            continue                                         # 오늘 분할 청산 중(잔여 다음 날)
        if complete and (p["id"] in w_pos or p["id"] in from_intraday):
            hit_stop = False                                 # 장중 실행기가 세션 내내 감시 — 손절 미도달로 확정
        if p["id"] in new_ids:
            if hit_stop:
                if config.FILL_DAY_STOP == "review":
                    # 2026-10-16: 체결일 손절가 도달 → D+1 손절 유예, D+1 종가로 재판별(daily 2단계), 매도면 D+2 시가
                    p["fillStop"] = {"date": date, "stop": p["stop"], "extreme": b["high"] if is_short(p) else b["low"]}
                    ev.setdefault("deferredStops", []).append(p)
                elif config.FILL_DAY_STOP == "next_open":
                    p["sellPending"] = {"reason": f"체결일 손절가 {p['stop']:,.0f} 도달 → 익일 시가 청산", "decisionKey": None,
                                        "date": date, "kind": "stop"}
                    ev.setdefault("deferredStops", []).append(p)
                else:
                    ev["exits"].append(_close_position(L, p, date, p["stop"], "stop",
                                                       note="체결일 손절(보수적)"))
            continue
        if hit_stop and _stop_suspended(p, date):
            hit_stop = False                                 # 체결일 손절 재판별 대기 — 오늘 종가로 재판별(daily)
        if hit_stop:
            ev["exits"].append(_close_position(
                L, p, date, p["stop"], "stop",
                note="목표·손절 동시 도달 → 손절 우선" if hit_tgt else None))
        elif hit_tgt and not p.get("sellPending"):
            p["reviewFlag"] = "target"                      # 청산하지 않음 — 장 마감 후 재판별
            p["targetHit"] = {"date": date, "target": p["target"], "high" if not is_short(p) else "low": extreme}

    # ⑤-1 Live PM 손절 상향·재판별 요청(2026-10-19) — 그날 손절 판정은 기존 손절가로 끝났으니 이제 원장에 반영
    for pid, mv in ((I or {}).get("stopMoves") or {}).items():
        p = next((x for x in L["positions"] if x["id"] == pid), None)
        ns = (mv or {}).get("newStop")
        if p is not None and ns is not None and ((ns < p["stop"]) if is_short(p) else (ns > p["stop"])):
            p["stopHistory"] = (p.get("stopHistory") or []) + [{"date": date, "from": p["stop"], "to": ns, "by": "livepm"}]
            p["stop"] = float(ns)
    for pid, rv in ((I or {}).get("reviews") or {}).items():
        p = next((x for x in L["positions"] if x["id"] == pid), None)
        if p is not None and not p.get("sellPending"):
            p["livepmReview"] = {"date": date, "ts": (rv or {}).get("ts"), "reason": (rv or {}).get("reason")}

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
    # 대용 담보 점검(2026-10-18) — 종가 기준 현금 + 대용가 < 요구 담보면 손실 큰 Short 부터 다음 영업일 시가 환매 예약
    if any(is_subst(p) for p in L["positions"]):
        c = collateral(L)
        if c["room"] < 0:
            short_gap = -c["room"]
            for p in sorted((x for x in L["positions"] if is_subst(x) and not x.get("sellPending")),
                            key=lambda x: _loss_rate(x, _px(x))):
                if short_gap <= 0:
                    break
                schedule_sell(L, p["code"], f"담보 부족 {short_gap:,.0f}원 — 반대매매(다음 영업일 시가 환매)", date=date, kind="margin")
                p["reviewFlag"] = None
                short_gap -= p["qty"] * _px(p) * config.SHORT_MARGIN
                ev.setdefault("marginCall", []).append({"id": p["id"], "code": p["code"]})
        L["collateral"] = {k: round(v) for k, v in collateral(L).items()}
    else:
        L.pop("collateral", None)
    value = sum(position_value(p) for p in L["positions"])
    eq = L["cash"] + value
    L["equity"].append({"date": date, "cash": round(L["cash"]), "value": round(value),
                        "equity": round(eq),
                        "retPct": round((eq / L["capital0"] - 1) * 100, 3),
                        "positions": len(L["positions"])})
    L["asof"] = date
    return ev
