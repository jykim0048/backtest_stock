"""하루 판단 총량 배분(daily.judge_budget, 2026-11-06) — Gemini 일일 한도 안에서 끝나게.
python -m unittest discover -s swing/tests -t ."""
import unittest

from swing.daily import judge_budget


def c(prefix, n):
    return [{"code": f"{prefix}{i}"} for i in range(n)]


class TestJudgeBudget(unittest.TestCase):
    def test_half_split_and_order(self):
        """11/5 재현 — Long 매트릭스 6 + 유니버스 24, Short 매트릭스 3 + 유니버스 5 + 촉매 2, 신규 30건."""
        (m, u), (sm, su, sc), ol, os_ = judge_budget(30, [c("m", 6), c("u", 24)], [c("sm", 3), c("su", 5), c("sc", 2)])
        self.assertEqual((len(m), len(u)), (6, 14))                        # Long 20 = 30 − Short 10(전부)
        self.assertEqual((len(sm), len(su), len(sc)), (3, 5, 2))
        self.assertEqual([len(x) for x in ol], [0, 10])                     # 유니버스 뒤쪽 10개 이연
        self.assertIsNone(os_)

    def test_both_sides_over(self):
        (m, u), (sm, su, sc), ol, os_ = judge_budget(20, [c("m", 10), c("u", 15)], [c("sm", 10), c("su", 15), c("sc", 3)])
        self.assertEqual((len(m) + len(u), len(sm) + len(su) + len(sc)), (10, 10))
        self.assertEqual((len(m), len(u)), (10, 0))                         # 매트릭스 먼저
        self.assertEqual([len(x) for x in os_], [0, 15, 3])

    def test_under_budget_untouched(self):
        (m, u), (sm, su, sc), ol, os_ = judge_budget(30, [c("m", 3), c("u", 4)], [c("sm", 2), c("su", 1), []])
        self.assertEqual((len(m), len(u), len(sm), len(su), len(sc)), (3, 4, 2, 1, 0))
        self.assertIsNone(ol)
        self.assertIsNone(os_)


if __name__ == "__main__":
    unittest.main()
