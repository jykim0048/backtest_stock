#!/usr/bin/env python3
"""
Daily pre-market stock screener — picks today's 급등 예상 종목 (surge candidates).

Runs BEFORE generate_report.py (~08:00 KST, 장 시작 전). Catalyst-first 설계:

  1. 전일 미국시장 분석   — yfinance: 미국 지수 + 섹터 ETF + 급등 특징주(day_gainers)
  2. 후보 구성            — 코스피200/코스닥150 유니버스(FinanceDataReader)에서
       ⓐ 공시 촉매 [주동력]: DART 시장 전체 공시를 전일 장마감~실행시각으로 스캔해
          유니버스 종목 중 '긍정 촉매' 공시(공급계약·수주·실적·임상·투자·자사주 등)를
          보유한 종목. 정정·해지 등 주요 내용 변경 없는 공시는 제외.
       ⓑ 가격 확인 [보조]: 거래대금회전율·등락률 상위 일부.
  3. 뉴스 보강            — 후보별 '전일 장마감(15:30 KST)~실행시각' 네이버 뉴스
  4. LLM 최종 선정        — Claude가 미국 특징주/섹터 + 공시 + 뉴스를 종합해 최종 N종목 선정

Outputs:
  watchlist.json                            — 선정 종목 (code/name/market). 기존 파이프라인이 소비.
  public/reports/selection/YYYY-MM-DD.json  — 선정 근거 (시장관 + 종목별 사유).

Graceful degradation: 어느 단계가 실패해도 가격 점수 상위로 watchlist를 채워
generate_report.py 가 항상 돌 수 있게 한다.

정확한 코스피200/코스닥150 멤버십은 KRX 로그인이 필요해 기본은 시가총액 상위 근사를 쓴다.
data/index_constituents.json (KOSPI200/KOSDAQ150 코드 배열)이 있으면 그 명단을 우선 사용한다.

Env (GitHub Actions secrets): GEMINI_API_KEY (or LLM_CHAIN + matching keys),
NAVER_CLIENT_ID/SECRET, DART_API_KEY, TAVILY_API_KEY
"""
import os
import sys
import json
import datetime
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor

# yfinance writes a cache; on read-only CI point it at a writable dir.
for _mod in ("appdirs", "platformdirs"):
    try:
        _m = __import__(_mod)
        _m.user_cache_dir = lambda *a, **k: "/tmp"
    except Exception:
        pass

import numpy as np
import pandas as pd
import yfinance as yf

import llm                          # provider-agnostic LLM with fallback chain
from analysis import sources

ROOT          = os.path.dirname(os.path.abspath(__file__))
CONSTITUENTS  = os.path.join(ROOT, "data", "index_constituents.json")
KRX_COMPANIES = os.path.join(ROOT, "public", "assets", "krx_companies.json")

# ---- mode -----------------------------------------------------------------
# "pre"      : 장마감 후~장전 스크리닝 (대시보드 '워치리스트', 자동매매 대상)
# "intraday" : 장중 스크리닝 (대시보드 '관심종목', 모니터링 전용)
MODE        = os.environ.get("SCREEN_MODE", "pre").strip().lower()
IS_INTRADAY = MODE == "intraday"

# 모드별 산출 파일. 장전/장중이 서로의 결과를 덮어쓰지 않도록 분리한다.
# *_DOWN 은 하락 관찰 리스트(참고용 — 자동매매 제외). catalyst(하방 사유)를 함께 담아
# generate_report 가 리포트에 그대로 노출한다.
if IS_INTRADAY:
    WATCHLIST      = os.path.join(ROOT, "intraday_watchlist.json")
    WATCHLIST_DOWN = os.path.join(ROOT, "intraday_watchlist_down.json")
    SELECTION_DIR  = os.path.join(ROOT, "public", "reports", "selection", "intraday")
else:
    WATCHLIST      = os.path.join(ROOT, "watchlist.json")
    WATCHLIST_DOWN = os.path.join(ROOT, "watchlist_down.json")
    SELECTION_DIR  = os.path.join(ROOT, "public", "reports", "selection")

# 장전 하락 리스트 경로(장중 중복 제거용 — 모드와 무관하게 고정 경로 참조)
PRE_WATCHLIST_DOWN = os.path.join(ROOT, "watchlist_down.json")

KST = datetime.timezone(datetime.timedelta(hours=9))

# ---- tunables -------------------------------------------------------------
N_FINAL        = int(os.environ.get("SCREEN_N_FINAL", "10"))       # 1회 실행 최대 선정 종목 수
PRICE_BACKUP   = int(os.environ.get("SCREEN_PRICE_BACKUP", "15"))  # ⓑ 가격 보조 후보 수
MAX_CANDIDATES = int(os.environ.get("SCREEN_MAX_CANDIDATES", "40"))  # LLM에 넘길 후보 상한
INTRADAY_CAP   = int(os.environ.get("SCREEN_INTRADAY_CAP", "0"))   # 장중 누적 관심종목 상한 (0=무제한, 2026-07-10 20→무제한)
KOSPI_TOP      = 200   # 코스피200 근사 (시총 상위)
KOSDAQ_TOP     = 150   # 코스닥150 근사 (시총 상위)
ENRICH_WORKERS = int(os.environ.get("SCREEN_ENRICH_WORKERS", "8"))
NEWS_FETCH     = 30    # 시간창 필터 전, 최신순으로 받아올 뉴스 건수 (Naver display)
NEWS_MAX       = 8     # 시간창 통과 후 후보당 최대 뉴스 수
US_MOVERS_TOP  = 20    # 미국 급등 특징주 상위 N (시총 하한 없음)

MAX_TOKENS = 3500

# 미국 지수 / 섹터 ETF (전일 세션 동향). name 은 한국어 표기.
US_INDICES = [
    {"ticker": "^GSPC", "name": "S&P500"},
    {"ticker": "^IXIC", "name": "나스닥"},
    {"ticker": "^DJI",  "name": "다우"},
    {"ticker": "^SOX",  "name": "필라델피아 반도체"},
    {"ticker": "^VIX",  "name": "VIX 변동성"},
]
US_SECTORS = [
    {"ticker": "XLK", "name": "기술"},
    {"ticker": "SMH", "name": "반도체"},
    {"ticker": "XLV", "name": "헬스케어"},
    {"ticker": "XBI", "name": "바이오"},
    {"ticker": "XLE", "name": "에너지"},
    {"ticker": "XLF", "name": "금융"},
    {"ticker": "XLI", "name": "산업재"},
    {"ticker": "XLB", "name": "소재"},
    {"ticker": "XLY", "name": "임의소비재"},
]

