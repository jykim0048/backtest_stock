#!/usr/bin/env python3
"""
Daily Deep Research generator (Approach A: deterministic fetch -> LLM analyze).

Pipeline per watchlist stock:
  1. Fetch RAW data via REST (analysis/sources.py):
       peers (yfinance + Reddit on the overseas peer group) · news (Naver+Tavily)
       · community (Naver cafe — Korean retail) · DART
  2. One LLM call (provider-agnostic via llm.py — Gemini by default, with an
     auto-failover chain) turns the raw data into the `analysis` schema
     (summaries, sentiment, insights; peer prices kept deterministic).
  3. Merge `analysis` into public/daily_market_report.json and public/reports/YYYY-MM-DD.json.

Must run AFTER generate_report.py (which writes the base report).
Degrades gracefully: a stock that fails keeps any existing analysis and never
crashes the batch (so the price report still commits).

Env: GEMINI_API_KEY (or LLM_CHAIN + matching keys), DART_API_KEY,
     NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, TAVILY_API_KEY
"""
import os
import sys
import json
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import llm                           # provider-agnostic LLM with fallback chain

from analysis import sources       # imports yfinance + redirects its cache to /tmp on CI
import yfinance as yf

ROOT          = os.path.dirname(os.path.abspath(__file__))
PEERS_PATH    = os.path.join(ROOT, "analysis", "peers.json")
REPORTS_DIR   = os.path.join(ROOT, "public", "reports")

# Input watchlist / merge-target report are env-overridable so the same deep-research
# generator serves both the 장전 워치리스트 (defaults) and the 장중 관심종목 pipeline:
#   ANALYSIS_WATCHLIST=intraday_watchlist.json
#   ANALYSIS_REPORT=public/intraday_report.json
#   ANALYSIS_ARCHIVE=0   # 장중은 최신본만 — 날짜별 아카이브 파일에는 병합하지 않음
WATCHLIST     = os.environ.get("ANALYSIS_WATCHLIST") or os.path.join(ROOT, "watchlist.json")
DAILY_PATH    = os.environ.get("ANALYSIS_REPORT")    or os.path.join(ROOT, "public", "daily_market_report.json")
ARCHIVE       = os.environ.get("ANALYSIS_ARCHIVE", "1") != "0"

if not os.path.isabs(WATCHLIST):
    WATCHLIST = os.path.join(ROOT, WATCHLIST)
if not os.path.isabs(DAILY_PATH):
    DAILY_PATH = os.path.join(ROOT, DAILY_PATH)

MAX_TOKENS  = 8192   # one full analysis is ~3.4-5.3k tokens; 4096 truncated mid-JSON
# Default low — free-tier LLM providers cap requests-per-minute; raise if you
# have headroom (each stock makes up to 2 calls: peer resolution + analysis).
CONCURRENCY = int(os.environ.get("ANALYSIS_CONCURRENCY", "3"))  # stocks processed in parallel

KST = datetime.timezone(datetime.timedelta(hours=9))

# 증분 분석(장중 비용 절감): 켜지면 이미 분석된 종목은 재분석하지 않고 이월본을 유지하되,
#   ① TTL 경과(ANALYSIS_TTL_HOURS) ② 지정 대비 가격 급변(ANALYSIS_PRICE_MOVE_PCT)
#   ③ 신규 공시 감지 시에만 다시 분석한다. (generate_report 의 REPORT_CARRY_ANALYSIS 와 짝)
INCREMENTAL    = os.environ.get("ANALYSIS_INCREMENTAL") == "1"
TTL_HOURS      = float(os.environ.get("ANALYSIS_TTL_HOURS", "2"))
PRICE_MOVE_PCT = float(os.environ.get("ANALYSIS_PRICE_MOVE_PCT", "5"))

