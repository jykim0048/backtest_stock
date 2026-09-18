"""generate_analysis peer 여론 연결(P2) 회귀 — fetch_peer_reddit 소스 순서·상태, _merge_stocktwits 결정성 (오프라인).

실행: python tests/test_peer_social_merge.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import generate_analysis as ga
from analysis import social, sources

PEERS = [{"name": "Eli Lilly", "ticker": "LLY"}, {"name": "Incyte", "ticker": "INCY"},
         {"name": "JCR", "ticker": "4552.T"}]


def _rss_post(i, url):
    return {"title": f"rss {i}", "url": url, "subreddit": "r/stocks", "date": "2026-09-17",
            "content": "body", "score": None, "num_comments": None, "source": "rss"}


def main():
    # ── fetch_peer_reddit: ① RSS 성공 → 웹검색 결과와 합쳐 URL dedup, status=ok ───────
    social.reddit_for_peers = lambda peers, **kw: {
        "posts": [_rss_post(1, "https://r/1"), _rss_post(2, "https://r/2")],
        "status": "ok", "queried": ["LLY@stocks", "INCY@stocks"]}
    sources.web_search = lambda q, **kw: [{"title": "web dup", "url": "https://r/2", "subreddit": "r/investing"},
                                         {"title": "web 3", "url": "https://r/3"}]
    sources.reddit_search = lambda q, **kw: (_ for _ in ()).throw(AssertionError("③ 폴백은 호출되면 안 됨"))
    r = ga.fetch_peer_reddit(PEERS)
    assert r["status"] == "ok" and [p["url"] for p in r["posts"]] == ["https://r/1", "https://r/2", "https://r/3"], r
    assert r["posts"][0]["source"] == "rss" and r["posts"][2]["source"] == "web"
    assert r["queried"][:2] == ["LLY@stocks", "INCY@stocks"] and "web:Eli Lilly" in r["queried"]

    # ── ② RSS unavailable(429) + 웹검색 빈 결과 + ③ 폴백도 빈 결과 → unavailable ────────
    social.reddit_for_peers = lambda peers, **kw: {"posts": [], "status": "unavailable", "queried": ["LLY@stocks"]}
    sources.web_search = lambda q, **kw: []
    sources.reddit_search = lambda q, **kw: []
    r = ga.fetch_peer_reddit(PEERS)
    assert r["status"] == "unavailable" and r["posts"] == [], r

    # ── RSS empty(성공했지만 0건) + 웹검색 빈 결과 → empty (unavailable 이 아님) ─────────
    social.reddit_for_peers = lambda peers, **kw: {"posts": [], "status": "empty", "queried": ["LLY@stocks"]}
    r = ga.fetch_peer_reddit(PEERS)
    assert r["status"] == "empty", r

    # ── ③ 거주지 폴백이 살리는 경우 ────────────────────────────────────────────────────
    social.reddit_for_peers = lambda peers, **kw: {"posts": [], "status": "unavailable", "queried": []}
    sources.reddit_search = lambda q, **kw: [{"title": "local", "url": "https://r/9", "subreddit": "r/x"}]
    r = ga.fetch_peer_reddit(PEERS)
    assert r["status"] == "ok" and r["posts"][0]["url"] == "https://r/9"

    # ── peer 없음 ─────────────────────────────────────────────────────────────────────
    assert ga.fetch_peer_reddit([]) == {"posts": [], "status": "empty", "queried": []}

    # ── _merge_stocktwits: 수치는 코드 값, summary 만 LLM(티커 매칭), 환각 티커 무시 ──────
    st = [
        {"ticker": "LLY", "name": "Eli Lilly", "status": "ok", "bullish": 6, "bearish": 1, "unlabeled": 23,
         "total": 30, "labeled": 7, "bullPct": 86, "windowDays": 7, "newest": "2026-09-18 07:16",
         "oldest": "2026-09-16 21:15",
         "messages": [{"createdAt": "a", "user": "u1", "sentiment": "Bullish", "body": "b1", "id": 1},
                      {"createdAt": "b", "user": "u2", "sentiment": None, "body": "b2", "id": 2},
                      {"createdAt": "c", "user": "u3", "sentiment": None, "body": "b3", "id": 3},
                      {"createdAt": "d", "user": "u4", "sentiment": None, "body": "b4", "id": 4}]},
        {"ticker": "INCY", "name": "Incyte", "status": "empty", "bullish": 0, "bearish": 0, "unlabeled": 0,
         "total": 0, "labeled": 0, "bullPct": None, "windowDays": 7, "newest": None, "oldest": None,
         "messages": []},
    ]
    llm = [{"ticker": "lly", "summary": "돌파 지속 여부 논쟁, 강세 우위"},
           {"ticker": "NVDA", "summary": "지어낸 티커"},
           {"ticker": "LLY", "bullish": 999, "summary": "중복은 마지막 것"}]   # 수치 필드는 무시돼야 함
    m = ga._merge_stocktwits(llm, st)
    assert [x["ticker"] for x in m] == ["LLY", "INCY"], m
    assert m[0]["bullish"] == 6 and m[0]["bullPct"] == 86 and m[0]["summary"] == "중복은 마지막 것"
    assert len(m[0]["samples"]) == 3 and set(m[0]["samples"][0]) == {"createdAt", "user", "sentiment", "body"}
    assert m[1]["status"] == "empty" and m[1]["summary"] == "" and m[1]["samples"] == []
    assert ga._merge_stocktwits(None, st)[0]["summary"] == ""
    assert ga._merge_stocktwits(llm, []) == []

    # ── _stocktwits_llm_view: 메시지 상위 6건·경량 키만 ───────────────────────────────
    v = ga._stocktwits_llm_view(st)
    assert len(v[0]["messages"]) == 4 and "id" not in v[0]["messages"][0] and v[0]["bullPct"] == 86
    assert "messages" in v[1] and v[1]["messages"] == []

    # ── 스키마: peers.stocktwits 는 ticker/summary 만 (수치 키 없음) ───────────────────
    props = ga.ANALYSIS_SCHEMA["properties"]["peers"]["properties"]
    assert set(props) == {"summary", "reddit", "stocktwits"}
    assert set(props["stocktwits"]["items"]["properties"]) == {"ticker", "summary"}

    print("ALL PASS (peer social merge: reddit source order/status, stocktwits deterministic merge, schema)")


if __name__ == "__main__":
    main()
