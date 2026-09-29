import unittest

from swing.tools import seed_demo_short


class TestDemoShort(unittest.TestCase):
    def test_dates_are_trading_days_and_hold_days_match(self):
        """데모 Short 체결·청산일이 휴장일(주말·추석)이 아니고 보유일이 영업일 수와 맞는지."""
        self.assertEqual(seed_demo_short.check_dates(), [])

    def test_demo_decision_has_all_roles(self):
        """데모 Short 판단도 Long 과 같은 12개 역할, trader·pm 은 Sell·Underweight 로 파싱."""
        from swing import agent_iface as A
        d = seed_demo_short._demo_decision("028260", "삼성물산", "2026-09-23", 162000, 170100, 150700)
        self.assertEqual(tuple(d["reports"]), A.REPORT_KEYS)
        t, p = A.parse_trader(d["reports"]["trader"]), A.parse_pm(d["reports"]["pm"])
        self.assertEqual((t["action"], t["entry"], t["stop"], p["rating"], p["target"]),
                         ("Sell", 162000, 170100, "Underweight", 150700))


if __name__ == "__main__":
    unittest.main()
