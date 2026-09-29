"""P1 오프라인 테스트 — LLM·수집·yfinance 를 가짜로 바꿔 파이프라인과 peer B안 검증."""
import datetime
import json
import os
import tempfile
import unittest

from swing import agent_iface as A
from swing import store
from swing.agent import peers as P
from swing.agent import pipeline as PL

ROLE_FILES = sorted(f for f in os.listdir(PL.ROLES) if f[:2].isdigit() and not f.startswith("14"))
TITLES = {}
for _f in ROLE_FILES:
    with open(os.path.join(PL.ROLES, _f), encoding="utf-8") as _h:
        TITLES[_f] = _h.readline().strip()

TRADER_MD = """**Action**: Buy

**Reasoning**: ATR 1.5배 아래 손절.

**Entry Price**: 44,500원
**Stop Loss**: 42,800원
**Position Sizing**: 포트폴리오의 6%

FINAL TRANSACTION PROPOSAL: **BUY**"""
PM_MD = """**Rating**: Overweight

**Executive Summary**: 지정가 분할.

**Investment Thesis**: 수급 동반.

**Price Target**: 47,000원
**Time Horizon**: 3영업일"""


class FakeLLM:
    def __init__(self, fail_on=None):
        self.calls, self.fail_on = [], fail_on

    def role_of(self, system):
        for f, t in TITLES.items():
            if t in system:
                return f
        return "peer"

    def __call__(self, system, user, max_tokens, schema):
        role = self.role_of(system)
        self.calls.append((role, user))
        if self.fail_on and role == self.fail_on:
            raise RuntimeError("quota")
        md = {"09_trader.md": TRADER_MD, "13_portfolio_manager.md": PM_MD}.get(role, f"[{role}] 본문")
        return {"markdown": md}, "fake-model"


def fake_collect(price_status="ok", raise_exc=None):
    def fn(stock, date, out, social_on):
        if raise_exc:
            raise raise_exc
        files = {"01_price.json": {"status": price_status, "latest_ohlcv": {"Close": 45000}},
                 "02_fundamentals.json": {"k": "FUND"}, "03_news_community.json": {"k": "NEWS"},
                 "04_flow.json": {"k": "FLOW"}, "05_disclosures.json": {"k": "DISC"}}
        for n, v in files.items():
            with open(os.path.join(out, n), "w", encoding="utf-8") as f:
                json.dump(v, f)
        with open(os.path.join(out, "ohlcv.csv"), "w", encoding="utf-8") as f:
            f.write("Date,Open,High,Low,Close,Volume\n" + "\n".join(
                f"2026-{1 + i // 28:02d}-{1 + i % 28:02d},{i},{i + 1},{i - 1},{i},{1000 + i}" for i in range(1, 100)))
        return {"legs": {"price": {"status": price_status}, "naver_news": {"status": "ok"}}}
    return fn


def agent(llm=None, collect=None, rounds=1, peer_fn=None):
    return PL.TradingAgent(llm_fn=llm or FakeLLM(), collect_fn=collect or fake_collect(),
                           resolve_fn=lambda c: {"code": c, "name": "테스트", "ticker": f"{c}.KS"},
                           peer_fn=peer_fn or (lambda s: {"source": "curated", "peers": []}),
                           rounds=rounds, runs_dir=tempfile.mkdtemp())


def req(purpose="entry"):
    return A.DecisionRequest(code="000100", name="테스트", date="2026-09-28", purpose=purpose,
                             sector="화학", signal="동반강세", reason="외국인 +100억",
                             position={"entry": 44500, "stop": 42800, "target": 47000, "holdDay": 2}
                             if purpose == "review" else None)


