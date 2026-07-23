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
import re
import sys
import json
import time
import datetime
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

import llm                           # provider-agnostic LLM with fallback chain

from analysis import sources       # imports yfinance + redirects its cache to /tmp on CI
import yfinance as yf

ROOT          = os.path.dirname(os.path.abspath(__file__))
PEERS_PATH    = os.path.join(ROOT, "analysis", "peers.json")

# Input watchlist / merge-target report are env-overridable so the same deep-research
# generator serves the 장전 워치리스트 (defaults), 장중 관심종목, 하락 관찰 pipelines:
#   ANALYSIS_WATCHLIST=intraday_watchlist.json
#   ANALYSIS_REPORT=public/intraday_report.json
#   ANALYSIS_ARCHIVE=0   # 장중은 최신본만 — 날짜별 아카이브 파일에는 병합하지 않음
#   ANALYSIS_ARCHIVE_DIR=public/reports/down   # 하락 관찰 등 별도 아카이브 디렉터리
WATCHLIST     = os.environ.get("ANALYSIS_WATCHLIST") or os.path.join(ROOT, "watchlist.json")
DAILY_PATH    = os.environ.get("ANALYSIS_REPORT")    or os.path.join(ROOT, "public", "daily_market_report.json")
REPORTS_DIR   = os.environ.get("ANALYSIS_ARCHIVE_DIR") or os.path.join(ROOT, "public", "reports")
ARCHIVE       = os.environ.get("ANALYSIS_ARCHIVE", "1") != "0"

if not os.path.isabs(WATCHLIST):
    WATCHLIST = os.path.join(ROOT, WATCHLIST)
if not os.path.isabs(DAILY_PATH):
    DAILY_PATH = os.path.join(ROOT, DAILY_PATH)
if not os.path.isabs(REPORTS_DIR):
    REPORTS_DIR = os.path.join(ROOT, REPORTS_DIR)

MAX_TOKENS  = 8192   # one full analysis is ~3.4-5.3k tokens; 4096 truncated mid-JSON
# Default low — free-tier LLM providers cap requests-per-minute; raise if you
# have headroom (each stock makes up to 2 calls: peer resolution + analysis).
CONCURRENCY = int(os.environ.get("ANALYSIS_CONCURRENCY", "3"))  # stocks processed in parallel
# 종목 1개 안에서 수집 소스들을 동시에 부르는 워커 수(2026-07-22 온디맨드 속도 개선).
# 배치에서는 CONCURRENCY(3) × 이 값이 최대 동시 외부 호출 — 소스별 API 가 달라 충돌 낮음.
FETCH_WORKERS = int(os.environ.get("ANALYSIS_FETCH_WORKERS", "8"))
# 동적 peer resolve(LLM+ticker 검증) 결과의 프로세스 내 캐시 — 온디맨드 상주 서버에서
# 6h 결과 캐시 만료 후 재조회 시 LLM 1회·yfinance 1회 절약. 배치에도 무해.
_PEER_CACHE = {}

KST = datetime.timezone(datetime.timedelta(hours=9))

# 증분 분석(장중 비용 절감): 켜지면 이미 분석된 종목은 재분석하지 않고 이월본을 유지하되,
#   ① TTL 경과(ANALYSIS_TTL_HOURS) ② 지정 대비 가격 급변(ANALYSIS_PRICE_MOVE_PCT)
#   ③ 신규 공시 감지 시에만 다시 분석한다. (generate_report 의 REPORT_CARRY_ANALYSIS 와 짝)
INCREMENTAL    = os.environ.get("ANALYSIS_INCREMENTAL") == "1"
TTL_HOURS      = float(os.environ.get("ANALYSIS_TTL_HOURS", "2"))
PRICE_MOVE_PCT = float(os.environ.get("ANALYSIS_PRICE_MOVE_PCT", "5"))