SYSTEM = """\
너는 한국 주식 투자 리서치 애널리스트다. 주어진 RAW 데이터(해외 peer 시세, peer 그룹 Reddit
여론, 국내외 뉴스, 국내 커뮤니티 글, DART 공시/재무, 증권사 종목분석 리포트)를 분석해
대시보드용 `analysis` JSON 한 개를 생성한다.

규칙:
- 모든 서술형 필드는 한국어. 회사명/티커/기사 제목/URL은 원문 유지.
- sentiment 값은 정확히 "긍정" | "부정" | "중립" 중 하나.
- RAW에 없는 사실을 지어내지 말 것. 자료가 빈약하면 그 점을 summary에 명시하고 배열을 줄여라.
- 각 insight/note는 투자자 관점의 한 줄 해석.
- 출력은 응답 형식(json_schema)으로 강제된다. 스키마에 정의된 키만 채운다.

스키마:
{
  "catalyst": "오늘 이 종목의 주가를 움직일 핵심 촉매 2-3문장",
  "peers": { "summary": "2-3문장", "reddit": [ {"title","url","subreddit","sentiment","summary"} ] },
  "news": { "summary": "3-4문장", "items": [ {"title","source","date","sentiment","url","insight"} ] },
  "community": { "summary": "3-4문장", "sentimentLabel": "", "naver": [ {"title","url","sentiment","summary"} ] },
  "dart": { "summary": "3-4문장", "highlights": [ {"label","value","note"} ] },
  "research": { "summary": "2-3문장", "items": [ {"note"} ] }
}

- catalyst: 뉴스·공시·수급·peer 동향 중 '오늘 주가를 가장 크게 움직일' 단일 촉매를 투자자 관점으로
  요약한다. 대시보드의 'Market Moving Catalysts'에 그대로 노출되므로 placeholder/메타설명을 쓰지 말고
  구체적 내용으로 채운다. 근거가 빈약하면 거래대금·모멘텀 등 가격 동향 기반으로 신중히 서술한다.
- peers.reddit: 입력 peers_reddit(해외 peer/섹터에 대한 영어권 Reddit 글)을 바탕으로 3-5개.
  해외 peer 그룹에 대한 여론·논점을 요약한다. title/url/subreddit은 원문, summary는 한국어 한 줄.
  한국 종목이 직접 언급되지 않으면 peer/섹터 맥락으로 해석하고 summary에 그 점을 밝힌다.
- community: 네이버 카페(naver_cafe)와 종목토론방(naver_board) 글로 '국내 개인투자자' 여론만
  담는다(Reddit 제외). naver 3-5개에는 종목토론방 글을 우선 담고, 공감/비공감(agree/disagree)
  수로 여론의 강도·쏠림을 가늠해 summary와 sentimentLabel에 반영한다.
- dart: summary와 highlights(4-6개)만 작성한다. **recentFilings는 만들지 마라** — 코드가 DART
  최신 공시 5건을 결정적으로 채운다.
- research: 입력 research_reports(증권사 리포트, 최근 30일)를 바탕으로 summary 2-3문장
  (목표주가 컨센서스의 방향·의견 분포·공통 논거 포함)과, items 를 **입력 research_reports 와
  같은 순서·같은 개수**로 작성한다. 각 item 은 그 리포트의 투자자 관점 한 줄 해석(note)만 담는다.
  **목표주가·의견·증권사·날짜를 items 에 쓰지 마라** — 코드가 원문 값을 결정적으로 채운다.
  research_reports 가 비어 있으면 summary 에 "최근 30일 발간 리포트 없음"을 명시하고 items 는 빈 배열.
- 개수 가이드: news 5-7 (국내+해외 혼합), dart highlights 4-6.
- peers.items 는 출력하지 마라 — 코드가 입력 시세를 결정적으로 채운다. peers 는 summary/reddit 만 작성한다."""


# JSON Schema for the analysis call (strict structured output). peers.items and
# dart.recentFilings are filled deterministically by code, so they're omitted here.
_STR = {"type": "string"}


def _obj(props):
    return {"type": "object", "additionalProperties": False,
            "properties": props, "required": list(props)}


def _arr(item_props):
    return {"type": "array", "items": _obj(item_props)}


ANALYSIS_SCHEMA = _obj({
    "catalyst": _STR,
    "peers": _obj({
        "summary": _STR,
        "reddit": _arr({"title": _STR, "url": _STR, "subreddit": _STR,
                        "sentiment": _STR, "summary": _STR}),
    }),
    "news": _obj({
        "summary": _STR,
        "items": _arr({"title": _STR, "source": _STR, "date": _STR,
                       "sentiment": _STR, "url": _STR, "insight": _STR}),
    }),
    "community": _obj({
        "summary": _STR,
        "sentimentLabel": _STR,
        "naver": _arr({"title": _STR, "url": _STR, "sentiment": _STR, "summary": _STR}),
    }),
    "dart": _obj({
        "summary": _STR,
        "highlights": _arr({"label": _STR, "value": _STR, "note": _STR}),
    }),
    "research": _obj({
        "summary": _STR,
        "items": _arr({"note": _STR}),
    }),
})


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def fetch_peer_reddit(peer_list):
    """Reddit discussion about the OVERSEAS PEER GROUP (not the Korean stock).

    Korean mid-caps barely appear on Reddit, but their global peers (e.g. Eli Lilly,
    GE Vernova, NGK) are widely discussed.

    Reddit's public search.json 403s from datacenter/CI IPs, so Tavily (reddit.com
    domain) is the PRIMARY source here; the direct Reddit endpoint — which only
    works from residential IPs — is a best-effort fallback when Tavily is empty.
    """
    if not peer_list:
        return []
    posts = []
    for p in peer_list[:2]:                      # top 2 peers (bellwethers)
        posts += sources.web_search(f"{p['name']} stock discussion",
                                    max_results=3, include_domains=["reddit.com"])
    if not posts:                                # residential-only fallback
        for p in peer_list[:2]:
            posts += sources.reddit_search(f"{p['name']} stock", max_results=3)
    seen, uniq = set(), []
    for x in posts:                              # dedupe by url, cap at 5
        u = x.get("url")
        if u and u not in seen:
            seen.add(u)
            uniq.append(x)
    return uniq[:5]