class TestPipeline(unittest.TestCase):
    def test_full_run_order_inputs_decision(self):
        llm = FakeLLM()
        d = agent(llm).decide(req())
        roles = [r for r, _ in llm.calls]
        self.assertEqual(len(roles), 13)
        self.assertEqual(roles, ["01_market_analyst.md", "02_sentiment_analyst.md", "03_news_analyst.md",
                                 "04_fundamentals_analyst.md", "05_flow_analyst.md",
                                 "06_bull_researcher.md", "07_bear_researcher.md",
                                 "08_research_manager.md", "09_trader.md", "10_aggressive_risk.md",
                                 "11_conservative_risk.md", "12_neutral_risk.md",
                                 "13_portfolio_manager.md"])
        u = dict(llm.calls)
        # 애널리스트는 자기 파일만
        self.assertIn('"FLOW"', u["05_flow_analyst.md"])
        for other in ('"FUND"', '"NEWS"', '"DISC"'):
            self.assertNotIn(other, u["05_flow_analyst.md"])
        self.assertIn('"NEWS"', u["03_news_analyst.md"])
        self.assertIn('"DISC"', u["03_news_analyst.md"])
        self.assertNotIn('"FLOW"', u["03_news_analyst.md"])
        self.assertIn("최근 60거래일", u["01_market_analyst.md"])
        self.assertNotIn("2026-06-01", u["01_market_analyst.md"])        # 1년치 중 앞부분 잘림
        # 전략 맥락은 RM·Trader·PM 에만
        for r in ("08_research_manager.md", "09_trader.md", "13_portfolio_manager.md"):
            self.assertIn("[전략 맥락] 스윙 모의투자 신규 진입", u[r])
        for r in ("01_market_analyst.md", "06_bull_researcher.md", "10_aggressive_risk.md"):
            self.assertNotIn("[전략 맥락]", u[r])
        self.assertIn(PL.NO_BEAR, u["06_bull_researcher.md"])
        self.assertIn(PL.NO_OPP, u["10_aggressive_risk.md"])
        self.assertIn("[10_aggressive_risk.md] 본문", u["11_conservative_risk.md"])   # 직전 발언 전달
        self.assertIn(PL.PAST_NONE, u["13_portfolio_manager.md"])
        # 결정
        self.assertIsNone(d.error)
        self.assertEqual((d.rating, d.action, d.entry, d.stop, d.target, d.weight),
                         ("Overweight", "Buy", 44500, 42800, 47000, 0.06))
        self.assertEqual(A.entry_verdict(d), (True, ""))
        for k in ("peers", "collect", "market", "debate", "research_manager", "trader", "risk_debate", "pm"):
            self.assertIn(k, d.reports)
        self.assertTrue(d.agent.startswith("trading_agent@"))

    def test_rounds_two(self):
        llm = FakeLLM()
        agent(llm, rounds=2).decide(req())
        self.assertEqual(len(llm.calls), 5 + 4 + 1 + 1 + 6 + 1)

    def test_price_unavailable_stops_without_llm(self):
        llm = FakeLLM()
        d = agent(llm, collect=fake_collect("unavailable")).decide(req())
        self.assertIn("가격 수집 실패", d.error)
        self.assertEqual(llm.calls, [])
        self.assertIn("collect", d.reports)

    def test_role_failure_keeps_partial_reports(self):
        d = agent(FakeLLM(fail_on="08_research_manager.md")).decide(req())
        self.assertIn("08_research_manager.md", d.error)
        self.assertIn("debate", d.reports)
        self.assertNotIn("trader", d.reports)
        self.assertFalse(A.entry_verdict(d)[0])

    def test_collect_exception_is_error(self):
        d = agent(collect=fake_collect(raise_exc=ConnectionError("net"))).decide(req())
        self.assertIn("ConnectionError", d.error)

    def test_chart_from_ohlcv(self):
        """I-1 차트: 스킬 ohlcv.csv 최근 90거래일 → Decision.chart (2026-09-29)."""
        d = agent().decide(req())
        self.assertIsNone(d.error)
        self.assertEqual(len(d.chart["dates"]), 90)
        self.assertEqual(d.chart["c"][-1], 99)
        self.assertIsNotNone(d.chart["sma50"][-1])
        self.assertIn("chart", d.to_dict())

    def test_review_context(self):
        llm = FakeLLM()
        agent(llm).decide(req("review"))
        u = dict(llm.calls)
        self.assertIn("보유 중 종목 매도 검토", u["13_portfolio_manager.md"])
        self.assertIn(f"보유 2/{PL.config.HOLD_DAYS}일", u["09_trader.md"])

    def test_peer_failure_does_not_block(self):
        def boom(stock):
            raise RuntimeError("yf down")
        tmp = tempfile.mkdtemp()
        old = P.MEMORY_FILE
        P.MEMORY_FILE = os.path.join(tmp, "peers_dynamic.json")
        try:
            d = agent(peer_fn=boom).decide(req())
        finally:
            P.MEMORY_FILE = old
        self.assertIsNone(d.error)
        self.assertIn("yf down", d.reports["peers"])


class PeerLLM:
    def __init__(self, proposals):
        self.proposals, self.calls = list(proposals), []

    def __call__(self, system, user, max_tokens, schema):
        self.calls.append(user)
        return {"peers": self.proposals.pop(0) if self.proposals else []}, "fake-model"


def pk(*tickers):
    return [{"name": t, "ticker": t, "note": "n"} for t in tickers]


def yf_check_fake(bad=("ZZZZ",)):
    def fn(peers):
        return ([p for p in peers if p["ticker"] not in bad],
                [{"ticker": p["ticker"], "reason": "yfinance 시세 없음"} for p in peers if p["ticker"] in bad])
    return fn


