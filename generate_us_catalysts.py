#!/usr/bin/env python3
"""미국 야간 촉매(SEC 공시 + 시장 뉴스) 수집 → public/us_catalysts.json

모닝브리프의 미국 정보(지수·섹터·무버 수치)에 '무슨 일이 있었는지'를 보태는
장중 시황 촉매의 미국판. us_night_catalysts.yml 이 미국 프리~애프터마켓
(KST 20:47~06:17) 동안 30분 간격으로 실행한다(Railway _scheduler dispatch +
GH cron 백스톱 — 실적 8-K 는 대부분 프리마켓(KST 19~22시)/애프터마켓(KST 05~06시)
접수라 정규장만 돌면 구조적으로 놓친다, 2026-07-24 확장).

동작 — 두 레그가 독립적으로 세션 파일에 누적한다:
  [SEC 레그]
  1. 유니버스 = screener.SECTOR_US_STOCKS + analysis/peers.json 의 미국 상장 티커
     (접미사 없는 심볼만 — .T/.TW/.HK 등 비미국 상장은 SEC 미대상). 약 50~70개.
  2. SEC 공식 ticker→CIK 맵(company_tickers.json, 무료)으로 CIK 집합 구성.
  3. edgartools get_current_filings 로 최신 8-K/6-K 피드를 받아 유니버스 교집합 중
     '이번 세션에서 아직 못 본'(seen accession) 신규분만 추린다.
  4. 신규분은 공시 원문(주 문서 + Ex-99 보도자료 첨부)을 SEC Archives 에서 직접
     내려받아 텍스트 발췌(bodyExcerpt) — 실적 수치·계약 내용이 요약에 실리는 핵심.
  5. yfinance 당일 등락률 보강 → LLM 1콜 한국어 요약·방향 판정 → append.
  [뉴스 레그] (2026-07-24 신설 — 지정학·매크로 등 SEC 공시로 안 잡히는 촉매)
  6. 네이버 뉴스(뉴욕증시·미국증시) + yfinance SPY/QQQ 헤드라인 + Tavily 웹검색으로
     미확인 헤드라인 수집 → LLM 1콜로 '시장을 실제로 움직인 사건'만 최대 3건 선별
     (리캡·전망·기보고 사건 제외) → kind="news" 로 append.
신규 0건 회차는 LLM·커밋 없음(비용 절감 핵심). 세션 = 미국 거래일(KST 17시 이후
시작 날짜). 새 세션 첫 회차에서 파일을 리셋한다.
실패 내성: 모든 단계 graceful — 한 레그가 죽어도 다른 레그는 진행, 기존 파일 보존.

Env: SEC_IDENTITY("이름 이메일" — SEC fair-use 식별, 키 아님), GEMINI_API_KEY 등 LLM 체인,
     NAVER_CLIENT_ID/SECRET·TAVILY_API_KEY(뉴스 레그 — 없으면 해당 소스만 조용히 생략).
의존성: edgartools 는 이 워크플로에서만 설치(requirements.txt 미포함 — pandas 드리프트 격리).
"""
import os
import re
import sys
import html as htmllib
import json
import datetime

import requests

import llm
from analysis import sources
from screener import SECTOR_US_STOCKS

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "public", "us_catalysts.json")
PEERS_PATH = os.path.join(ROOT, "analysis", "peers.json")
KST = datetime.timezone(datetime.timedelta(hours=9))

# or 폴백: Actions 에서 vars 미설정 시 env 가 '빈 문자열'로 들어와 get() 기본값이
# 안 먹힌다 — 빈 UA 는 SEC 가 즉시 403 (2026-07-23 밤 13회차 전멸 원인 1).
# 개인 이메일 하드코딩 금지(public 레포) — 실 연락처는 Actions vars.SEC_IDENTITY 로.
SEC_IDENTITY = (os.environ.get("SEC_IDENTITY")
                or "QuantAntigravity jykim0048@users.noreply.github.com")
