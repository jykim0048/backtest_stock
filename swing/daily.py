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
  5) 유니버스(2026-10-13): 1) 뒤에 업종별 종목 수급 모멘텀을 산출(store universe/<date>)하고, 매트릭스 후보 3)·4)에
     **같은 모멘텀 점수**를 매겨 점수순 정렬. 유니버스 자체도 **독립 후보 소스**로 점수 ≥ UNI_CAND_MIN 종목을
     (보유·대기·매트릭스 후보·쿨다운 제외, 상한 UNI_NEW_PER_DAY / UNI_NEW_SHORT_PER_DAY) 같은 trading_agent 에 넘긴다.
     주문·포지션·거래에 source(matrix|universe) 가 남는다. 산출·채점 실패는 노트만 남기고 매트릭스 흐름은 계속.
주간 브리핑이 오늘자(asof)가 아니면 2)①·3)·4)·5)후보는 건너뛴다(스테일 신호로 매매 금지) — 1)·2)②~④·5)산출은 진행.
"""
import datetime
import traceback

from . import agent_iface as A
from . import config, engine, signals, tradedays, universe


def _dkey(date, code, purpose, side="long"):
    return f"decision/{date}/{code}/{purpose}" + ("-short" if side == "short" else "")


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class Progress:
    """실행 진행률 — store `progress` 문서 하나를 갱신(대시보드 헤더 "실행 중 n/N", 2026-09-29).
    판단(LLM) 한 건이 끝날 때마다 쓰므로 하루 수십 번 쓰기뿐이다. 실패해도 런을 멈추지 않는다."""

    def __init__(self, store, date, agent):
        self.store = store
        self.doc = {"date": date, "status": "running", "agent": agent, "startedAt": _now(),
                    "phase": "settle", "total": 0, "done": 0, "last": None}

    def update(self, **kw):
        self.doc.update(kw, updatedAt=_now())
        try:
            self.store.put("progress", self.doc)
        except Exception:
            traceback.print_exc()

    def step(self, d, name, side, purpose):
        self.update(done=self.doc["done"] + 1,
                    last={"code": d.code, "name": name, "side": side, "purpose": purpose,
                          "rating": d.rating, "action": d.action, "error": (d.error or "")[:120] or None})


def _safe_decide(agent, req):
    try:
        return agent.decide(req)
    except Exception as ex:
        traceback.print_exc()
        return A.Decision(code=req.code, date=req.date, purpose=req.purpose, side=req.side,
                          agent=getattr(agent, "name", ""), error=f"{type(ex).__name__}: {ex}")


def run_day(date, store, agent, prices, wb, *, force=False, log=print, uni=None, cat=None):
    """하루 처리 → run 로그 dict. 원장·판단·로그는 store 에 저장. uni = universe.UniverseRunner(None 이면 유니버스 생략),
    cat = catalysts.CatalystRunner(None 이면 하방 촉매 Short 소스 생략, 2026-10-15)."""
    run = {"date": date, "startedAt": datetime.datetime.now().isoformat(timespec="seconds"),
           "agent": getattr(agent, "name", ""), "notes": []}
    if not tradedays.is_trading_day(date):
        run["skip"] = "휴장일"
        return run
    L = store.get("ledger") or engine.new_ledger()
    if L.get("asof") and date <= L["asof"] and not force:
        run["skip"] = f"이미 처리됨(asof {L['asof']})"
        return run
    prog = Progress(store, date, run["agent"])
    prog.update()

    # 1) 원장 갱신 — 후보 종목 전일 종가도 같은 호출로 받는다
    wb_ok = bool(wb) and signals.asof_date(wb) == date
    if wb and not wb_ok:
        run["notes"].append(f"주간 브리핑 asof {signals.asof_date(wb)} ≠ {date} — 신규·신호 매도 검토 생략")
    elif not wb:
        run["notes"].append("주간 브리핑 없음 — 신규·신호 매도 검토 생략")
    held = {p["code"] for p in L["positions"]}
    pend = {o["code"] for o in L["orders"]}
    cands = signals.buy_candidates(wb, held | pend) if wb_ok else []
    scands = (signals.short_candidates(wb, held | pend | {c["code"] for c in cands})
              if wb_ok and config.SHORT_ENABLED else [])
    for c in cands + scands:
        c["source"] = "matrix"
    need = (held | {o["code"] for o in L["orders"] if o["validDate"] <= date} | {c["code"] for c in cands}
            | {c["code"] for c in scands})
    bars = prices.daily_bars(sorted(need), date)
    missing = sorted(held - set(bars))
    if missing:
        run["notes"].append(f"보유 종목 시세 없음: {', '.join(missing)}")
    ev = engine.settle_day(L, date, bars)
    nxt = tradedays.next_trading_day(date)
    run["entryDate"] = nxt                                   # 신규 후보 주문의 진입(유효)일 — 대시보드 표기(2026-10-16)
    run["retries"] = _retry_unfilled(L, ev, bars, date, nxt)   # 미도달 재주문(2026-10-16, 방식 A)
    run["events"] = {k: [x.get("id") for x in v] for k, v in ev.items() if isinstance(v, list)}
    run["exits"] = [{"code": t["code"], "name": t["name"], "side": t.get("side", "long"), "reason": t["reason"],
                     "retPct": t["retPct"]} for t in ev.get("exits", [])]

    # 5) 유니버스 산출 + 공통 모멘텀 채점(매트릭스 후보 정렬) + 유니버스 독립 후보(2026-10-13)
    ucands, uscands, skipped_uni_cap, skipped_uni_short_cap = [], [], [], []
    if uni is not None:
        prog.update(phase="universe")
        uni_doc = None
        try:
            uni_doc = uni.build_or_load(date, wb, L)
        except Exception as ex:
            traceback.print_exc()
            run["notes"].append(f"유니버스 산출 실패: {type(ex).__name__}: {str(ex)[:120]}")
        for side, xs in (("long", cands), ("short", scands)):
            try:
                uni.attach_scores(xs, side, date)
            except Exception as ex:
                traceback.print_exc()
                run["notes"].append(f"매트릭스 {side} 후보 모멘텀 채점 실패: {type(ex).__name__}: {str(ex)[:120]}")
        if uni_doc and wb_ok:
            decided = universe.recent_decisions(store, date, config.UNI_COOLDOWN_DAYS)
            ex_codes = held | pend | {c["code"] for c in cands} | {c["code"] for c in scands}
            ucands = universe.universe_candidates(uni_doc, "long", ex_codes, decided)
            skipped_uni_cap, ucands = ucands[config.UNI_NEW_PER_DAY:], ucands[:config.UNI_NEW_PER_DAY]
            if config.SHORT_ENABLED:
                uscands = universe.universe_candidates(uni_doc, "short", ex_codes | {c["code"] for c in ucands}, decided,
                                                       fut_only=config.UNI_SHORT_FUT_ONLY)
                skipped_uni_short_cap, uscands = uscands[config.UNI_NEW_SHORT_PER_DAY:], uscands[:config.UNI_NEW_SHORT_PER_DAY]
            extra = sorted({c["code"] for c in ucands + uscands} - set(bars))
            if extra:
                bars.update(prices.daily_bars(extra, date))
            run["notes"].append(f"유니버스 후보 Long {len(ucands)}(상한 초과 {len(skipped_uni_cap)}) · "
                                f"Short {len(uscands)}(상한 초과 {len(skipped_uni_short_cap)}) · 쿨다운 제외 {len(decided)}종목")
    skipped_cap, cands = cands[config.MAX_NEW_PER_DAY:], cands[:config.MAX_NEW_PER_DAY]
    skipped_short_cap, scands = scands[config.MAX_NEW_SHORT_PER_DAY:], scands[:config.MAX_NEW_SHORT_PER_DAY]

    # 6) 하방 촉매 Short 후보(2026-10-15, 세 번째 소스) — 매트릭스·유니버스 뒤, 같은 공통 채점·쿨다운·주식선물 옵션
    ccands, skipped_cat_cap = [], []
    if cat is not None and config.CAT_SHORT_ENABLED and config.SHORT_ENABLED:
        prog.update(phase="catalyst")
        try:
            decided_c = universe.recent_decisions(store, date, config.UNI_COOLDOWN_DAYS)
            ex_c = held | pend | {c["code"] for c in cands + scands + ucands + uscands}
            fut = None
            if config.UNI_SHORT_FUT_ONLY and uni is not None:
                try:
                    fut = uni.dbq.stock_future_underlyings()
                except Exception as ex:
                    run["notes"].append(f"촉매 주식선물 목록 실패(한정 해제): {type(ex).__name__}")
            ccands = cat.short_candidates(date, ex_c, decided_c, fut=fut)
            if ccands and uni is not None:
                try:
                    uni.attach_scores(ccands, "short", date, source="catalyst")
                except Exception as ex:
                    traceback.print_exc()
                    run["notes"].append(f"촉매 후보 모멘텀 채점 실패: {type(ex).__name__}: {str(ex)[:120]}")
            ccands.sort(key=lambda c: (-c["catalyst"]["strength"], universe.score_key(c),
                                       -(c["catalyst"]["changePct"] if c["catalyst"]["changePct"] is not None else 0)))
            skipped_cat_cap, ccands = ccands[config.CAT_NEW_SHORT_PER_DAY:], ccands[:config.CAT_NEW_SHORT_PER_DAY]
            extra = sorted({c["code"] for c in ccands} - set(bars))
            if extra:
                bars.update(prices.daily_bars(extra, date))
            run["notes"].append(f"하방 촉매 후보 Short {len(ccands)}(상한 초과 {len(skipped_cat_cap)})")
        except Exception as ex:
            traceback.print_exc()
            run["notes"].append(f"하방 촉매 후보 실패: {type(ex).__name__}: {str(ex)[:120]}")
            ccands = []

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
    prog.update(phase="review", total=len(todo) + len(cands) + len(scands) + len(ucands) + len(uscands))
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
        prog.step(d, t["name"], side, "review")
        run["reviews"].append({"code": code, "name": t["name"], "side": side, "trigger": req.reason, "kinds": t["kinds"],
                               "rating": d.rating, "sell": action.startswith("sell"), "action": action,
                               "error": d.error, "target": p.get("target"), "stop": p.get("stop"),
                               "decisionKey": key})

    # 3) 신규 분석·주문(Long) · 4) 신규 Short — 매트릭스 후보 뒤에 유니버스 후보(독립 소스, source 로 구분)
    prog.update(phase="long")
    run["candidates"] = (_new_entries(L, store, agent, cands, bars, date, nxt, "long", prog)
                         + _new_entries(L, store, agent, ucands, bars, date, nxt, "long", prog))
    run["capSkipped"] = [c["code"] for c in skipped_cap]
    run["uniCapSkipped"] = [c["code"] for c in skipped_uni_cap]
    prog.update(phase="short")
    run["shortCandidates"] = (_new_entries(L, store, agent, scands, bars, date, nxt, "short", prog)
                              + _new_entries(L, store, agent, uscands, bars, date, nxt, "short", prog)
                              + _new_entries(L, store, agent, ccands, bars, date, nxt, "short", prog))
    run["shortCapSkipped"] = [c["code"] for c in skipped_short_cap]
    run["uniShortCapSkipped"] = [c["code"] for c in skipped_uni_short_cap]
    run["catShortCapSkipped"] = [c["code"] for c in skipped_cat_cap]

    if wb_ok:
        store.put(f"signals/{date}", signals.compact(wb))
    store.put("ledger", L)
    run["equity"] = L["equity"][-1] if L["equity"] else None
    run["finishedAt"] = datetime.datetime.now().isoformat(timespec="seconds")
    store.put(f"run/{date}", run)
    prog.update(status="done", phase="done", finishedAt=run["finishedAt"])
    log(f"[swing] {date} 청산 {len(run['exits'])} · 검토 {len(run['reviews'])} · "
        f"후보 {len(run['candidates'])} · 주문 {sum(r['ordered'] for r in run['candidates'])} · "
        f"숏 후보 {len(run['shortCandidates'])} · 숏 주문 {sum(r['ordered'] for r in run['shortCandidates'])} · "
        f"유니버스 {sum(1 for r in run['candidates'] + run['shortCandidates'] if r.get('source') == 'universe')} · "
        f"촉매 {sum(1 for r in run['shortCandidates'] if r.get('source') == 'catalyst')} · "
        f"재시도 {sum(1 for r in run.get('retries') or [] if r['ok'])}/{len(run.get('retries') or [])} · "
        f"평가 {run['equity']['equity'] if run['equity'] else '-'}")
    return run


def _retry_unfilled(L, ev, bars, date, nxt):
    """미도달 취소 주문 → 같은 레벨로 다음 영업일 재주문(재분석 없음). 반환 [{code,name,side,retry,ok,why,orderId}].
    가드: 재시도 횟수 ≤ ENTRY_RETRY_MAX · 그날 종가가 진입가 ±ENTRY_RETRY_BAND · 손절가 미침범(Long 종가>손절, Short 종가<손절) ·
    보유·대기 아님. 가드에 걸리면 중단(쿨다운이 끝나고 다시 선정되면 그때 재분석)."""
    out = []
    held = {p["code"] for p in L["positions"]}
    for o in ev.get("cancelled") or []:
        if not str(o.get("note") or "").startswith("미도달"):
            continue
        n = int(o.get("retry") or 0) + 1
        row = {"code": o["code"], "name": o.get("name"), "side": o.get("side", "long"), "retry": n, "ok": False, "why": ""}
        b = bars.get(o["code"]) or {}
        close, entry, stop = b.get("close"), o.get("entry"), o.get("stop")
        if n > config.ENTRY_RETRY_MAX:
            row["why"] = f"재시도 상한 {config.ENTRY_RETRY_MAX}회"
        elif not close or not entry:
            row["why"] = "종가 없음"
        elif abs(close / entry - 1) > config.ENTRY_RETRY_BAND:
            row["why"] = f"종가 {close:,.0f} 가 진입가 ±{config.ENTRY_RETRY_BAND * 100:.0f}% 밖"
        elif stop and ((o.get("side", "long") == "long" and close <= stop) or (o.get("side") == "short" and close >= stop)):
            row["why"] = f"종가 {close:,.0f} 가 손절가 {stop:,.0f} 침범"
        elif o["code"] in held or any(x["code"] == o["code"] for x in L["orders"]):
            row["why"] = "보유·대기 중"
        else:
            new = engine.place_entry(L, code=o["code"], name=o.get("name"), date=date, valid_date=nxt, entry=entry, stop=stop,
                                     target=o.get("target"), weight=o.get("weight") or config.DEFAULT_WEIGHT, sector=o.get("sector"),
                                     signal=o.get("signal"), decision_key=o.get("decisionKey"), side=o.get("side", "long"),
                                     source=o.get("source"))
            new["retry"] = n
            row.update(ok=new["status"] == "open", why=new.get("note") or f"재시도 {n}/{config.ENTRY_RETRY_MAX}", orderId=new["id"])
        out.append(row)
    return out


def _new_entries(L, store, agent, cands, bars, date, nxt, side, prog=None):
    """후보 → trading_agent 판단 → 조건 충족 시 다음 영업일 1일 유효 지정가(Long 매수·Short 공매도)."""
    rows = []
    for c in cands:
        b = bars.get(c["code"]) or {}
        req = A.DecisionRequest(code=c["code"], name=c["name"], date=date, purpose="entry", side=side,
                                sector=c["sector"], signal=c["signal"], reason=c["reason"],
                                last_close=b.get("close"), source=c.get("source") or "matrix",
                                score=c.get("score") if c.get("metrics") is not None else None, metrics=c.get("metrics"),
                                catalyst=c.get("catalyst"))
        d = _safe_decide(agent, req)
        key = _dkey(date, c["code"], "entry", side)
        store.put(key, d.to_dict())
        ok, why = A.entry_verdict(d, last_close=b.get("close"))
        row = {"code": c["code"], "name": c["name"], "side": side, "signal": c["signal"],
               "sector": c["sector"], "source": c.get("source") or "matrix", "rankAll": c.get("rankAll"),
               "score": c.get("score") if c.get("metrics") is not None else None, "matrixScore": c.get("matrixScore"),
               "rating": d.rating, "action": d.action, "weight": A.effective_weight(d),
               "pmWeight": getattr(d, "pm_weight", None), "traderWeight": d.weight,
               "entry": d.entry, "stop": d.stop, "target": A.effective_target(d),
               "ordered": False, "why": why, "decisionKey": key,
               "pmNote": (d.summary or "")[:240], "traderNote": (A.trader_reason(d) or "")[:240],   # 툴팁용 한 줄(2026-10-16)
               "catalyst": ({k: (c.get("catalyst") or {}).get(k) for k in ("kind", "title", "strength", "ruleKind", "changePct", "direct")}
                            if c.get("catalyst") else None)}
        if ok:
            o = engine.place_entry(L, code=c["code"], name=c["name"], date=date, valid_date=nxt,
                                   entry=d.entry, stop=d.stop, target=A.effective_target(d),
                                   weight=A.effective_weight(d), sector=c["sector"],
                                   signal=c["signal"], decision_key=key, side=side, source=c.get("source") or "matrix")
            row.update(ordered=o["status"] == "open", orderId=o["id"], qty=o["qty"],
                       why=o.get("note", ""))
        rows.append(row)
        if prog:
            prog.step(d, c["name"], side, "entry")
    return rows
