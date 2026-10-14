"""유니버스 독립 후보 소스 + 공통 모멘텀 채점(2026-10-13) — daily.run_day 통합(오프라인 mock)."""
import tempfile
import unittest

from swing import agent_iface as A
from swing import config, daily, prices, store, universe as U, universe_mock as M

DATE = "2026-10-12"          # 월(영업일)
SECS = ("운송·창고", "통신", "보험", "제조", "IT 서비스")


def wb():
    return {"asof": f"{DATE} 16:13 KST",
            "sectorFlow": {"rows": [{"name": "운송·창고", "signal": "동반강세", "chgPct": 2.3},
                                    {"name": "통신", "signal": "동반약세", "chgPct": -3.8},
                                    {"name": "보험", "signal": "혼조"}, {"name": "제조", "signal": "동반약세"},
                                    {"name": "IT 서비스", "signal": "수급유입", "chgPct": -1.4}]},
            "sectorScreen": {"matrix": {
                "동반강세": [{"code": "011200", "name": "HMM", "sector": "운송·창고", "score": 2, "reason": "외국인 +202억"},
                          {"code": "003490", "name": "대한항공", "sector": "운송·창고", "score": 1, "reason": "기관"}],
                "수급유입": [], "수급이탈": [],
                "동반약세": [{"code": "017670", "name": "SK텔레콤", "sector": "통신", "score": -2, "reason": "순매도"}]}}}


class AnyAgent:
    """entry: Long 은 Buy(종가 진입·−5% 손절), Short 은 Sell(종가 진입·+5% 손절). 요청을 기록한다."""
    name = "any"

    def __init__(self):
        self.reqs = []

    def decide(self, req):
        self.reqs.append(req)
        d = A.Decision(code=req.code, date=req.date, purpose=req.purpose, side=req.side, agent=self.name)
        if req.purpose == "review":
            d.rating = "Hold"
            return d
        px = req.last_close or 10000
        if req.side == "short":
            d.rating, d.action, d.entry, d.stop, d.target = "Sell", "Sell", px, px * 1.05, px * 0.9
        else:
            d.rating, d.action, d.entry, d.stop, d.target = "Buy", "Buy", px, px * 0.95, px * 1.1
        d.weight = 0.05
        return d


def runner(st, **kw):
    secs = [s for s in M.load_sectors() if s["name"] in SECS]
    caps = {x["code"]: x["cap"] for s in secs for x in s["stocks"]}
    return U.UniverseRunner(st, M.MockDbQuote(secs), M.MockHubFlow(), caps_loader=lambda d: caps, **kw)


class TestCandidates(unittest.TestCase):
    def test_universe_candidates_rules(self):
        st = store.FileStore(tempfile.mkdtemp())
        doc = runner(st).build_or_load(DATE, wb(), None)
        self.assertTrue(doc["rankLong"] and doc["rankShort"])
        self.assertEqual([x["rankAll"] for x in doc["rankLong"][:3]], [1, 2, 3])
        self.assertEqual([x["score"] for x in doc["rankLong"]], sorted([x["score"] for x in doc["rankLong"]], reverse=True))
        # rank 모드: 통합 순위를 위에서부터(제외 없음이면 동일 순서)
        ranked = U.universe_candidates(doc, "long", mode="rank")
        self.assertEqual([c["code"] for c in ranked], [x["code"] for x in doc["rankLong"] if x["score"] >= config.UNI_CAND_MIN])
        self.assertIn("전 업종 통합 롱 모멘텀 1위", ranked[0]["reason"])
        old = dict(doc); old.pop("rankLong"); old.pop("rankShort")
        self.assertEqual([c["code"] for c in U.universe_candidates(old, "long", mode="rank")], [c["code"] for c in ranked])
        # 기본(roundrobin, 2026-10-14 결정): 첫 바퀴는 상방 업종마다 1개씩 — 앞 k개가 서로 다른 업종, 바퀴 수 기록, 통합 순위 병기
        longs = U.universe_candidates(doc, "long")
        self.assertTrue(longs)
        self.assertEqual(config.UNI_PICK_MODE, "roundrobin")
        n_sec = len({s["name"] for s in doc["sectors"] if s["dir"] == "long" and any(x["score"] >= config.UNI_CAND_MIN for x in s["top"])})
        first = longs[:n_sec]
        self.assertEqual(len({c["sector"] for c in first}), n_sec)
        self.assertTrue(all(c["pickMode"] == "roundrobin" for c in longs))
        self.assertIn("업종 라운드로빈 1번째(1바퀴", longs[0]["reason"])
        self.assertIsNotNone(longs[0]["rankAll"])
        self.assertEqual({c["code"] for c in longs}, {c["code"] for c in ranked})        # 집합은 같고 순서만 다름
        # sectorcap 모드: 업종당 ≤ 2
        from collections import Counter
        sc = U.universe_candidates(doc, "long", mode="sectorcap")
        self.assertTrue(all(v <= config.UNI_SECTOR_CAP for v in Counter(c["sector"] for c in sc).values()))
        self.assertTrue(all(c["source"] == "universe" and c["score"] >= config.UNI_CAND_MIN for c in longs))
        self.assertEqual([c["score"] for c in ranked], sorted([c["score"] for c in ranked], reverse=True))   # rank 모드만 점수순
        self.assertTrue(all(c["signal"] in ("동반강세", "수급유입") for c in longs))
        top = longs[0]["code"]
        self.assertNotIn(top, [c["code"] for c in U.universe_candidates(doc, "long", exclude={top})])
        self.assertNotIn(top, [c["code"] for c in U.universe_candidates(doc, "long", decided={top: DATE})])
        shorts = U.universe_candidates(doc, "short")
        self.assertTrue(all(c["signal"] == "동반약세" and c["sector"] == "통신" for c in shorts))   # 제조(대분류)는 제외
        self.assertTrue(all(c["metrics"]["hasStockFuture"] for c in U.universe_candidates(doc, "short", fut_only=True)))
        self.assertIn("모멘텀", longs[0]["reason"])

    def test_attach_scores_same_scale(self):
        st = store.FileStore(tempfile.mkdtemp())
        r = runner(st)
        cands = [{"code": "003490", "name": "대한항공", "sector": "운송·창고", "signal": "동반강세", "score": 1, "reason": "기관"},
                 {"code": "011200", "name": "HMM", "sector": "운송·창고", "signal": "동반강세", "score": 2, "reason": "외국인"}]
        r.attach_scores(cands, "long", DATE)
        for c in cands:
            self.assertEqual(c["source"], "matrix")
            self.assertIn(c["matrixScore"], (1, 2))
            self.assertIsNotNone(c["score"])
            self.assertTrue(-5 <= c["score"] <= 5)
            self.assertIn("tvMom", c["metrics"])
            self.assertIn("모멘텀", c["reason"])
        self.assertGreaterEqual(cands[0]["score"], cands[1]["score"])         # 점수순 정렬
        # 유니버스 Top 의 같은 종목과 점수가 일치(같은 지표·같은 함수)
        doc = r.build_or_load(DATE, wb(), None)
        tops = {x["code"]: x for s in doc["sectors"] for x in s["top"]}
        for c in cands:
            if c["code"] in tops:
                self.assertEqual(c["score"], tops[c["code"]]["score"])