PEER_SYSTEM = """\
너는 한국 주식의 해외 비교기업(peer)을 찾는 애널리스트다. 주어진 한국 종목과 사업이 가장
유사한 '해외 상장' 비교기업 4-5개를 고른다.
- ticker 는 Yahoo Finance 에서 조회 가능한 정확한 심볼이어야 한다
  (미국=AAPL, 일본=6479.T, 대만=3008.TW, 홍콩=2382.HK, 독일=ENR.DE, 영국=BAB.L, 이탈리아=FCT.MI 등).
- 한국 상장사는 제외하고 해외 기업만 고른다.
출력은 정확히 이 JSON 객체만 (peers 배열에 4-5개):
{"peers": [{"name": "회사명", "ticker": "야후티커", "note": "왜 peer인지 한국어 한 줄"}]}"""


def _valid_tickers(peer_list):
    """yfinance 로 시세가 조회되는 ticker 만 남긴다 (LLM 환각 ticker 제거)."""
    if not peer_list:
        return []
    tickers = [p["ticker"] for p in peer_list]
    try:
        df = yf.download(tickers, period="5d", group_by="ticker",
                         progress=False, threads=True, auto_adjust=True)
    except Exception:
        return peer_list                         # 검증 자체가 실패하면 과도하게 거르지 않음
    keep = []
    for p in peer_list:
        try:
            tdf = sources._ticker_frame(df, p["ticker"])
            closes = tdf["Close"].dropna() if (tdf is not None and "Close" in tdf.columns) else None
            if closes is not None and len(closes) >= 1:
                keep.append(p)
        except Exception:
            pass
    return keep


def _filing_key(disclosures):
    """최신 공시 1건의 시그니처('접수일자|보고서명'). 없으면 ''."""
    if disclosures:
        d = disclosures[0]
        return f"{d.get('date','')}|{d.get('title','')}"
    return ""


def _latest_filing_key(code):
    """현재 최신 공시 시그니처를 가볍게 조회(DART). 실패/미상은 None(재분석 강제 안 함)."""
    try:
        corp = sources.dart_corp_code(code)
        if not corp:
            return None
        return _filing_key(sources.dart_disclosures(corp) or [])
    except Exception:
        return None


def _needs_analysis(stock, entry, now):
    """증분 모드 재분석 여부 판정 → (bool, 사유).

    entry: 리포트 항목(이월된 analysis + 현재가 basePrice 포함). 판정 순서는 비용 오름차순:
    신규 → TTL → 가격급변(무료) → 신규공시(DART 1콜) 순으로 확인한다.
    """
    a = (entry or {}).get("analysis")
    if not a:
        return True, "신규"
    meta = a.get("_meta") or {}
    asof = meta.get("asOf")
    if not asof:
        return True, "메타없음"
    try:
        age_h = (now - datetime.datetime.fromisoformat(asof)).total_seconds() / 3600
    except Exception:
        return True, "asOf파싱실패"
    if age_h >= TTL_HOURS:
        return True, f"TTL({age_h:.1f}h≥{TTL_HOURS}h)"
    base, cur = meta.get("price"), (entry or {}).get("basePrice")
    if base and cur and base > 0:
        move = abs(cur - base) / base * 100
        if move >= PRICE_MOVE_PCT:
            return True, f"가격급변({move:.1f}%≥{PRICE_MOVE_PCT}%)"
    cur_key = _latest_filing_key(stock["code"])
    if cur_key is not None and cur_key != meta.get("filingKey", ""):
        return True, "신규공시"
    return False, f"캐시({age_h:.1f}h)"