FORMS = ("8-K", "6-K")      # 6-K: TSMC·ASML 등 외국계 미국상장(peer 다수)의 주요 공시
MAX_TOKENS = 2048


def _log(msg):
    print(f"[us-catalysts] {msg}")


def session_key(now=None):
    """미국 거래일 키 — KST 17시 이후 시작하는 밤 세션은 그날 날짜, 새벽은 전날 날짜."""
    now = now or datetime.datetime.now(KST)
    d = now.date() if now.hour >= 17 else now.date() - datetime.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def build_universe():
    """{ticker: 표시명} — SECTOR_US_STOCKS + peers.json 미국 티커(중복 제거)."""
    uni = {}
    for lst in SECTOR_US_STOCKS.values():
        for tkr, name in lst:
            uni.setdefault(tkr.upper(), name)
    try:
        with open(PEERS_PATH, encoding="utf-8") as f:
            peers = json.load(f) or {}
        for plist in peers.values():
            if not isinstance(plist, list):
                continue                                   # "_comment" 등 메타 키 스킵
            for p in plist:
                if not isinstance(p, dict):
                    continue
                t = str(p.get("ticker") or "").upper()
                if t and "." not in t and t.isalnum():     # 미국 상장(접미사 없음)만
                    uni.setdefault(t, p.get("name") or t)
    except Exception as e:
        _log(f"peers.json 로드 실패(섹터 대표주만 사용): {e}")
    return uni


def cik_map(tickers):
    """SEC 공식 ticker→CIK 맵(company_tickers.json) — {cik(int): ticker}."""
    r = requests.get("https://www.sec.gov/files/company_tickers.json",
                     headers={"User-Agent": SEC_IDENTITY}, timeout=20)
    r.raise_for_status()
    want = {t.upper() for t in tickers}
    out = {}
    for row in (r.json() or {}).values():
        t = str(row.get("ticker") or "").upper()
        if t in want:
            out[int(row["cik_str"])] = t
    return out


def scan_new_filings(ciks, seen):
    """edgartools 최신 피드에서 유니버스 CIK 의 미확인 8-K/6-K 를 추린다."""
    from edgar import set_identity, get_current_filings
    set_identity(SEC_IDENTITY)
    found = []
    for form in FORMS:
        try:
            feed = get_current_filings(form=form, page_size=100)
        except Exception as e:
            _log(f"current filings({form}) 조회 실패: {e}")
            continue
        for f in feed:
            try:
                cik = int(getattr(f, "cik", 0) or 0)
                if cik not in ciks:
                    continue
                acc = str(getattr(f, "accession_no", "")
                          or getattr(f, "accession_number", "")).strip()
                if not acc or acc in seen:
                    continue
                found.append({
                    "accession": acc,
                    "_cik": cik,               # 원문 발췌용(출력 파일에는 미저장)
                    "ticker": ciks[cik],
                    "company": str(getattr(f, "company", "") or ""),
                    "form": str(getattr(f, "form", form) or form),
                    "items": str(getattr(f, "items", "") or ""),   # 8-K 항목 코드(있으면)
                    "filedAt": str(getattr(f, "filing_date", "") or ""),
                    "url": (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                            f"{acc.replace('-', '')}/{acc}-index.htm"),
                })
            except Exception:
                continue
    return found