# 조건부 후속 라운드(하이브리드, 2026-07-23): 이상 신호(최근 공시 키워드·공매도 비중 급증·
# 목표주가 컨센서스 괴리)가 감지된 종목만, 1차 분석에서 LLM 이 추가 조사(화이트리스트 도구
# 최대 N건)를 요청할 수 있고 코드가 실행해 2차 호출로 최종본을 만든다. 트리거 미감지 종목은
# 기존 단일 호출 경로와 완전 동일(스키마·프롬프트·비용 불변). 기본 off — Railway 온디맨드만
# 켜는 것을 전제(배치는 종목수 × 2차 호출이라 무료 쿼터 압박).
FOLLOWUP_ENABLE          = os.environ.get("FOLLOWUP_ENABLE") == "1"
FOLLOWUP_MAX_QUERIES     = int(os.environ.get("FOLLOWUP_MAX_QUERIES", "3"))
FOLLOWUP_DISCLOSURE_DAYS = int(os.environ.get("FOLLOWUP_DISCLOSURE_DAYS", "7"))
FOLLOWUP_SHORT_PCT       = float(os.environ.get("FOLLOWUP_SHORT_PCT", "10"))
FOLLOWUP_GAP_PCT         = float(os.environ.get("FOLLOWUP_GAP_PCT", "30"))
FOLLOWUP_TOOL_TIMEOUT    = float(os.environ.get("FOLLOWUP_TOOL_TIMEOUT", "10"))
# 상승·하락 재료 '충돌' 트리거: 방향성 신호(방향성 공시·공매도·컨센서스 괴리·외인/기관
# 동반 수급)가 양방향 동시 존재하면 개별 단독 임계치 미달이어도 게이트 통과 — 상반 재료의
# 우세 판정이야말로 후속 조사 가치가 가장 크다. 충돌용 임계치는 단독 트리거보다 낮게.
FOLLOWUP_CONFLICT_SHORT_PCT = float(os.environ.get("FOLLOWUP_CONFLICT_SHORT_PCT", "7"))
FOLLOWUP_CONFLICT_GAP_PCT   = float(os.environ.get("FOLLOWUP_CONFLICT_GAP_PCT", "15"))

# 수급(투자자 매매동향) 프록시 — KIS 허브 토큰을 공유하는 모니터 서버(/flow).
# 일별 개인/외국인/기관계/기금 순매수 대금(백만원) + 당일 가집계(잠정) 랭킹.
FLOW_API_BASE = os.environ.get(
    "FLOW_API_BASE", "https://tradingstrategies-production-09d4.up.railway.app")

