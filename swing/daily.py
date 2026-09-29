"""일일 작업(5단계) — 평일 16:30 KST(swing-cron). 순서:
  1) 원장 갱신: 오늘 일봉으로 체결·손절·매도 예약 실행, 목표가 도달·보유 만기는 재판별 표시(engine.settle_day)
  2) 매도 검토: 보유 종목 중 ① 신호 트리거(섹터 수급이탈/동반약세 전환 or 종목 하방 칸)
     ② 목표가 도달 ③ 보유 만기 ④ 연장 보유(매일) → trading_agent → PM Sell/Underweight 면 다음 영업일
     시가 매도 예약, 아니면 계속 보유(②~④는 새 목표가·손절가 반영, 목표 도달은 본전 손절)
  3) 신규 분석: 동반강세·수급유입 칸 종목(보유·주문 대기 제외, 상한 MAX_NEW_PER_DAY) →
     trading_agent → 조건 충족 시 다음 영업일 1일 유효 지정가 주문
  4) 신규 Short(2026-09-29): 동반약세·수급이탈 칸 종목(보유·주문 대기·3) 후보 제외, 상한
     MAX_NEW_SHORT_PER_DAY) → trading_agent(공매도 맥락) → PM Sell/Underweight + Trader Sell 이면
     다음 영업일 1일 유효 지정가 공매도. 보유 Short 는 2)에서 상방 신호·목표·만기로 환매 검토
     (PM Buy/Overweight 면 환매)
주간 브리핑이 오늘자(asof)가 아니면 2)①·3)·4)는 건너뛴다(스테일 신호로 매매 금지) — 1)·2)②~④는 진행.
"""
import datetime
import traceback

from . import agent_iface as A
from . import config, engine, signals, tradedays


def _dkey(date, code, purpose, side="long"):
    return f"decision/{date}/{code}/{purpose}" + ("-short" if side == "short" else "")


