"""railway_server KRX 마스터 TTL 재로드(_refresh_master) — 오프라인(raw fetch 를 가짜로)."""
import json
import time
import unittest

import railway_server as rs

ROWS = [{"name": f"종목{i}", "code": f"{i:06d}", "market": "코스닥", "ticker": f"{i:06d}.KQ"}
        for i in range(2100)] + [
    {"name": "한화머시너리앤서비스홀딩스", "code": "0220W0", "market": "유가", "ticker": "0220W0.KS"}]


class TestMasterReload(unittest.TestCase):
    def setUp(self):
        self.saved = (rs._BY_CODE, rs._BY_NAME, rs._TICKERS, dict(rs._MASTER_STATE), rs._raw_fetch)

    def tearDown(self):
        rs._BY_CODE, rs._BY_NAME, rs._TICKERS, st, rs._raw_fetch = self.saved
        rs._MASTER_STATE.clear()
        rs._MASTER_STATE.update(st)

    def _expire(self):
        rs._MASTER_STATE["ts"] = time.time() - rs.KRX_MASTER_TTL - 1

    def test_reload_after_ttl_picks_up_new_listing(self):
        rs._raw_fetch = lambda rel: json.dumps(ROWS, ensure_ascii=False).encode("utf-8")
        self._expire()
        self.assertEqual(rs.resolve("0220W0")["name"], "한화머시너리앤서비스홀딩스")
        self.assertEqual(rs.resolve("0220w0")["market"], "KOSPI")
        self.assertEqual(rs._TICKERS["0220W0"], "0220W0.KS")
        self.assertEqual(rs._MASTER_STATE["src"], "raw")

    def test_within_ttl_no_fetch(self):
        calls = []
        rs._raw_fetch = lambda rel: calls.append(rel)
        rs._MASTER_STATE["ts"] = time.time()
        rs.resolve("005930")
        self.assertEqual(calls, [])

    def test_bad_or_short_body_keeps_current(self):
        before = rs._BY_CODE
        for body in (None, b"[]", json.dumps(ROWS[:10]).encode("utf-8"), b"{not json"):
            rs._raw_fetch = lambda rel, body=body: body
            self._expire()
            rs._refresh_master()
            self.assertIs(rs._BY_CODE, before)


if __name__ == "__main__":
    unittest.main()