def _clean_html(raw, limit):
    """HTML → 공백 정리된 평문. XBRL 인라인(ix:) 태그도 텍스트는 남는다."""
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", raw)
    t = re.sub(r"<[^>]+>", " ", t)
    t = htmllib.unescape(t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


def fetch_filing_text(cik, acc, limit=3500):
    """공시 원문 텍스트 발췌 — 실패 시 ""(graceful).

    8-K 주 문서는 형식적 문구가 대부분이고 알맹이(실적 수치·계약)는 Ex-99
    보도자료 첨부에 있는 경우가 많다 — Ex-99 를 우선 배치하고 최대 2문서 합침.
    """
    base = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
            f"{acc.replace('-', '')}")
    try:
        r = requests.get(f"{base}/index.json",
                         headers={"User-Agent": SEC_IDENTITY}, timeout=15)
        r.raise_for_status()
        items = ((r.json().get("directory") or {}).get("item")) or []
        htmls = [str(i.get("name") or "") for i in items
                 if str(i.get("name") or "").lower().endswith((".htm", ".html"))
                 and "index" not in str(i.get("name") or "").lower()]
        ex99 = [n for n in htmls if "ex99" in n.lower() or "ex-99" in n.lower()]
        rest = [n for n in htmls if n not in ex99]
        texts = []
        for name in (ex99 + rest)[:2]:
            try:
                d = requests.get(f"{base}/{name}",
                                 headers={"User-Agent": SEC_IDENTITY}, timeout=15)
                d.raise_for_status()
                texts.append(_clean_html(d.text, limit))
            except Exception:
                continue
        return " ".join(texts)[:limit]
    except Exception as e:
        _log(f"원문 발췌 실패({acc}): {e}")
        return ""


BODY_MAX_FILINGS = 8   # 회차당 원문 발췌 상한(폭주 회차 보호 — 초과분은 유형만으로 요약)


def attach_bodies(filings):
    """신규 공시에 원문 발췌(bodyExcerpt) 부착 — 실패·상한 초과분은 빈 문자열."""
    for i, f in enumerate(filings):
        f["bodyExcerpt"] = (fetch_filing_text(f["_cik"], f["accession"])
                            if i < BODY_MAX_FILINGS else "")
    got = sum(1 for f in filings if f["bodyExcerpt"])
    _log(f"원문 발췌 {got}/{len(filings)}건")
    return filings


def attach_rates(filings):
    """신규 공시 티커의 당일 등락률(%) 보강 — 실패 시 생략(graceful)."""
    tickers = sorted({f["ticker"] for f in filings})
    try:
        import yfinance as yf
        df = yf.download(tickers, period="2d", group_by="ticker",
                         progress=False, threads=True, auto_adjust=True, timeout=15)
        for f in filings:
            try:
                tdf = df[f["ticker"]] if len(tickers) > 1 else df
                closes = tdf["Close"].dropna()
                if len(closes) >= 2:
                    f["changePct"] = round(
                        (float(closes.iloc[-1]) / float(closes.iloc[-2]) - 1) * 100, 2)
            except Exception:
                pass
    except Exception as e:
        _log(f"등락률 보강 실패(생략): {e}")
    return filings


CAT_SYSTEM = """\
너는 한국 데이 트레이딩 데스크의 미국 담당 애널리스트다. 밤사이 접수된 SEC 공시
(8-K=주요 이벤트, 6-K=외국계 미국상장사 수시보고) 목록으로 한국어 촉매 요약을 만든다.
- 입력 필드: ticker, company, form, items(8-K 항목 코드, 없을 수 있음), changePct(당일 등락률),
  bodyExcerpt(공시 원문 발췌 — 없을 수 있음).
- **bodyExcerpt 가 있으면 그 실제 내용이 요약의 핵심이다**: 무슨 공시인지(실적·계약·
  인수합병·경영진 변경·자사주 등)와 핵심 수치(매출·EPS·가이던스 등)를 구체적으로 담는다.
  수치·사실은 bodyExcerpt 에 있는 것만 인용한다(지어내기 금지).
  예: "인텔 2분기 매출 $12.9B(예상 상회)·3분기 가이던스 하향 8-K — 한국 반도체 장비주에 혼조 신호".
- bodyExcerpt 가 없으면 공시 유형(form·items)과 당일 주가 반응(changePct)만 근거로
  담백하게 서술한다. 예: "8-K(실적 관련 항목) 제출, 주가 +3.2% 반응".
- direction: 그 공시·주가 반응이 시사하는 방향(bullish|neutral|bearish). 근거가 약하면 neutral.
- category: 종목 성격 — "nasdaq"(기술·성장·반도체·나스닥 상장주) | "sp"(전통·금융·산업·
  헬스케어 등 광범위/NYSE). 개별 기업이라 "macro" 는 쓰지 않는다.
- summary 끝에 가능하면 한국 증시 연관 한 마디(밸류체인·경쟁사 관점).
출력은 정확히 이 JSON 만:
{"catalysts": [{"ticker": "...", "stock": "회사명", "category": "nasdaq|sp",
               "direction": "bullish|neutral|bearish", "summary": "한국어 1-2문장"}]}"""