# 섹터별 대표 미국 종목(모닝 브리핑 급등/급락 테마의 '관련주' 표시용). 부분일치 아닌
# 명시적 티커 목록 — 섹터 히트맵 상하위 테마가 뽑히면 이 안에서 등락률 상위(절대값)를 고른다.
SECTOR_US_STOCKS = {
    "기술":       [("AAPL", "Apple"), ("MSFT", "Microsoft"), ("ORCL", "Oracle"),
                    ("CRM", "Salesforce"), ("ADBE", "Adobe"), ("IBM", "IBM")],
    "반도체":     [("NVDA", "NVIDIA"), ("AMD", "AMD"), ("AVGO", "Broadcom"),
                    ("TSM", "TSMC"), ("ASML", "ASML"), ("MU", "Micron")],
    "헬스케어":   [("UNH", "UnitedHealth"), ("JNJ", "Johnson & Johnson"), ("LLY", "Eli Lilly"),
                    ("PFE", "Pfizer"), ("ABBV", "AbbVie")],
    "바이오":     [("REGN", "Regeneron"), ("VRTX", "Vertex Pharmaceuticals"), ("GILD", "Gilead Sciences"),
                    ("MRNA", "Moderna"), ("AMGN", "Amgen")],
    "에너지":     [("XOM", "ExxonMobil"), ("CVX", "Chevron"), ("COP", "ConocoPhillips"),
                    ("SLB", "SLB"), ("EOG", "EOG Resources")],
    "금융":       [("JPM", "JPMorgan Chase"), ("BAC", "Bank of America"), ("WFC", "Wells Fargo"),
                    ("GS", "Goldman Sachs"), ("MS", "Morgan Stanley")],
    "산업재":     [("CAT", "Caterpillar"), ("HON", "Honeywell"), ("GE", "GE Aerospace"),
                    ("BA", "Boeing"), ("UPS", "UPS")],
    "소재":       [("LIN", "Linde"), ("SHW", "Sherwin-Williams"), ("APD", "Air Products"),
                    ("FCX", "Freeport-McMoRan"), ("NEM", "Newmont")],
    "임의소비재": [("AMZN", "Amazon"), ("TSLA", "Tesla"), ("HD", "Home Depot"),
                    ("MCD", "McDonald's"), ("NKE", "Nike")],
}

# 섹터별 대표 국내 종목 코드(모닝 브리핑 '국내 밸류체인' 표시용). 코스피200/코스닥150
# 유니버스(load_universe)에 이미 있는 전일 등락률을 그대로 조회하므로 추가 네트워크 호출 없음.
SECTOR_KR_STOCKS = {
    "기술":       ["035420", "035720", "018260", "259960", "293490"],   # NAVER/카카오/삼성SDS/크래프톤/카카오게임즈
    "반도체":     ["005930", "000660", "058470", "039030", "240810", "095340"],  # 삼성전자/SK하이닉스/리노공업/이오테크닉스/원익IPS/ISC
    "헬스케어":   ["207940", "000100", "128940", "185750", "326030"],   # 삼성바이오로직스/유한양행/한미약품/종근당/SK바이오팜
    "바이오":     ["068270", "196170", "145020", "214150", "141080"],   # 셀트리온/알테오젠/휴젤/클래시스/레고켐바이오
    "에너지":     ["096770", "010950", "078930", "047050"],             # SK이노베이션/S-Oil/GS/포스코인터내셔널
    "금융":       ["105560", "055550", "086790", "316140", "006800", "016360"],  # KB/신한/하나/우리금융지주+미래에셋·삼성증권
    "산업재":     ["034020", "267270", "042670", "006360", "000150"],   # 두산에너빌리티/HD현대건설기계/HD현대인프라코어/GS건설/두산
    "소재":       ["051910", "011170", "011780", "003670"],             # LG화학/롯데케미칼/금호석유/포스코퓨처엠
    "임의소비재": ["139480", "004170", "008770", "383220"],             # 이마트/신세계/호텔신라/F&F
}

# 긍정 촉매 공시 키워드 (섹터별로 촉매 유형이 달라 광범위하게 포함). report_nm 부분일치.
POSITIVE_KEYWORDS = [
    # 계약·수주·납품
    "공급계약", "단일판매", "수주", "납품", "계약체결", "계약 체결", "양산",
    # 실적·이익
    "잠정실적", "영업실적", "영업(잠정)", "손익구조", "흑자전환", "매출액",
    # 기술·제약·바이오
    "임상", "품목허가", "허가", "승인", "기술이전", "기술수출", "라이선스",
    "특허", "신약", "국책", "과제선정", "과제 선정", "인증",
    # 투자·M&A·제휴
    "시설투자", "신규시설", "신규 시설", "투자판단", "타법인주식", "출자",
    "지분취득", "지분 취득", "인수", "합병", "영업양수", "양수도", "제휴",
    "협약", "업무협약", "공동개발", "공동연구",
    # 수급(긍정)
    "자기주식취득", "자기주식 취득", "자사주", "무상증자",
    # 정부·수출
    "수출", "정부과제", "국가핵심기술",
    # 지배구조(상승 모멘텀)
    "최대주주변경", "최대주주 변경", "경영권", "공개매수",
]

# 비실질 변경 / 부정 공시 — 위 긍정 키워드와 겹쳐도 이게 있으면 제외.
EXCLUDE_KEYWORDS = [
    "정정", "첨부추가", "철회", "취소", "해지", "연기", "기각", "각하", "무효",
    "불성실공시", "관리종목", "상장폐지", "투자주의", "투자경고", "투자위험",
    "감자", "횡령", "배임", "소송",
]


def _warn(msg):
    print(f"[screener] {msg}", file=sys.stderr)


def _load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


# ----------------------------------------------------------------------------
# 시간창 (전일 장 마감 15:30 KST ~ 실행 시각)
# ----------------------------------------------------------------------------
def overnight_cutoff(now=None):
    """뉴스/공시 수집 시작 시각 = 직전 거래일 15:30 KST (전일 장 마감).

    주말은 금요일로 당긴다(공휴일 미반영). 스크리너는 장전(08:00 KST)에 도므로
    '전일' = 직전 거래일을 의미한다.
    """
    now = now or datetime.datetime.now(KST)
    d = now.date() - datetime.timedelta(days=1)
    while d.weekday() >= 5:                       # 토(5)/일(6) 건너뜀
        d -= datetime.timedelta(days=1)
    return datetime.datetime.combine(d, datetime.time(15, 30), tzinfo=KST)


