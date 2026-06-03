#!/usr/bin/env python3
"""
Daily Deep Research generator (Approach A: deterministic fetch -> LLM analyze).

Pipeline per watchlist stock:
  1. Fetch RAW data via REST (analysis/sources.py):
       peers (yfinance + Reddit on the overseas peer group) · news (Naver+Tavily)
       · community (Naver cafe — Korean retail) · DART
  2. One Claude (Sonnet 4.6) call turns the raw data into the `analysis` schema
     (summaries, sentiment, insights; peer prices kept deterministic).
  3. Merge `analysis` into public/daily_market_report.json and public/reports/YYYY-MM-DD.json.

Must run AFTER generate_report.py (which writes the base report).
Degrades gracefully: a stock that fails keeps any existing analysis and never
crashes the batch (so the price report still commits).

Env: ANTHROPIC_API_KEY, DART_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, TAVILY_API_KEY
"""
import os
import re
import sys
import json
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from anthropic import Anthropic

from analysis import sources

ROOT          = os.path.dirname(os.path.abspath(__file__))
WATCHLIST     = os.path.join(ROOT, "watchlist.json")
PEERS_PATH    = os.path.join(ROOT, "analysis", "peers.json")
REPORTS_DIR   = os.path.join(ROOT, "public", "reports")
DAILY_PATH    = os.path.join(ROOT, "public", "daily_market_report.json")

MODEL       = "claude-sonnet-4-6"
MAX_TOKENS  = 4096
CONCURRENCY = int(os.environ.get("ANALYSIS_CONCURRENCY", "6"))  # stocks processed in parallel

SYSTEM = """\
너는 한국 주식 투자 리서치 애널리스트다. 주어진 RAW 데이터(해외 peer 시세, peer 그룹 Reddit
여론, 국내외 뉴스, 국내 커뮤니티 글, DART 공시/재무)를 분석해 대시보드용 `analysis` JSON
한 개를 생성한다.

규칙:
- 모든 서술형 필드는 한국어. 회사명/티커/기사 제목/URL은 원문 유지.
- sentiment 값은 정확히 "긍정" | "부정" | "중립" 중 하나.
- RAW에 없는 사실을 지어내지 말 것. 자료가 빈약하면 그 점을 summary에 명시하고 배열을 줄여라.
- 각 insight/note는 투자자 관점의 한 줄 해석.
- 반드시 아래 스키마와 '정확히' 동일한 키 구조의 JSON만 출력. 마크다운 펜스/설명 금지.

스키마:
{
  "peers": { "summary": "2-3문장", "items": [ {"name","ticker","price","changePct","note"} ], "reddit": [ {"title","url","subreddit","sentiment","summary"} ] },
  "news": { "summary": "3-4문장", "items": [ {"title","source","date","sentiment","url","insight"} ] },
  "community": { "summary": "3-4문장", "sentimentLabel": "", "naver": [ {"title","url","sentiment","summary"} ] },
  "dart": { "summary": "3-4문장", "highlights": [ {"label","value","note"} ], "recentFilings": [ {"date","title","type","insight"} ] }
}

- peers.reddit: 입력 peers_reddit(해외 peer/섹터에 대한 영어권 Reddit 글)을 바탕으로 3-5개.
  해외 peer 그룹에 대한 여론·논점을 요약한다. title/url/subreddit은 원문, summary는 한국어 한 줄.
  한국 종목이 직접 언급되지 않으면 peer/섹터 맥락으로 해석하고 summary에 그 점을 밝힌다.
- community: 네이버 카페/종목토론방 등 '국내 개인투자자' 여론만 담는다(Reddit 제외). naver 3-5개.
- 개수 가이드: news 5-7 (국내+해외 혼합), dart highlights 4-6 & recentFilings 3-5.
- peers.items 는 입력으로 받은 항목을 그대로(가격/등락률/note 변경 금지) 쓰고 peers.summary/peers.reddit 만 작성한다."""


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def fetch_peer_reddit(peer_list):
    """Reddit discussion about the OVERSEAS PEER GROUP (not the Korean stock).

    Korean mid-caps barely appear on Reddit, but their global peers (e.g. Eli Lilly,
    GE Vernova, NGK) are widely discussed — so we search Reddit on the top peers.
    Reddit public JSON first; Tavily (reddit.com) as fallback when blocked/empty.
    """
    if not peer_list:
        return []
    posts = []
    for p in peer_list[:2]:                      # top 2 peers (bellwethers)
        posts += sources.reddit_search(f"{p['name']} stock", max_results=3)
    if not posts:
        lead = peer_list[0]["name"]
        posts = sources.tavily_search(f"{lead} stock reddit", max_results=4,
                                      include_domains=["reddit.com"])
    seen, uniq = set(), []
    for x in posts:                              # dedupe by url, cap at 5
        u = x.get("url")
        if u and u not in seen:
            seen.add(u)
            uniq.append(x)
    return uniq[:5]


