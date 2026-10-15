"""하방 촉매 Short 후보 리플레이(2026-10-15, PLAN_CATALYST_SHORT 4단계).

기간의 각 영업일에 대해 main 장중 촉매 아카이브로 `catalysts.collect_bearish → short_candidates` 를 돌리고(규칙·강도 기준만, LLM 없음),
후보 종목의 **다음 영업일 종가 기준** 1·3·5영업일 수익률(Short 관점 = 하락이 이익)을 CHARTDAY 로 집계한다. 10/13 두산퓨얼셀 같은
누락 사례가 후보로 나오는지, 강도별 적중률(하락 비율)·평균 수익률이 어떤지 보는 사전 검증. 운용 로직 변경 없음.

사용(Railway): GET /admin/catrep?token=…&from=2026-09-01&to=2026-10-14[&min=4][&drop=20] → 백그라운드, 결과 store `sim/catalyst_replay/<from>_<to>`
CLI: python -m swing.tools.replay_catalyst --from 2026-09-01 --to 2026-10-14 (오프라인은 fetch/dbq 주입 테스트 참고)"""
import datetime
import sys
import threading
import time

from swing import catalysts as C
from swing import config, tradedays, universe as U

HORIZONS = (1, 3, 5)


def _closes(dbq, code, d1, d2):
    rows = dbq.chart_day(code, d1, d2)
    m = {}
    for r in rows:
        d, c = U._iso(r.get("Date")), U._num(r.get("Prpr"))
        if len(d) == 10 and c:
            m[d] = c
    ds = sorted(m)
    return ds, [m[d] for d in ds]


def forward_returns(dates, closes, date):
    """date(신호일) 다음 영업일 종가를 진입가로, 그 뒤 1·3·5영업일 종가 수익률(%) → {ret1, ret3, ret5, entryDate}. 부족하면 None."""
    out = {f"ret{h}": None for h in HORIZONS}
    out["entryDate"] = None
    idx = [i for i, d in enumerate(dates) if d > date]
    if not idx:
        return out
    i0 = idx[0]
    e = closes[i0]
    out["entryDate"] = dates[i0]
    for h in HORIZONS:
        j = i0 + h
        if j < len(dates) and e:
            out[f"ret{h}"] = round((closes[j] / e - 1) * 100, 2)
    return out


def replay(d_from, d_to, *, fetch=None, dbq=None, min_strength=None, max_drop=None, progress=None, log=print):
    """→ {from, to, days, rows[], summary{by strength/kind}, missing[]}"""
    min_strength = config.CAT_SHORT_MIN if min_strength is None else min_strength
    max_drop = config.CAT_MAX_DROP if max_drop is None else max_drop
    days = tradedays.trading_days(d_from, d_to)
    rows, missing, n_arch = [], [], 0
    for i, d in enumerate(days):
        arch = C.fetch_archive(d, fetch=fetch, log=log)
        if not arch:
            missing.append(d)
            continue
        n_arch += 1
        entries = C.collect_bearish(arch)
        cands, skipped = C.short_candidates(entries, min_strength=min_strength, max_drop=max_drop)
        for c in cands:
            k = c["catalyst"]
            rows.append({"date": d, "code": c["code"], "name": c["name"], "strength": k["strength"], "kind": k["kind"],
                         "ruleKind": k.get("ruleKind"), "title": k.get("title"), "changePct": k.get("changePct"),
                         "newsRounds": k.get("newsRounds"), "amended": k.get("amended"), "reason": c["reason"],
                         "entryDate": None, **{f"ret{h}": None for h in HORIZONS}})
        if progress:
            progress(f"{d} 후보 {len(cands)} ({i + 1}/{len(days)})")
    # 수익률 — 종목별 CHARTDAY 1콜(기간 전체)
    if dbq is not None and rows:
        px_from = (datetime.date.fromisoformat(d_from) - datetime.timedelta(days=10)).isoformat()
        px_to = (datetime.date.fromisoformat(d_to) + datetime.timedelta(days=14)).isoformat()
        cache = {}
        for n, r in enumerate(rows):
            if r["code"] not in cache:
                try:
                    cache[r["code"]] = _closes(dbq, r["code"], px_from, px_to)
                except Exception as e:
                    log(f"[catrep] {r['code']} CHARTDAY 실패: {str(e)[:60]}")
                    cache[r["code"]] = ([], [])
            ds, cs = cache[r["code"]]
            r.update(forward_returns(ds, cs, r["date"]))
            if progress and n % 20 == 0:
                progress(f"수익률 {n}/{len(rows)}")
    return {"from": d_from, "to": d_to, "days": len(days), "archives": n_arch, "missing": missing,
            "minStrength": min_strength, "maxDrop": max_drop, "rows": rows, "summary": summarize(rows)}


def summarize(rows):
    """강도·종류별: 건수, 1/3/5일 하락 비율(Short 적중), 평균 수익률(Short 관점 = −ret)."""
    def agg(xs):
        o = {"n": len(xs)}
        for h in HORIZONS:
            v = [x[f"ret{h}"] for x in xs if x.get(f"ret{h}") is not None]
            o[f"n{h}"] = len(v)
            o[f"hit{h}"] = round(sum(1 for r in v if r < 0) / len(v) * 100, 1) if v else None      # 하락 비율 %
            o[f"short{h}"] = round(-sum(v) / len(v), 2) if v else None                              # Short 평균 수익 %
        return o
    out = {"all": agg(rows)}
    for s in sorted({r["strength"] for r in rows}, reverse=True):
        out[f"strength{s}"] = agg([r for r in rows if r["strength"] == s])
    for k in sorted({r.get("kind") or "?" for r in rows}):
        out[f"kind:{k}"] = agg([r for r in rows if (r.get("kind") or "?") == k])
    for k in sorted({r.get("ruleKind") for r in rows if r.get("ruleKind")}):
        out[f"rule:{k}"] = agg([r for r in rows if r.get("ruleKind") == k])
    return out


def start_background(store, d_from, d_to, **kw):
    """swing-web /admin/catrep — 스레드로 돌리고 진행은 store `sim/catalyst_replay/progress`."""
    key = f"sim/catalyst_replay/{d_from}_{d_to}"

    def prog(msg):
        store.put("sim/catalyst_replay/progress", {"key": key, "msg": msg, "at": datetime.datetime.now().isoformat(timespec="seconds")})

    def run():
        t0 = time.time()
        try:
            prog("시작")
            dbq = U.DbQuote(log=print)
            res = replay(d_from, d_to, dbq=dbq, progress=prog, **kw)
            res["secs"] = round(time.time() - t0, 1)
            store.put(key, res)
            prog(f"완료 {res['secs']}s · 후보 {len(res['rows'])}")
        except Exception as e:
            prog(f"실패 {type(e).__name__}: {str(e)[:120]}")
    threading.Thread(target=run, daemon=True).start()
    return key


if __name__ == "__main__":
    a = sys.argv[1:]
    f = a[a.index("--from") + 1] if "--from" in a else "2026-09-01"
    t = a[a.index("--to") + 1] if "--to" in a else datetime.date.today().isoformat()
    res = replay(f, t, dbq=U.DbQuote())
    import json
    print(json.dumps(res["summary"], ensure_ascii=False, indent=1))
    for r in res["rows"]:
        print(r["date"], r["code"], r["name"], r["strength"], r.get("ret1"), r.get("ret3"), r.get("ret5"), (r.get("title") or r.get("reason"))[:40])