class TestPeers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = P.MEMORY_FILE
        P.MEMORY_FILE = os.path.join(self.tmp, "memory", "peers_dynamic.json")
        self.st = store.FileStore(os.path.join(self.tmp, "store"))
        self.stock = {"code": "079160", "name": "CJ CGV", "ticker": "079160.KS"}
        self.prof = lambda t: {"sector": "Communication Services", "industry": "Entertainment",
                               "industryKey": "entertainment"}
        self.today = datetime.date(2026, 9, 29)

    def tearDown(self):
        P.MEMORY_FILE = self.old

    def run_(self, llm, today=None, stock=None):
        return P.resolve_peers(stock or self.stock, self.st, llm, profile_fn=self.prof,
                               yf_check=yf_check_fake(), today=today or self.today)

    def mem(self):
        with open(P.MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)

    def test_curated_no_llm(self):
        llm = PeerLLM([])
        info = self.run_(llm, stock={"code": "005930", "name": "삼성전자", "ticker": "005930.KS"})
        self.assertEqual((info["source"], info["llmCalls"]), ("curated", 0))
        self.assertEqual(self.mem(), {})

    def test_propose_validate_cache(self):
        llm = PeerLLM([pk("6479.T", "005930.KS", "ZZZZ", "IMAX", "CNK", "IMAX")])
        info = self.run_(llm)
        self.assertEqual(info["source"], "llm-proposed")
        self.assertEqual([p["ticker"] for p in info["peers"]], ["IMAX", "CNK", "6479.T"])   # 미국 앞으로
        reasons = {r["ticker"]: r["reason"] for r in info["rejected"]}
        self.assertEqual(reasons["005930.KS"], "한국 상장사 제외")
        self.assertEqual(reasons["ZZZZ"], "yfinance 시세 없음")
        self.assertIn("079160", self.mem())
        self.assertEqual(self.st.get(P.STORE_KEY)["079160"]["source"], "llm-proposed")
        # 두 번째: 캐시 적중, LLM 0콜
        llm2 = PeerLLM([])
        info2 = self.run_(llm2)
        self.assertEqual((info2["source"], info2["llmCalls"], len(llm2.calls)), ("llm-proposed", 0, 0))

    def test_retry_then_fail_then_cooldown(self):
        llm = PeerLLM([pk("ZZZZ", "IMAX"), pk("ZZZZ")])
        info = self.run_(llm)
        self.assertEqual(info["llmCalls"], 2)
        self.assertIn("ZZZZ", llm.calls[1])                        # 거절 사유를 붙여 재제안
        self.assertEqual(info["source"], "industry-default")        # 업종 기본표 폴백
        self.assertEqual(self.st.get(P.STORE_KEY)["079160"]["failedAt"], "2026-09-29")
        llm2 = PeerLLM([pk("IMAX", "CNK")])
        self.assertEqual(self.run_(llm2, today=self.today + datetime.timedelta(days=5))["llmCalls"], 0)
        info3 = self.run_(llm2, today=self.today + datetime.timedelta(days=P.RETRY_DAYS + 1))
        self.assertEqual((info3["llmCalls"], info3["source"]), (1, "llm-proposed"))

    def test_ttl_expiry(self):
        self.run_(PeerLLM([pk("IMAX", "CNK")]))
        llm = PeerLLM([pk("AMC", "CNK")])
        info = self.run_(llm, today=self.today + datetime.timedelta(days=P.TTL_DAYS + 1))
        self.assertEqual(info["llmCalls"], 1)
        self.assertEqual(info["peers"][0]["ticker"], "AMC")


class TestDefaultCollect(unittest.TestCase):
    def test_passes_path_and_creates_dir(self):
        """스킬 collect 는 out 을 Path 로 받고 폴더가 있어야 한다(2026-09-29 첫 실운용 가격 레그 실패 회귀)."""
        import sys
        import types
        from pathlib import Path
        seen = {}

        def fake_collect(stock, date, out, social_on=True, full_dart=False):
            seen.update(out=out, exists=out.is_dir(), social=social_on)
            (out / "ohlcv.csv").write_text("x")              # indicators.run 과 같은 경로 결합
            return {"legs": {"price": "ok"}}
        old_mod, old_ensure = sys.modules.get("collect"), PL.ensure_kit_path
        sys.modules["collect"] = types.SimpleNamespace(collect=fake_collect)
        PL.ensure_kit_path = lambda: None
        try:
            out = os.path.join(tempfile.mkdtemp(), "005930", "2026-09-29", "entry")
            m = PL._default_collect({"code": "005930"}, "2026-09-29", out, False)
        finally:
            PL.ensure_kit_path = old_ensure
            if old_mod is None:
                sys.modules.pop("collect", None)
            else:
                sys.modules["collect"] = old_mod
        self.assertIsInstance(seen["out"], Path)
        self.assertTrue(seen["exists"])
        self.assertTrue(os.path.exists(os.path.join(out, "ohlcv.csv")))
        self.assertEqual(m["legs"]["price"], "ok")


if __name__ == "__main__":
    unittest.main()
