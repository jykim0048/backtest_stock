"""스크리너 종목명 폴백 회귀 — krx_companies.json 미등재(신규 상장·영숫자 코드) 종목명이 코드로 찍히던 문제 (오프라인).

2026-11-09 한화머시너리앤서비스홀딩스(0220W0)가 종목 선정·일일 리포트에 "0220W0" 으로 표기됨.
실행: python tests/test_screener_names.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import screener as s


def main():
    tmp = tempfile.mkdtemp()
    comp = os.path.join(tmp, "krx_companies.json")
    cons = os.path.join(tmp, "index_constituents.json")
    with open(comp, "w", encoding="utf-8") as f:
        json.dump([
            {"name": "삼성전자", "code": "005930", "market": "유가증권", "ticker": "005930.KS"},
            {"name": "", "code": "123456", "market": "코스닥", "ticker": "123456.KQ"},
        ], f, ensure_ascii=False)
    with open(cons, "w", encoding="utf-8") as f:
        json.dump({"KOSPI200": ["005930", "0220W0"], "KOSDAQ150": ["123456"],
                   "names": {"005930": "삼성전자(현재명)", "0220W0": "한화머시너리앤서비스홀딩스",
                             "123456": "빈이름보완", "999999": ""}}, f, ensure_ascii=False)

    orig = (s.KRX_COMPANIES, s.CONSTITUENTS)
    try:
        s.KRX_COMPANIES, s.CONSTITUENTS = comp, cons
        m = s._krx_master_map()
        # 미등재 영숫자 코드 → 구성종목 현재명, 시장은 None(SSOT 보정 안 함)
        assert m["0220W0"] == {"name": "한화머시너리앤서비스홀딩스", "market": None}, m.get("0220W0")
        # 기존 등재 종목의 이름·시장은 그대로
        assert m["005930"] == {"name": "삼성전자", "market": "KOSPI"}
        # 등재됐지만 이름이 빈 종목만 채움
        assert m["123456"] == {"name": "빈이름보완", "market": "KOSDAQ"}
        assert "999999" not in m                      # 빈 이름은 무시
        assert s._krx_name_map()["0220W0"] == "한화머시너리앤서비스홀딩스"

        # 마스터 파일이 없어도 구성종목 이름은 남는다
        s.KRX_COMPANIES = os.path.join(tmp, "nope.json")
        assert s._krx_name_map().get("0220W0") == "한화머시너리앤서비스홀딩스"
        # 둘 다 없으면 빈 dict (기존 '코드로 대체' 경로)
        s.CONSTITUENTS = os.path.join(tmp, "nope2.json")
        assert s._krx_master_map() == {}
    finally:
        s.KRX_COMPANIES, s.CONSTITUENTS = orig

    print("ALL PASS (screener name fallback)")


if __name__ == "__main__":
    main()
