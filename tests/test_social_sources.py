"""analysis/social.py 회귀 — StockTwits 라벨 집계·신선도 창, Reddit RSS 파싱·예산·회로차단 (오프라인).

실행: python tests/test_social_sources.py
픽스처: tests/fixtures/stocktwits_LLY_sample.json (2026-09-17 실스트림 발췌),
        Atom 샘플은 TradingAgents tests/test_reddit_fallback.py 의 것을 축약.
"""
import os
import sys
import json
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIX = os.path.join(ROOT, "tests", "fixtures")

from analysis import social

ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/alpha</name></author>
    <category term="stocks" label="r/stocks"/>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;Great &lt;b&gt;quarter&lt;/b&gt; for NVDA&amp;#39;s datacenter unit.&lt;/p&gt;&lt;/div&gt;&lt;!-- SC_ON --&gt;</content>
    <id>t3_a1</id>
    <link href="https://www.reddit.com/r/stocks/comments/a1/nvda_earnings/" />
    <published>2026-05-20T14:30:00+00:00</published>
    <title>NVDA earnings beat,
      stock pops</title>
  </entry>
  <entry>
    <category term="stocks" label="r/stocks"/>
    <content type="html">&lt;p&gt;Forward P/E discussion&lt;/p&gt;</content>
    <id>t3_a2</id>
    <link href="https://www.reddit.com/r/stocks/comments/a2/is_nvda_overvalued/" />
    <published>2026-05-19T09:00:00Z</published>
    <title>Is NVDA overvalued?</title>
  </entry>
