"""llm 관용 파싱 + 기본 체인 순서 회귀 (오프라인).

실행: python tests/test_llm_parse_chain.py
배경(2026-09-03): ① 유효 JSON 뒤 여분 텍스트("Extra data")에 응답 전체가 버려져
링크 실패 ② 무료 티어 20/일짜리 3.5-flash 가 1순위라 상시 429 + 폴백 쿼터 잠식.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import llm


def main():
    # ── 관용 파싱 ─────────────────────────────────────────────────────────────
    obj = {"briefing": ["a"], "regime": {"stance": "neutral"}}
    clean = json.dumps(obj, ensure_ascii=False)

    # 정상 응답: 엄격 경로, 여분 0
    assert llm._parse_json(clean) == (obj, 0)

    # 실측 유형: 유효 JSON + 꼬리 텍스트 (2026-09-03 "Extra data: char 2241")
    tail = "\n이상으로 분석을 마칩니다."
    data, junk = llm._parse_json(clean + tail)
    assert data == obj and junk == len(tail)

    # 유효 JSON 두 개 연속 → 첫 번째만 취득
    data, junk = llm._parse_json(clean + clean)
    assert data == obj and junk == len(clean)

    # 앞 공백은 무시하고 파싱
    data, junk = llm._parse_json("  \n" + clean + "x")
    assert data == obj and junk == 1

    # 앞부분부터 깨진 응답: 기존대로 JSONDecodeError 전파(링크 전환 경로)
    for bad in ("모델이 설명부터 시작", '{"a": ', ""):
        try:
            llm._parse_json(bad)
            raise AssertionError(f"should raise: {bad!r}")
        except json.JSONDecodeError:
            pass

    # ── 기본 체인 순서: 대용량 lite 1순위, 20/일 모델들은 폴백 ────────────────
    links = llm.DEFAULT_CHAIN.split(",")
    assert links[0] == "gemini:gemini-3.1-flash-lite", links[0]
    assert links[1:3] == ["gemini:gemini-3.5-flash", "gemini:gemini-3-flash-preview"]
    assert len(links) == 5

    print("ALL PASS (llm lenient parse + chain order)")


if __name__ == "__main__":
    main()