def intraday_cutoff(now=None):
    """장중 모드 뉴스/공시 시간창 시작 = 당일 장 시작(09:00 KST).

    장중(09:00~15:30)에 30분 간격으로 도므로 '오늘 장 들어 나온 이슈'만 본다.
    장 시작 전(09:00 이전)에 돌면 음수 창이 되지 않도록 직전 거래일 마감으로 당긴다.
    """
    now = now or datetime.datetime.now(KST)
    today_open = datetime.datetime.combine(now.date(), datetime.time(9, 0), tzinfo=KST)
    if now < today_open or now.weekday() >= 5:
        return overnight_cutoff(now)
    return today_open


def news_cutoff(now=None):
    """현재 모드의 뉴스/공시 수집 시작 시각."""
    return intraday_cutoff(now) if IS_INTRADAY else overnight_cutoff(now)


def _parse_pubdate(s):
    """Naver pubDate(RFC822) → tz-aware datetime. 실패 시 None."""
    try:
        dt = parsedate_to_datetime(s)
    except Exception:
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=KST)
    return dt


# ----------------------------------------------------------------------------
# 1) 전일 미국시장 분석 (지수 + 섹터 + 급등/급락 특징주)
# ----------------------------------------------------------------------------
def us_market_brief():
    items = US_INDICES + US_SECTORS
    tickers = [x["ticker"] for x in items]
    sector_stock_tickers = sorted({t for lst in SECTOR_US_STOCKS.values() for t, _ in lst})
    all_tickers = tickers + sector_stock_tickers
    try:
        df = yf.download(all_tickers, period="5d", group_by="ticker",
                         progress=False, threads=True, auto_adjust=True)
    except Exception as e:
        _warn(f"US market download failed: {e}")
        df = None

    def chg(ticker):
        try:
            if isinstance(df.columns, pd.MultiIndex):
                closes = df[ticker]["Close"].dropna()
            else:
                closes = df["Close"].dropna()
            if len(closes) >= 2:
                last, prev = float(closes.iloc[-1]), float(closes.iloc[-2])
                return round(last, 2), round((last - prev) / prev * 100, 2)
        except Exception:
            pass
        return None, None

    def pack(group):
        out = []
        for x in group:
            price, pct = chg(x["ticker"])
            if pct is not None:
                out.append({"name": x["name"], "ticker": x["ticker"],
                            "price": price, "changePct": pct})
        return out

    # 섹터별 대표 종목 등락률(모닝 브리핑 급등/급락 테마 '관련주' 표시용). 절대값 기준 정렬.
    sector_stocks = {}
    if df is not None:
        for sector, stocks in SECTOR_US_STOCKS.items():
            rows = []
            for ticker, name in stocks:
                _, pct = chg(ticker)
                if pct is not None:
                    rows.append({"symbol": ticker, "name": name, "changePct": pct})
            rows.sort(key=lambda d: abs(d["changePct"]), reverse=True)
            sector_stocks[sector] = rows

    return {
        "indices": pack(US_INDICES) if df is not None else [],
        "sectors": sorted(pack(US_SECTORS), key=lambda d: d["changePct"], reverse=True) if df is not None else [],
        "movers": us_movers(),
        "losers": us_losers(),
        "sectorStocks": sector_stocks,
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d"),
    }


def _us_screen(screen_id):
    """yfinance predefined screener 결과를 [{symbol,name,changePct}]로 파싱.

    야후 비공식 엔드포인트라 실패하면 빈 리스트로 흡수한다.
    """
    try:
        r = yf.screen(screen_id)
        quotes = r.get("quotes", []) if isinstance(r, dict) else []
    except Exception as e:
        _warn(f"{screen_id} screen failed: {e}")
        return []
    out = []
    for q in quotes:
        sym = q.get("symbol")
        pct = q.get("regularMarketChangePercent")
        if sym and pct is not None:
            out.append({
                "symbol": sym,
                "name": q.get("shortName") or q.get("longName") or sym,
                "changePct": round(float(pct), 2),
            })
    return out


def us_movers(top=US_MOVERS_TOP):
    """미국 당일 급등 특징주 (day_gainers). 등락률 내림차순 상위 N. 시총 하한 없음."""
    movers = _us_screen("day_gainers")
    movers.sort(key=lambda d: d["changePct"], reverse=True)
    return movers[:top]


def us_losers(top=US_MOVERS_TOP):
    """미국 당일 급락 특징주 (day_losers). 등락률 오름차순(최대 낙폭) 상위 N.

    급락 테마(국내 약세 주의) 추론용. movers 와 대칭.
    """
    losers = _us_screen("day_losers")
    losers.sort(key=lambda d: d["changePct"])
    return losers[:top]


# ----------------------------------------------------------------------------
# 2) 유니버스 (코스피200 + 코스닥150 근사)
# ----------------------------------------------------------------------------
def _load_constituents():
    """data/index_constituents.json 가 있으면 정확한 멤버십 코드 집합을 반환, 없으면 None."""
    try:
        with open(CONSTITUENTS, encoding="utf-8") as f:
            data = json.load(f)
        codes = set()
        for key in ("KOSPI200", "KOSDAQ150"):
            codes.update(str(c).zfill(6) for c in data.get(key, []))
        return codes or None
    except Exception:
        return None


def _krx_master_map():
    """{6자리코드: {"name", "market"}} dict를 정적 마스터 파일에서 로드한다. 실패 시 빈 dict.

    public/assets/krx_companies.json (KRX 전체 종목 마스터, ~2700개)을 쓴다.
    pykrx/KRX 직접 호출은 GitHub Actions 해외 IP에서 빈 응답을 받으므로
    네트워크 의존 없는 정적 파일을 사용한다. 분기(코스피200·코스닥150 리뷰) 단위로 교체.

    market 은 "KOSPI"(유가증권) | "KOSDAQ"(코스닥) | None(코넥스/기타)로 정규화한다.
    종목코드별 시장 구분의 단일 진실 소스(SSOT) — index_constituents.json 의 분류 오류로
    .KS/.KQ suffix 가 반대로 붙어 yfinance 시세가 누락되는 문제를 여기서 바로잡는다.
    """
    try:
        with open(KRX_COMPANIES, encoding="utf-8") as f:
            companies = json.load(f)
    except Exception as e:
        _warn(f"krx_companies.json 로드 실패: {e}")
        return {}
    out = {}
    for c in companies:
        code = str(c.get("code", "")).zfill(6)
        if not code:
            continue
        raw = c.get("market", "") or ""
        market = "KOSPI" if "유가" in raw else ("KOSDAQ" if "코스닥" in raw else None)
        out[code] = {"name": c.get("name"), "market": market}
    return out


def _krx_name_map():
    """{6자리코드: 종목명} — _krx_master_map 에서 파생(기존 호출부 호환)."""
    return {code: m["name"] for code, m in _krx_master_map().items() if m.get("name")}