class TestRunDay(unittest.TestCase):
    def test_run_day_two_sources(self):
        st = store.FileStore(tempfile.mkdtemp())
        ag, px = AnyAgent(), prices.MockPrices()
        run = daily.run_day(DATE, st, ag, px, wb(), uni=runner(st))
        self.assertIsNotNone(st.get(f"universe/{DATE}"))
        cands, scands = run["candidates"], run["shortCandidates"]
        mx = [c for c in cands if c["source"] == "matrix"]
        un = [c for c in cands if c["source"] == "universe"]
        self.assertEqual({c["code"] for c in mx}, {"011200", "003490"})
        self.assertTrue(un)
        self.assertLessEqual(len(un), config.UNI_NEW_PER_DAY)
        self.assertFalse({c["code"] for c in un} & {c["code"] for c in mx})           # 매트릭스 종목은 유니버스에서 제외
        self.assertTrue(all(c["score"] is not None for c in mx + un))                # 공통 점수
        self.assertEqual([c["score"] for c in mx], sorted([c["score"] for c in mx], reverse=True))
        self.assertTrue(all(c["ordered"] for c in mx + un))
        sun = [c for c in scands if c["source"] == "universe"]
        self.assertTrue(sun)
        self.assertLessEqual(len(sun), config.UNI_NEW_SHORT_PER_DAY)
        self.assertEqual({c["sector"] for c in sun}, {"통신"})
        # 판단 요청에 소스·점수·지표가 전달됨
        self.assertTrue(all(c["source"] == "universe" for c in un))
        req = next(r for r in ag.reqs if r.code == un[0]["code"])
        self.assertEqual(req.source, "universe")
        self.assertIsNotNone(req.score)
        self.assertIn("tvMom", req.metrics)
        self.assertEqual(next(r for r in ag.reqs if r.code == "011200").source, "matrix")
        # 주문에 source
        L = st.get("ledger")
        srcs = {o["code"]: o.get("source") for o in L["orders"]}
        self.assertEqual(srcs["011200"], "matrix")
        self.assertEqual(srcs[un[0]["code"]], "universe")
        self.assertIn("유니버스 후보", " ".join(run["notes"]))
        # 다음 날: 체결된 포지션에 source 전파 · 어제 분석 종목은 쿨다운으로 유니버스 후보에서 제외
        nxt = "2026-10-13"
        wb2 = dict(wb(), asof=f"{nxt} 16:13 KST")
        ag2 = AnyAgent()
        run2 = daily.run_day(nxt, st, ag2, px, wb2, uni=runner(st))
        L2 = st.get("ledger")
        self.assertTrue(any(p.get("source") == "universe" for p in L2["positions"]))
        un2 = {c["code"] for c in run2["candidates"] + run2["shortCandidates"] if c["source"] == "universe"}
        self.assertFalse(un2 & ({c["code"] for c in un} | {c["code"] for c in sun}))

    def test_universe_failure_keeps_matrix_flow(self):
        st = store.FileStore(tempfile.mkdtemp())

        class Boom(U.UniverseRunner):
            def build_or_load(self, *a):
                raise RuntimeError("DB 토큰 없음")

            def attach_scores(self, cands, side, date):
                raise RuntimeError("허브 다운")
        run = daily.run_day(DATE, st, AnyAgent(), prices.MockPrices(), wb(), uni=Boom(st))
        self.assertEqual({c["code"] for c in run["candidates"]}, {"011200", "003490"})
        self.assertTrue(all(c["source"] == "matrix" and c["score"] is None for c in run["candidates"]))
        self.assertTrue(any("유니버스 산출 실패" in n for n in run["notes"]))
        self.assertTrue(any("채점 실패" in n for n in run["notes"]))

    def test_no_runner_unchanged(self):
        st = store.FileStore(tempfile.mkdtemp())
        run = daily.run_day(DATE, st, AnyAgent(), prices.MockPrices(), wb())
        self.assertEqual({c["source"] for c in run["candidates"]}, {"matrix"})
        self.assertIsNone(st.get(f"universe/{DATE}"))


if __name__ == "__main__":
    unittest.main()
