"""촉매 상장 유니버스 게이트 회귀 — 해외 종목(키옥시아·HP) KOSPI 오분류 차단 (오프라인).

실행: python tests/test_catalyst_listed_gate.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import generate_intraday_briefing as g
import build_dart_corp_map as b


def main():
    # ── 정규화 규칙: 빌드 스크립트와 소비자가 반드시 동일해야 함 ──────────────
    for s in ("삼성전자", "SK 하이닉스", "DL이앤씨", "(주)한화", "포스코 주식회사",
              "HD현대·중공업", "hp", "키옥시아"):
        assert g._norm_listed_name(s) == b._norm_listed_name(s), s
    assert g._norm_listed_name("(주)SK 하이닉스") == "SK하이닉스"
    assert g._norm_listed_name("삼성전자주식회사") == "삼성전자"
    assert g._norm_listed_name("hp") == "HP"   # 대소문자 무시 매칭

    names = {g._norm_listed_name(k): v for k, v in {
        "삼성전자": "005930", "SK하이닉스": "000660", "DL이앤씨": "375500",
        "한화": "000880",
    }.items()}

    # ── 상장사: 표기 변형(공백·(주)·중점·소문자) 포함 매칭 ───────────────────
    assert g._listed_code("삼성전자", names) == "005930"
    assert g._listed_code("SK 하이닉스", names) == "000660"
    assert g._listed_code("(주)한화", names) == "000880"
    assert g._listed_code("sk하이닉스", names) == "000660"
    # 우선주 표기는 본주로 매칭
    assert g._listed_code("삼성전자우", names) == "005930"
    assert g._listed_code("삼성전자우B", names) == "005930"

    # ── 해외 종목: 미매칭 → None (드롭 대상) — 2026-08-27 실사례 ─────────────
    for foreign in ("키옥시아", "HP", "엔비디아", "TSMC", "테슬라"):
        assert g._listed_code(foreign, names) is None, foreign
    # 빈/이상 입력 내성
    assert g._listed_code("", names) is None
    assert g._listed_code(None, names) is None
    assert g._listed_code("우", names) is None   # '우' 제거 후 빈 문자열이어도 안전

    # ── 로더 fail-open: 파일 부재 시 빈 dict (검증 생략 경로) ─────────────────
    orig = g.LISTED_NAMES_PATH
    try:
        g.LISTED_NAMES_PATH = os.path.join(ROOT, "no_such_dir", "nope.json")
        assert g._load_listed_names() == {}
    finally:
        g.LISTED_NAMES_PATH = orig

    print("ALL PASS (catalyst listed-universe gate)")


if __name__ == "__main__":
    main()