def gather_raw(stock, peer_cfg):
    """Collect raw data for one stock."""
    code, name = stock["code"], stock["name"]
    peer_list = peer_cfg.get(code, [])
    peers = sources.get_peer_quotes(peer_list)

    raw = {
        "stock": {"code": code, "name": name, "market": stock["market"]},
        "peers_items": peers,                                   # deterministic, reused verbatim
        "peers_reddit": fetch_peer_reddit(peer_list),           # Reddit on the peer group
        "naver_news": sources.naver_search("news", name, display=8),
        "overseas_news": sources.tavily_search(f"{name} stock news", max_results=5),
        "naver_cafe": sources.naver_search("cafearticle", name, display=6),  # 국내 community
        "dart": {},
    }
    corp = sources.dart_corp_code(code)
    if corp:
        raw["dart"] = {
            "financials": sources.dart_financials(corp),
            "disclosures": sources.dart_disclosures(corp),
            "major_holders": sources.dart_major_holders(corp),
        }
    return raw, peers


def extract_json(text):
    """Pull the first balanced JSON object out of an LLM response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object found in response")
    return json.loads(text[start:end + 1])


def analyze_stock(client, stock, peer_cfg):
    raw, peers = gather_raw(stock, peer_cfg)
    user = (f"종목: {stock['name']} ({stock['code']}, {stock['market']})\n"
            f"RAW 데이터(JSON):\n{json.dumps(raw, ensure_ascii=False)}")

    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text")
    analysis = extract_json(text)

    # Force deterministic peer items (LLM only authored peers.summary).
    analysis.setdefault("peers", {})["items"] = peers
    return analysis


def merge_into_report(path, analysis_by_code):
    report = load_json(path)
    if not isinstance(report, list):
        return False
    changed = False
    for stock in report:
        a = analysis_by_code.get(stock.get("code"))
        if a:
            stock["analysis"] = a
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return changed


def main():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        sys.exit(1)

    watchlist = load_json(WATCHLIST, [])
    peer_cfg = {k: v for k, v in (load_json(PEERS_PATH, {}) or {}).items()
                if not k.startswith("_")}
    client = Anthropic()

    # Pre-warm the (large) DART corpCode map once so parallel workers don't race on it.
    sources._load_corp_map()

    workers = max(1, min(CONCURRENCY, len(watchlist)))
    print(f"=== Generating deep research for {len(watchlist)} stocks "
          f"({workers}-way parallel) ===")
    analysis_by_code = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyze_stock, client, s, peer_cfg): s
                   for s in watchlist}
        for fut in as_completed(futures):
            stock = futures[fut]
            name = stock["name"]
            try:
                analysis_by_code[stock["code"]] = fut.result()
                print(f"  Done {name} ({stock['code']})")
            except Exception as e:
                print(f"  Skipped {name}: {e}", file=sys.stderr)

    if not analysis_by_code:
        print("No analysis produced; leaving reports unchanged.", file=sys.stderr)
        return

    merge_into_report(DAILY_PATH, analysis_by_code)
    print(f"  Updated {DAILY_PATH}")

    today = datetime.date.today().strftime("%Y-%m-%d")
    dated = os.path.join(REPORTS_DIR, f"{today}.json")
    if os.path.exists(dated):
        merge_into_report(dated, analysis_by_code)
        print(f"  Updated {dated}")

    print(f"=== Done: {len(analysis_by_code)}/{len(watchlist)} stocks analyzed ===")


if __name__ == "__main__":
    main()
