"""신규 후보 보충 판단(2026-11-09, PLAN_NEW_ONLY.md) — 그날 하루 처리가 주간 브리핑 날짜 게이트(wb_ok)에 걸려
매트릭스·유니버스 신규 후보 판단을 통째로 생략했을 때, 브리핑이 당일자로 복구된 뒤 **신규 후보 판단·주문만** 다시 돌린다.

  python -m swing.run_daily --new-only [--dry-run] [--date YYYY-MM-DD]

규칙
- 청산·재판별·매도 검토·재시도·비중 채우기·복기는 손대지 않는다. 하방 촉매 Short 도 제외(정규 런에서 이미 판단).
- 후보 수집은 run_day 와 같은 함수(daily.collect_matrix_candidates / collect_universe_candidates). 유니버스는 그날 문서를
  읽기만 하고(재산출 없음), 문서가 없으면 유니버스 소스는 생략(노트).
- 주문은 daily._new_entries → run.entryDate(다음 영업일) 1일 유효 지정가. 판단 총량(JUDGE_PER_DAY − 그날 재판별 수)·
  Long/Short 반반 배분(daily.judge_budget)은 정규 런과 동일.
- 가드(하나라도 걸리면 아무것도 쓰지 않음): 생략 노트가 있는 날 · run 에 신규 후보 행 없음(중복 방지) · 원장 asof == date ·
  새 브리핑 asof == date · progress 가 running 아님 · 장중(평일 09:00~15:30 KST) 아님.
- --dry-run 은 후보·총량 배분만 출력하고 store 에 아무것도 쓰지 않는다(읽기 전용 래퍼).
- 판단 실패 행은 그대로 남겨 기존 --redo 가 재분석할 수 있게 한다.
"""
import datetime

from . import config, daily, signals, tradedays

KST = datetime.timezone(datetime.timedelta(hours=9))
SKIP_NOTE = "신규·신호 매도 검토 생략"          # daily.run_day 가 wb 게이트에서 남기는 노트의 공통 꼬리


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class _ReadOnlyStore:
    """dry-run 용 — 읽기는 그대로, 쓰기는 버린다(개수만 센다)."""

    def __init__(self, inner):
        self.inner, self.dropped = inner, 0

    def get(self, key):
        return self.inner.get(key)

    def keys(self, prefix=""):
        return self.inner.keys(prefix)

    def put(self, key, doc):
        self.dropped += 1


def guard(date, store, wb, now=None):
    """실행 가능 여부 — (거부 사유 | None, run, ledger)."""
    run = store.get(f"run/{date}")
    if not run:
        return f"run/{date} 없음(그날 정산 런이 없음)", None, None
    if not any(SKIP_NOTE in str(n) for n in run.get("notes") or []):
        return "그날 신규 검토가 생략되지 않음(보충 대상 아님)", run, None
    if prior_rows(run):
        return "이미 신규 후보 판단 있음(중복 방지)", run, None
    L = store.get("ledger")
    if not L or L.get("asof") != date:
        return f"원장 asof {(L or {}).get('asof')} ≠ {date} — 이후 런이 돌아 소급 불가", run, None
    wa = signals.asof_date(wb)
    if not wb or wa != date:
        return f"주간 브리핑 asof {wa} ≠ {date} — 여전히 스테일", run, None
    prog = store.get("progress") or {}
    if prog.get("status") == "running":
        return "다른 실행 진행 중(progress running)", run, None
    now = now or datetime.datetime.now(KST)
    hm = now.hour * 100 + now.minute
    if now.weekday() < 5 and 900 <= hm < 1530:
        return "장중(09:00~15:30 KST) 실행 금지 — Live Trader 가 원장을 함께 씀", run, None
    return None, run, L


def prior_rows(run):
    """그날 run 에 이미 있는 매트릭스·유니버스 신규 후보 행 — 하방 촉매 행(source=catalyst)은 wb 게이트와 무관하게 정규 런에서
    판단되므로 제외(2026-11-09 실측: 촉매 Short 1건이 있던 날 가드가 거부). 비어 있지 않으면 보충 대상이 아니다."""
    return [r for r in (run.get("candidates") or []) + (run.get("shortCandidates") or [])
            if (r.get("source") or "matrix") != "catalyst"]


def _row(c, side):
    return {"code": c["code"], "name": c.get("name"), "side": side, "source": c.get("source") or "matrix",
            "signal": c.get("signal"), "sector": c.get("sector"), "score": c.get("score"), "rankAll": c.get("rankAll")}


