import json
import os
import unittest

from swing import signals as S

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def wb():
    return {"asof": "2026-09-28 16:13 KST",
            "sectorFlow": {"rows": [{"name": "화학", "signal": "동반강세"},
                                    {"name": "운송·창고", "signal": "동반약세"},
                                    {"name": "IT 서비스", "signal": "수급유입"}]},
            "sectorScreen": {"matrix": {
                "동반강세": [{"code": "096770", "name": "SK이노베이션", "sector": "화학", "score": 2}],
                "수급유입": [{"code": "035420", "name": "NAVER", "sector": "IT 서비스", "score": 2},
                          {"code": "096770", "name": "dup", "sector": "화학"}],
                "수급이탈": [],
                "동반약세": [{"code": "028260", "name": "삼성물산", "sector": "유통", "score": -2}]}}}


class TestSignals(unittest.TestCase):
    def test_candidates_order_dedupe_exclude(self):
        c = S.buy_candidates(wb())
        self.assertEqual([x["code"] for x in c], ["096770", "035420"])
        self.assertEqual(c[0]["signal"], "동반강세")
        self.assertEqual([x["code"] for x in S.buy_candidates(wb(), {"096770"})], ["035420"])

    def test_review_triggers(self):
        pos = [{"code": "000001", "name": "a", "sector": "운송 · 창고"},     # 섹터 전환
               {"code": "028260", "name": "b", "sector": "화학"},           # 종목 하방 칸
               {"code": "000003", "name": "c", "sector": "화학"},           # 해당 없음
               {"code": "000004", "name": "d", "sector": "운송·창고",
                "sellPending": {"reason": "x"}}]                           # 이미 매도 예약
        t = {x["code"]: x for x in S.review_triggers(pos, wb())}
        self.assertEqual(set(t), {"000001", "028260"})
        self.assertEqual(t["000001"]["signal"], "동반약세")
        self.assertIn("종목 동반약세 칸 등재", t["028260"]["reasons"])

    def test_asof(self):
        self.assertEqual(S.asof_date(wb()), "2026-09-28")
        self.assertIsNone(S.asof_date({}))

    def test_real_archive(self):
        # main 아카이브는 이 브랜치에 없음 → git 이력에서 복원한 축약본(swing/data/wb_history)으로 검증
        p = os.path.join(ROOT, "swing", "data", "wb_history", "2026-09-14.json")
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        c = S.buy_candidates(d)
        self.assertTrue(c and all(len(x["code"]) == 6 for x in c))
        self.assertTrue(all(x["signal"] in ("동반강세", "수급유입") for x in c))
        self.assertEqual(S.buy_candidates(S.compact(d)), c)          # 축약 재적용 불변
        self.assertEqual(S.sector_signals(S.compact(d)), S.sector_signals(d))


if __name__ == "__main__":
    unittest.main()