</feed>
"""


class _Resp:
    def __init__(self, status=200, content=b"", payload=None, headers=None):
        self.status_code = status
        self.content = content
        self.text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        self._payload = payload
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def _fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return json.load(f)


def _label(m):
    return ((m.get("entities") or {}).get("sentiment") or {}).get("basic")


def main():
    fx = _fixture("stocktwits_LLY_sample.json")
    msgs = fx["messages"]
    exp_bull = sum(1 for m in msgs if _label(m) == "Bullish")
    exp_bear = sum(1 for m in msgs if _label(m) == "Bearish")
    exp_none = len(msgs) - exp_bull - exp_bear
    assert exp_bull and exp_bear and exp_none, "픽스처에 라벨 3종이 모두 있어야 한다"
    newest = datetime.datetime.fromisoformat(msgs[0]["created_at"].replace("Z", "+00:00"))

    # ── is_us_ticker ────────────────────────────────────────────────────────────
    for ok in ("LLY", "NVDA", "BRK-B", "incy"):
        assert social.is_us_ticker(ok), ok
    for bad in ("6479.T", "2382.HK", "ENR.DE", "", None, "329180.KS"):
        assert not social.is_us_ticker(bad), bad

    # ── StockTwits: 라벨 집계(코드 결정적) + 창 안(now = 최신+1일) ─────────────────
    social._st_cache.clear()
    social.requests.get = lambda url, **kw: _Resp(200, b"{}", payload=fx)
    r = social.stocktwits_stream("lly", fresh_days=7, now=newest + datetime.timedelta(days=1))
    assert r["status"] == "ok" and r["ticker"] == "LLY", r["status"]
    assert (r["bullish"], r["bearish"], r["unlabeled"]) == (exp_bull, exp_bear, exp_none), r
    assert r["total"] == len(msgs) and r["labeled"] == exp_bull + exp_bear
    assert r["bullPct"] == round(100 * exp_bull / (exp_bull + exp_bear))
    assert r["messages"][0]["sentiment"] in ("Bullish", "Bearish", None)
    assert all(len(m["body"]) <= 281 for m in r["messages"])
    assert r["messages"][0]["createdAt"].startswith("2026-09-18")   # KST 변환(Z 22:16 → 07:16 KST)

    # ── StockTwits: 창 밖(now = 최신+30일) → empty (unavailable 이 아님) ─────────────
    social._st_cache.clear()
    r2 = social.stocktwits_stream("LLY", fresh_days=7, now=newest + datetime.timedelta(days=30))
    assert r2["status"] == "empty" and r2["total"] == 0 and r2["bullPct"] is None, r2["status"]

    # ── StockTwits: 캐시(같은 키 두 번째 호출은 네트워크 없이) ─────────────────────
    social._st_cache.clear()
    calls = {"n": 0}
    def _cnt(url, **kw):
        calls["n"] += 1
        return _Resp(200, b"{}", payload=fx)
    social.requests.get = _cnt
    kw = dict(fresh_days=7, now=newest + datetime.timedelta(days=1))
    social.stocktwits_stream("LLY", **kw); social.stocktwits_stream("LLY", **kw)
    assert calls["n"] == 1, calls

    # ── StockTwits: HTTP 오류 → unavailable, 비미국 티커 → skipped(네트워크 0) ─────
    social._st_cache.clear()
    social.requests.get = lambda url, **kw: _Resp(503, b"cf")
    assert social.stocktwits_stream("LLY")["status"] == "unavailable"
    social.requests.get = _cnt; calls["n"] = 0
    assert social.stocktwits_stream("7011.T")["status"] == "skipped" and calls["n"] == 0

    # ── stocktwits_for_peers: 미국 상장만, 상위 top 개 ────────────────────────────
    social._st_cache.clear()
    social.requests.get = lambda url, **kw: _Resp(200, b"{}", payload=fx)
    peers = [{"name": "MHI", "ticker": "7011.T"}, {"name": "Eli Lilly", "ticker": "LLY"},
             {"name": "Nvidia", "ticker": "NVDA"}, {"name": "Incyte", "ticker": "INCY"}]
    got = social.stocktwits_for_peers(peers, top=2, fresh_days=7, now=newest + datetime.timedelta(days=1))
    assert [g["ticker"] for g in got] == ["LLY", "NVDA"] and got[0]["name"] == "Eli Lilly"

    # ── Reddit RSS 파싱: 제목 공백 정규화·link href·published→date·SC_OFF 본문·라벨 ──
    social.reset_reddit_budget()
    social.REDDIT_GAP_S = 0
    social.requests.get = lambda url, **kw: _Resp(200, ATOM.encode("utf-8"))
    posts = social.reddit_rss("NVDA", sub="stocks", limit=5)
    assert posts is not None and len(posts) == 2, posts
    p0 = posts[0]
    assert p0["title"] == "NVDA earnings beat, stock pops"
    assert p0["url"].endswith("/nvda_earnings/") and p0["subreddit"] == "r/stocks"
    assert p0["date"] == "2026-05-20" and p0["author"] == "/u/alpha"
    assert p0["content"] == "Great quarter for NVDA's datacenter unit."
    assert p0["score"] is None and p0["num_comments"] is None and p0["source"] == "rss"
    assert posts[1]["content"] == "Forward P/E discussion"

    # ── Reddit fresh_days 창: 2026-05 글은 오래됨 → 걸러져 [] (글 없음 ≠ 실패) ──────
    social.reset_reddit_budget()
    assert social.reddit_rss("NVDA", fresh_days=7) == []

    # ── Reddit 예산: 기본 2콜 → 3번째는 네트워크 없이 None ────────────────────────
    social.reset_reddit_budget()
    social.REDDIT_BUDGET = 2
    calls["n"] = 0
    def _atom_cnt(url, **kw):
        calls["n"] += 1
        return _Resp(200, ATOM.encode("utf-8"))
    social.requests.get = _atom_cnt
    assert social.reddit_rss("A") is not None and social.reddit_rss("B") is not None
    assert social.reddit_rss("C") is None and calls["n"] == 2, (calls, social.reddit_status())
    assert "budget" in social.reddit_status()["reason"] or social.reddit_status()["calls"] == 2

    # ── Reddit 회로차단: 첫 429 → blocked, 이후 호출은 네트워크 없이 None ───────────
    social.reset_reddit_budget()
    social.REDDIT_BUDGET = 5
    seq = iter([_Resp(429, b""), _Resp(200, ATOM.encode("utf-8"))])
    calls["n"] = 0
    def _seq(url, **kw):
        calls["n"] += 1
        return next(seq)
    social.requests.get = _seq
    assert social.reddit_rss("A") is None
    assert social.reddit_status()["blocked"] is True
    assert social.reddit_rss("B") is None and calls["n"] == 1

    # ── reddit_for_peers: 상태 구분(unavailable / ok / empty) + 예산 내 peer 수 ──────
    social.reset_reddit_budget(); social.REDDIT_BUDGET = 2
    social.requests.get = _atom_cnt; calls["n"] = 0
    res = social.reddit_for_peers(peers, subs=("stocks",), limit=5)
    assert res["status"] == "ok" and res["queried"] == ["LLY@stocks", "NVDA@stocks"], res["queried"]
    assert len(res["posts"]) == 2 and calls["n"] == 2          # 두 peer 같은 피드 → URL dedup
    social.reset_reddit_budget()
    social.requests.get = lambda url, **kw: _Resp(429, b"")
    res = social.reddit_for_peers(peers)
    assert res["status"] == "unavailable" and res["posts"] == []
    social.reset_reddit_budget()
    social.requests.get = lambda url, **kw: _Resp(200, ATOM.encode("utf-8"))
    res = social.reddit_for_peers(peers, fresh_days=7)
    assert res["status"] == "empty" and res["posts"] == []
    social.reset_reddit_budget()
    assert social.reddit_for_peers([{"name": "MHI", "ticker": "7011.T"}])["status"] == "unavailable"

    print("ALL PASS (social sources: stocktwits tally/window/cache, reddit rss parse/budget/breaker)")


if __name__ == "__main__":
    main()