def resolve_peers(stock, peer_cfg):
    """정적 peers.json 을 우선 사용하고, 없으면 LLM 이 해외 peer 를 제안 → ticker 유효성 검증.

    동적 와치리스트(screener.py)로 매일 종목이 바뀌므로, peers.json 에 없는 종목도
    peer 분석이 비지 않도록 런타임에 해외 비교기업을 찾아준다.
    """
    code = stock["code"]
    if peer_cfg.get(code):
        return peer_cfg[code]                     # 큐레이션된 정적 peer 우선
    try:
        data = llm.generate_json(
            PEER_SYSTEM,
            f"종목: {stock['name']} ({code}, {stock['market']}). 해외 peer 4-5개.",
            max_tokens=2048,   # headroom so residual thinking can't truncate the JSON
        )
        arr = data.get("peers", []) if isinstance(data, dict) else []
        proposed = [{"name": p.get("name", ""), "ticker": p.get("ticker", ""),
                     "note": p.get("note", "")} for p in arr if p.get("ticker")]
    except Exception as e:
        print(f"[peers] resolve failed for {stock['name']}: {e}", file=sys.stderr)
        return []
    valid = _valid_tickers(proposed)
    print(f"[peers] {stock['name']}: {len(valid)}/{len(proposed)} peer tickers valid",
          file=sys.stderr)
    return valid


