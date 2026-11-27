"""RiskGate(L5) — LLM 결정을 축소·거부만 한다, 키우지 않는다(PLAN_PM §7). 통과·강등분만 Live Trader 지시가 된다.

  G1 모르는 종목·형식 오류 → 거부            G2 tighten_stop 이 불리한 방향이거나 현재가를 넘음 → 거부
  G3 확신도 < MIN_CONF_EXIT 인 exit/trim → flag_review 로 강등
  G4 FLIP_MIN 분 안에 같은 종목 지시가 이미 있음 → 거부(중복·번복 방지)
  G5 Live Trader 가 그 종목 진입(잔여분 추가 등)을 오늘 진행 중 → exit/trim 을 flag_review 로 강등
  G6 한 호출의 exit+trim 명목 합계 > 평가액 × MAX_REDUCE_PER_CALL → 큰 것부터 한도까지, 나머지 flag_review
  G7 판단 시간(DECIDE_START~END) 밖 → 거부   G8 Live Trader 가 이미 청산 중(손절·예약·지시) → 거부
  G9 거래 상태가 허용하지 않음 → 거부
  G10 이번 호출에 트리거가 없는 종목의 exit/trim → flag_review 로 강등. 예외: T8(순노출 이탈)이 떴고 넘친 방향 포지션이거나,
      논거 무효(thesisValid=false)·확신도 MIN_CONF_UNTRIGGERED 이상(2026-11-27 — 11/27 T5 호출에서 트리거 없는
      이수스페셜티케미컬을 순노출 +16.15%(T8 기준 +17% 전)를 이유로 줄였다)
trimPct 는 0~1 비율. LLM 이 퍼센트(50.0)로 답하면 100 으로 나눈다(2026-11-27 — 50.0 이 상한 0.9 로 잘려 90% 매도).
"""
import datetime

from .. import engine
from . import config as C


def _min(a, b):
    return (datetime.datetime.fromisoformat(b) - datetime.datetime.fromisoformat(a)).total_seconds() / 60


def trim_pct(v):
    """LLM trimPct → 0.1~0.9 비율. 1 보다 크면 퍼센트로 보고 100 으로 나눈다. 없으면 0.5."""
    try:
        x = float(v) if v is not None else 0.5
    except (TypeError, ValueError):
        x = 0.5
    if x > 1:
        x /= 100.0
    return min(0.9, max(0.1, x))


def review(decisions, *, ts, book, state, intraday=None, recent=(), triggers=None, net=None):
    """decisions = LLM decisions 목록. recent = 오늘 이미 낸 지시 목록. 반환 [{code, pid, decision, outcome, action, why, …}].
    outcome = hold | pass | downgrade | reject. pass·downgrade 이고 action ≠ hold 인 것만 지시가 된다.
    triggers = 이번 호출 트리거 목록(None 이면 G10 생략), net = runner.net_info(breach 로 T8 넘친 방향)."""
    hm = ts[11:16]
    trig_codes = {t.get("code") for t in triggers or [] if t.get("code") not in (None, "*")}
    t8_side = ((net or {}).get("breach") or {}).get("side") if any(t.get("id") == "T8" for t in triggers or []) else None
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
        if not (C.at(ts[:10], "DECIDE_START") <= hm < C.at(ts[:10], "DECIDE_END")):
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
            side = "short" if engine.is_short(p) else "long"
            if triggers is not None and code not in trig_codes and t8_side != side and not (
                    d.get("thesisValid") is False and (d.get("confidence") or 0) >= C.MIN_CONF_UNTRIGGERED):
                r.update(outcome="downgrade", action="flag_review",
                         why="G10 트리거 없는 종목 축소 — T8 넘친 방향이거나 논거 무효·확신도 "
                             f"{C.MIN_CONF_UNTRIGGERED} 이상일 때만")
                continue
            pct = 1.0 if act == "exit" else trim_pct(d.get("trimPct"))
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
