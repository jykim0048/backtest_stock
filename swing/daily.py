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
    # 매트릭스 후보 쿨다운(2026-10-16) — 유니버스·하방 촉매와 같은 규칙: 최근 UNI_COOLDOWN_DAYS 영업일(판단일 포함) 안에
    # 진입 판단을 받은 종목은 결과(주문·Hold·미체결)와 무관하게 제외. 보유·대기 종목은 원래 제외.
    mcool = universe.recent_decisions(store, date, config.UNI_COOLDOWN_DAYS) if config.MATRIX_COOLDOWN else {}
    run["matrixCooldown"] = sorted(c["code"] for c in cands + scands if c["code"] in mcool)
    if run["matrixCooldown"]:
        cands = [c for c in cands if c["code"] not in mcool]
        scands = [c for c in scands if c["code"] not in mcool]
        run["notes"].append(f"매트릭스 후보 쿨다운 제외 {len(run['matrixCooldown'])}종목")
    for c in cands + scands:
        c["source"] = "matrix"
    # Short 후보는 주식선물 상장 종목만(2026-10-16, UNI_SHORT_FUT_ONLY 기본 켬) — 매트릭스도 유니버스·촉매와 같은 필터.
    # 목록은 FCODES(하루 캐시). 유니버스 어댑터가 없거나 목록을 못 받으면 필터 없이 진행하고 노트로 남긴다(fail-open).
    fut_set, run["shortFutSkipped"] = None, []
    if scands and config.UNI_SHORT_FUT_ONLY:
        if uni is not None:
            fut_set = uni.fut_set(date)
        if fut_set:
            run["shortFutSkipped"] = [c["code"] for c in scands if c["code"] not in fut_set]
            scands = [c for c in scands if c["code"] in fut_set]
            if run["shortFutSkipped"]:
                run["notes"].append(f"매트릭스 Short 후보 주식선물 미상장 제외 {len(run['shortFutSkipped'])}종목")
        else:
            run["notes"].append("주식선물 목록 없음 — 매트릭스 Short 선물 한정 필터 생략")
    backlog = _retry_backlog(L, date)                         # 미도달 재주문 소급분(2026-10-16) — 시세 필요
    need = (held | {o["code"] for o in L["orders"] if o["validDate"] <= date} | {c["code"] for c in cands}
            | {c["code"] for c in scands} | {o["code"] for o in backlog})
    bars = prices.daily_bars(sorted(need), date)
    missing = sorted(held - set(bars))
    if missing:
        run["notes"].append(f"보유 종목 시세 없음: {', '.join(missing)}")
    ev = engine.settle_day(L, date, bars)
    nxt = tradedays.next_trading_day(date)
    run["entryDate"] = nxt                                   # 신규 후보 주문의 진입(유효)일 — 대시보드 표기(2026-10-16)
    run["retries"] = _retry_unfilled(L, ev, bars, date, nxt, backlog)   # 미도달 재주문(2026-10-16, 방식 A)
    retried = {r["code"] for r in run["retries"] if r["ok"]}
    if retried:                                               # 재주문 종목은 오늘 신규 후보에서 뺀다(대기 주문 = pend)
        pend |= retried
        cands = [c for c in cands if c["code"] not in retried]
        scands = [c for c in scands if c["code"] not in retried]
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
            if config.SHORT_ENABLED:
                uscands = universe.universe_candidates(uni_doc, "short", ex_codes | {c["code"] for c in ucands}, decided,
                                                       fut_only=config.UNI_SHORT_FUT_ONLY)
            # 상한 이연(2026-10-16): 한쪽 적격 후보가 상한보다 적으면 남는 자리를 다른 쪽 상한에 더한다(총 분석 수 = Long+Short 상한 합 이내)
            cap_l, cap_s = config.UNI_NEW_PER_DAY, (config.UNI_NEW_SHORT_PER_DAY if config.SHORT_ENABLED else 0)
            if config.UNI_CAP_CARRY:
                cap_l, cap_s = cap_l + max(0, cap_s - len(uscands)), cap_s + max(0, cap_l - len(ucands))
            run["uniCaps"] = {"long": cap_l, "short": cap_s, "base": [config.UNI_NEW_PER_DAY, config.UNI_NEW_SHORT_PER_DAY]}
            skipped_uni_cap, ucands = ucands[cap_l:], ucands[:cap_l]
            skipped_uni_short_cap, uscands = uscands[cap_s:], uscands[:cap_s]
            extra = sorted({c["code"] for c in ucands + uscands} - set(bars))
            if extra:
                bars.update(prices.daily_bars(extra, date))
            carry = "" if (cap_l, cap_s) == (config.UNI_NEW_PER_DAY, config.UNI_NEW_SHORT_PER_DAY) else f" · 상한 이연 Long {cap_l}/Short {cap_s}"
            run["notes"].append(f"유니버스 후보 Long {len(ucands)}(상한 초과 {len(skipped_uni_cap)}) · "
                                f"Short {len(uscands)}(상한 초과 {len(skipped_uni_short_cap)}) · 쿨다운 제외 {len(decided)}종목" + carry)
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
                fut = fut_set if fut_set is not None else uni.fut_set(date)
                if fut is None:
                    run["notes"].append("촉매 주식선물 목록 없음(한정 해제)")
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
        fs = p.get("fillStop") or {}
        if fs and not fs.get("reviewed") and fs.get("date") and fs["date"] < date:
            # 체결일 손절가 도달 → 체결 다음 영업일 종가로 재판별(2026-10-16) — 매도면 다음 영업일 시가
            flag = p["reviewFlag"] = "fill_stop"
            ext = f"고가 {fs.get('extreme') or 0:,.0f}" if engine.is_short(p) else f"저가 {fs.get('extreme') or 0:,.0f}"
            why = (f"체결일({fs['date'][5:]}) 손절가 {fs.get('stop') or 0:,.0f} 도달({ext}) — {date[5:]} 종가 "
                   f"{p.get('lastClose') or 0:,.0f} 기준 재판별")
        elif flag == "target":
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
        flag = next((k for k in t["kinds"] if k in ("fill_stop", "target", "expiry", "extended")), None)
        kind = {"fill_stop": "stop", "target": "review_target", "expiry": "review_expiry", "extended": "review_expiry"}.get(flag, "pm_sell")
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
        elif d.error and flag in ("target", "expiry", "fill_stop"):
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
        if p.get("fillStop") and flag == "fill_stop":
            p["fillStop"]["reviewed"] = date                 # 재판별 끝 — 이후 손절은 정상 적용
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