def run_new_only(date, store, agent, prices, wb, *, uni=None, dry_run=False, log=print, now=None):
    """생략된 신규 후보 판단·주문 보충 → 요약 dict. dry_run 이면 후보·배분만 반환(쓰기 없음)."""
    why, run, L = guard(date, store, wb, now)
    if why:
        return {"skip": why}
    if dry_run:
        store = _ReadOnlyStore(store)
    nxt = run.get("entryDate") or tradedays.next_trading_day(date)
    tmp = {"notes": []}                                   # 수집 단계의 노트·필드(dry-run 때 run 오염 방지)
    if uni is not None and not store.get(f"universe/{date}"):
        tmp["notes"].append("유니버스 문서 없음 — 유니버스 후보 생략(보충 런은 재산출하지 않음)")
        uni = None
    cands, scands, fut_set, held, pend = daily.collect_matrix_candidates(L, store, wb, date, True, uni, tmp)
    bars = {}
    ucands, uscands, sk_u, sk_us = daily.collect_universe_candidates(
        L, store, wb, date, True, uni, tmp, cands, scands, held, pend, bars, prices)
    sk_m, cands = cands[config.MAX_NEW_PER_DAY:], cands[:config.MAX_NEW_PER_DAY]
    sk_ms, scands = scands[config.MAX_NEW_SHORT_PER_DAY:], scands[:config.MAX_NEW_SHORT_PER_DAY]
    n_rev = len(run.get("reviews") or [])
    budget = max(config.ENTRY_JUDGE_MIN, config.JUDGE_PER_DAY - n_rev)
    (cands, ucands), (scands, uscands, _), over_l, over_s = daily.judge_budget(budget, [cands, ucands], [scands, uscands, []])
    jb = None
    if over_l or over_s:
        over_l, over_s = over_l or [[], []], over_s or [[], [], []]
        sk_m += over_l[0]; sk_u += over_l[1]; sk_ms += over_s[0]; sk_us += over_s[1]
        jb = {"budget": budget, "reviews": n_rev,
              "deferredLong": [c["code"] for c in sum(over_l, [])], "deferredShort": [c["code"] for c in sum(over_s, [])]}
    plan = {"date": date, "entryDate": nxt, "budget": budget, "reviews": n_rev,
            "long": [_row(c, "long") for c in cands + ucands], "short": [_row(c, "short") for c in scands + uscands],
            "skipped": {"matrixCap": [c["code"] for c in sk_m], "uniCap": [c["code"] for c in sk_u],
                        "matrixShortCap": [c["code"] for c in sk_ms], "uniShortCap": [c["code"] for c in sk_us],
                        "cooldown": tmp.get("matrixCooldown") or [], "shortFut": tmp.get("shortFutSkipped") or []},
            "judgeBudget": jb, "notes": list(tmp["notes"])}
    for side in ("long", "short"):
        for r in plan[side]:
            log(f"[swing] {date} 보충 후보 {side} {r['code']} {r['name']} ({r['source']}·{r['signal']}·점수 {r['score']})")
    log(f"[swing] {date} 보충 판단 총량 {budget}(재판별 {n_rev}) — Long {len(plan['long'])} · Short {len(plan['short'])}"
        + (f" · 이연 Long {len(jb['deferredLong'])}/Short {len(jb['deferredShort'])}" if jb else ""))
    if dry_run:
        return {"dryRun": True, "long": len(plan["long"]), "short": len(plan["short"]), "plan": plan,
                "storeWritesDropped": store.dropped}

    codes = sorted({c["code"] for c in cands + ucands + scands + uscands} - set(bars))
    if codes:
        bars.update(prices.daily_bars(codes, date))
    prog = daily.Progress(store, date, getattr(agent, "name", ""))
    prog.update(phase="new-only", total=len(cands) + len(ucands) + len(scands) + len(uscands))
    daily._exposure_setup(L, store, bars, date, tmp)        # 보충 주문도 종목 상한·섹터 브레이크 적용(2026-11-10, engine.place_entry)
    longs = (daily._new_entries(L, store, agent, cands, bars, date, nxt, "long", prog)
             + daily._new_entries(L, store, agent, ucands, bars, date, nxt, "long", prog))
    sfut = (uni.fut_set(date) if config.UNI_SHORT_FUT_ONLY and uni is not None and (scands or uscands) else None)
    shorts = (daily._new_entries(L, store, agent, scands, bars, date, nxt, "short", prog, fut=sfut)
              + daily._new_entries(L, store, agent, uscands, bars, date, nxt, "short", prog, fut=sfut))
    daily.engine.EXPOSURE_CTX = None
    ordered = sum(1 for r in longs + shorts if r.get("ordered"))
    failed = sum(1 for r in longs + shorts if str(r.get("why") or "").startswith("판단 실패"))
    ncap = sum(1 for r in longs + shorts if r.get("stopCap"))

    # 정규 런의 촉매 행은 보존하고 뒤에 보충 행을 잇는다(대시보드·redo 가 같은 목록을 읽는다)
    run["candidates"] = list(run.get("candidates") or []) + longs
    run["shortCandidates"] = list(run.get("shortCandidates") or []) + shorts
    run["capSkipped"], run["uniCapSkipped"] = plan["skipped"]["matrixCap"], plan["skipped"]["uniCap"]
    run["shortCapSkipped"], run["uniShortCapSkipped"] = plan["skipped"]["matrixShortCap"], plan["skipped"]["uniShortCap"]
    for k in ("matrixCooldown", "shortFutSkipped", "uniCaps"):
        if k in tmp:
            run[k] = tmp[k]
    if jb:
        run["judgeBudget"] = jb
    at = _now()
    run["newOnly"] = {"at": at, "long": len(longs), "short": len(shorts), "ordered": ordered, "failed": failed,
                      "budget": budget, "prevNotes": [n for n in run.get("notes") or [] if SKIP_NOTE in str(n)]}
    extra = tmp["notes"] + ([f"손절 폭 상한 적용 {ncap}종목"] if ncap else [])
    run["notes"] = list(run.get("notes") or []) + extra + [
        f"신규 후보 보충 판단({at[11:16]}) Long {len(longs)}(주문 {sum(1 for r in longs if r.get('ordered'))}) · "
        f"Short {len(shorts)}(주문 {sum(1 for r in shorts if r.get('ordered'))}) · 판단 실패 {failed}"]
    store.put(f"signals/{date}", signals.compact(wb))
    store.put("ledger", L)
    store.put(f"run/{date}", run)
    prog.update(status="done", phase="done", finishedAt=_now())
    res = {"date": date, "at": at, "entryDate": nxt, "long": len(longs), "short": len(shorts),
           "ordered": ordered, "failed": failed, "budget": budget}
    log(f"[swing] {date} 신규 후보 보충 끝 — {res}")
    return res
