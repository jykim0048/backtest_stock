"""DART 공시 제목 규칙 방향 태그·촉매 백스톱 회귀(오프라인, 2026-10-15).

실행: python tests/test_dart_rule_direction.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from analysis import sources as S


def main():
    bear = ["주요사항보고서(교환사채권발행결정)", "주요사항보고서(신주인수권부사채권 발행결정)", "유상증자결정",
            "[기재정정]주요사항보고서(전환사채권발행결정)", "감자결정", "관리종목지정", "불성실공시법인지정",
            "매매거래정지", "횡령ㆍ배임혐의발생", "감사의견거절", "회생절차개시신청", "영구전환사채 발행",
            "신종자본증권 발행결정"]
    for t in bear:
        d, k = S.dart_rule_direction(t)
        assert d == "bearish", (t, d)
        assert k in ("dilution", "distress"), (t, k)
    bull = ["단일판매ㆍ공급계약체결", "주요사항보고서(자기주식취득결정)", "무상증자결정", "품목허가 획득", "특허권취득"]
    for t in bull:
        assert S.dart_rule_direction(t)[0] == "bullish", t
    # 해지·철회·취소는 규칙 적용 안 함(LLM 판단)
    assert S.dart_rule_direction("단일판매ㆍ공급계약해지") == (None, None)
    assert S.dart_rule_direction("유상증자결정 철회") == (None, None)
    assert S.dart_rule_direction("조회공시요구(현저한시황변동)") == (None, None)
    assert S.dart_rule_direction("") == (None, None)
    # 키워드 필터에 교환사채·감자·관리종목 포함
    for k in ("교환사채", "감자", "관리종목", "영구", "신종자본증권"):
        assert k in S.DART_CATALYST_KEYWORDS, k
        assert any(k in x for x in S.DART_PRIORITY_KEYWORDS), k
    print("OK test_dart_rule_direction")


if __name__ == "__main__":
    main()