CAT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"catalysts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"ticker": {"type": "string"}, "stock": {"type": "string"},
                       "category": {"type": "string"},
                       "direction": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["ticker", "stock", "direction", "summary"]}}},
    "required": ["catalysts"],
}


def _norm_category(v, allow_macro=True):
    """LLM 출력 category 정규화 — 허용값 밖이면 폴백."""
    v = str(v or "").lower().replace("s&p", "sp").replace("snp", "sp").strip()
    if v in ("nasdaq", "sp") or (allow_macro and v == "macro"):
        return v
    return "macro" if allow_macro else "sp"


def summarize(filings):
    """LLM 1콜 — 실패 시 기계적 폴백(공시 유형 문구)."""
    view = [{k: f.get(k) for k in ("ticker", "company", "form", "items",
                                   "changePct", "bodyExcerpt")}
            for f in filings]
    by_ticker = {}
    if llm.configured():
        try:
            data = llm.generate_json(CAT_SYSTEM, json.dumps(view, ensure_ascii=False),
                                     max_tokens=MAX_TOKENS, schema=CAT_SCHEMA)
            for c in (data or {}).get("catalysts", []):
                t = str(c.get("ticker") or "").upper()
                if t:
                    by_ticker[t] = c
        except Exception as e:
            _log(f"LLM 요약 실패(기계적 폴백): {e}")
    out = []
    for f in filings:
        c = by_ticker.get(f["ticker"], {})
        chg = f.get("changePct")
        direction = str(c.get("direction") or "neutral").lower()
        if direction not in ("bullish", "neutral", "bearish"):
            direction = "neutral"
        out.append({
            "ticker": f["ticker"],
            "stock": c.get("stock") or f.get("company") or f["ticker"],
            "category": _norm_category(c.get("category"), allow_macro=False),
            "market": "US",
            "kind": "sec",
            "form": f["form"],
            "direction": direction,
            "summary": c.get("summary") or (
                f"{f['form']} 공시 접수"
                + (f" · 당일 {chg:+.1f}%" if isinstance(chg, (int, float)) else "")),
            "url": f["url"],
            "filedAt": f.get("filedAt", ""),
            "changePct": chg,
            "accession": f["accession"],
        })
    return out


# ----------------------------------------------------------------------------
# 뉴스 레그 — SEC 공시로 안 잡히는 시장 촉매(지정학·매크로·대형주 실적 보도)
# ----------------------------------------------------------------------------
NEWS_MAX_PICKS = 3        # 회차당 신규 뉴스 촉매 상한(30분마다 돌므로 세션 누적은 충분)
NEWS_FRESH_HOURS = 13     # 이 시간보다 오래된 기사 제외(전일 세션 리캡 유입 방지)