SYSTEM = """\
너는 한국 주식 투자 리서치 애널리스트다. 주어진 RAW 데이터(해외 peer 시세, peer 그룹 Reddit
여론, 국내외 뉴스, 국내 커뮤니티 글, DART 공시/재무, 증권사 종목분석 리포트, 투자자 수급)를
분석해 대시보드용 `analysis` JSON 한 개를 생성한다.

규칙:
- 모든 서술형 필드는 한국어. 회사명/티커/기사 제목/URL은 원문 유지.
- sentiment 값은 정확히 "긍정" | "부정" | "중립" 중 하나.
- RAW에 없는 사실을 지어내지 말 것. 자료가 빈약하면 그 점을 summary에 명시하고 배열을 줄여라.
- 각 insight/note는 투자자 관점의 한 줄 해석.
- 출력은 응답 형식(json_schema)으로 강제된다. 스키마에 정의된 키만 채운다.

스키마:
{
  "catalyst": "오늘 이 종목의 주가를 움직일 핵심 촉매 2-3문장",
  "direction": "bullish|neutral|bearish",
  "flowComment": "수급(투자자별 순매수·공매도·대차잔고) 해석 2-3문장",
  "peers": { "summary": "2-3문장", "reddit": [ {"title","url","subreddit","sentiment","summary"} ] },
  "news": { "summary": "3-4문장", "items": [ {"title","source","date","sentiment","url","insight"} ] },
  "community": { "summary": "3-4문장", "sentimentLabel": "", "naver": [ {"title","url","sentiment","summary"} ] },
  "dart": { "summary": "3-4문장", "highlights": [ {"label","value","note"} ] },
  "research": { "summary": "2-3문장", "items": [ {"note"} ] }
}

- investor_flow(입력): today_rank 는 당일 장중 가집계(잠정) 순매수/순매도 랭킹(거래대금,
  단위 백만원, 음수=순매도). 외국인·기관 동반 순매수/순매도와 랭킹 진입은 catalyst·direction
  판단의 주요 근거로 활용하라. 단, 잠정치(today_rank)는 '가집계'임을 감안한다.
  naver_trend(네이버 일자별): frgn/orgn/prsn=외국인/기관/개인 순매매량(주식 수, 음수=순매도),
  holdRatio=외국인 보유율%. 일자별 투자자 매매 추세(누가 사고 누가 파는지, 연속 매도 후
  첫 매수 같은 전환 여부)는 이것으로 해석하라. 외국인 보유율의 추세적 상승/하락도 수급 신호다.
  shorts_recent(공매도 일별: pbmn=공매도 대금 억원, pbmnRlim/volRlim=거래대금/거래량 비중%)와
  loans_recent(대차거래 일별: newQty=체결, rdmpQty=상환, rmndChg=잔고 증감 주수, rmndAmt=잔고 억원)도
  하방 압력 신호로 활용하라 — 공매도 비중 급증(예: 거래대금의 10%+)과 대차잔고 누증은 하방 압력,
  잔고 급감은 숏커버 가능성. 수치는 입력값을 그대로 인용하고 지어내지 말 것.
- flowComment: investor_flow 를 해석한 2-3문장 — ① 투자자별 순매수 추세(누가 사고 누가 파는지,
  추세 전환 여부) ② 공매도 비중 수준·급증 여부 ③ 대차잔고 방향(누증/상환 우위)을 투자자 관점으로.
  대시보드 '수급' 탭 상단에 그대로 노출된다. 유의미한 수치(억원·%·주)를 1-2개 인용하라.
  investor_flow 가 입력에 없으면 정확히 "수급 데이터 없음" 이라고만 쓴다.
- catalyst 에도 investor_flow 의 유의미한 신호(외국인·기관 동반 매매, 공매도 비중 급증, 대차잔고
  급변, 가집계 랭킹 진입)가 있으면 구체 수치와 함께 반영하라 — 수급이 오늘의 지배적 동인이면
  catalyst 의 첫 문장으로 쓴다.
- catalyst: 뉴스·공시·수급·peer 동향 중 '오늘 주가를 가장 크게 움직일' 단일 촉매를 투자자 관점으로
  요약한다. 대시보드의 'Market Moving Catalysts'에 그대로 노출되므로 placeholder/메타설명을 쓰지 말고
  구체적 내용으로 채운다. 근거가 빈약하면 거래대금·모멘텀 등 가격 동향 기반으로 신중히 서술한다.
- direction: 위 catalyst 가 시사하는 '오늘의 지배적 방향' 판정. 상방 재료가 지배적이면 "bullish",
  하방 재료(악재·기대감 소멸·규제·실적 쇼크 등)가 지배적이면 "bearish", 혼재/불분명하면 "neutral".
  bearish 로 판정하면 자동매매가 이 종목의 신규 매수를 건너뛴다 — catalyst 서술과 방향이
  일치해야 한다.
- peers.reddit: 입력 peers_reddit(해외 peer/섹터에 대한 영어권 Reddit 글)을 바탕으로 3-5개.
  해외 peer 그룹에 대한 여론·논점을 요약한다. title/url/subreddit은 원문, summary는 한국어 한 줄.
  한국 종목이 직접 언급되지 않으면 peer/섹터 맥락으로 해석하고 summary에 그 점을 밝힌다.
- community: 네이버 카페(naver_cafe)와 종목토론방(naver_board) 글로 '국내 개인투자자' 여론만
  담는다(Reddit 제외). naver 3-5개에는 종목토론방 글을 우선 담고, 공감/비공감(agree/disagree)
  수로 여론의 강도·쏠림을 가늠해 summary와 sentimentLabel에 반영한다.
- dart: summary와 highlights(4-6개)만 작성한다. **recentFilings는 만들지 마라** — 코드가 DART
  최신 공시 5건을 결정적으로 채운다.
- research: 입력 research_reports(증권사 리포트, 최근 60일)를 바탕으로 summary 2-3문장
  (목표주가 컨센서스의 방향·의견 분포·공통 논거 포함)과, items 를 **입력 research_reports 와
  같은 순서·같은 개수**로 작성한다. 각 item 은 그 리포트의 투자자 관점 한 줄 해석(note)만 담는다.
  **목표주가·의견·증권사·날짜를 items 에 쓰지 마라** — 코드가 원문 값을 결정적으로 채운다.
  research_reports 가 비어 있으면 summary 에 "최근 60일 발간 리포트 없음"을 명시하고 items 는 빈 배열.
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
    "direction": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
    "flowComment": _STR,
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

# 후속 라운드 1차 호출용 스키마 변형: 기존 필드 + followUp(추가 조사 요청).
# 트리거 감지 종목에만 쓰인다 — 평상시 경로는 ANALYSIS_SCHEMA 그대로.
FOLLOWUP_TOOLS = ("news_search", "web_search", "dart_history", "board_deep")

ANALYSIS_SCHEMA_FU = _obj({
    **ANALYSIS_SCHEMA["properties"],
    "followUp": _obj({
        "needed": {"type": "boolean"},
        "reason": _STR,
        "queries": _arr({"tool": {"type": "string", "enum": list(FOLLOWUP_TOOLS)},
                         "query": _STR}),
    }),
})

FOLLOWUP_SYSTEM_EXTRA = """

