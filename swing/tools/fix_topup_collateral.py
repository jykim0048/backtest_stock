"""Short 추가 진입 담보 오기록 보정 — 2026-11-12 (engine.settle_day 수정과 한 쌍).

사고: cash_first(2026-11-10 도입)가 추가 진입(잔여분, topupOf) 주문에도 주문 단위로 담보를 골랐다. 본 포지션이 대용 담보인데
추가분이 현금 담보로 잡히면 현금은 '진입 금액 + 매도세'만큼 빠지는데, 포지션 평가(position_value)는 대용식(진입가 − 현재가)이라
진입 금액이 평가액에서 사라진다. 2026-11-10 한국금융지주 18주(221만), 2026-11-11 키움증권 117주(2,195만)·한국금융지주 40주(486만)
→ 11/11 당일 수익률이 −0.24% 가 아니라 −5.75% 로 기록됐다.

보정: 해당 주문마다 빠진 담보(수량 × 체결가)를 현금으로 돌려주고, 체결일부터의 평가액 기록(equity 행)에 같은 금액을 더한다.
주문은 대용 담보로 고쳐 적는다(collateralFix 에 원래 값·환급액). 매도세·대차수수료는 실제 비용이라 그대로.
본 포지션이 이미 청산됐으면 손대지 않고 알리기만 한다(청산 손익에도 같은 금액이 빠져 있어 따로 봐야 함).

  python swing/tools/fix_topup_collateral.py            # 미리보기(쓰기 없음)
  python swing/tools/fix_topup_collateral.py --apply    # 운영 저장소에 반영(반영 전 원장을 backup/ledger/pre-fix-topup-collateral 에 남김)

정산(swing-cron 17:05~)·new-only 실행 중에는 돌리지 않는다(둘 다 원장을 통째로 덮어씀). 두 번 돌려도 같은 결과(이미 고친 주문은 건너뜀).
"""
import argparse
import copy
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from swing import engine as E, store as S  # noqa: E402

BACKUP_KEY = "backup/ledger/pre-fix-topup-collateral"


def find(L):
    """(고칠 주문들, 본 포지션이 청산돼 못 고친 주문들)."""
    pos = {p["id"]: p for p in L["positions"]}
    fix, closed = [], []
    for o in L.get("orderLog") or []:
        if not (o.get("status") == "filled" and o.get("side") == "short" and o.get("topupOf")):
            continue
        if (o.get("collateral") or "cash") != "cash" or o.get("collateralFix"):
            continue
        p = pos.get(o["topupOf"])
        if p is None:
            closed.append(o)
        elif E.is_subst(p):
            fix.append(o)
    return fix, closed


def apply_fix(L, today):
    """L 을 고친다(제자리). 반환 = [(주문, 환급액)]."""
    fix, _ = find(L)
    out = []
    for o in fix:
        refund = o["qty"] * o["fillPrice"]
        L["cash"] += refund
        for row in L.get("equity") or []:
            if row["date"] >= o["validDate"]:
                row["cash"] = round(row["cash"] + refund)
                row["equity"] = round(row["equity"] + refund)
                row["retPct"] = round((row["equity"] / L["capital0"] - 1) * 100, 3)
        o["collateralFix"] = {"date": today, "was": o.get("collateral") or "cash", "refund": round(refund)}
        o["collateral"] = "subst"
        out.append((o, refund))
    if out and "collateral" in L:
        L["collateral"] = {k: round(v) for k, v in E.collateral(L).items()}
    return out


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--today", default=None, help="기록용 날짜(기본 = 원장 asof)")
    a = ap.parse_args(argv)
    st = S.from_env()
    cur = st.get("ledger")
    if not cur:
        raise SystemExit("원장 없음")
    L = copy.deepcopy(cur)
    _, closed = find(L)
    eq_before = {r["date"]: r["equity"] for r in L.get("equity") or []}
    done = apply_fix(L, a.today or L.get("asof"))
    for o, r in done:
        print(f"[fix] {o['validDate']} {o['id']} {o.get('name')}({o['code']}) {o['qty']:,}주 × {o['fillPrice']:,.0f} → 현금 +{r:,.0f}")
    for o in closed:
        print(f"[fix] 건너뜀(본 포지션 청산됨 — 따로 확인) {o['validDate']} {o['id']} {o.get('name')} {o['qty']:,}주")
    if not done:
        print("[fix] 고칠 주문 없음")
        return
    print(f"[fix] 현금 {cur['cash']:,.0f} → {L['cash']:,.0f}")
    for r in (L.get("equity") or [])[-4:]:
        print(f"[fix] 평가액 {r['date']}: {eq_before.get(r['date']):,} → {r['equity']:,} ({r['retPct']:+.3f}%)")
    if not a.apply:
        print("[fix] 미리보기 — --apply 로 반영")
        return
    if st.get(BACKUP_KEY) is None:
        st.put(BACKUP_KEY, cur)
        print("[fix] 원장 백업:", BACKUP_KEY)
    if st.get("ledger") != cur:
        raise SystemExit("원장이 그사이 바뀌었습니다(정산·new-only 실행 중?) — 다시 실행하세요")
    st.put("ledger", L)
    print(f"[fix] 반영 완료 — 주문 {len(done)}건")


if __name__ == "__main__":
    main()
