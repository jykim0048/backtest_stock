"""하방 촉매 리플레이(2026-10-15) — 오프라인: 아카이브·CHARTDAY 주입."""
import unittest

from swing.tests.test_catalysts import arch
from swing.tools import replay_catalyst as R


class FakeDbq:
    def __init__(self):
        self.calls = 0

    def chart_day(self, code, d1, d2):
        self.calls += 1
        # 10-13 종가 100 → 10-14 95(진입) → 10-15 90 → 10-16 92 → 10-17 88 → 10-20 85 → 10-21 80
        px = {"20261013": 100, "20261014": 95, "20261015": 90, "20261016": 92, "20261017": 88, "20261020": 85, "20261021": 80}
        if code == "412540":
            px = {k: v * 2 for k, v in px.items()}
        return [{"Date": d, "Prpr": str(v)} for d, v in px.items()]


class TestReplay(unittest.TestCase):
    def test_forward_returns(self):
        ds = ["2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-20"]
        cs = [100, 95, 90, 92, 88, 85]
        r = R.forward_returns(ds, cs, "2026-10-13")
        self.assertEqual(r["entryDate"], "2026-10-14")
        self.assertAlmostEqual(r["ret1"], round((90 / 95 - 1) * 100, 2))
        self.assertAlmostEqual(r["ret3"], round((88 / 95 - 1) * 100, 2))
        self.assertAlmostEqual(r["ret5"], round((85 / 95 - 1) * 100, 2)) if len(ds) > 6 else self.assertIsNone(r["ret5"])
        self.assertIsNone(R.forward_returns(ds, cs, "2026-10-20")["entryDate"])

    def test_replay_end_to_end(self):
        dbq = FakeDbq()
        res = R.replay("2026-10-13", "2026-10-14", fetch=lambda d: arch() if d == "2026-10-13" else None, dbq=dbq, min_strength=4, max_drop=20)
        self.assertEqual(res["archives"], 1)
        self.assertEqual(res["missing"], ["2026-10-14"])
        codes = [r["code"] for r in res["rows"]]
        self.assertEqual(sorted(codes), ["336260", "412540"])
        r = next(x for x in res["rows"] if x["code"] == "336260")
        self.assertEqual(r["entryDate"], "2026-10-14")
        self.assertLess(r["ret1"], 0)
        self.assertEqual(dbq.calls, 2)                               # 종목당 1콜
        s = res["summary"]
        self.assertEqual(s["all"]["n"], 2)
        self.assertEqual(s["all"]["hit1"], 100.0)
        self.assertGreater(s["all"]["short1"], 0)
        self.assertIn("strength4", s)
        self.assertIn("rule:dilution", s)
        # dbq 없이도 후보 집계는 된다(수익률 None)
        res2 = R.replay("2026-10-13", "2026-10-13", fetch=lambda d: arch(), dbq=None, min_strength=2)
        self.assertEqual(len(res2["rows"]), 3)
        self.assertIsNone(res2["rows"][0]["ret1"])
        self.assertIsNone(res2["summary"]["all"]["hit1"])


if __name__ == "__main__":
    unittest.main()
