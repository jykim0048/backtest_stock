"""RiskGate(L5) — LLM 결정을 축소·거부만 한다, 키우지 않는다(PLAN_PM §7). 통과·강등분만 Live Trader 지시가 된다.

  G1 모르는 종목·형식 오류 → 거부            G2 tighten_stop 이 불리한 방향이거나 현재가를 넘음 → 거부
  G3 확신도 < MIN_CONF_EXIT 인 exit/trim → flag_review 로 강등
  G4 FLIP_MIN 분 안에 같은 종목 지시가 이미 있음 → 거부(중복·번복 방지)
  G5 Live Trader 가 그 종목 진입(잔여분 추가 등)을 오늘 진행 중 → exit/trim 을 flag_review 로 강등
  G6 한 호출의 exit+trim 명목 합계 > 평가액 × MAX_REDUCE_PER_CALL → 큰 것부터 한도까지, 나머지 flag_review
  G7 판단 시간(DECIDE_START~END) 밖 → 거부   G8 Live Trader 가 이미 청산 중(손절·예약·지시) → 거부
  G9 거래 상태가 허용하지 않음 → 거부
"""
import datetime

from .. import engine
from . import config as C


def _min(a, b):
    return (datetime.datetime.fromisoformat(b) - datetime.datetime.fromisoformat(a)).total_seconds() / 60


def review(decisions, *, ts, book, state, intraday=None, recent=()):
    """decisions = LLM decisions 목록. recent = 오늘 이미 낸 지시 목록. 반환 [{code, pid, decision, outcome, action, why, …}].
    outcome = hold | pass | downgrade | reject. pass·downgrade 이고 action ≠ hold 인 것만 지시가 된다."""
    hm = ts[11:16]
    intraday = intraday or {}
    exiting = set((intraday.get("exits") or {}).keys())
    entering = {r.get("code") for r in (intraday.get("orders") or {}).values() if not r.get("done")}
    by_code = {p["code"]: (pid, p) for pid, p in book.positions.items()}
    eq = book.equity() or 1
    out = []
    for d in decisions or []:
        code, act = d.get("code"), d.get("action")
        r = {"code": code, "pid": None, "decision": d, "outcome": "pass", "action": act, "why": None}
        out.append(r)
        if code not in by_code or act not in C.ACTIONS:
            r.update(outcome="reject", why="G1 모르는 종목·액션")
            continue
        pid, p = by_code[code]
        r["pid"] = pid
        if act == "hold":
            r["outcome"] = "hold"
            continue
        px = book.px(code)
        if not (C.DECIDE_START <= hm < C.DECIDE_END):
            r.update(outcome="reject", why="G7 판단 시간 밖")
            continue
        if not state.allows(act):
            r.update(outcome="reject", why=f"G9 상태 {state.state}")
            continue
        if pid in exiting:
            r.update(outcome="reject", why="G8 Live Trader 가 이미 청산 중")
            continue
        if any(i["code"] == code and _min(i["ts"], ts) < C.FLIP_MIN for i in recent):
            r.update(outcome="reject", why=f"G4 {C.FLIP_MIN}분 안에 같은 종목 지시 있음")
            continue
        if act == "tighten_stop":
            ns, short = d.get("newStop"), engine.is_short(p)
            ok = isinstance(ns, (int, float)) and px is not None and (
                (ns < p["stop"] and ns > px) if short else (ns > p["stop"] and ns < px))
            if not ok:
                r.update(outcome="reject", why="G2 손절가를 불리하게 옮기거나 현재가를 넘음")
            else:
                r["newStop"] = float(ns)
            continue
        if act in ("exit", "trim"):
            if (d.get("confidence") or 0) < C.MIN_CONF_EXIT:
                r.update(outcome="downgrade", action="flag_review", why=f"G3 확신도 {d.get('confidence')} < {C.MIN_CONF_EXIT}")
                continue
            if code in entering:
                r.update(outcome="downgrade", action="flag_review", why="G5 Live Trader 진입 진행 중")
                continue
            pct = 1.0 if act == "exit" else min(0.9, max(0.1, float(d.get("trimPct") or 0.5)))
            r["pct"] = pct
            r["notional"] = p["qty"] * pct * (px or p["entry"])
    # G6 — 한 호출 위험 축소 합계 한도
    reduce = sorted([r for r in out if r["outcome"] == "pass" and r["action"] in ("exit", "trim")], key=lambda r: -r["notional"])
    cap, used = C.MAX_REDUCE_PER_CALL * eq, 0.0
    for r in reduce:
        if used + r["notional"] > cap and used > 0:
            r.update(outcome="downgrade", action="flag_review", why=f"G6 한 호출 축소 한도 {C.MAX_REDUCE_PER_CALL:.0%} 초과")
        else:
            used += r["notional"]
    return out
