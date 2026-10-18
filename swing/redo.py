"""판단 실패 재분석(2026-10-18) — LLM 체인 장애(1순위 503 + 폴백 한도 소진)로 '판단 실패'가 난 후보·매도 검토만
다시 trading_agent 에 넘겨, 같은 날 run 로그·원장을 고친다.

  python -m swing.run_daily --date 2026-10-16 --redo      # 수동(그날 이후 런이 아직 없을 때만)
  run_daily 는 하루 처리 뒤 실패가 있으면 SWING_REDO_WAIT_S(기본 900초) 기다렸다가 1회 자동 재분석

규칙
- 원장 asof == date 일 때만(다음 영업일 런이 이미 돌았으면 시세·현금이 달라져 소급 주문 금지).
- 신규 후보: run 행의 why 가 '판단 실패' 인 것. 보유·대기 중이 된 종목은 건너뜀. 성공하면 원래 런과 같은 규칙으로
  다음 영업일 1일 유효 지정가 주문(daily._new_entries 재사용), 행을 새 결과로 바꾸고 redo 메타(시각·이전 오류)를 남긴다.
- 후보 요청(사유·지표·촉매)은 행의 req(2026-10-18~)를 쓰고, 없으면 그날 저장본(signals/·universe/·catalyst/)에서 재구성.
- 매도 검토: 판단 실패로 '계속 보유'가 된 것만(목표·만기·체결일 손절 실패는 이미 원 규칙대로 매도 예약 = 보수적, 손대지 않음).
"""
import datetime
import traceback

from . import agent_iface as A
from . import config, daily, engine, signals, tradedays, universe

FAIL_PREFIX = "판단 실패"


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def failed_rows(run):
    """(키, 인덱스, 행) — 신규 후보 판단 실패."""
    out = []
    for k in ("candidates", "shortCandidates"):
        for i, r in enumerate(run.get(k) or []):
            if not r.get("ordered") and str(r.get("why") or "").startswith(FAIL_PREFIX):
                out.append((k, i, r))
    return out


def failed_reviews(run):
    return [(i, r) for i, r in enumerate(run.get("reviews") or []) if r.get("error") and r.get("action") == "hold"]


def count_failed(run):
    return len(failed_rows(run)) + len(failed_reviews(run))


def _rebuild(store, date, row, uni=None, cat=None, cache=None):
    """행 → 후보 dict(_new_entries 입력). req 가 있으면 그대로, 없으면 그날 저장본에서 재구성."""
    c = {"code": row["code"], "name": row["name"], "sector": row.get("sector"), "signal": row.get("signal"),
         "source": row.get("source") or "matrix", "score": row.get("score"), "rankAll": row.get("rankAll"),
         "matrixScore": row.get("matrixScore"), "reason": "", "metrics": None, "catalyst": row.get("catalyst")}
    if row.get("req"):
        c.update(row["req"])
        return c
    cache = cache if cache is not None else {}
    side, src = row.get("side", "long"), c["source"]
    try:
        if src == "universe":
            doc = cache.setdefault("uni", store.get(f"universe/{date}"))
            for x in universe.universe_candidates(doc, side, fut_only=config.UNI_SHORT_FUT_ONLY and side == "short"):
                if x["code"] == row["code"]:
                    return {**x, "rankAll": row.get("rankAll") or x.get("rankAll")}
        elif src == "catalyst":
            doc = cache.setdefault("cat", store.get(f"catalyst/{date}") or {})
            from . import catalysts
            ents = {e["code"]: e for e in doc.get("entries") or [] if e.get("code")}
            if row["code"] in ents:
                xs, _ = catalysts.short_candidates({row["code"]: ents[row["code"]]},
                                                   sector_of=(cat.sector_of(date) if cat is not None else None))
                if xs:
                    c.update(xs[0])
        else:
            wb = cache.setdefault("sig", store.get(f"signals/{date}"))
            fn = signals.short_candidates if side == "short" else signals.buy_candidates
            for x in fn(wb):
                if x["code"] == row["code"]:
                    c.update(x, source="matrix")
                    break
        if src != "universe" and uni is not None:
            uni.attach_scores([c], side, date, source=src)
    except Exception as ex:
        traceback.print_exc()
        c["reason"] = c.get("reason") or f"(재분석 — 요청 재구성 일부 실패: {type(ex).__name__})"
    return c