[추가 조사 요청(followUp) — 이 종목은 코드가 이상 신호를 감지했다]
- 감지된 트리거: {triggers}
- RAW 만으로 catalyst/direction 판단이 불충분하면 followUp.needed=true 로 하고, 확인할
  내용을 queries(최대 {n}건)로 요청하라. RAW 로 충분하면 needed=false — 추가 조사는
  지연·비용이 들므로 트리거를 규명하는 데 꼭 필요한 질의만 요청한다.
- 사용 가능한 tool:
  news_search(query=한국어 검색어): 네이버 뉴스 재검색 — 트리거 관련 구체 키워드로.
  web_search(query=영어 검색어): 해외 웹 — peer·원자재·섹터 맥락.
  dart_history(query=""): 이 회사의 과거 1년 공시 목록 — 반복 유증·CB 이력 확인용.
  board_deep(query=""): 종목토론방 5페이지 — 개인투자자 여론 심층.
- 트리거가 conflict(상승·하락 재료 동시 존재)면, 어느 쪽 재료가 지배적인지 가릴 수 있는
  질의를 우선하라 — 최종 direction 판정과 catalyst 에 그 근거를 명시한다.
- reason 에는 무엇을 왜 확인하려는지 한 줄. needed=false 면 reason=""·queries=[]."""

# 2차(refine) 호출: RAW 재전송 없이 1차 분석 + 후속 수집 결과만으로 최종본을 만든다(A안).
REFINE_SYSTEM = SYSTEM + """

[2차 호출 — 후속 조사 반영]
- 이번 입력은 RAW 대신 ① 네가 작성한 1차 분석(JSON) ② 추가 수집 결과(followUpResults)다.
- followUpResults 가 1차 판단을 바꾸면 catalyst/direction 등 해당 필드에 구체 근거와 함께
  반영하고, 바꾸지 않으면 1차 내용을 유지한다(불필요한 재서술 금지).