def _safe_decide(agent, req):
    try:
        return agent.decide(req)
    except Exception as ex:
        traceback.print_exc()
        return A.Decision(code=req.code, date=req.date, purpose=req.purpose, side=req.side,
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
        run["notes"].append(f"주간 브리핑 asof {signals.asof_date(wb)} ≠ {date} — 신규·신호 매도 검토 생략")
    elif not wb:
        run["notes"].append("주간 브리핑 없음 — 신규·신호 매도 검토 생략")
    held = {p["code"] for p in L["positions"]}
    pend = {o["code"] for o in L["orders"]}
    cands = signals.buy_candidates(wb, held | pend) if wb_ok else []
    skipped_cap = cands[config.MAX_NEW_PER_DAY:]
    cands = cands[:config.MAX_NEW_PER_DAY]
    scands = (signals.short_candidates(wb, held | pend | {c["code"] for c in cands})
              if wb_ok and config.SHORT_ENABLED else [])
    skipped_short_cap = scands[config.MAX_NEW_SHORT_PER_DAY:]
    scands = scands[:config.MAX_NEW_SHORT_PER_DAY]
    need = (held | {o["code"] for o in L["orders"] if o["validDate"] <= date} | {c["code"] for c in cands}
            | {c["code"] for c in scands})
    bars = prices.daily_bars(sorted(need), date)
    missing = sorted(held - set(bars))
    if missing:
        run["notes"].append(f"보유 종목 시세 없음: {', '.join(missing)}")
    ev = engine.settle_day(L, date, bars)
    run["events"] = {k: [x.get("id") for x in v] for k, v in ev.items() if isinstance(v, list)}
    run["exits"] = [{"code": t["code"], "name": t["name"], "side": t.get("side", "long"), "reason": t["reason"],
                     "retPct": t["retPct"]} for t in ev.get("exits", [])]
    nxt = tradedays.next_trading_day(date)

    # 2) 매도 검토 — ① 신호 트리거(섹터 전환·하방 칸, 주간 브리핑 오늘자일 때만)
    #    ② 목표가 도달 · 보유 만기 · 연장 보유(매일) 재판별(2026-09-29, 브리핑과 무관)
    run["reviews"] = []
    todo = {}
    if wb_ok:
        for t in signals.review_triggers(L["positions"], wb):
            todo[t["code"]] = {"name": t["name"], "sector": t["sector"], "signal": t["signal"],
                               "reasons": list(t["reasons"]), "kinds": ["signal"],
                               "side": t.get("side", "long")}
    for p in L["positions"]:
        if p.get("sellPending"):
            continue
        flag = p.get("reviewFlag")
        if flag == "target":
            th = p.get("targetHit") or {}
            ext = f"저가 {th.get('low') or 0:,.0f}" if engine.is_short(p) else f"고가 {th.get('high') or 0:,.0f}"
            why = f"목표가 {th.get('target') or p.get('target') or 0:,.0f} 도달({ext})"
        elif flag == "expiry":
            why = f"보유 만기 {p['holdDay']}/{config.HOLD_DAYS}영업일" + (" · 연장 보유 중" if p.get("extended") else "")
        elif p.get("extended"):
            flag, why = "extended", f"연장 보유 재판별 {p['holdDay']}영업일째"
        else:
            continue
        e = todo.setdefault(p["code"], {"name": p["name"], "sector": p.get("sector"), "signal": p.get("signal"),
                                        "reasons": [], "kinds": [], "side": p.get("side", "long")})
        e["reasons"].insert(0, why)
        e["kinds"].insert(0, flag)
    for code, t in todo.items():
        p = next(x for x in L["positions"] if x["code"] == code)
        flag = next((k for k in t["kinds"] if k in ("target", "expiry", "extended")), None)
        kind = {"target": "review_target", "expiry": "review_expiry", "extended": "review_expiry"}.get(flag, "pm_sell")
        side = p.get("side", "long")
        req = A.DecisionRequest(code=code, name=t["name"], date=date, purpose="review",
                                sector=t["sector"], signal=t["signal"], side=side,
                                reason="; ".join(t["reasons"]), last_close=p.get("lastClose"),
                                position={k: p.get(k) for k in ("entry", "stop", "target", "fillDate",
                                                                "holdDay", "qty", "reviewFlag", "extended")})
        d = _safe_decide(agent, req)
        key = _dkey(date, code, "review", side)
        store.put(key, d.to_dict())
        sell, action = A.sell_verdict(d), "hold"
        if sell:
            engine.schedule_sell(L, code, f"PM {d.rating} — {req.reason}", key, date, kind=kind)
            action = "sell"
        elif d.error and flag in ("target", "expiry"):
            # 판단 실패 시 원래 규칙(목표가 익절·만기 청산)대로 다음 날 시가 매도 — 보수적
            engine.schedule_sell(L, code, f"재판별 실패({d.error[:60]}) — 원 규칙대로 "
                                 + ("환매" if side == "short" else "매도"), key, date, kind=kind)
            action = "sell_fallback"
        elif flag:
            before = (p["target"], p["stop"])
            engine.apply_hold(L, code, target=d.target, stop=d.stop,
                              decision_key=key, date=date)
            action = "hold_updated" if (p["target"], p["stop"]) != before else "hold"
        if p.get("sellPending"):
            p["reviewFlag"] = None
        run["reviews"].append({"code": code, "name": t["name"], "side": side, "trigger": req.reason, "kinds": t["kinds"],
                               "rating": d.rating, "sell": action.startswith("sell"), "action": action,
                               "error": d.error, "target": p.get("target"), "stop": p.get("stop"),
                               "decisionKey": key})

    # 3) 신규 분석·주문(Long) · 4) 신규 Short
    run["candidates"] = _new_entries(L, store, agent, cands, bars, date, nxt, "long")
    run["capSkipped"] = [c["code"] for c in skipped_cap]
    run["shortCandidates"] = _new_entries(L, store, agent, scands, bars, date, nxt, "short")
    run["shortCapSkipped"] = [c["code"] for c in skipped_short_cap]

    if wb_ok:
        store.put(f"signals/{date}", signals.compact(wb))
    store.put("ledger", L)
    run["equity"] = L["equity"][-1] if L["equity"] else None
    run["finishedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    store.put(f"run/{date}", run)
    log(f"[swing] {date} 청산 {len(run['exits'])} · 검토 {len(run['reviews'])} · "
        f"후보 {len(run['candidates'])} · 주문 {sum(r['ordered'] for r in run['candidates'])} · "
        f"숏 후보 {len(run['shortCandidates'])} · 숏 주문 {sum(r['ordered'] for r in run['shortCandidates'])} · "
        f"평가 {run['equity']['equity'] if run['equity'] else '-'}")
    return run


def _new_entries(L, store, agent, cands, bars, date, nxt, side):
    """후보 → trading_agent 판단 → 조건 충족 시 다음 영업일 1일 유효 지정가(Long 매수·Short 공매도)."""
    rows = []
    for c in cands:
        b = bars.get(c["code"]) or {}
        req = A.DecisionRequest(code=c["code"], name=c["name"], date=date, purpose="entry", side=side,
                                sector=c["sector"], signal=c["signal"], reason=c["reason"],
                                last_close=b.get("close"))
        d = _safe_decide(agent, req)
        key = _dkey(date, c["code"], "entry", side)
        store.put(key, d.to_dict())
        ok, why = A.entry_verdict(d, last_close=b.get("close"))
        row = {"code": c["code"], "name": c["name"], "side": side, "signal": c["signal"],
               "sector": c["sector"], "rating": d.rating, "action": d.action,
               "entry": d.entry, "stop": d.stop, "target": A.effective_target(d),
               "ordered": False, "why": why, "decisionKey": key}
        if ok:
            o = engine.place_entry(L, code=c["code"], name=c["name"], date=date, valid_date=nxt,
                                   entry=d.entry, stop=d.stop, target=A.effective_target(d),
                                   weight=A.effective_weight(d), sector=c["sector"],
                                   signal=c["signal"], decision_key=key, side=side)
            row.update(ordered=o["status"] == "open", orderId=o["id"], qty=o["qty"],
                       why=o.get("note", ""))
        rows.append(row)
    return rows