def _load_universe_yfinance():
    """index_constituents.json 종목코드 목록을 yfinance 배치 다운로드로 채운다.

    외부 서비스(fdr·pykrx) 없이 yfinance 하나만 사용하므로 가장 안정적.
    Marcap은 거래대금 × 200 으로 근사한다(일 회전율 ~0.5% 가정).
    """
    constituents = _load_constituents()
    if not constituents:
        raise RuntimeError("index_constituents.json 없음 — yfinance 로더 사용 불가")

    # constituents.json 에서 KOSPI/KOSDAQ 분류를 다시 읽어 suffix 결정
    with open(CONSTITUENTS, encoding="utf-8") as f:
        raw = json.load(f)
    kospi_codes  = {str(c).zfill(6) for c in raw.get("KOSPI200", [])}
    kosdaq_codes = {str(c).zfill(6) for c in raw.get("KOSDAQ150", [])}

    tickers_ks = [f"{c}.KS" for c in sorted(kospi_codes)]
    tickers_kq = [f"{c}.KQ" for c in sorted(kosdaq_codes)]
    all_tickers = tickers_ks + tickers_kq

    hist = yf.download(
        all_tickers, period="5d",
        group_by="ticker", progress=False, threads=True, auto_adjust=True,
    )
    if hist is None or hist.empty:
        raise RuntimeError("yfinance returned empty data for KRX universe")

    rows = []
    for ticker in all_tickers:
        try:
            if isinstance(hist.columns, pd.MultiIndex):
                tk_df = hist[ticker].dropna(how="all")
            else:
                tk_df = hist.dropna(how="all")
            if len(tk_df) < 2:
                continue
            last = tk_df.iloc[-1]
            prev = tk_df.iloc[-2]
            close = float(last["Close"])
            prev_close = float(prev["Close"])
            volume = float(last["Volume"])
            if close <= 0 or volume <= 0:
                continue
            chg = (close - prev_close) / prev_close * 100 if prev_close else 0.0
            amount = close * volume
            code = ticker.split(".")[0].zfill(6)
            market = "KOSPI" if ticker.endswith(".KS") else "KOSDAQ"
            rows.append({
                "Code":        code,
                "Name":        code,          # pykrx로 이름 보완 (아래 단계)
                "Market":      market,
                "Close":       close,
                "ChagesRatio": round(chg, 2),
                "Amount":      amount,
                "Volume":      volume,
                "Marcap":      amount * 200,  # 일 회전율 0.5% 가정 근사치
            })
        except Exception:
            continue

    # pykrx로 종목명 보완 (가격은 yfinance, 이름만 pykrx).
    # get_market_price_change_by_ticker는 시장당 1회 호출로 '종목명' 컬럼을
    # 포함한 전체 종목 DataFrame을 반환한다(개별 조회 X → 일괄).
    name_map = _krx_name_map()
    if name_map:
        for row in rows:
            row["Name"] = name_map.get(row["Code"], row["Code"])
        _warn(f"pykrx 종목명 일괄 보완: {len(name_map)}개")
    else:
        _warn("pykrx 종목명 보완 실패 — 코드로 대체")

    if not rows:
        raise RuntimeError("yfinance KRX universe: 유효 종목 없음")

    df = pd.DataFrame(rows)
    _warn(f"yfinance universe: {len(df)} stocks (KOSPI {(df['Market']=='KOSPI').sum()}, KOSDAQ {(df['Market']=='KOSDAQ').sum()})")
    return df