def redo_failed(date, store, agent, prices, *, uni=None, cat=None, log=print):
    """판단 실패 후보·매도 검토 재분석 → 요약 dict. run/<date>·ledger 를 고쳐 저장."""
    run = store.get(f"run/{date}")
    if not run:
        return {"skip": f"run/{date} 없음"}
    L = store.get("ledger") or {}
    if L.get("asof") != date:
        return {"skip": f"원장 asof {L.get('asof')} ≠ {date} — 이후 런이 돌아 소급 재분석 불가"}
    rows, revs = failed_rows(run), failed_reviews(run)
    if not rows and not revs:
        return {"skip": "판단 실패 없음"}
    nxt = run.get("entryDate") or tradedays.next_trading_day(date)
    held = {p["code"] for p in L["positions"]}
    pend = {o["code"] for o in L["orders"]}
    cache, todo = {}, []
    for k, i, r in rows:
        if r["code"] in held or r["code"] in pend:
            continue
        todo.append((k, i, r, _rebuild(store, date, r, uni, cat, cache)))
    bars = prices.daily_bars(sorted({t[3]["code"] for t in todo} | {r["code"] for _, r in revs}), date) if (todo or revs) else {}
    res = {"date": date, "at": _now(), "entries": 0, "entryOk": 0, "ordered": 0, "reviews": 0, "reviewOk": 0}
    for k, i, r, c in todo:
        log(f"[swing] 재분석 {date} {c['code']} {c['name']} ({r.get('side')}·{c.get('source')})")
        new = daily._new_entries(L, store, agent, [c], bars, date, nxt, r.get("side", "long"))[0]
        new["redo"] = {"at": _now(), "prevWhy": str(r.get("why") or "")[:300]}
        run[k][i] = new
        res["entries"] += 1
        res["entryOk"] += 0 if str(new.get("why") or "").startswith(FAIL_PREFIX) else 1
        res["ordered"] += 1 if new.get("ordered") else 0
    for i, r in revs:
        p = next((x for x in L["positions"] if x["code"] == r["code"]), None)
        if p is None or p.get("sellPending"):
            continue
        flag = next((k for k in r.get("kinds") or [] if k in ("fill_stop", "target", "expiry", "extended")), None)
        kind = {"fill_stop": "stop", "target": "review_target", "expiry": "review_expiry",
                "extended": "review_expiry"}.get(flag, "pm_sell")
        side = p.get("side", "long")
        req = A.DecisionRequest(code=r["code"], name=r["name"], date=date, purpose="review", sector=p.get("sector"),
                                signal=p.get("signal"), side=side, reason=r.get("trigger") or "",
                                last_close=p.get("lastClose"),
                                position={k: p.get(k) for k in ("entry", "stop", "target", "fillDate",
                                                                "holdDay", "qty", "reviewFlag", "extended")})
        log(f"[swing] 재검토 {date} {r['code']} {r['name']} ({side})")
        d = daily._safe_decide(agent, req)
        key = r.get("decisionKey") or daily._dkey(date, r["code"], "review", side)
        store.put(key, d.to_dict())
        action = "hold"
        if A.sell_verdict(d):
            engine.schedule_sell(L, r["code"], f"PM {d.rating} — {req.reason}", key, date, kind=kind)
            action = "sell"
        elif flag and not d.error:
            before = (p["target"], p["stop"])
            engine.apply_hold(L, r["code"], target=d.target, stop=d.stop, decision_key=key, date=date)
            action = "hold_updated" if (p["target"], p["stop"]) != before else "hold"
        if p.get("sellPending"):
            p["reviewFlag"] = None
        res["reviews"] += 1
        res["reviewOk"] += 0 if d.error else 1
        run["reviews"][i] = {**r, "rating": d.rating, "sell": action.startswith("sell"), "action": action,
                             "error": d.error, "target": p.get("target"), "stop": p.get("stop"),
                             "redo": {"at": _now(), "prevError": str(r.get("error") or "")[:300]}}
    run.setdefault("redo", []).append(res)
    run["notes"] = [n for n in run.get("notes") or [] if not n.startswith("판단 실패 재분석")] + [
        f"판단 실패 재분석 후보 {res['entryOk']}/{res['entries']}(주문 {res['ordered']}) · 매도 검토 {res['reviewOk']}/{res['reviews']}"]
    store.put("ledger", L)
    store.put(f"run/{date}", run)
    log(f"[swing] {date} " + run["notes"][-1])
    return res
