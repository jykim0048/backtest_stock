import unittest

from swing import agent_iface as A

TRADER = """**Action**: Buy

**Reasoning**: 근거.

**Entry Price**: 12,300원
**Stop Loss**: 11,500 원
**Position Sizing**: 포트폴리오의 7%

FINAL TRANSACTION PROPOSAL: **BUY**"""

PM = """**Rating**: Overweight

**Executive Summary**: 분할 진입.

**Investment Thesis**: ...

**Price Target**: 13,800원
**Time Horizon**: 1~2주"""


def req(purpose="entry"):
    return A.DecisionRequest(code="005930", name="삼성전자", date="2026-09-28",
                             purpose=purpose, last_close=12400)


class TestParse(unittest.TestCase):
    def test_markdown(self):
        d = A.decision_from_markdown(req(), TRADER, PM)
        self.assertEqual((d.rating, d.action, d.entry, d.stop, d.target, d.weight),
                         ("Overweight", "Buy", 12300, 11500, 13800, 0.07))
        self.assertEqual(A.entry_verdict(d), (True, ""))

    def test_price_rules(self):
        self.assertIsNone(A.parse_price("26만~27만원"))
        self.assertIsNone(A.parse_price("5% 아래"))
        self.assertEqual(A.parse_price("약 1,234.5 원"), 1234.5)

    def test_missing_rating_is_review(self):
        d = A.decision_from_markdown(req(), TRADER, "등급 없음")
        self.assertEqual(d.rating, "REVIEW")
        self.assertFalse(A.entry_verdict(d)[0])

    def test_verdicts(self):
        d = A.decision_from_markdown(req(), TRADER.replace("11,500", "12,500"), PM)
        self.assertEqual(A.entry_verdict(d), (False, "손절가 ≥ 진입가"))
        d = A.decision_from_markdown(req(), TRADER.replace("Action**: Buy", "Action**: Hold"), PM)
        self.assertEqual(A.entry_verdict(d)[1], "Trader Hold")
        d = A.Decision(code="1", date="d", purpose="review", rating="Underweight")
        self.assertTrue(A.sell_verdict(d))
        d.error = "x"
        self.assertFalse(A.sell_verdict(d))

    def test_weight_and_target(self):
        d = A.Decision(code="1", date="d", purpose="entry", entry=100, target=90, weight=0.3)
        self.assertEqual(A.effective_weight(d), 0.10)
        self.assertIsNone(A.effective_target(d))
        d.weight = None
        self.assertEqual(A.effective_weight(d), 0.05)


if __name__ == "__main__":
    unittest.main()
