"""_finalize_regime 판정표 회귀 — regime 후검증(강등·승격·signals) (오프라인).

실행: python tests/test_regime_finalize.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import generate_intraday_briefing as g

IDX_DOWN = {"kospi": {"rate": -1.5}, "kosdaq": {"rate": -2.1}}      # 평균 -1.8
IDX_UP = {"kospi": {"rate": 0.8}, "kosdaq": {"rate": 0.4}}          # 평균 +0.6
IDX_CRASH = {"kospi": {"rate": -2.4}, "kosdaq": {"rate": -2.8}}     # 평균 -2.6 (극단)
IDX_SURGE = {"kospi": {"rate": 2.9}, "kosdaq": {"rate": 2.3}}       # 평균 +2.6 (극단)
FLOW_SELL = {"kospi": {"foreign": -3000, "institution": -800}}
FLOW_BUY = {"kospi": {"foreign": 1200, "institution": 400}}


def _llm(stance, conf="high", reason="r"):
    return {"stance": stance, "confidence": conf, "reason": reason}


def main():
    # ── 정합 회차: 강등·승격 없음, signals 만 부착 ─────────────────────────────
    r = g._finalize_regime(_llm("risk_off"), IDX_DOWN, FLOW_SELL)
    assert r["stance"] == "risk_off" and r["confidence"] == "high"
    assert r["signals"] == {"idxAvg": -1.8, "flowSum": -3800,
                            "demoted": False, "promoted": False}
    print("정합 유지 OK")

    # ── ② 모순 강등: 가격 또는 수급이 판정과 반대면 high→low (stance 는 유지) ──
    r = g._finalize_regime(_llm("risk_off"), IDX_UP, FLOW_SELL)     # 지수 상승 상충
    assert r["stance"] == "risk_off" and r["confidence"] == "low"
    assert r["signals"]["demoted"] is True and "강등" in r["reason"]
    r = g._finalize_regime(_llm("risk_off"), IDX_DOWN, FLOW_BUY)    # 수급 양수 상충
    assert r["confidence"] == "low" and r["signals"]["demoted"] is True
    r = g._finalize_regime(_llm("risk_on"), IDX_DOWN, FLOW_BUY)     # risk_on 대칭
    assert r["confidence"] == "low" and r["signals"]["demoted"] is True
    r = g._finalize_regime(_llm("risk_off", conf="low"), IDX_UP, FLOW_BUY)  # 이미 low — 무변화
    assert r["confidence"] == "low" and r["signals"]["demoted"] is False
    print("모순 강등 OK")

    # ── ③ 극단 승격(가격 권위): LLM neutral 이어도 방향 stance 로 ─────────────
    r = g._finalize_regime(_llm("neutral", conf="low"), IDX_CRASH, FLOW_SELL)
    assert r["stance"] == "risk_off" and r["confidence"] == "low"   # PROMOTE_HIGH 기본 OFF
    assert r["signals"]["promoted"] is True and "극단 승격" in r["reason"]
    r = g._finalize_regime(_llm("neutral", conf="low"), IDX_SURGE, FLOW_BUY)  # 상방 대칭
    assert r["stance"] == "risk_on" and r["signals"]["promoted"] is True
    # 수급이 방향과 '적극 반대'면 승격 보류(휩소 방어)
    r = g._finalize_regime(_llm("neutral", conf="low"), IDX_CRASH, FLOW_BUY)
    assert r["stance"] == "neutral" and r["signals"]["promoted"] is False
    # 수급 결측이면 가격 권위로 승격 허용
    r = g._finalize_regime(_llm("neutral", conf="low"), IDX_CRASH, {})
    assert r["stance"] == "risk_off" and r["signals"]["flowSum"] is None
    # 이미 그 방향이면 승격 아님(플래그 False)
    r = g._finalize_regime(_llm("risk_off"), IDX_CRASH, FLOW_SELL)
    assert r["stance"] == "risk_off" and r["signals"]["promoted"] is False
    print("극단 승격 OK")

    # ── ① LLM 무효/누락 → 기계 폴백 후 동일 후검증 ────────────────────────────
    r = g._finalize_regime({"stance": "bogus"}, IDX_UP, FLOW_BUY)
    assert r["stance"] == "neutral" and r["confidence"] == "low"    # 평균 +0.6 → neutral
    r = g._finalize_regime(None, IDX_CRASH, FLOW_SELL)
    assert r["stance"] == "risk_off"                                # 기계 -2.6 → risk_off
    print("무효 폴백 OK")

    # ── 결측 내성: 지수·수급 없음 → 강등·승격 없이 통과 ────────────────────────
    r = g._finalize_regime(_llm("risk_off"), {}, {})
    assert r["confidence"] == "high"
    assert r["signals"] == {"idxAvg": None, "flowSum": None,
                            "demoted": False, "promoted": False}
    print("결측 내성 OK")

    # ── env 노브: PROMOTE_HIGH=1 이면 승격 시 high / DEMOTE=0 이면 강등 안함 ───
    g.REGIME_PROMOTE_HIGH = True
    try:
        r = g._finalize_regime(_llm("neutral", conf="low"), IDX_CRASH, FLOW_SELL)
        assert r["confidence"] == "high" and r["signals"]["promoted"] is True
    finally:
        g.REGIME_PROMOTE_HIGH = False
    g.REGIME_DEMOTE = False
    try:
        r = g._finalize_regime(_llm("risk_off"), IDX_UP, FLOW_BUY)
        assert r["confidence"] == "high" and r["signals"]["demoted"] is False
    finally:
        g.REGIME_DEMOTE = True
    print("env 노브 OK")
    print("ALL PASS")


if __name__ == "__main__":
    main()
