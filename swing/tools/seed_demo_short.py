"""대시보드 확인용 **테스트 Short 데이터** 주입(로컬 파일 저장소 전용).

엔진은 아직 Long 전용이라 Short 탭·PF 카드·방향 필터를 눈으로 확인할 방법이 없어, 원장에 표시용
Short 보유·청산을 넣는다. 모든 항목에 demo=true(대시보드 TEST 표시), 재실행하면 이전 demo 항목을 지우고
다시 넣는다. **이 원장으로 run_daily 를 이어 돌리지 말 것**(엔진이 Short 를 Long 규칙으로 정산함).

  python swing/tools/seed_demo_short.py --store swing/data/local          # 넣기
  python swing/tools/seed_demo_short.py --store swing/data/local --remove # 빼기
"""
import argparse
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _ROOT)
from swing import config, store  # noqa: E402

# (코드, 이름, 섹터, 신호, 진입가, 손절가, 목표가, 수량, 체결일, 보유일, 종가)
HOLD = [
    ("028260", "삼성물산", "유통", "동반약세", 162000, 170100, 150700, 300, "2026-09-24", 2, 158500),
    ("016360", "삼성증권", "증권", "동반약세", 61500, 64600, 57200, 700, "2026-09-25", 1, 62300),
    ("006800", "미래에셋증권", "증권", "수급이탈", 13200, 13860, 12280, 3000, "2026-09-22", 3, 12790),
]
# (코드, 이름, 섹터, 신호, 진입일, 진입가, 청산일, 청산가, 사유, 수량, 보유일)
CLOSED = [
    ("257720", "실리콘투", "유통", "동반약세", "2026-09-15", 41000, "2026-09-19", 38150, "target", 1000, 5),
    ("039490", "키움증권", "증권", "동반약세", "2026-09-16", 255000, "2026-09-17", 267750, "stop", 150, 2),
    ("000720", "현대건설", "건설", "수급이탈", "2026-09-17", 58900, "2026-09-23", 57400, "expiry", 700, 5),
    ("047040", "대우건설", "건설", "수급이탈", "2026-09-18", 9650, "2026-09-22", 9980, "pm_sell", 4000, 3),
]


def _is_demo(x):
    return bool(x.get("demo"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True, help="로컬 파일 저장소 폴더(예: swing/data/local)")
    ap.add_argument("--remove", action="store_true")
    a = ap.parse_args(argv)
    if os.environ.get("DATABASE_URL"):
        raise SystemExit("DATABASE_URL 이 설정돼 있음 — 테스트 데이터는 로컬 파일 저장소에만 넣는다")
    st = store.FileStore(a.store)
    L = st.get("ledger")
    if not L:
        raise SystemExit("원장이 없음 — 먼저 mock 백필로 데모 데이터를 만든다")
    L["positions"] = [p for p in L["positions"] if not _is_demo(p)]
    L["trades"] = [t for t in L["trades"] if not _is_demo(t)]
    if a.remove:
        st.put("ledger", L)
        print("demo Short 제거")
        return
    for i, (code, name, sec, sig, e, s_, t, q, fd, hd, c) in enumerate(HOLD):
        key = f"decision/{fd}/{code}/entry-demo"
        L["positions"].append({"id": f"demo-p{i}", "code": code, "name": name, "side": "short", "qty": q,
                               "entry": float(e), "stop": float(s_), "target": float(t), "fillDate": fd,
                               "holdDay": hd, "sector": sec, "signal": sig, "decisionKey": key,
                               "lastClose": float(c), "sellPending": None, "demo": True})
        st.put(key, {"code": code, "date": fd, "purpose": "entry", "rating": "Underweight", "action": "Sell",
                     "entry": e, "stop": s_, "target": t, "weight": 0.05, "agent": "demo",
                     "summary": f"[TEST] {sec} {sig} — 공매도 진입 {e:,} · 손절 {s_:,}(진입가 위) · 목표 {t:,}(진입가 아래)",
                     "reports": {"trader": "**Action**: Sell\n\n(테스트 데이터 — 실제 판단 아님)"}})
    for i, (code, name, sec, sig, ed, e, xd, x, why, q, hd) in enumerate(CLOSED):
        # 숏 손익 = (진입가 − 환매가) × 수량 − 매도세(공매도 진입이 매도, 환매는 매수라 세금 없음)
        pnl = q * (e - x) - q * e * config.SELL_TAX
        key = f"decision/{ed}/{code}/entry-demo"
        L["trades"].append({"id": f"demo-t{i}", "code": code, "name": name, "side": "short", "qty": q,
                            "entryDate": ed, "entryPrice": float(e), "exitDate": xd, "exitPrice": float(x),
                            "reason": why, "holdDays": hd, "tax": round(q * e * config.SELL_TAX),
                            "pnl": round(pnl), "retPct": round(pnl / (q * e) * 100, 2), "sector": sec,
                            "signal": sig, "decisionKey": key, "exitDecisionKey": None, "demo": True})
        st.put(key, {"code": code, "date": ed, "purpose": "entry", "rating": "Sell", "action": "Sell",
                     "entry": e, "agent": "demo", "summary": f"[TEST] {sec} {sig} — 공매도 진입 {e:,}",
                     "reports": {"trader": "**Action**: Sell\n\n(테스트 데이터 — 실제 판단 아님)"}})
    L["trades"].sort(key=lambda t: t["exitDate"])
    st.put("ledger", L)
    print(f"demo Short 주입: 보유 {len(HOLD)} · 청산 {len(CLOSED)}")


if __name__ == "__main__":
    main()