NEWS_SYSTEM = """\
너는 한국 데이 트레이딩 데스크의 미국 담당 애널리스트다. 밤사이(미국 세션) 뉴스
헤드라인 목록(headlines)에서 '시장을 실제로 움직이는' 신규 촉매만 고른다.
- 포함: 매크로(연준·금리·핵심 지표 서프라이즈), 지정학(전쟁·군사 긴장·제재·관세),
  대형주 실적·가이던스, 섹터 전반에 파급되는 빅뉴스(M&A·규제·사고).
- 제외: 단순 시황 리캡("나스닥 상승 마감"류), 전망·칼럼·해설, reported 목록과 같은
  사건의 재보도, 한국 증시와 무관한 로컬 뉴스.
- 최대 {max_picks}건. 확실한 촉매가 없으면 빈 배열을 반환한다(억지로 고르지 말 것).
- 각 pick 필드:
  idx: 입력 headlines 의 번호(그 기사가 근거).
  name: 표 첫 칸에 들어갈 **뉴스의 실제 주체(2~12자)** — 그 사건의 '주어'를 그대로 쓴다.
        개별기업이면 기업명(예: "인텔","애플","테슬라","엔비디아"), 해외 비상장·신규상장·
        중국기업 등도 그 이름 그대로(예: "CXMT","창신메모리","화웨이"), 매크로/지정학이면
        핵심 주체(예: "국제유가","미국 관세","연준","미-이란"). **절대 국내 관련주로 바꾸지
        말 것**(국내주는 relatedStock 칸). **긴 문장 금지**(사건 설명은 summary 로).
  category: "macro"(유가·금리·관세·지정학·지표 등 개별종목 아님) | "nasdaq"(기술·성장·
            반도체 중심) | "sp"(전통·금융·산업 등 광범위/NYSE).
  relatedStock: 이 촉매로 실제 매매할 **국내 상장 대표 관련주 1개(한국 종목명)**.
    category=macro 면 반드시 채운다 — 예: 국제유가→"S-Oil", 미국 관세→"현대차",
    연준 금리인상→"KB금융", 지정학 방산→"한화에어로스페이스", CXMT 급등→"삼성전자".
    개별 미국주(nasdaq/sp)면 국내 밸류체인 대표 1개 또는 "".
  ticker: 대표 티커(개별 기업 사건일 때만, 없으면 "").
  direction: 한국 증시 관점 방향(bullish|neutral|bearish). 근거 약하면 neutral.
  summary: 한국어 1-2문장 — 무슨 일이 있었는지 + 한국 증시/밸류체인 함의 한 마디.
- 헤드라인에 없는 사실·수치를 지어내지 말 것.
출력은 정확히 이 JSON 만: {{"picks": [{{"idx": 0, "name": "...", "category": "macro|nasdaq|sp",
"relatedStock": "...", "ticker": "...", "direction": "bullish|neutral|bearish", "summary": "..."}}]}}"""

NEWS_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"picks": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"idx": {"type": "integer"}, "name": {"type": "string"},
                       "category": {"type": "string"}, "relatedStock": {"type": "string"},
                       "ticker": {"type": "string"}, "direction": {"type": "string"},
                       "summary": {"type": "string"}},
        "required": ["idx", "name", "direction", "summary"]}}},
    "required": ["picks"],
}


def _related_stock_article(name, now):
    """국내 관련주 종목명 → 그 종목 관련 최신 네이버 뉴스 URL(없으면 None). 매크로 촉매의
    '원문'을 국내 관련주 기사로 돌리기 위함(사용자 요청 2026-07-27). 최신순 top, graceful."""
    try:
        items = sources.naver_search("news", name, display=5) or []
    except Exception as e:
        _log(f"관련주 뉴스 검색 실패({name}): {e}")
        return None
    floor = _fresh_epoch(now)
    dated = []
    for it in items:
        link = (it.get("link") or "").strip()
        if not link:
            continue
        try:
            ts = datetime.datetime.strptime(
                it.get("date") or "", "%a, %d %b %Y %H:%M:%S %z").timestamp()
        except Exception:
            ts = 0
        dated.append((ts, link))
    if not dated:
        return None
    fresh = [l for ts, l in dated if ts >= floor]
    return fresh[0] if fresh else dated[0][1]      # 신선분 우선, 없으면 top


def _fresh_epoch(now):
    return (now - datetime.timedelta(hours=NEWS_FRESH_HOURS)).timestamp()


