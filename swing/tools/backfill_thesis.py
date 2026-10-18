"""보유 포지션·대기 주문에 진입 판단 근거(thesis·invalidation·horizon) 소급 기록 — Live PM M0(PLAN_PM §5).

2026-10-18 이전에 체결된 포지션엔 thesis 가 없으므로, 저장된 진입 판단(decisionKey → decision/<date>/<code>/entry…)
에서 agent_iface.thesis_fields 로 뽑아 붙인다. 이미 thesis 가 있는 항목은 건드리지 않는다. 기본은 미리보기(쓰기 없음).

  python swing/tools/backfill_thesis.py                    # 미리보기, 저장소 = store.from_env()
  python swing/tools/backfill_thesis.py --apply            # 원장에 기록
  python swing/tools/backfill_thesis.py --store file:swing/data/demo
"""
import argparse
import os
import sys
from types import SimpleNamespace

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from swing import agent_iface as A  # noqa: E402
from swing import store as S  # noqa: E402


def backfill(st, apply=False, log=print):
    """반환: {"filled": n, "missing": [code…], "skipped": n}. apply=True 면 원장을 저장한다."""
    L = st.get("ledger")
    if not L:
        log("[thesis] 원장 없음")
        return {"filled": 0, "missing": [], "skipped": 0}
    out = {"filled": 0, "missing": [], "skipped": 0}
    for kind, rows in (("position", L.get("positions") or []), ("order", L.get("orders") or [])):
        for x in rows:
            if x.get("thesis"):
                out["skipped"] += 1
                continue
            doc = st.get(x["decisionKey"]) if x.get("decisionKey") else None
            f = A.thesis_fields(SimpleNamespace(summary=(doc or {}).get("summary") or "",
                                                reports=(doc or {}).get("reports") or {}))
            if not f:
                out["missing"].append(x["code"])
                log(f"[thesis] {kind} {x['code']} {x.get('name') or ''}: 판단 원문 없음({x.get('decisionKey')})")
                continue
            x.update(f)
            out["filled"] += 1
            log(f"[thesis] {kind} {x['code']} {x.get('name') or ''}: {f.get('thesis', '')[:60]}")
    if apply and out["filled"]:
        st.put("ledger", L)
    log(f"[thesis] 기록 {out['filled']} · 원문 없음 {len(out['missing'])} · 이미 있음 {out['skipped']}"
        + ("" if apply else " (미리보기 — --apply 로 저장)"))
    return out


def _store(spec):
    if spec and spec.startswith("file:"):
        return S.FileStore(spec[5:])
    return S.from_env()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="원장에 기록(없으면 미리보기)")
    ap.add_argument("--store", help="file:<dir> (기본 store.from_env())")
    a = ap.parse_args(argv)
    backfill(_store(a.store), apply=a.apply)


if __name__ == "__main__":
    main()