def _load_universe_fdr():
    """FinanceDataReader로 KRX 종목 목록 로드. 404 등 네트워크 오류 시 예외를 올린다."""
    import FinanceDataReader as fdr
    df = fdr.StockListing("KRX")
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])].copy()
    for col in ("Close", "ChagesRatio", "Amount", "Volume", "Marcap"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df = df.dropna(subset=["Marcap", "Amount"])
    df["Code"] = df["Code"].astype(str).str.zfill(6)
    return df


def _load_universe_pykrx():
    """pykrx로 KRX 직접 스크래핑. fdr 실패 시 fallback.

    08:00 KST 장전 실행이므로 당일 데이터는 없음 → 직전 거래일 기준으로 조회.
    """
    from pykrx import stock as pkstock
    # overnight_cutoff()는 직전 거래일 15:30 KST를 반환한다.
    base_date = overnight_cutoff().strftime("%Y%m%d")
    rows = []
    for market, label in (("KOSPI", "KOSPI"), ("KOSDAQ", "KOSDAQ")):
        tickers = pkstock.get_market_ticker_list(base_date, market=market)
        for ticker in tickers:
            try:
                name = pkstock.get_market_ticker_name(ticker)
                ohlcv = pkstock.get_market_ohlcv(base_date, base_date, ticker)
                if ohlcv.empty:
                    continue
                row = ohlcv.iloc[-1]
                cap_df = pkstock.get_market_cap(base_date, base_date, ticker)
                marcap = float(cap_df["시가총액"].iloc[-1]) if not cap_df.empty else 0.0
                amount = float(row.get("거래대금", 0) or 0)
                rows.append({
                    "Code": ticker.zfill(6),
                    "Name": name,
                    "Market": label,
                    "Close": float(row.get("종가", 0) or 0),
                    "ChagesRatio": float(row.get("등락률", 0) or 0),
                    "Amount": amount,
                    "Volume": float(row.get("거래량", 0) or 0),
                    "Marcap": marcap,
                })
            except Exception as e:
                _warn(f"pykrx ticker {ticker} failed: {e}")
    if not rows:
        raise RuntimeError("pykrx returned no rows")
    df = pd.DataFrame(rows)
    df = df.dropna(subset=["Marcap", "Amount"])
    return df


def load_universe():
    """KRX 유니버스 DataFrame을 로드한다. yfinance → fdr → pykrx 순으로 시도.

    columns: Code, Name, Market, Close, ChagesRatio, Amount, Volume, Marcap
    """
    df = None
    for loader, name in (
        (_load_universe_yfinance, "yfinance"),
        (_load_universe_fdr,      "FinanceDataReader"),
        (_load_universe_pykrx,    "pykrx"),
    ):
        try:
            df = loader()
            _warn(f"universe loaded via {name}: {len(df)} stocks")
            break
        except Exception as e:
            _warn(f"{name} failed: {e}; trying next source...")
    if df is None or df.empty:
        raise RuntimeError("모든 유니버스 소스 실패 (yfinance, fdr, pykrx)")
    df["Code"] = df["Code"].astype(str).str.zfill(6)

    # 시장 구분을 KRX 마스터로 보정한다(SSOT). yfinance/fdr/index_constituents 분류가
    # 어긋나 .KS/.KQ suffix 가 반대로 붙으면 generate_report·/api/prices 의 yfinance 조회가
    # 실패해 일부 종목 시세가 누락된다(예: SK바이오팜 326030 을 KOSDAQ 으로 → .KQ 조회 실패).
    # 이 보정은 시장별 시총 상위(nlargest) 분류 전에 적용돼야 한다.
    master = _krx_master_map()
    if master:
        corrected = df["Code"].map(lambda c: (master.get(c) or {}).get("market"))
        mismatch = corrected.notna() & (corrected != df["Market"])
        if int(mismatch.sum()):
            _warn(f"시장 구분 보정(KRX 마스터): {int(mismatch.sum())}종목")
        df["Market"] = corrected.where(corrected.notna(), df["Market"])

    explicit = _load_constituents()
    if explicit:
        uni = df[df["Code"].isin(explicit)]
        if len(uni) >= 100:
            _warn(f"universe from index_constituents.json: {len(uni)} stocks")
            return uni.reset_index(drop=True)
        _warn("index_constituents.json too small; falling back to market-cap proxy")

    kospi  = df[df["Market"] == "KOSPI"].nlargest(KOSPI_TOP, "Marcap")
    kosdaq = df[df["Market"] == "KOSDAQ"].nlargest(KOSDAQ_TOP, "Marcap")
    uni = pd.concat([kospi, kosdaq]).reset_index(drop=True)
    _warn(f"universe (market-cap proxy): KOSPI {len(kospi)} + KOSDAQ {len(kosdaq)} = {len(uni)}")
    return uni


def _zscore(s):
    s = s.astype(float)
    std = s.std()
    return (s - s.mean()) / std if std and not np.isnan(std) else s * 0.0


def mechanical_score(uni):
    """거래대금회전율·등락률·거래대금을 종합 점수화해 정렬한 DataFrame 반환 (가격 신호)."""
    df = uni.copy()
    df["turnover"] = df["Amount"] / df["Marcap"]            # 거래대금회전율 (규모 대비 관심)
    df["score"] = (
        _zscore(df["turnover"]) * 1.0 +
        _zscore(df["ChagesRatio"]) * 1.0 +
        _zscore(np.log1p(df["Amount"].clip(lower=0))) * 0.5
    )
    return df.sort_values("score", ascending=False).reset_index(drop=True)


# ----------------------------------------------------------------------------
# ⓐ 공시 촉매 후보 (DART 시장 전체 스캔)
# ----------------------------------------------------------------------------
def _is_catalyst(title):
    if any(x in title for x in EXCLUDE_KEYWORDS):          # 정정·해지·부정 공시 제외
        return False
    return any(k in title for k in POSITIVE_KEYWORDS)      # 긍정 촉매 광범위 포함


def disclosure_candidates(universe_codes):
    """전일 장마감~당일 DART 시장 전체 공시를 스캔해 유니버스 내 '촉매' 보유 종목을 반환.

    Returns {code: [ {date, title}, ... ]}. per-stock 호출 없이 시장 전체를 corp_cls(Y/K)와
    날짜 범위로 한 번에 스캔하므로 유니버스 크기와 무관하게 호출 수가 고정된다.

    주의: DART list.json 의 rcept_dt 는 '날짜'만 제공(시각 없음). 따라서 시간창은
    거래일 단위로 근사한다(직전 거래일 + 당일). 가격민감 공시는 대부분 장 마감 후
    접수되어 overnight 과 실무상 거의 일치한다.
    """
    bgn = news_cutoff().strftime("%Y%m%d")                 # pre=직전 거래일 / intraday=당일
    end = datetime.datetime.now(KST).strftime("%Y%m%d")    # 당일
    items = []
    for cls in ("Y", "K"):                                 # 유가증권 + 코스닥
        items += sources.dart_market_disclosures(bgn, end, cls)

    by_code = {}
    for it in items:
        code = (it.get("stock_code") or "").strip().zfill(6)
        if len(code) != 6 or code not in universe_codes:
            continue
        if not _is_catalyst(it.get("title", "")):
            continue
        by_code.setdefault(code, [])
        if len(by_code[code]) < 5:                         # 종목당 최대 5건
            by_code[code].append({"date": it.get("date", ""), "title": it.get("title", "")})
    return by_code


# ----------------------------------------------------------------------------
# 후보 풀 구성: ⓐ 공시(우선) ∪ ⓑ 가격(보조)
# ----------------------------------------------------------------------------
def _pre_watchlist_codes():
    """장중 모드에서 제외할 장전 워치리스트(watchlist.json) 종목코드 집합.
    pre 모드면 빈 집합(자기 자신을 제외할 필요 없음)."""
    if not IS_INTRADAY:
        return set()
    data = _load_json(os.path.join(ROOT, "watchlist.json"), []) or []
    return {str(s.get("code", "")).zfill(6) for s in data
            if isinstance(s, dict) and s.get("code")}


def build_pool(uni, disclosure_map):
    scored = mechanical_score(uni)
    by_code = {r["Code"]: r for r in scored.to_dict("records")}

    pool, seen = [], set()

    # 장중 관심종목은 장전 워치리스트와 겹치지 않게 한다 — 장전 종목을 제외 집합에 미리 넣어
    # 공시(ⓐ)·가격(ⓑ) 후보 양쪽에서 스킵한다.
    exclude = _pre_watchlist_codes()
    seen |= exclude

    # ⓐ 공시 촉매 종목 — 우선. 동률 시 가격 점수 순.
    disc_codes = [c for c in disclosure_map if c in by_code and c not in exclude]
    disc_codes.sort(key=lambda c: by_code[c]["score"], reverse=True)
    for c in disc_codes:
        if len(pool) >= MAX_CANDIDATES:
            break
        row = dict(by_code[c])
        row["disclosures"] = disclosure_map[c]
        row["source"] = "공시"
        pool.append(row)
        seen.add(c)

    # ⓑ 가격 상위 — 보조(촉매가 가격에 이미 반영된 케이스 보완).
    for r in scored.head(PRICE_BACKUP).to_dict("records"):
        if len(pool) >= MAX_CANDIDATES:
            break
        if r["Code"] in seen:
            continue
        row = dict(r)
        row["disclosures"] = disclosure_map.get(r["Code"], [])
        row["source"] = "가격"
        pool.append(row)
        seen.add(r["Code"])

    return pool


# ----------------------------------------------------------------------------
# 3) 뉴스 보강 (전일 장마감~실행시각)
# ----------------------------------------------------------------------------
def _news_one(row, cutoff):
    name, code = row["Name"], row["Code"]
    news = []
    for n in sources.naver_search("news", name, display=NEWS_FETCH):  # 최신순
        dt = _parse_pubdate(n.get("date", ""))
        if dt is not None and dt >= cutoff:
            news.append({"title": n["title"], "date": n.get("date", "")})
        if len(news) >= NEWS_MAX:
            break
    return code, news


def enrich_news(pool):
    """후보별 뉴스(pre=전일 장마감~ / intraday=당일 09:00~)를 병렬 수집해 각 row에 붙인다."""
    cutoff = news_cutoff()
    _warn(f"news window: {cutoff.strftime('%Y-%m-%d %H:%M')} KST ~ now")
    news_by_code = {}
    workers = max(1, min(ENRICH_WORKERS, len(pool)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, news in ex.map(lambda r: _news_one(r, cutoff), pool):
            news_by_code[code] = news
    for row in pool:
        row["news"] = news_by_code.get(row["Code"], [])
    return pool


# ----------------------------------------------------------------------------
# 4) LLM 최종 선정
# ----------------------------------------------------------------------------
SYSTEM = """\
너는 한국 주식 데이 트레이딩 애널리스트다. 장 시작 전, 주어진 데이터로 오늘의 종목을
'상승 워치리스트(picks)'와 '하락 관찰 리스트(downPicks)'로 분류해 선정한다.

입력:
- us_market: 전일 미국 지수/섹터 ETF 등락 + 급등 특징주(movers). 어떤 섹터·테마가 강했는지,
  어떤 미국 종목이 급등했는지를 보고 '국내 동조/밸류체인 연결' 종목을 추론하는 데 쓴다.
- candidates: 코스피200/코스닥150 유니버스에서 1차 선별된 후보. 각 후보는
  source('공시'=촉매 공시 보유 / '가격'=거래대금·모멘텀 상위), 전일 등락률(changePct),
  거래대금(amountKRW), 전일 장마감 이후 공시(disclosures)와 뉴스(news)를 포함한다.

선정 원칙(중요도 순):
1) **방향 게이트(최우선)**: 각 후보의 '지배적 재료'가 상방인지 하방인지 먼저 판정한다.
   악재(계약 해지·수주 실패·기대감 소멸·규제·실적 쇼크·경쟁 심화·소송 등)가 지배적인 종목은
   절대 picks 에 넣지 않는다 — 하방 근거가 분명하면 downPicks 로 분류한다.
   picks 는 자동매매(매수)에 쓰이므로, 상승 근거가 분명한 종목만 담는다.
2) picks: 의미 있는 '촉매 공시'(공급계약·수주·실적·임상/허가·투자·자사주 등)가 있는 종목 최우선.
   전일 미국시장 강세 섹터·급등 특징주와 테마/밸류체인이 연결된 종목, 우호적 뉴스 흐름 종목.
3) downPicks: 하방 재료가 뚜렷해 '오늘 하락 압력이 우려되는' 관찰 종목. reason 에 하방 근거를,
   catalyst 에 핵심 하방 촉매 한 줄을 쓴다. 참고용이며 매매에 쓰이지 않는다.
4) 거래대금·모멘텀(가격)은 위 재료의 '시장 반응 강도' 확인용으로만 쓴다.
- 근거(공시/뉴스/미국 테마) 없는 단순 가격 등락 종목은 어느 리스트에도 넣지 마라.
  후보 목록 안에서만 고른다. 같은 종목을 두 리스트에 중복해 넣지 마라.
- **개수 제한 없음**: 근거가 충분한 종목은 모두 담고, 개수를 채우려고 근거가 약한 종목을
  억지로 넣지 마라. 마땅한 종목이 없으면 빈 배열([])도 허용한다.

반드시 아래 스키마와 정확히 동일한 JSON만 출력한다. 마크다운 펜스/설명 금지.
{
  "marketView": "전일 미국장(지수·섹터·특징주) 요약과 오늘 국내 시장 관점 3-4문장 (한국어)",
  "picks": [
    {"code": "6자리", "name": "종목명", "market": "KOSPI|KOSDAQ",
     "reason": "선정 사유 1-2문장 (공시/미국테마/뉴스 근거 명시)",
     "catalyst": "핵심 촉매 한 줄"}
  ],
  "downPicks": [
    {"code": "6자리", "name": "종목명", "market": "KOSPI|KOSDAQ",
     "reason": "하방 우려 사유 1-2문장 (악재 근거 명시)",
     "catalyst": "핵심 하방 촉매 한 줄"}
  ]
}"""


SYSTEM_INTRADAY = """\
너는 한국 주식 데이 트레이딩 애널리스트다. **지금은 장중**이다. 이미 거래가 진행 중인 상태에서,
'지금부터 추가 급등 여지가 큰' 한국 종목 {n}개를 선정한다.

입력:
- us_market: 전일 미국 지수/섹터 ETF 등락 + 급등 특징주(movers). 국내 동조/밸류체인 연결 추론용.
- candidates: 코스피200/코스닥150 유니버스에서 1차 선별된 후보. 각 후보는 source('공시'=당일 촉매 공시
  보유 / '가격'=장중 거래대금·등락 상위), **장중 실시간 등락률(changePct, 전일 종가 대비 현재가)**,
  거래대금(amountKRW), **당일 장중 접수 공시(disclosures)**, 당일 뉴스(news)를 포함한다.

선정 원칙(중요도 순):
1) **오늘 장중에 새로 나온 촉매 공시**(공급계약·수주·실적·임상/허가·투자·자사주 등)를 가진 종목 최우선.
2) 당일 우호적 뉴스가 막 나온 종목, 전일 미국시장 강세 섹터·급등 특징주와 테마/밸류체인이 연결된 종목.
3) 장중 등락률·거래대금은 '시장이 이미 반응 중인 강도' 확인용. 다만 **이미 상한가 근처까지 급등해
   추격 여력이 적은 종목보다, 촉매가 분명하고 모멘텀이 막 붙기 시작한 종목**을 우선한다.
- **방향 게이트**: 각 후보의 지배적 재료가 상방인지 하방인지 먼저 판정한다. 하방(악재·계약 해지·
  기대감 소멸·규제·실적 쇼크 등)이 지배적인 종목은 절대 picks 에 넣지 않는다 — 하방 근거가
  분명하면 downPicks(하락 관찰, 참고용)로 분류한다. picks 는 자동매매(매수) 대상이다.
- downPicks: **오늘 장중에 새로 나온 악재**(부정 공시·악재 뉴스·급락 촉매)로 하락 압력이 우려되는
  관찰 종목. reason 에 하방 근거를, catalyst 에 핵심 하방 촉매 한 줄을 쓴다. 매매에 쓰이지 않는다.
- 근거(당일 공시/뉴스/미국 테마) 없는 단순 가격 등락은 어느 리스트에도 넣지 마라.
  후보 목록 안에서만 고른다. 같은 종목을 두 리스트에 중복해 넣지 마라.

반드시 아래 스키마와 정확히 동일한 JSON만 출력한다. 마크다운 펜스/설명 금지.
{
  "marketView": "현재 장중 시장 상황과 오늘 주목할 테마 3-4문장 (한국어)",
  "picks": [
    {"code": "6자리", "name": "종목명", "market": "KOSPI|KOSDAQ",
     "reason": "선정 사유 1-2문장 (당일 공시/미국테마/뉴스 근거 명시)",
     "catalyst": "핵심 촉매 한 줄"}
  ],
  "downPicks": [
    {"code": "6자리", "name": "종목명", "market": "KOSPI|KOSDAQ",
     "reason": "하방 우려 사유 1-2문장 (당일 악재 근거 명시)",
     "catalyst": "핵심 하방 촉매 한 줄"}
  ]
}
**중요: picks/downPicks 는 근거(촉매 공시·당일 뉴스·미국 테마 연결)가 충분한 종목만 담는다.
각각 최대 {n}개이며, 근거가 분명한 종목이 그보다 적으면 적게 담고, 마땅한 종목이 없으면
빈 배열([])도 허용한다. 개수를 채우려고 근거가 약한 종목을 억지로 넣지 마라.**"""


_STR = {"type": "string"}
_PICK_ITEMS = {"type": "array", "items": {
    "type": "object", "additionalProperties": False,
    "properties": {"code": _STR, "name": _STR, "market": _STR,
                   "reason": _STR, "catalyst": _STR},
    "required": ["code", "name", "market", "reason", "catalyst"]}}
SELECT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "marketView": _STR,
        "picks": _PICK_ITEMS,
        "downPicks": _PICK_ITEMS,   # 하락 관찰(참고용) — 장전 모드에서만 사용
    },
    "required": ["marketView", "picks"],
}


def llm_select(us_brief, pool):
    """LLM 으로 최종 N종목 선정. 실패 시 None 반환(호출부에서 fallback)."""
    if not llm.configured():
        _warn("no LLM provider configured; using mechanical fallback")
        return None

    candidates = []
    for r in pool:
        candidates.append({
            "code": r["Code"],
            "name": r["Name"],
            "market": r["Market"],
            "source": r.get("source", ""),
            "changePct": round(float(r["ChagesRatio"]), 2),
            "amountKRW": int(r["Amount"]),
            "disclosures": [d["title"] for d in r.get("disclosures", [])][:5],
            "news": [n["title"] for n in r.get("news", [])][:5],
        })

    user = json.dumps({"us_market": us_brief, "candidates": candidates}, ensure_ascii=False)
    system = (SYSTEM_INTRADAY if IS_INTRADAY else SYSTEM).replace("{n}", str(N_FINAL))
    try:
        result = llm.generate_json(system, user, max_tokens=MAX_TOKENS, schema=SELECT_SCHEMA)
    except Exception as e:
        _warn(f"LLM selection failed: {e}")
        return None

    valid = {r["Code"]: r for r in pool}          # 후보 목록 안의 종목만 신뢰 (환각 방지)

    def _validated(raw, exclude=()):
        out = []
        for p in raw or []:
            code = str(p.get("code", "")).zfill(6)
            if code in valid and code not in exclude \
                    and not any(code == x["code"] for x in out):
                out.append({
                    "code": code,
                    "name": valid[code]["Name"],
                    "market": valid[code]["Market"],
                    "reason": p.get("reason", ""),
                    "catalyst": p.get("catalyst", ""),
                    "changePct": round(float(valid[code]["ChagesRatio"]), 2),
                })
        return out

    picks = _validated(result.get("picks"))
    # 하락 관찰(장전·장중 공통). 상승 리스트와 중복되면 상승을 우선하고 제외한다.
    down = _validated(result.get("downPicks"), exclude={x["code"] for x in picks})
    if not picks:
        # 장중: 근거가 충분한 종목이 없으면 빈 결과도 정상(억지로 채우지 않는다).
        #       하락 관찰만 포착된 회차도 유효하므로 downPicks 는 유지한다.
        # 장전: 기존대로 None 을 반환해 호출부가 기계적 fallback 으로 채운다.
        if IS_INTRADAY:
            return {"marketView": result.get("marketView", ""), "picks": [], "downPicks": down}
        return None
    return {"marketView": result.get("marketView", ""), "picks": picks, "downPicks": down}


def fallback_select(pool):
    """LLM 불가 시: 풀은 이미 '공시 우선 → 가격' 순이므로 상위 N_FINAL 을 선정."""
    picks = []
    for r in pool[:N_FINAL]:
        disc = r.get("disclosures") or []
        cat = disc[0]["title"] if disc else "기계적 스크리닝 상위 (뉴스/LLM 미적용)"
        picks.append({
            "code": r["Code"], "name": r["Name"], "market": r["Market"],
            "reason": (f"[{r.get('source','')}] 전일 등락률 {float(r['ChagesRatio']):+.2f}%, "
                       f"거래대금 {int(r['Amount']):,}원."),
            "catalyst": cat,
            "changePct": round(float(r["ChagesRatio"]), 2),
        })
    return {"marketView": "LLM 선정을 사용할 수 없어 후보 점수 상위 종목으로 대체했습니다.",
            "picks": picks}


# ----------------------------------------------------------------------------
# 출력
# ----------------------------------------------------------------------------
def write_outputs(selection, us_brief):
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    picks = selection["picks"]
    os.makedirs(SELECTION_DIR, exist_ok=True)
    selection_path = os.path.join(SELECTION_DIR, f"{today}.json")

    if IS_INTRADAY:
        # 장중은 매 회차 결과를 누적(append)한다 — 워크플로가 30분마다 돌며 그날 새로 포착한
        # 종목을 기존 관심종목에 더한다. 당일 첫 실행(오늘 selection 파일이 아직 없음)이면
        # 전일 누적이 이어지지 않도록 리셋하고 새로 시작한다. 중복은 code 로 제거한다.
        first_run = not os.path.exists(selection_path)
        prev = [] if first_run else (_load_json(WATCHLIST, []) or [])
        # 장전 워치리스트와 겹치는 종목은 제외한다. 기존 누적(prev)에 남아있던 중복도
        # 정리하고, picks 의 장전 중복도 seen 으로 막는다.
        pre_codes = _pre_watchlist_codes()
        prev = [p for p in prev if isinstance(p, dict)
                and str(p.get("code", "")).zfill(6) not in pre_codes]
        seen = {str(p.get("code")) for p in prev} | pre_codes
        watchlist = list(prev)
        added = 0
        for p in picks:
            if p["code"] in seen or str(p["code"]).zfill(6) in pre_codes:
                continue
            watchlist.append({"code": p["code"], "name": p["name"], "market": p["market"]})
            seen.add(p["code"])
            added += 1
        if INTRADAY_CAP and len(watchlist) > INTRADAY_CAP:    # 상한 초과 시 최근 종목 우선 유지
            watchlist = watchlist[-INTRADAY_CAP:]
        print(f"  Intraday append   : +{added} new (first_run={first_run}) → 누적 {len(watchlist)}종목")
    else:
        watchlist = [{"code": p["code"], "name": p["name"], "market": p["market"]} for p in picks]

    with open(WATCHLIST, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)
    print(f"  Updated watchlist : {WATCHLIST} ({len(watchlist)} stocks)")

    # 하락 관찰 리스트 — LLM 미가동(fallback) 실행이면 빈 리스트로 갱신해
    # 전일 하락 리스트가 오늘 것처럼 남지 않게 한다.
    down = selection.get("downPicks") or []
    _down_entry = lambda p: {"code": p["code"], "name": p["name"], "market": p["market"],
                             "reason": p.get("reason", ""), "catalyst": p.get("catalyst", ""),
                             "changePct": p.get("changePct")}
    if IS_INTRADAY:
        # 상승 누적과 동일한 당일 누적(append) — 첫 회차 리셋, code 중복 제거, 상한 공유.
        # 장전 상승/장전 하락/장중 상승 누적에 이미 있는 코드는 스킵(상승 우선 + 중복 방지).
        pre_down_codes = {str(p.get("code", "")).zfill(6)
                          for p in (_load_json(PRE_WATCHLIST_DOWN, []) or [])
                          if isinstance(p, dict)}
        up_codes = {str(w.get("code", "")).zfill(6) for w in watchlist}
        exclude_d = pre_codes | pre_down_codes | up_codes
        prev_down = [] if first_run else (_load_json(WATCHLIST_DOWN, []) or [])
        prev_down = [p for p in prev_down if isinstance(p, dict)
                     and str(p.get("code", "")).zfill(6) not in exclude_d]
        seen_d = {str(p.get("code")) for p in prev_down} | exclude_d
        down_list = list(prev_down)
        added_d = 0
        for p in down:
            if p["code"] in seen_d:
                continue
            down_list.append(_down_entry(p))
            seen_d.add(p["code"])
            added_d += 1
        if INTRADAY_CAP and len(down_list) > INTRADAY_CAP:
            down_list = down_list[-INTRADAY_CAP:]
        with open(WATCHLIST_DOWN, "w", encoding="utf-8") as f:
            json.dump(down_list, f, ensure_ascii=False, indent=2)
        print(f"  Intraday down     : +{added_d} new → 누적 {len(down_list)}종목 ({WATCHLIST_DOWN})")
    else:
        with open(WATCHLIST_DOWN, "w", encoding="utf-8") as f:
            json.dump([_down_entry(p) for p in down], f, ensure_ascii=False, indent=2)
        print(f"  Updated downlist  : {WATCHLIST_DOWN} ({len(down)} stocks)")

    payload = {
        "date": today,
        "mode": MODE,                                              # "pre" | "intraday"
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "usMarket": us_brief,
        "marketView": selection.get("marketView", ""),
        "picks": picks,
        "downPicks": down,
    }
    with open(selection_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  Saved selection   : {selection_path}")


def main():
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    label = "Intraday screener (장중 관심종목)" if IS_INTRADAY else "Pre-market screener (장전 워치리스트)"
    print(f"=== {label} ({today}) ===")

    print("1) 전일 미국시장 분석 (지수·섹터·급등 특징주)...")
    us_brief = us_market_brief()
    if us_brief.get("sectors"):
        top = us_brief["sectors"][0]
        print(f"   미국 섹터 1위: {top['name']} {top['changePct']:+.2f}%")
    if us_brief.get("movers"):
        print(f"   미국 급등 특징주 {len(us_brief['movers'])}개 (1위 "
              f"{us_brief['movers'][0]['symbol']} {us_brief['movers'][0]['changePct']:+.1f}%)")

    print("2) 유니버스 + 후보 구성 (공시 촉매 + 가격)...")
    uni = load_universe()
    universe_codes = set(uni["Code"])

    # 섹터별 대표 국내 종목의 전일 등락률(모닝 브리핑 '국내 밸류체인' 표시용).
    # uni 에 이미 로드된 코스피200/코스닥150 데이터를 조회만 하므로 추가 네트워크 호출 없음.
    code_to_row = {row["Code"]: row for row in uni.to_dict("records")}
    kr_sector_stocks = {}
    for sector, codes in SECTOR_KR_STOCKS.items():
        rows = []
        for code in codes:
            row = code_to_row.get(code)
            if row is not None:
                rows.append({"code": code, "name": row["Name"],
                             "changePct": round(float(row["ChagesRatio"]), 2)})
        rows.sort(key=lambda d: abs(d["changePct"]), reverse=True)
        kr_sector_stocks[sector] = rows
    us_brief["krSectorStocks"] = kr_sector_stocks
    disclosure_map = disclosure_candidates(universe_codes)
    print(f"   공시 촉매 종목: {len(disclosure_map)}개")
    pool = build_pool(uni, disclosure_map)
    n_disc = sum(1 for r in pool if r.get("source") == "공시")
    print(f"   후보 풀: {len(pool)}종목 (공시 {n_disc} + 가격 {len(pool) - n_disc})")

    print("3) 뉴스 보강 (전일 장마감~실행시각)...")
    try:
        pool = enrich_news(pool)
    except Exception as e:
        _warn(f"news enrichment failed: {e}")
        for r in pool:
            r.setdefault("news", [])

    print("4) LLM 최종 선정...")
    selection = llm_select(us_brief, pool)
    if selection is None:
        # 장중: LLM 호출 실패 시 기계적으로 채우지 않고 이번 회차를 비운다(기존 누적 유지).
        # 장전: 파이프라인이 항상 돌도록 후보 점수 상위로 채운다.
        selection = ({"marketView": "장중 LLM 선정 실패 — 이번 회차는 신규 선정 없이 기존 관심종목을 유지합니다.",
                      "picks": []} if IS_INTRADAY else fallback_select(pool))

    write_outputs(selection, us_brief)
    print("   선정 종목: " + ", ".join(f"{p['name']}({p['code']})" for p in selection["picks"]))
    down = selection.get("downPicks") or []
    if down:
        print("   하락 관찰: " + ", ".join(f"{p['name']}({p['code']})" for p in down))
    print("=== Done ===")


if __name__ == "__main__":
    main()
