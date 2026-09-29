"""일일 작업(5단계) — 평일 16:30 KST(swing-cron). 순서:
  1) 원장 갱신: 오늘 일봉으로 체결·손절·목표·만기·PM 매도 예약 실행(engine.settle_day)
  2) 매도 검토: 보유 종목 중 트리거(섹터 수급이탈/동반약세 전환 or 종목 하방 칸) →
     trading_agent → PM Sell/Underweight 면 다음 영업일 시가 매도 예약
  3) 신규 분석: 동반강세·수급유입 칸 종목(보유·주문 대기 제외, 상한 MAX_NEW_PER_DAY) →
     trading_agent → 조건 충족 시 다음 영업일 1일 유효 지정가 주문
주간 브리핑이 오늘자(asof)가 아니면 2)·3)은 건너뛴다(스테일 신호로 매매 금지) — 1)은 진행.
"""
import datetime
import traceback

from . import agent_iface as A
from . import config, engine, signals, tradedays


def _dkey(date, code, purpose):
    return f"decision/{date}/{code}/{purpose}"


def _safe_decide(agent, req):
    try:
        return agent.decide(req)
    except Exception as ex:
        traceback.print_exc()
        return A.Decision(code=req.code, date=req.date, purpose=req.purpose,
                          agent=getattr(agent, "name", ""), error=f"{type(ex).__name__}: {ex}")


def run_day(date, store, agent, prices, wb, *, force=False, log=print):
    """하루 처리 → run 로그 dict. 원장·판단·로그는 store 에 저장."""
    run = {"date": date, "startedAt": datetime.datetime.now().isoformat(timespec="seconds"),
           "agent": getattr(agent, "name", ""), "notes": []}
    if not tradedays.is_trading_day(date):
        run["skip"] = "휴장일"
        return run
    L = store.get("ledger") or engine.new_ledger()
    if L.get("asof") and date <= L["asof"] and not force:
        run["skip"] = f"이미 처리됨(asof {L['asof']})"
        return run

    # 1) 원장 갱신 — 후보 종목 전일 종가도 같은 호출로 받는다
    wb_ok = bool(wb) and signals.asof_date(wb) == date
    if wb and not wb_ok:
        run["notes"].append(f"주간 브리핑 asof {signals.asof_date(wb)} ≠ {date} — 신규·매도 검토 생략")
    elif not wb:
        run["notes"].append("주간 브리핑 없음 — 신규·매도 검토 생략")
    held = {p["code"] for p in L["positions"]}
    pend = {o["code"] for o in L["orders"]}
    cands = signals.buy_candidates(wb, held | pend) if wb_ok else []
    skipped_cap = cands[config.MAX_NEW_PER_DAY:]
    cands = cands[:config.MAX_NEW_PER_DAY]
    need = held | {o["code"] for o in L["orders"] if o["validDate"] <= date} | {c["code"] for c in cands}
    bars = prices.daily_bars(sorted(need), date)
    missing = sorted(held - set(bars))
    if missing:
        run["notes"].append(f"보유 종목 시세 없음: {', '.join(missing)}")
    ev = engine.settle_day(L, date, bars)
    run["events"] = {k: [x.get("id") for x in v] for k, v in ev.items() if isinstance(v, list)}
    run["exits"] = [{"code": t["code"], "name": t["name"], "reason": t["reason"],
                     "retPct": t["retPct"]} for t in ev.get("exits", [])]
    nxt = tradedays.next_trading_day(date)

    # 2) 매도 검토
    run["reviews"] = []
    if wb_ok:
        for t in signals.review_triggers(L["positions"], wb):
            p = next(x for x in L["positions"] if x["code"] == t["code"])
            req = A.DecisionRequest(code=t["code"], name=t["name"], date=date, purpose="review",
                                    sector=t["sector"], signal=t["signal"],
                                    reason="; ".join(t["reasons"]), last_close=p.get("lastClose"),
                                    position={k: p.get(k) for k in ("entry", "stop", "target",
                                                                    "fillDate", "holdDay", "qty")})
            d = _safe_decide(agent, req)
            key = _dkey(date, t["code"], "review")
            store.put(key, d.to_dict())
            sell = A.sell_verdict(d)
            if sell:
                engine.schedule_sell(L, t["code"], f"PM {d.rating} — {req.reason}", key, date)
            run["reviews"].append({"code": t["code"], "name": t["name"], "trigger": req.reason,
                                   "rating": d.rating, "sell": sell, "error": d.error,
                                   "decisionKey": key})

    # 3) 신규 분석·주문
    run["candidates"] = []
    for c in cands:
        b = bars.get(c["code"]) or {}
        req = A.DecisionRequest(code=c["code"], name=c["name"], date=date, purpose="entry",
                                sector=c["sector"], signal=c["signal"], reason=c["reason"],
                                last_close=b.get("close"))
        d = _safe_decide(agent, req)
        key = _dkey(date, c["code"], "entry")
        store.put(key, d.to_dict())
        ok, why = A.entry_verdict(d)
        row = {"code": c["code"], "name": c["name"], "signal": c["signal"],
               "sector": c["sector"], "rating": d.rating, "action": d.action,
               "entry": d.entry, "stop": d.stop, "target": A.effective_target(d),
               "ordered": False, "why": why, "decisionKey": key}
        if ok:
            o = engine.place_entry(L, code=c["code"], name=c["name"], date=date, valid_date=nxt,
                                   entry=d.entry, stop=d.stop, target=A.effective_target(d),
                                   weight=A.effective_weight(d), sector=c["sector"],
                                   signal=c["signal"], decision_key=key)
            row.update(ordered=o["status"] == "open", orderId=o["id"], qty=o["qty"],
                       why=o.get("note", ""))
        run["candidates"].append(row)
    run["capSkipped"] = [c["code"] for c in skipped_cap]

    if wb_ok:
        store.put(f"signals/{date}", signals.compact(wb))
    store.put("ledger", L)
    run["equity"] = L["equity"][-1] if L["equity"] else None
    run["finishedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    store.put(f"run/{date}", run)
    log(f"[swing] {date} 청산 {len(run['exits'])} · 검토 {len(run['reviews'])} · "
        f"후보 {len(run['candidates'])} · 주문 {sum(r['ordered'] for r in run['candidates'])} · "
        f"평가 {run['equity']['equity'] if run['equity'] else '-'}")
    return run