def gather_headlines(now):
    """네이버 뉴스 + yfinance(SPY/QQQ) + Tavily 헤드라인 — [{title, url, src}]."""
    heads, seen_urls = [], set()

    def _add(title, url, src):
        title, url = (title or "").strip(), (url or "").strip()
        if title and url and url not in seen_urls:
            seen_urls.add(url)
            heads.append({"title": title, "url": url, "src": src})

    # 1) 네이버 뉴스 — 한국어 야간 미국시장 보도(연합인포맥스 등이 실시간 커버)
    floor = _fresh_epoch(now)
    for q in ("뉴욕증시", "미국 증시"):
        for it in sources.naver_search("news", q, display=10):
            try:      # pubDate RFC822 — 파싱 실패 시 포함(과필터 방지)
                ts = datetime.datetime.strptime(
                    it.get("date") or "", "%a, %d %b %Y %H:%M:%S %z").timestamp()
                if ts < floor:
                    continue
            except Exception:
                pass
            _add(it.get("title"), it.get("link"), "naver")

    # 2) yfinance 시장 헤드라인(키리스) — SPY/QQQ 뉴스 피드
    try:
        import yfinance as yf
        for sym in ("SPY", "QQQ"):
            for it in (yf.Ticker(sym).news or [])[:8]:
                c = it.get("content") or it   # 신·구 yfinance 포맷 모두 수용
                url = (((c.get("canonicalUrl") or {}).get("url"))
                       if isinstance(c.get("canonicalUrl"), dict) else c.get("link"))
                ts = c.get("providerPublishTime")
                if isinstance(ts, (int, float)) and ts < floor:
                    continue
                _add(c.get("title"), url, "yahoo")
    except Exception as e:
        _log(f"yfinance 뉴스 실패(생략): {e}")

    # 3) Tavily 웹검색(→Brave 폴백) — 영문권 속보 보강, 회차당 1쿼리로 절제
    for it in sources.web_search("US stock market moving news today", max_results=5):
        _add(it.get("title"), it.get("url"), "web")

    return heads


def collect_news(state, now):
    """미확인 헤드라인 → LLM 선별 → kind='news' 촉매 목록. 실패 시 []."""
    seen = set(state.get("seen") or [])
    fresh = [h for h in gather_headlines(now) if h["url"] not in seen]
    if not fresh:
        _log("신규 헤드라인 0건 — 뉴스 레그 종료")
        return []
    _log(f"신규 헤드라인 {len(fresh)}건")
    if not llm.configured():
        _log("LLM 미설정 — 뉴스 레그 스킵(선별 불가)")
        return []

    view = {
        "headlines": [{"idx": i, "title": h["title"], "src": h["src"]}
                      for i, h in enumerate(fresh)],
        # 기보고 사건 재선별 방지 — 이번 세션에 이미 실린 촉매 한 줄씩
        "reported": [f"{c.get('stock', '')}: {c.get('summary', '')}"
                     for c in (state.get("catalysts") or [])][-20:],
    }
    try:
        data = llm.generate_json(NEWS_SYSTEM.format(max_picks=NEWS_MAX_PICKS),
                                 json.dumps(view, ensure_ascii=False),
                                 max_tokens=1024, schema=NEWS_SCHEMA)
    except Exception as e:
        _log(f"뉴스 LLM 선별 실패(이번 회차 생략): {e}")
        return []

    # 선별 여부와 무관하게 이번 회차 헤드라인은 전부 seen 처리(재평가 방지)
    state["seen"] = sorted(seen | {h["url"] for h in fresh})

    out = []
    for p in (data or {}).get("picks", [])[:NEWS_MAX_PICKS]:
        try:
            h = fresh[int(p.get("idx", -1))]
        except (IndexError, ValueError, TypeError):
            continue
        direction = str(p.get("direction") or "neutral").lower()
        if direction not in ("bullish", "neutral", "bearish"):
            direction = "neutral"
        category = _norm_category(p.get("category"), allow_macro=True)
        stock = (p.get("name") or "").strip() or h["title"][:20]
        related = (p.get("relatedStock") or "").strip()
        # 종목명(stock)은 항상 뉴스의 '실제 주체'(해외 기업·지수·원자재·이슈, 예: CXMT·인텔·
        # 국제유가·연준)를 그대로 둔다 — 국내 관련주로 덮어쓰지 않는다(사용자 요청 2026-07-28:
        # 매크로도 해외 주체를 표기). 국내 대표 관련주는 relatedStock 로 분리하고, 그 관련주의
        # 최신 뉴스를 relatedUrl 로 붙여 프런트가 '관련주 딥리서치/뉴스'로 액션화한다
        # (2026-07-27 매크로 액션화 기능은 relatedStock/relatedUrl 로 이관·유지).
        related_url = (_related_stock_article(related, now)
                       if (category == "macro" and related) else None)
        out.append({
            "ticker": str(p.get("ticker") or "").upper(),
            "stock": stock,                 # 해외 주체(CXMT 등) — 국내주로 치환하지 않음
            "category": category,
            "relatedStock": related,        # 국내 대표 관련주(매매 액션용)
            "relatedUrl": related_url,      # 그 관련주 최신 뉴스(없으면 None)
            "market": "US",
            "kind": "news",
            "form": "",
            "direction": direction,
            "summary": p.get("summary") or h["title"],
            "url": h["url"],                # 원문 = 사건 기사 그대로
            "filedAt": now.strftime("%Y-%m-%d"),
            "changePct": None,
            "accession": h["url"],          # 뉴스는 URL 이 dedup 키
        })
    return out


