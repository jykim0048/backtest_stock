"""딥리서치 공시 표시(catalyst_keywords·tone) — sources_kr.disclosure_flags (2026-11-10 main 키워드 동기화 + 주식소각). 오프라인."""
import importlib.util
import os
import sys
import unittest

SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent", "kit", "scripts")


def load():
    if SCRIPTS not in sys.path:
        sys.path.insert(0, SCRIPTS)
    spec = importlib.util.spec_from_file_location("sources_kr_under_test", os.path.join(SCRIPTS, "sources_kr.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestDisclosureFlags(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.S = load()
        except ImportError as ex:                              # requests 등 수집 의존성이 없는 환경
            raise unittest.SkipTest(f"sources_kr import 불가: {ex}")

    def test_keywords_match_main_list(self):
        kw = self.S.DART_CATALYST_KEYWORDS
        for k in ("교환사채", "영구", "신종자본증권", "감자", "관리종목", "불성실공시", "매매거래정지", "횡령", "배임",
                  "감사의견", "상장폐지", "담보제공", "잠정실적", "영업(잠정)실적", "공개매수", "소각"):
            self.assertIn(k, kw)
        self.assertEqual(len(kw), len(set(kw)))
        self.assertEqual(len(kw), 40)                          # main analysis/sources.DART_CATALYST_KEYWORDS 와 같은 개수

    def test_stock_cancellation_is_flagged_bull(self):
        f = self.S.disclosure_flags
        self.assertEqual(f("주식소각결정"), (["소각"], "bull"))
        self.assertEqual(f("주식 소각 결정"), (["소각"], "bull"))
        self.assertEqual(f("[기재정정]주식소각결정"), (["소각"], "correction"))
        self.assertEqual(f("전환사채발행후만기전사채취득(소각)")[0], ["전환사채", "소각"])      # 사채 소각 — 표시는 되고
        self.assertEqual(f("전환사채발행후만기전사채취득(소각)")[1], "bear")                    # 방향은 종전대로(전환사채)

    def test_tone_table(self):
        f = self.S.disclosure_flags
        self.assertEqual(f("주요사항보고서(자기주식취득결정)"), (["자기주식"], "bull"))
        self.assertEqual(f("주요사항보고서(자기주식처분결정)"), (["자기주식"], "bear"))           # 종전엔 bull 로 표시되던 것
        self.assertEqual(f("공개매수신고서"), (["공개매수"], "neutral"))
        self.assertEqual(f("감자결정"), (["감자"], "bear"))
        self.assertEqual(f("관리종목지정"), (["관리종목"], "bear"))
        self.assertEqual(f("교환사채권발행결정"), (["교환사채"], "bear"))
        self.assertEqual(f("단일판매ㆍ공급계약체결"), (["공급계약", "단일판매"], "bull"))
        self.assertEqual(f("분기보고서 (2026.09)"), ([], "neutral"))
        self.assertEqual(f(None), ([], "neutral"))


if __name__ == "__main__":
    unittest.main()
