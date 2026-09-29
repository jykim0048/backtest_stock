"""차트 지표가 스킬 indicators.compute_indicators(pandas)와 같은 값인지 — 애널리스트 인용 수치와 차트 일치."""
import math
import os
import random
import sys
import tempfile
import unittest

from swing import chart

KIT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent", "kit", "scripts")


def series(n=300, seed=7):
    rng, px, rows = random.Random(seed), 50_000.0, []
    for i in range(n):
        o = px * (1 + rng.uniform(-0.01, 0.01))
        c = o * (1 + rng.uniform(-0.03, 0.03))
        h, l = max(o, c) * (1 + rng.uniform(0, 0.01)), min(o, c) * (1 - rng.uniform(0, 0.01))
        rows.append({"date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", "open": o, "high": h, "low": l,
                     "close": c, "volume": rng.randint(1000, 9000)})
        px = c
    return rows


class TestChart(unittest.TestCase):
    def test_matches_kit_compute_indicators(self):
        try:
            import pandas as pd
        except ImportError:
            self.skipTest("pandas 없음")
        sys.path.insert(0, KIT)
        try:
            import indicators as kit
        finally:
            sys.path.remove(KIT)
        rows = series()
        df = pd.DataFrame({"Open": [r["open"] for r in rows], "High": [r["high"] for r in rows],
                           "Low": [r["low"] for r in rows], "Close": [r["close"] for r in rows],
                           "Volume": [r["volume"] for r in rows]})
        ref = kit.compute_indicators(df)
        mine = chart.indicators([r["close"] for r in rows])
        pairs = {"ema10": "close_10_ema", "sma50": "close_50_sma", "sma200": "close_200_sma", "bm": "boll",
                 "bu": "boll_ub", "bl": "boll_lb", "rsi": "rsi", "macd": "macd", "macds": "macds", "macdh": "macdh"}
        for k, col in pairs.items():
            for i, (a, b) in enumerate(zip(mine[k], ref[col].tolist())):
                if b is None or (isinstance(b, float) and math.isnan(b)):
                    self.assertIsNone(a, f"{k}[{i}] 는 None 이어야 함")
                else:
                    self.assertIsNotNone(a, f"{k}[{i}]")
                    self.assertAlmostEqual(a, b, delta=1e-6 * max(1.0, abs(b)), msg=f"{k}[{i}]")

    def test_build_tail_and_csv(self):
        rows = series(120)
        c = chart.build(rows, n=90)
        self.assertEqual(len(c["dates"]), 90)
        self.assertTrue(all(len(c[k]) == 90 for k in ("o", "h", "l", "c", "v") + chart.SERIES))
        self.assertIsNone(c["sma200"][-1])                    # 120행 → SMA200 워밍업 부족
        self.assertIsNotNone(c["sma50"][-1])
        p = os.path.join(tempfile.mkdtemp(), "ohlcv.csv")
        with open(p, "w", encoding="utf-8") as f:
            f.write("Date,Open,High,Low,Close,Volume\n")
            for r in rows:
                f.write(f"{r['date']},{r['open']},{r['high']},{r['low']},{r['close']},{r['volume']}\n")
        self.assertEqual(chart.from_csv(p)["c"], c["c"])
        self.assertIsNone(chart.build([]))


if __name__ == "__main__":
    unittest.main()