def _retry_backlog(L, date):
    """재시도 판정을 아직 받지 않은 과거 미도달 취소(orderLog) — 유효일 ENTRY_RETRY_FROM 이상·오늘 이전·ENTRY_RETRY_MAX 영업일 이내."""
    out = []
    for o in L.get("orderLog") or []:
        vd = o.get("validDate") or ""
        if (o.get("status") != "cancelled" or not str(o.get("note") or "").startswith("미도달") or o.get("retryChecked")
                or not vd or vd >= date or (config.ENTRY_RETRY_FROM and vd < config.ENTRY_RETRY_FROM)):
            continue
        out.append(o)
    return out


def _retry_unfilled(L, ev, bars, date, nxt, backlog=()):
    """미도달 취소 주문 → 같은 레벨로 다음 영업일 재주문(재분석 없음). 반환 [{code,name,side,retry,ok,why,orderId,backfill}].
    대상 = 오늘 미도달 취소 + backlog(기능 배포 전·런 누락 등으로 판정을 못 받은 ENTRY_RETRY_FROM 이후 미도달 취소).
    가드: 재시도 횟수 ≤ ENTRY_RETRY_MAX · 유효일 이후 ENTRY_RETRY_MAX 영업일 이내 · 오늘 종가가 진입가 ±ENTRY_RETRY_BAND ·
    손절가 미침범(Long 종가>손절, Short 종가<손절) · 보유·대기 아님. 가드에 걸리면 중단(쿨다운이 끝나고 다시 선정되면 그때 재분석).
    판정한 취소 주문에는 retryChecked 를 남겨 다음 날 다시 보지 않는다."""
    out = []
    held = {p["code"] for p in L["positions"]}
    todays = [o for o in ev.get("cancelled") or [] if str(o.get("note") or "").startswith("미도달")]
    seen = set()
    for o in todays + [x for x in backlog if x.get("id") not in {t.get("id") for t in todays}]:
        if o.get("id") in seen or o.get("retryChecked"):
            continue
        seen.add(o.get("id"))
        o["retryChecked"] = date
        n = int(o.get("retry") or 0) + 1
        late = o.get("validDate") and o["validDate"] < date
        if late and len(tradedays.trading_days(o["validDate"], date)) - 1 > config.ENTRY_RETRY_MAX:
            out.append({"code": o["code"], "name": o.get("name"), "side": o.get("side", "long"), "retry": n, "ok": False,
                        "why": "유효일 이후 기간 경과", "backfill": True})
            continue
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
            o["retriedBy"] = new["id"]
            row.update(ok=new["status"] == "open", why=new.get("note") or f"재시도 {n}/{config.ENTRY_RETRY_MAX}" + (" · 소급" if late else ""),
                       orderId=new["id"])
        if late:
            row["backfill"] = True
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
