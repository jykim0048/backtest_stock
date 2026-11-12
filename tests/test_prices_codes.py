"""/api/prices?codes= 티커 결정(ticker_utils.codes_to_tickers) — 오프라인."""
import unittest

from ticker_utils import codes_to_tickers

KRX = {"036930": "036930.KQ", "093370": "093370.KS", "240810": "240810.KQ"}


class TestCodesToTickers(unittest.TestCase):
    def test_bare_codes_use_krx_master(self):
        self.assertEqual(codes_to_tickers("036930,093370,240810", KRX),
                         {"036930": "036930.KQ", "093370": "093370.KS", "240810": "240810.KQ"})

    def test_explicit_suffix_wins_and_unknown_falls_back_to_ks(self):
        self.assertEqual(codes_to_tickers("036930.KS, 999999 ,,1234", KRX),
                         {"036930": "036930.KS", "999999": "999999.KS", "001234": "001234.KS"})

    def test_empty(self):
        self.assertEqual(codes_to_tickers("", KRX), {})
        self.assertEqual(codes_to_tickers(None, KRX), {})

    def test_real_master_has_kosdaq_suffix(self):
        t = codes_to_tickers("036930,005930")                      # public/assets/krx_companies.json
        self.assertEqual((t["036930"], t["005930"]), ("036930.KQ", "005930.KS"))


if __name__ == "__main__":
    unittest.main()