def gather_raw(stock, peer_list):
    """Collect raw data for one stock. peer_list is already resolved (static or LLM)."""
    code, name = stock["code"], stock["name"]
    peers = sources.get_peer_quotes(peer_list)

    raw = {
        "stock": {"code": code, "name": name, "market": stock["market"]},
        "peers_items": peers,                                   # deterministic, reused verbatim
        "peers_reddit": fetch_peer_reddit(peer_list),           # Reddit on the peer group
        "naver_news": sources.naver_search("news", name, display=8),
        "overseas_news": sources.web_search(f"{name} stock news", max_results=5),
        "naver_cafe": sources.naver_search("cafearticle", name, display=6),  # 국내 community
        "naver_board": sources.naver_board(code, pages=2),                   # 종목토론방(개인투자자)
        "research_reports": sources.naver_research(code, days=30),           # 증권사 리포트(최근 30일)
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


def analyze_stock(stock, peer_cfg):
    peer_list = resolve_peers(stock, peer_cfg)
    raw, peers = gather_raw(stock, peer_list)
    user = (f"종목: {stock['name']} ({stock['code']}, {stock['market']})\n"
            f"RAW 데이터(JSON):\n{json.dumps(raw, ensure_ascii=False)}")

    # llm.generate_json returns parsed JSON from the first chain link that
    # succeeds (auto-failover on quota/billing/rate-limit). Providers that
    # support it enforce ANALYSIS_SCHEMA; all return valid, escaped JSON.
    analysis, model_used = llm.generate_json(
        SYSTEM, user, max_tokens=MAX_TOKENS, schema=ANALYSIS_SCHEMA, return_model=True)
    analysis["generatedBy"] = model_used   # surfaced on the dashboard

    # Force deterministic peer items (LLM only authored peers.summary/peers.reddit).
    analysis.setdefault("peers", {})["items"] = peers

    # Deterministic recent filings: latest 5 DART disclosures, no LLM curation.
    disclosures = (raw.get("dart") or {}).get("disclosures") or []
    analysis.setdefault("dart", {})["recentFilings"] = [
        {"date": d.get("date", ""), "title": d.get("title", ""),
         "filer": d.get("filer", ""), "url": d.get("url", "")}
        for d in disclosures[:5]
    ]

    # 증권사 리포트: 원문 값(제목·증권사·날짜·목표주가·의견·링크)은 코드가 결정적으로
    # 채우고 LLM 은 note(입력과 같은 순서) / summary 만 담당한다 (목표주가 환각 방지).
    reports = raw.get("research_reports") or []
    llm_notes = (analysis.get("research") or {}).get("items") or []
    res = analysis.setdefault("research", {})
    res["items"] = [
        {**{k: r.get(k) for k in ("title", "broker", "date", "url", "pdfUrl",
                                  "targetPrice", "opinion")},
         "note": (llm_notes[i].get("note", "") if i < len(llm_notes)
                  and isinstance(llm_notes[i], dict) else "")}
        for i, r in enumerate(reports)
    ]
    tps = [r["targetPrice"] for r in reports if r.get("targetPrice")]
    if tps:
        cons = {"avg": round(sum(tps) / len(tps)), "high": max(tps), "low": min(tps),
                "n": len(tps)}
        base = stock.get("basePrice") or stock.get("price")
        if base:
            cons["upsidePct"] = round((cons["avg"] / base - 1) * 100, 1)
        opinions = {}
        for r in reports:
            o = (r.get("opinion") or "").strip()
            if o:
                opinions[o] = opinions.get(o, 0) + 1
        cons["opinions"] = opinions
        res["consensus"] = cons
    # 증분 분석용 메타: 최신 공시 시그니처(신규 공시 감지). asOf/price 는 main 에서 채운다.
    if INCREMENTAL:
        analysis["_meta"] = {"filingKey": _filing_key(disclosures)}
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
            # Surface the LLM catalyst to the top-level field the dashboard's
            # "Market Moving Catalysts" box reads (overrides the placeholder
            # generate_report.py writes).
            if a.get("catalyst"):
                stock["catalyst"] = a["catalyst"]
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return changed


def main():
    if not llm.configured():
        print("ERROR: no LLM provider configured (set GEMINI_API_KEY, or LLM_CHAIN "
              "with a matching key)", file=sys.stderr)
        sys.exit(1)

    watchlist = load_json(WATCHLIST, [])
    peer_cfg = {k: v for k, v in (load_json(PEERS_PATH, {}) or {}).items()
                if not k.startswith("_")}

    # Pre-warm the (large) DART corpCode map once so parallel workers don't race on it.
    sources._load_corp_map()

    # 증분 모드: 리포트(이월된 analysis + 현재가 포함)를 읽어 재분석 대상만 추린다.
    now = datetime.datetime.now(KST)
    if INCREMENTAL:
        entry_by_code = {e.get("code"): e for e in (load_json(DAILY_PATH, []) or [])
                         if isinstance(e, dict)}
        targets = []
        for s in watchlist:
            need, why = _needs_analysis(s, entry_by_code.get(s["code"]), now)
            if need:
                targets.append(s)
                print(f"  [분석] {s['name']} ({s['code']}) — {why}")
            else:
                print(f"  [스킵] {s['name']} ({s['code']}) — {why}")
        print(f"=== 증분: 전체 {len(watchlist)} 중 {len(targets)}종목만 분석 "
              f"(이월 {len(watchlist) - len(targets)}) ===")
    else:
        targets = watchlist

    workers = max(1, min(CONCURRENCY, len(targets) or 1))
    print(f"=== Generating deep research for {len(targets)} stocks "
          f"({workers}-way parallel) ===")
    analysis_by_code = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(analyze_stock, s, peer_cfg): s
                   for s in targets}
        for fut in as_completed(futures):
            stock = futures[fut]
            name = stock["name"]
            try:
                a = fut.result()
                if INCREMENTAL:
                    # 증분 메타 스탬프: 분석 시각·분석 시점 현재가(가격급변 감지 기준)
                    m = a.setdefault("_meta", {})
                    m["asOf"] = now.isoformat()
                    e = entry_by_code.get(stock["code"])
                    if e and e.get("basePrice"):
                        m["price"] = e["basePrice"]
                analysis_by_code[stock["code"]] = a
                print(f"  Done {name} ({stock['code']})")
            except Exception as e:
                print(f"  Skipped {name}: {e}", file=sys.stderr)

    if not analysis_by_code:
        # 증분 모드에서 재분석 0건은 정상(이월본이 이미 리포트에 있음).
        print("재분석 대상 없음 — 이월된 분석 유지." if INCREMENTAL
              else "No analysis produced; leaving reports unchanged.", file=sys.stderr)
        return

    merge_into_report(DAILY_PATH, analysis_by_code)
    print(f"  Updated {DAILY_PATH}")

    # 장전 워치리스트만 날짜별 아카이브에 병합한다. 장중 관심종목(ANALYSIS_ARCHIVE=0)은
    # 최신본(intraday_report.json)만 갱신하므로 이 단계를 건너뛴다.
    if ARCHIVE:
        # KST, to match generate_report.py / archive_report.yml — the CI cron runs
        # at 23:00 UTC, so a UTC date would point at the wrong dated report file.
        kst = datetime.timezone(datetime.timedelta(hours=9))
        today = datetime.datetime.now(kst).strftime("%Y-%m-%d")
        dated = os.path.join(REPORTS_DIR, f"{today}.json")
        if os.path.exists(dated):
            merge_into_report(dated, analysis_by_code)
            print(f"  Updated {dated}")

    print(f"=== Done: {len(analysis_by_code)}/{len(watchlist)} stocks analyzed ===")


if __name__ == "__main__":
    main()
