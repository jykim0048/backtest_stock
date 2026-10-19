"""하루 재정산 — 백업 원장에서 그날을 다시 처리하되 **그날 저장된 판단을 그대로 재사용**(LLM 재호출 없음).

2026-10-19 사고(Live Trader 시세 전부 실패 → 정산이 무체결 감시 주문 10건을 미도달로 취소) 복구용으로 만들었다.
엔진·Live Trader 수정이 배포된 코드로 돌려야 한다(Railway swing-web 컨테이너에서 실행).

  python swing/tools/resettle_day.py --date 2026-10-19 --backup backup/ledger/pre-2026-10-19            # 미리보기(쓰기 없음)
  python swing/tools/resettle_day.py --date 2026-10-19 --backup backup/ledger/pre-2026-10-19 --apply    # 운영 저장소에 반영

- 미리보기: 읽기는 운영 저장소, 쓰기는 메모리(OverlayStore). 결과 요약과 원래 런 대비 차이를 출력한다.
- --apply: 같은 미리보기를 돌린 뒤 메모리에 쓴 문서를 그대로 운영 저장소에 쓴다. 덮어쓰기 전 현재 원장을
  `backup/ledger/post-<date>-orig` 에 한 번 남긴다(이미 있으면 그대로).
- 판단: decision/<date>/<code>/<purpose> 저장본을 Decision 으로 되살린다. 저장본이 없으면 진입은 주문 없음, 검토는 보유
  (오류 없이) — 새로 생긴 검토 대상이 원 규칙 폴백 매도로 빠지지 않게.
- 쿨다운: 재실행에서는 그날 자신의 판단이 이미 저장돼 있으므로 쿨다운 계산에서 그날 판단을 뺀다(원래 런과 같은 후보).
- 유니버스는 저장본(universe/<date>) 재사용(force 아님), 하방 촉매·점수 보강은 원래처럼 다시 조회한다.
"""
import argparse
import dataclasses
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from swing import agent_iface as A  # noqa: E402
from swing import daily, run_daily, store as S, universe  # noqa: E402


class OverlayStore:
    """읽기는 base, 쓰기는 메모리. keys 는 둘의 합집합."""

    def __init__(self, base):
        self.base, self.mem = base, {}

    def get(self, key, default=None):
        if key in self.mem:
            return self.mem[key]
        return self.base.get(key, default)

    def put(self, key, obj):
        self.mem[key] = obj

    def keys(self, prefix=""):
        return sorted(set(self.base.keys(prefix)) | {k for k in self.mem if k.startswith(prefix)})


class StoredAgent:
    """그날 저장된 판단을 그대로 돌려준다(LLM 호출 없음)."""
    name = "stored"

    def __init__(self, store, log=print):
        self.store, self.log = store, log
        self.missing = []

    def decide(self, req):
        key = daily._dkey(req.date, req.code, req.purpose, req.side)
        doc = self.store.get(key)
        if not doc:
            self.missing.append(key)
            self.log(f"[resettle] 저장된 판단 없음 {key} — " + ("보유" if req.purpose == "review" else "주문 없음"))
            return A.Decision(code=req.code, date=req.date, purpose=req.purpose, side=req.side, agent=self.name,
                              rating="Hold" if req.purpose == "review" else "REVIEW")
        fields = {f.name for f in dataclasses.fields(A.Decision)}
        return A.Decision(**{k: v for k, v in doc.items() if k in fields})


_LIVE = object()


def resettle(date, base, backup_key, *, prices=None, wb=_LIVE, uni=_LIVE, cat=_LIVE, log=print):
    """미리보기 실행 → (overlay, run, agent). base 는 바꾸지 않는다. prices·wb·uni·cat 은 테스트 주입용(기본 = 운영 설정)."""
    pre = base.get(backup_key)
    if not pre:
        raise SystemExit(f"백업 원장 없음: {backup_key}")
    ov = OverlayStore(base)
    ov.put("ledger", pre)
    ag = StoredAgent(ov, log=log)
    a = SimpleNamespace(universe="live", force=False, universe_only=False)
    uni = run_daily._runner(ov, a) if uni is _LIVE else uni
    cat = run_daily._cat_runner(ov, a) if cat is _LIVE else cat
    wb = run_daily.load_wb(date) if wb is _LIVE else wb
    prices = prices or run_daily.make_prices(os.environ.get("SWING_PRICES", "dbhub"))
    orig = universe.recent_decisions

    def same_day_excluded(store, d, days=3):
        out = orig(store, d, days)
        return {c: x for c, x in out.items() if x != d}
    universe.recent_decisions = same_day_excluded
    try:
        run = daily.run_day(date, ov, ag, prices, wb, force=True, uni=uni, cat=cat, log=log)
    finally:
        universe.recent_decisions = orig
    return ov, run, ag


def _summary(L, run):
    return {"asof": L.get("asof"), "positions": len(L["positions"]), "orders": len(L["orders"]), "cash": round(L["cash"]),
            "equity": (L.get("equity") or [{}])[-1].get("equity"), "filled": len((run or {}).get("filled") or []),
            "exits": len((run or {}).get("exits") or [])}


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--date", required=True)
    ap.add_argument("--backup", required=True, help="재처리 시작 원장 키(예: backup/ledger/pre-2026-10-19)")
    ap.add_argument("--prices", default=os.environ.get("SWING_PRICES", "dbhub"))
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    base = S.from_env()
    cur = base.get("ledger")
    ov, run, ag = resettle(a.date, base, a.backup, prices=run_daily.make_prices(a.prices))
    new = ov.get("ledger")
    old_run = base.get(f"run/{a.date}") or {}
    print("[resettle] 현재 원장 :", _summary(cur, old_run))
    print("[resettle] 재정산 원장:", _summary(new, run))
    old_codes = {p["code"] for p in cur["positions"]}
    new_codes = {p["code"] for p in new["positions"]}
    print("[resettle] 새로 생긴 보유:", sorted(new_codes - old_codes), "· 사라진 보유:", sorted(old_codes - new_codes))
    print("[resettle] 다음 날 주문:", sorted((o["code"], o.get("side"), o["qty"]) for o in new["orders"]))
    print("[resettle] 저장된 판단 없음:", len(ag.missing), ag.missing[:10])
    if not a.apply:
        print("[resettle] 미리보기 — --apply 로 반영")
        return
    post_key = f"backup/ledger/post-{a.date}-orig"
    if base.get(post_key) is None:
        base.put(post_key, cur)
        print("[resettle] 현재 원장 백업:", post_key)
    for k, v in ov.mem.items():
        base.put(k, v)
    print(f"[resettle] 반영 완료 — 문서 {len(ov.mem)}개 쓰기")


if __name__ == "__main__":
    main()
