"""장 시간이 밀린 날(수능일·새해 첫 거래일) — Live Trader·Live PM 시각 상수를 그날만 늦춘다(2026-11-19 사용자 결정)."""
import os
import tempfile
import unittest
from unittest import mock

from swing import history, session_hours as SH, store
from swing.livepm import config as PC
from swing.tests.test_vwap_exec import ledger_with, path, run
from swing.vwap import config as VC
from swing.vwap.executor import Executor

CSAT = "2026-11-19"


class TestShift(unittest.TestCase):
    def test_delays_and_shift(self):
        self.assertEqual(SH.delays("2026-11-18"), (0, 0))
        self.assertEqual(SH.delays(CSAT), (60, 60))
        self.assertEqual(SH.shift("09:05", CSAT), "10:05")
        self.assertEqual(SH.shift("15:19", CSAT), "16:19")
        self.assertEqual(SH.shift("15:19", "2026-11-18"), "15:19")
        self.assertEqual(SH.shift("09:00", "2027-01-04"), "10:00")          # 새해 첫 거래일 — 개장만 늦춤
        self.assertEqual(SH.shift("15:30", "2027-01-04"), "15:30")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"SWING_SESSION_SHIFTS": "2027-11-18:60:60, bad, 2026-11-19:30:0"}):
            self.assertEqual(SH.delays("2027-11-18"), (60, 60))
            self.assertEqual(SH.delays(CSAT), (30, 0))                         # 환경변수가 파일 값보다 우선
            self.assertEqual(VC.at("2027-11-18", "ENTRY_END"), "16:19")
        self.assertEqual(VC.at("2027-11-18", "ENTRY_END"), "15:19")             # 환경변수 없으면 평소(캐시가 섞이지 않음)

    def test_trader_and_pm_times(self):
        self.assertEqual([VC.at(CSAT, k) for k in ("SESSION_START", "ENTRY_START", "ENTRY_END", "SESSION_END", "COMPLETE_AT")],
                         ["10:00", "10:05", "16:19", "16:35", "16:30"])
        self.assertEqual([VC.at("2026-11-18", k) for k in ("ENTRY_START", "ENTRY_END", "SESSION_END")], ["09:05", "15:19", "15:35"])
        self.assertEqual(PC.at(CSAT, "SCHEDULE"), ("10:10", "12:00", "14:30", "15:50"))
        self.assertEqual([PC.at(CSAT, k) for k in ("SESSION_START", "DECIDE_START", "DECIDE_END", "SESSION_END")],
                         ["09:55", "10:00", "16:20", "16:30"])


class TestExecutorLateSession(unittest.TestCase):
    ROWS = [("10:00", 10000), ("10:05", 10000), ("15:40", 9900), ("16:10", 9890)]

    def fills_after_1520(self, date):
        ex = Executor(store.FileStore(tempfile.mkdtemp()), date, ledger_with(order={"validDate": date}), log=lambda *_: None)
        run(ex, path("000010", self.ROWS, date=date))
        return ex, [f["ts"][11:16] for f in ex.doc["orders"]["o1"]["fills"] if f["ts"][11:16] >= "15:20"]

    def test_csat_day_keeps_entering_until_1619(self):
        ex, late = self.fills_after_1520(CSAT)
        self.assertTrue(late)                                                  # 15:40·16:10 VWAP 분할 체결
        self.assertFalse(ex.doc["complete"])                                   # 16:10 은 아직 완주(16:30) 전

    def test_normal_day_stops_at_1519(self):
        _ex, late = self.fills_after_1520("2026-11-18")
        self.assertEqual(late, [])


class TestMinuteBars(unittest.TestCase):
    def test_csat_bars_reach_1630(self):
        ticks = [{"ts": f"{CSAT}T{hm}:00+09:00", "price": p} for hm, p in (("10:00", 100), ("15:45", 101), ("16:25", 102))]
        bars = history.minute_bars(ticks, 5)
        self.assertEqual([b["t"] for b in bars], ["10:00", "15:45", "16:25"])
        normal = [dict(t, ts=t["ts"].replace(CSAT, "2026-11-18")) for t in ticks]
        self.assertEqual([b["t"] for b in history.minute_bars(normal, 5)], ["10:00"])   # 평소엔 15:30 이후 버림


if __name__ == "__main__":
    unittest.main()