- research.items 는 1차와 같은 개수·순서를 유지한다(코드가 원문 값을 채우는 규칙 동일).
- followUpResults 의 수치·사실만 인용하고 지어내지 말 것. followUp 필드는 출력하지 않는다."""


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
    tops = peer_list[:2]                         # top 2 peers (bellwethers)
    # 두 peer 검색은 독립 — 병렬 수집(2026-07-22 온디맨드 속도 개선)
    with ThreadPoolExecutor(max_workers=2) as ex:
        for res in ex.map(lambda p: sources.web_search(
                f"{p['name']} stock discussion", max_results=3,
                include_domains=["reddit.com"]), tops):
            posts += res
    if not posts:                                # residential-only fallback
        for p in tops:
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
                         progress=False, threads=True, auto_adjust=True, timeout=15)
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
    if code in _PEER_CACHE:
        return _PEER_CACHE[code]                  # 프로세스 내 resolve 캐시(LLM·검증 절약)
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
    _PEER_CACHE[code] = valid    # LLM 성공 결과만 캐시(예외 경로는 다음 호출에서 재시도)
    return valid


def _norm_opinion(o):
    """증권사별 투자의견 표기(매수/Buy/BUY/Outperform 등)를 매수·중립·매도로 정규화.
    컨센서스 의견 분포 집계용 — 리포트 개별 표시는 원문을 유지한다."""
    s = (o or "").strip().lower().replace(" ", "").replace("-", "")
    if not s:
        return ""
    if any(k in s for k in ("buy", "매수", "outperform", "overweight", "비중확대", "적극", "상회")):
        return "매수"
    if any(k in s for k in ("sell", "underperform", "underweight", "매도", "비중축소", "reduce", "하회")):
        return "매도"
    if any(k in s for k in ("hold", "neutral", "중립", "marketperform", "시장수익률")):
        return "중립"
    return (o or "").strip()


def fetch_investor_flow(code, timeout=12):
    """KIS 허브 프록시(/flow)에서 종목 수급 — 당일 가집계(잠정) 랭킹 + 공매도/대차 일별.
    KIS 일별 대금(daily)은 시간제한(00:00~15:40 차단)으로 장중 공백이라 미사용 —
    일자별 투자자 동향은 네이버 trend 로 일원화(2026-07-16). 실패 시 빈 dict."""
    try:
        r = requests.get(f"{FLOW_API_BASE}/flow", params={"code": code}, timeout=timeout)
        r.raise_for_status()
        d = r.json()
        if d.get("status") == "success":
            return {"rank": d.get("rank") or [],
                    "shorts": d.get("shorts") or [], "loans": d.get("loans") or [],
                    "rankAsof": d.get("rankAsof", "")}
    except Exception as e:
        print(f"[flow] {code} 수급 조회 실패: {e}", file=sys.stderr)
    return {}


def _fetch_base_price(stock):
    """온디맨드 경로(가격 미포함)용 현재가 — yfinance fast_info. 리포트 경로처럼
    이미 가격이 있으면 네트워크 없이 None(호출부가 기존 가격 우선 사용)."""
    if stock.get("basePrice") or stock.get("price"):
        return None
    suffix = ".KS" if stock.get("market") == "KOSPI" else ".KQ"
    px = yf.Ticker(f"{stock['code']}{suffix}").fast_info.last_price
    return float(px) if px else None


# 희석·지배구조·구조 변경 등 '공시 원문까지 파야 판단되는' 유형 — 단독 트리거.
_FOLLOWUP_KW = re.compile(
    r"유상증자|전환사채|신주인수권|교환사채|최대주주|합병|분할|감자|조회공시|공급계약|소송")
# 방향성 공시(충돌 감지용): 단독 트리거와 별개로 상승/하락 재료를 분류한다.
_FU_KW_BULL = re.compile(r"공급계약|단일판매|수주|무상증자|자기주식|자사주")
_FU_KW_BEAR = re.compile(r"유상증자|전환사채|신주인수권|교환사채|감자|소송|횡령|배임")


def _followup_triggers(raw, flow, stock, base_px):
    """후속 라운드 트리거 판정(결정적 규칙, 추가 네트워크 0) → 사유 문자열 리스트.

    이미 수집된 raw/flow 만 본다. 빈 리스트면 기존 단일 호출 경로 그대로 진행.
    단독 트리거: ① 최근 N일 내 공시 제목 키워드 ② 공매도 거래대금 비중 급증
    ③ 목표주가 컨센서스와 현재가의 큰 괴리.
    충돌 트리거: 방향성 신호(방향성 공시·공매도·괴리 부호·외인/기관 3일 동반 수급)를
    상승/하락으로 분류해 양방향이 동시에 있으면 — 개별 신호가 단독 임계치 미달이어도 —
    conflict 로 게이트를 통과시킨다(상반 재료의 우세 판정이 후속 조사의 핵심 가치).
    """
    trig, bulls, bears = [], [], []
    today = datetime.datetime.now(KST).date()

    # ① 공시: 단독 키워드 트리거(첫 건) + 방향성 분류(충돌용, 각 방향 첫 건)
    solo_hit = False
    for d in ((raw.get("dart") or {}).get("disclosures") or [])[:5]:
        title = d.get("title", "")
        try:  # date 는 sources._fmt_date 형식(YYYY-MM-DD) — 오래된 공시가 계속 걸리지 않게
            if (today - datetime.date.fromisoformat(d.get("date", ""))).days \
                    > FOLLOWUP_DISCLOSURE_DAYS:
                continue
        except ValueError:
            continue
        m = _FOLLOWUP_KW.search(title)
        if m and not solo_hit:
            trig.append(f"disclosure:{m.group(0)}({d.get('date', '')})")
            solo_hit = True
        mb = _FU_KW_BULL.search(title)
        if mb and not any(b.startswith("공시") for b in bulls):
            bulls.append(f"공시({mb.group(0)})")
        ms = _FU_KW_BEAR.search(title)
        if ms and not any(b.startswith("공시") for b in bears):
            bears.append(f"공시({ms.group(0)})")

    # ② 공매도 비중: 단독(급증) + 충돌용(완화 임계치, 하락 신호)
    ratios = []
    for s in ((flow or {}).get("shorts") or [])[:3]:
        try:
            ratios.append(float(s.get("pbmnRlim") or 0))
        except (TypeError, ValueError):
            pass
    short_r = max(ratios) if ratios else 0.0
    if short_r >= FOLLOWUP_SHORT_PCT:
        trig.append(f"short_surge:{short_r:.1f}%")
    if short_r >= FOLLOWUP_CONFLICT_SHORT_PCT:
        bears.append(f"공매도{short_r:.1f}%")

    # ③ 컨센서스 괴리: 단독(절대값) + 충돌용(부호 → 방향 신호)
    tps = [r.get("targetPrice") for r in (raw.get("research_reports") or [])
           if r.get("targetPrice")]
    base = stock.get("basePrice") or stock.get("price") or base_px
    if tps and base:
        gap = (sum(tps) / len(tps) / base - 1) * 100
        if abs(gap) >= FOLLOWUP_GAP_PCT:
            trig.append(f"consensus_gap:{gap:+.0f}%")
        if gap >= FOLLOWUP_CONFLICT_GAP_PCT:
            bulls.append(f"괴리{gap:+.0f}%")
        elif gap <= -FOLLOWUP_CONFLICT_GAP_PCT:
            bears.append(f"괴리{gap:+.0f}%")

    # ④ 외국인·기관 동반 수급(네이버 trend, 최신순·전일까지 확정): 3일 연속 동반
    #    순매수/순매도만 방향 신호로(단발 소량 순매매 노이즈 배제).
    t3 = [(r.get("frgn"), r.get("orgn"))
          for r in ((flow or {}).get("trend") or [])[:3]]
    if len(t3) == 3 and all(f is not None and o is not None for f, o in t3):
        if all(f > 0 and o > 0 for f, o in t3):
            bulls.append("동반순매수3일")
        elif all(f < 0 and o < 0 for f, o in t3):
            bears.append("동반순매도3일")

    # ⑤ 충돌: 상승·하락 재료 동시 존재 → 트리거 (단독 임계치와 무관)
    if bulls and bears:
        trig.append(f"conflict:{'+'.join(bulls)} vs {'+'.join(bears)}")
    return trig


def _followup_fetch(queries, code, corp):
    """화이트리스트 도구 실행 — 건당 타임아웃, 실패/빈 결과 leg 는 제외.

    반환: [{tool, query, results}] (refine 호출의 followUpResults 입력).
    타임아웃된 leg 의 워커는 버려두고 진행한다(shutdown wait=False) — 후속 라운드가
    온디맨드 응답을 도구 하나 때문에 붙잡지 않게.
    """
    def _one(q):
        tool, query = q["tool"], (q.get("query") or "").strip()
        if tool == "news_search":
            return sources.naver_search("news", query, display=8) if query else []
        if tool == "web_search":
            return sources.web_search(query, max_results=5) if query else []
        if tool == "dart_history":
            return sources.dart_disclosures(corp, days=365) if corp else []
        if tool == "board_deep":
            return sources.naver_board(code, pages=5)
        return []

    out = []
    pool = ThreadPoolExecutor(max_workers=min(3, len(queries)) or 1)
    deadline = time.perf_counter() + FOLLOWUP_TOOL_TIMEOUT  # 라운드 전체 상한(건당 아님)
    try:
        for q, fut in [(q, pool.submit(_one, q)) for q in queries]:
            try:
                res = fut.result(timeout=max(0.1, deadline - time.perf_counter()))
                if res:
                    out.append({"tool": q["tool"], "query": q.get("query", ""),
                                "results": res})
            except Exception as e:
                print(f"[research] {code} followup {q.get('tool')} 실패/타임아웃: {e}",
                      file=sys.stderr)
    finally:
        pool.shutdown(wait=False)
    return out


def analyze_stock(stock, peer_cfg):
    code, name = stock["code"], stock["name"]
    t0 = time.perf_counter()

    # ── 수집 병렬화(2026-07-22): 기존 완전 순차(외부 호출 ~20개 합산 30-60s)를 futures
    #    그래프로 — wall time = 최장 leg. 실패한 leg 는 기존 graceful 과 동일한 빈 기본값.
    #    의존성 체인(peer→quotes/reddit, DART corp→3종)은 메인 스레드가 해소한다 —
    #    풀 태스크가 같은 풀의 결과를 기다리지 않게(워커 고갈 데드락 방지).
    durations = {}

    def _timed(label, fn, *a, **kw):
        def run():
            t = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                durations[label] = time.perf_counter() - t
        return run

    def _res(fut, label, default):
        try:
            return fut.result()
        except Exception as e:
            print(f"[research] {code} {label} 실패(빈 값 대체): {e}", file=sys.stderr)
            return default

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        def sub(label, fn, *a, **kw):
            return pool.submit(_timed(label, fn, *a, **kw))

        f_peers = sub("peers", resolve_peers, stock, peer_cfg)
        f_news  = sub("naver_news", sources.naver_search, "news", name, display=8)
        f_osn   = sub("overseas_news", sources.web_search, f"{name} stock news", max_results=5)
        f_cafe  = sub("naver_cafe", sources.naver_search, "cafearticle", name, display=6)
        f_board = sub("naver_board", sources.naver_board, code, pages=2)
        f_rsch  = sub("research", sources.combined_research, code, days=60)
        f_flow  = sub("flow", fetch_investor_flow, code)
        f_trend = sub("trend", sources.naver_investor_trend_full, code)
        f_px    = sub("base_price", _fetch_base_price, stock)   # 온디맨드 컨센서스 괴리용

        # DART: corp 코드는 정적 맵이라 ~0초 — 메인 스레드에서 확정 후 3종 병렬 제출
        dart_futs = {}
        try:
            corp = sources.dart_corp_code(code)
        except Exception as e:
            print(f"[research] {code} dart_corp 실패: {e}", file=sys.stderr)
            corp = None
        if corp:
            dart_futs = {
                "financials":    sub("dart_financials", sources.dart_financials, corp),
                "disclosures":   sub("dart_disclosures", sources.dart_disclosures, corp),
                "major_holders": sub("dart_major_holders", sources.dart_major_holders, corp),
            }

        # peer 확정(동적 종목은 LLM+ticker 검증 — 다른 leg 들과 병렬 진행) 후
        # peer 의존 2건(시세·Reddit) 제출
        peer_list = _res(f_peers, "peers", [])
        f_quotes = sub("peer_quotes", sources.get_peer_quotes, peer_list)
        f_reddit = sub("peer_reddit", fetch_peer_reddit, peer_list)

        peers = _res(f_quotes, "peer_quotes", [])
        raw = {
            "stock": {"code": code, "name": name, "market": stock["market"]},
            "peers_items": peers,                                   # deterministic, reused verbatim
            "peers_reddit": _res(f_reddit, "peer_reddit", []),      # Reddit on the peer group
            "naver_news": _res(f_news, "naver_news", []),
            "overseas_news": _res(f_osn, "overseas_news", []),
            "naver_cafe": _res(f_cafe, "naver_cafe", []),           # 국내 community
            "naver_board": _res(f_board, "naver_board", []),        # 종목토론방(개인투자자)
            "research_reports": _res(f_rsch, "research", []),       # 증권사 리포트(네이버+FnGuide)
            "dart": {},
        }
        if dart_futs:
            raw["dart"] = {k: _res(f, k, {} if k == "financials" else [])
                           for k, f in dart_futs.items()}
        # 투자자 수급(KIS): LLM 프롬프트에는 최근 10거래일 + 당일 가집계 랭킹만 (경량화).
        # 전체(20일)는 아래에서 analysis["flow"] 로 결정적으로 실어 대시보드가 렌더링.
        flow = _res(f_flow, "flow", {})
        # 네이버 일자별 매매동향(수량·보유율): KIS daily(대금)가 장중 시간제한으로 비는
        # 구간을 메운다 — 전일까지 확정치라 장중에도 항상 조회 가능.
        trend = _res(f_trend, "trend", [])
        base_px = _res(f_px, "base_price", None)

    fetch_s = time.perf_counter() - t0
    slow = max(durations.items(), key=lambda kv: kv[1]) if durations else ("-", 0.0)

    if trend:
        flow = dict(flow) if flow else {}
        flow["trend"] = trend
    if any(flow.get(k) for k in ("rank", "shorts", "loans", "trend")):
        raw["investor_flow"] = {"today_rank": flow.get("rank") or [],
                                "shorts_recent": (flow.get("shorts") or [])[:10],
                                "loans_recent": (flow.get("loans") or [])[:10],
                                "naver_trend": (flow.get("trend") or [])[:10]}

    user = (f"종목: {stock['name']} ({stock['code']}, {stock['market']})\n"
            f"RAW 데이터(JSON):\n{json.dumps(raw, ensure_ascii=False)}")

    # 조건부 후속 라운드: 트리거 감지 종목만 followUp 필드가 붙은 스키마·프롬프트로 1차 호출.
    # 미감지(대부분)는 기존 경로와 완전 동일.
    triggers = _followup_triggers(raw, flow, stock, base_px) if FOLLOWUP_ENABLE else []
    system, schema = SYSTEM, ANALYSIS_SCHEMA
    if triggers:
        system = SYSTEM + FOLLOWUP_SYSTEM_EXTRA.format(
            triggers="; ".join(triggers), n=FOLLOWUP_MAX_QUERIES)
        schema = ANALYSIS_SCHEMA_FU

    # llm.generate_json returns parsed JSON from the first chain link that
    # succeeds (auto-failover on quota/billing/rate-limit). Providers that
    # support it enforce the schema; all return valid, escaped JSON.
    t1 = time.perf_counter()
    analysis, model_used = llm.generate_json(
        system, user, max_tokens=MAX_TOKENS, schema=schema, return_model=True)
    llm1_s = time.perf_counter() - t1

    # 2라운드(A안): 모델이 요청한 후속 질의를 실행하고, RAW 재전송 없이 1차 분석 + 추가
    # 수집 결과만으로 최종본을 다시 받는다. 출력 스키마는 followUp 없는 기존 스키마 —
    # 3라운드는 구조적으로 불가. 어떤 실패든 1차 분석 유지(graceful).
    fu_req = analysis.pop("followUp", None) or {}
    fu_meta = None
    if triggers and fu_req.get("needed") and fu_req.get("queries"):
        t2 = time.perf_counter()
        queries = [q for q in fu_req["queries"]
                   if q.get("tool") in FOLLOWUP_TOOLS][:FOLLOWUP_MAX_QUERIES]
        fu_results = _followup_fetch(queries, code, corp)
        if fu_results:
            refine_user = (
                f"종목: {stock['name']} ({stock['code']}, {stock['market']})\n"
                f"1차 분석(JSON):\n{json.dumps(analysis, ensure_ascii=False)}\n"
                f"추가 수집 결과 followUpResults(JSON):\n"
                f"{json.dumps(fu_results, ensure_ascii=False)}")
            try:
                analysis, model_used = llm.generate_json(
                    REFINE_SYSTEM, refine_user, max_tokens=MAX_TOKENS,
                    schema=ANALYSIS_SCHEMA, return_model=True)
                fu_meta = {"triggers": triggers, "reason": fu_req.get("reason", ""),
                           "queries": queries,
                           "seconds": round(time.perf_counter() - t2, 1)}
            except llm.LLMError as e:
                print(f"[research] {code} followup refine 실패(1차 유지): {e}",
                      file=sys.stderr)

    analysis["generatedBy"] = model_used   # surfaced on the dashboard
    if fu_meta:
        analysis["followUp"] = fu_meta     # 대시보드 배지·트리거율 튜닝용 메타
    # 단계별 계측(온디맨드 서버·Actions 로그 공용) — 병목 진단용
    fu_note = ""
    if FOLLOWUP_ENABLE:
        fu_note = " followup=" + ("+".join(t.split(":", 1)[0] for t in triggers)
                                  if triggers else "miss")
        if fu_meta:
            fu_note += f" fu={fu_meta['seconds']}s({len(fu_meta['queries'])}건)"
    print(f"[research] {code} fetch={fetch_s:.1f}s(최장 {slow[0]} {slow[1]:.1f}s) "
          f"llm={llm1_s:.1f}s({model_used}){fu_note} "
          f"total={time.perf_counter() - t0:.1f}s", file=sys.stderr)

    # Force deterministic peer items (LLM only authored peers.summary/peers.reddit).
    analysis.setdefault("peers", {})["items"] = peers

    # 수급 원본(최근 20거래일 + 가집계 랭킹)은 LLM 을 거치지 않고 그대로 싣는다 — 수치 환각 방지.
    if flow:
        analysis["flow"] = flow

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
        # 온디맨드(Railway) 경로는 가격 없이 들어온다 — 수집 병렬 단계에서 미리 받아둔
        # 현재가(base_px)로 보강 (기존엔 여기서 순차 yfinance 호출, 2026-07-22 이동).
        base = stock.get("basePrice") or stock.get("price") or base_px
        if base:
            cons["upsidePct"] = round((cons["avg"] / base - 1) * 100, 1)
        opinions = {}
        for r in reports:
            o = _norm_opinion(r.get("opinion"))
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
            # 방향 판정 — bearish 면 대시보드 자동매매가 신규 매수를 건너뛴다.
            d = str(a.get("direction", "")).lower()
            if d in ("bullish", "neutral", "bearish"):
                stock["direction"] = d
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

    # 리포트의 현재가(basePrice)를 종목 dict 에 주입 — 증권사 리포트 목표주가
    # 컨센서스의 '현재가 대비 괴리(upsidePct)' 계산에 필요 (watchlist.json 엔 가격이 없다).
    _price_by_code = {e.get("code"): e.get("basePrice")
                      for e in (load_json(DAILY_PATH, []) or []) if isinstance(e, dict)}
    for s in watchlist:
        if not s.get("basePrice") and _price_by_code.get(s.get("code")):
            s["basePrice"] = _price_by_code[s["code"]]

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