def main():
    now = datetime.datetime.now(KST)
    sess = session_key(now)
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:
        state = {}
    reset = state.get("session") != sess
    if reset:
        _log(f"새 세션 시작: {sess} (이전: {state.get('session')})")
        state = {"session": sess, "seen": [], "catalysts": []}

    seen_before = list(state.get("seen") or [])

    # ── SEC 레그 (실패해도 뉴스 레그는 진행) ─────────────────────────────
    sec_cats = []
    try:
        uni = build_universe()
        _log(f"유니버스 {len(uni)}티커")
        ciks = cik_map(uni)
        _log(f"CIK 매핑 {len(ciks)}/{len(uni)}")
        seen = set(state.get("seen") or [])
        new = scan_new_filings(ciks, seen)
        if new:
            _log(f"신규 공시 {len(new)}건: "
                 + ", ".join(f"{f['ticker']}({f['form']})" for f in new))
            new = attach_bodies(new)
            new = attach_rates(new)
            sec_cats = summarize(new)
            state["seen"] = sorted(seen | {f["accession"] for f in new})
        else:
            _log("신규 공시 0건")
    except Exception as e:
        _log(f"SEC 레그 실패(뉴스 레그는 진행): {e}")

    # ── 뉴스 레그 — 지정학·매크로 등 공시 밖 촉매 (seen 은 함수 안에서 갱신) ──
    news_cats = []
    try:
        news_cats = collect_news(state, now)
        if news_cats:
            _log(f"뉴스 촉매 {len(news_cats)}건: "
                 + ", ".join(c["stock"] for c in news_cats))
    except Exception as e:
        _log(f"뉴스 레그 실패: {e}")

    added = sec_cats + news_cats
    seen_changed = list(state.get("seen") or []) != seen_before
    # seen 만 바뀐 회차도 저장 — 안 하면 다음 회차가 같은 헤드라인을 LLM 재평가(낭비)
    if not added and not reset and not seen_changed:
        _log("신규 촉매 0건 — 종료(파일 무변경)")
        return

    state["catalysts"] = (state.get("catalysts") or []) + added
    state["asof"] = now.strftime("%Y-%m-%d %H:%M KST")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    _log(f"저장: +{len(added)}건, 누적 {len(state['catalysts'])}건 ({OUT_PATH})")


if __name__ == "__main__":
    main()
