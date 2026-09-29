import unittest

from swing.tools import seed_demo_short


class TestDemoShort(unittest.TestCase):
    def test_dates_are_trading_days_and_hold_days_match(self):
        """데모 Short 체결·청산일이 휴장일(주말·추석)이 아니고 보유일이 영업일 수와 맞는지."""
        self.assertEqual(seed_demo_short.check_dates(), [])


if __name__ == "__main__":
    unittest.main()
