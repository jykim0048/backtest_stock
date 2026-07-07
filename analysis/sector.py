"""
Sector & theme quant analysis — top-down (macro -> sentiment -> sector -> stock).

Implements the sector-theme-research skill's 5-phase workflow using the same
REST-direct pattern as analysis/sources.py (no MCP): FRED for macro, CNN Fear &
Greed for sentiment, yfinance for sector ETFs and stock financials, and the
existing analysis/sources.py (DART/Naver/Tavily) for theme-stock catalysts. The
LLM step (llm.generate_json) synthesizes the collected RAW data into the report.

All fetchers degrade gracefully: on any error they return an empty result and
log to stderr, so a single failing API never kills the run. Every number is
tagged with its source (skill best-practices requirement).

Env vars: FRED_API_KEY (macro), plus the sources.py / llm.py keys.
"""
import os
import sys
import json
import datetime

import requests

# yfinance writes a cache; on read-only CI point it at a writable dir.
for _mod in ("appdirs", "platformdirs"):
    try:
        _m = __import__(_mod)
        _m.user_cache_dir = lambda *a, **k: "/tmp"
    except Exception:
        pass

import pandas as pd
import yfinance as yf

import llm
from analysis import sources

UA = {"User-Agent": "Mozilla/5.0 (quant-antigravity sector)"}
KST = datetime.timezone(datetime.timedelta(hours=9))


def _warn(msg):
    print(f"[sector] {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Phase 1) FRED macro
# ----------------------------------------------------------------------------
# (series_id, display name, unit) — skill workflow.md Step 1-1
# "NAPMPI"(ISM 제조업 PMI로 잘못 표시되던 항목)는 2026-07 제거함: 애초에 종합 PMI가
# 아니라 그 하위지수인 "ISM Manufacturing: Production Index"였고, 그마저도 2016-06
# ISM이 자사 데이터를 FRED에서 전부 내려달라 요청해 시리즈 자체가 폐기(discontinued)됐다.
# FRED에는 대체 가능한 ISM 계열 시리즈가 없어 항목을 뺀다(잘못된 값을 보여주는 것보다 낫다).
FRED_SERIES = [
    ("FEDFUNDS",   "기준금리",        "%"),
    ("DGS10",      "10Y 국채금리",    "%"),
    ("DGS2",       "2Y 국채금리",     "%"),
    ("T10Y2Y",     "장단기 스프레드", "%p"),
    ("CPIAUCSL",   "CPI",             "idx"),
    ("CPILFESL",   "Core CPI",        "idx"),
    ("UNRATE",     "실업률",          "%"),
    ("INDPRO",     "산업생산",        "idx"),
    ("VIXCLS",     "VIX",             "pt"),
    ("BAMLH0A0HYM2", "HY 스프레드",   "%"),
]


def _fred_key():
    return os.environ.get("FRED_API_KEY")


def fred_series(series_id, limit=24):
    """Return newest-first observations [{date, value}] for a FRED series.
    Empty list if key missing or on error (graceful degradation)."""
    key = _fred_key()
    if not key:
        _warn("FRED_API_KEY missing")
        return []
    try:
        r = requests.get(
            "https://api.stlouisfed.org/fred/series/observations",
            params={"series_id": series_id, "api_key": key, "file_type": "json",
                    "sort_order": "desc", "limit": limit},
            timeout=20,
        )
        r.raise_for_status()
        out = []
        for o in r.json().get("observations", []):
            v = o.get("value")
            if v in (None, ".", ""):
                continue
            try:
                out.append({"date": o.get("date", ""), "value": float(v)})
            except ValueError:
                continue
        return out
    except Exception as e:
        _warn(f"fred_series({series_id}) failed: {e}")
        return []


def fetch_macro():
    """Phase 1: collect FRED series -> {id: {name, unit, latest, prev, observations}}."""
    macro = {}
    for sid, name, unit in FRED_SERIES:
        obs = fred_series(sid, limit=13)
        latest = obs[0]["value"] if obs else None
        prev = obs[1]["value"] if len(obs) > 1 else None
        macro[sid] = {
            "name": name, "unit": unit,
            "latest": latest, "prev": prev,
            "asof": obs[0]["date"] if obs else None,
            "source": f"FRED, {sid}" + (f", as of {obs[0]['date']}" if obs else " [Data Unavailable]"),
        }

    # 장단기 스프레드는 FRED 의 T10Y2Y 시리즈를 그대로 믿지 않고 DGS10-DGS2 를 직접 계산한다.
    # DGS10/DGS2/T10Y2Y 는 서로 독립적으로 "최신 관측치"를 가져오는데, 국채금리 시리즈는
    # 발표일이 하루씩 어긋나는 경우가 있어(예: DGS10 최신치가 화요일자인데 T10Y2Y 최신치는
    # 월요일자) 화면에 보이는 10Y-2Y 값과 스프레드 숫자가 안 맞는 것처럼 보일 수 있다.
    # 두 시리즈가 모두 있으면 항상 같은 latest/prev 위치끼리 빼서 내부 정합성을 보장하고,
    # 어느 한쪽이라도 없으면 FRED 의 T10Y2Y 자체 값으로 폴백한다.
    d10, d2 = macro.get("DGS10"), macro.get("DGS2")
    if d10 and d2 and d10.get("latest") is not None and d2.get("latest") is not None:
        prev_spread = (round(d10["prev"] - d2["prev"], 2)
                       if d10.get("prev") is not None and d2.get("prev") is not None else None)
        macro["T10Y2Y"] = {
            "name": "장단기 스프레드", "unit": "%p",
            "latest": round(d10["latest"] - d2["latest"], 2),
            "prev": prev_spread,
            "asof": d10.get("asof"),
            "source": f"FRED, DGS10({d10.get('asof')}) - DGS2({d2.get('asof')}) 직접 계산",
        }
    return macro


def classify_regime(macro):
    """4-quadrant regime (확장/과열/침체/회복) from FRED (skill workflow.md Step 1-2).
    Rate direction (FEDFUNDS trend) x inflation direction (Core CPI trend)."""
    def _dir(sid):
        m = macro.get(sid, {})
        a, b = m.get("latest"), m.get("prev")
        if a is None or b is None:
            return 0
        return 1 if a > b else (-1 if a < b else 0)

    rate_up = _dir("FEDFUNDS") >= 0
    infl_up = _dir("CPILFESL") >= 0
    inverted = (macro.get("T10Y2Y", {}).get("latest") or 0) < 0

    if rate_up and infl_up:
        regime = "과열"
        reason = "금리·물가 동반 상승 국면"
        prefer = ["Energy", "Materials", "Industrials"]
    elif rate_up and not infl_up:
        regime = "침체" if inverted else "확장"
        reason = "긴축 사이클 속 물가 둔화" + (" · 장단기 금리 역전" if inverted else "")
        prefer = ["Utilities", "Healthcare", "Staples"] if inverted else ["Tech", "Discretionary", "Financials"]
    elif not rate_up and infl_up:
        regime = "침체"
        reason = "금리 하락에도 물가 압력 지속(스태그 우려)"
        prefer = ["Utilities", "Healthcare", "Staples"]
    else:
        regime = "회복"
        reason = "금리·물가 동반 안정 → 완화적 환경"
        prefer = ["Financials", "Industrials", "Real Estate"]
    return {"regime": regime, "regimeReason": reason, "preferredSectors": prefer,
            "yieldInverted": inverted}


# ----------------------------------------------------------------------------
# Phase 2) CNN Fear & Greed
# ----------------------------------------------------------------------------
def _rating_for(score):
    if score < 25:  return "Extreme Fear"
    if score < 45:  return "Fear"
    if score <= 55: return "Neutral"
    if score <= 74: return "Greed"
    return "Extreme Greed"


def fetch_fear_greed():
    """Phase 2: CNN Fear & Greed (7 components + history). Falls back to
    alternative.me on error. Returns dict; empty-ish on total failure."""
    try:
        r = requests.get(
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            headers=UA, timeout=20)
        r.raise_for_status()
        d = r.json()
        fg = d.get("fear_and_greed", {}) or {}
        score = round(float(fg.get("score", 0)))
        comp_keys = [
            ("market_momentum_sp500", "Momentum"),
            ("stock_price_strength", "Strength"),
            ("stock_price_breadth", "Breadth"),
            ("put_call_options", "Put/Call"),
            ("market_volatility_vix", "Volatility"),
            ("junk_bond_demand", "Junk Bond"),
            ("safe_haven_demand", "Safe Haven"),
        ]
        components = []
        for k, label in comp_keys:
            c = d.get(k, {}) or {}
            if "score" in c or "rating" in c:
                s = c.get("score")
                components.append({
                    "name": label,
                    "score": round(float(s)) if isinstance(s, (int, float)) else None,
                    "rating": c.get("rating", ""),
                })
        return {
            "score": score,
            "rating": fg.get("rating") or _rating_for(score),
            "prevClose": fg.get("previous_close"),
            "prevWeek": fg.get("previous_1_week"),
            "prevMonth": fg.get("previous_1_month"),
            "prevYear": fg.get("previous_1_year"),
            "components": components,
            "source": "CNN Fear & Greed Index, "
                      + datetime.datetime.now(KST).strftime("%Y-%m-%d"),
        }
    except Exception as e:
        _warn(f"CNN fear&greed failed: {e} — alternative.me 폴백")
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=8", timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        if not data:
            return {"score": None, "rating": "[Data Unavailable]", "components": [],
                    "source": "[Data Unavailable]"}
        cur = data[0]
        score = int(cur.get("value", 0))
        return {
            "score": score,
            "rating": cur.get("value_classification") or _rating_for(score),
            "prevClose": int(data[1]["value"]) if len(data) > 1 else None,
            "prevWeek": int(data[7]["value"]) if len(data) > 7 else None,
            "prevMonth": None, "prevYear": None,
            "components": [],
            "note": "CNN 미가용 — alternative.me(크립토 지수) 대체값",
            "source": "alternative.me Fear & Greed (crypto), "
                      + datetime.datetime.now(KST).strftime("%Y-%m-%d"),
        }
    except Exception as e:
        _warn(f"alternative.me fear&greed failed: {e}")
        return {"score": None, "rating": "[Data Unavailable]", "components": [],
                "source": "[Data Unavailable]"}


def interpret_sentiment(score):
    """Skill workflow.md Step 2-2 strategy direction from F&G score."""
    if score is None:
        return "센티먼트 데이터 미가용"
    if score < 25:
        return "Extreme Fear — 역발상: 품질 우량주 매수 기회 탐색, Defensives 확인"
    if score < 45:
        return "Fear — 선별적 매수, 실적 확인된 Quality Growth 중심"
    if score <= 55:
        return "Neutral — 모멘텀 추종 + 밸류 균형"
    if score <= 74:
        return "Greed — 모멘텀 전략, 강한 성장 섹터 추종"
    return "Extreme Greed — 리스크 관리 강화, 비중 축소·Trailing Stop"


# ----------------------------------------------------------------------------
# Phase 3) Sector ETF performance (yfinance)
# ----------------------------------------------------------------------------
US_SECTOR_ETFS = [
    ("XLK", "Technology"), ("XLV", "Healthcare"), ("XLF", "Financials"),
    ("XLE", "Energy"), ("XLI", "Industrials"), ("XLP", "Consumer Staples"),
    ("XLY", "Consumer Discretionary"), ("XLB", "Materials"),
    ("XLRE", "Real Estate"), ("XLU", "Utilities"), ("XLC", "Communication Services"),
    ("SMH", "Semiconductors"), ("XBI", "Biotech"),
]

# 브리핑 usTheme(스크리너 US_SECTORS 한글 섹터명) → 섹터 ETF 티커.
# 테마별 심층분석에서 phase 3 수익률을 해당 테마에 붙일 때 사용.
BRIEFING_THEME_ETF = {
    "기술": "XLK", "반도체": "SMH", "헬스케어": "XLV", "바이오": "XBI",
    "에너지": "XLE", "금융": "XLF", "산업재": "XLI", "소재": "XLB",
    "임의소비재": "XLY",
}


def _returns_from_close(close):
    """Compute 1W/1M/3M/6M/YTD % returns from a daily close Series (oldest->newest).
    1Y 는 표시하지 않는다 — period="1y" 다운로드(~250거래일)로는 252일 전 종가가
    항상 부족해 공란이었고, 단기 트레이딩 대시보드에는 1W 가 더 유효한 신호."""
    if close is None or len(close) < 2:
        return {}
    last = float(close.iloc[-1])

    def _ret(days):
        if len(close) <= days:
            return None
        base = float(close.iloc[-1 - days])
        return round((last / base - 1) * 100, 2) if base else None

    # YTD: first trading day of current year
    ytd = None
    try:
        yr = close.index[-1].year
        ys = close[close.index >= f"{yr}-01-01"]
        if len(ys) >= 1:
            base = float(ys.iloc[0])
            ytd = round((last / base - 1) * 100, 2) if base else None
    except Exception:
        pass
    return {"ret1W": _ret(5), "ret1M": _ret(21), "ret3M": _ret(63),
            "ret6M": _ret(126), "retYTD": ytd}


def fetch_sector_etfs():
    """Phase 3: 13 sector ETFs + SPY benchmark, 1Y daily -> period returns.
    Returns (rows, benchmark) — benchmark 는 SPY 의 수익률 dict (스코어카드의
    상대 모멘텀 계산용). Source-tagged rows."""
    tickers = [t for t, _ in US_SECTOR_ETFS] + ["SPY"]
    rows = []
    try:
        df = yf.download(tickers, period="1y", interval="1d", group_by="ticker",
                         progress=False, threads=True, auto_adjust=True)
    except Exception as e:
        _warn(f"sector ETF download failed: {e}")
        df = None

    def _close_of(tk):
        try:
            if df is not None:
                if isinstance(df.columns, pd.MultiIndex):
                    if tk in df.columns.get_level_values(0):
                        return df[tk]["Close"].dropna()
                elif "Close" in df.columns:
                    return df["Close"].dropna()
        except Exception as e:
            _warn(f"sector ETF parse {tk}: {e}")
        return None

    for tk, name in US_SECTOR_ETFS:
        rets = _returns_from_close(_close_of(tk))
        rows.append({
            "etf": tk, "name": name, **rets,
            "source": f"Yahoo Finance, {tk}, "
                      + datetime.datetime.now(KST).strftime("%Y-%m-%d")
                      + ("" if rets else " [Data Unavailable]"),
        })
    # sort by 3M momentum (skill scoring proxy), unknowns last
    rows.sort(key=lambda r: (r.get("ret3M") is None, -(r.get("ret3M") or -999)))
    benchmark = _returns_from_close(_close_of("SPY"))
    return rows, benchmark


def fetch_etf_pes(tickers):
    """스코어카드 밸류에이션 항목용 — 섹터 ETF 별 trailing PE (yfinance .info).
    ETF 는 PE 미제공인 경우가 흔해 결측은 None 으로 남긴다."""
    pes = {}
    for tk in tickers:
        try:
            info = yf.Ticker(tk).info or {}
            pe = info.get("trailingPE")
            pes[tk] = round(pe, 1) if isinstance(pe, (int, float)) and pe > 0 else None
        except Exception as e:
            _warn(f"etf pe {tk}: {e}")
            pes[tk] = None
    return pes


# --- Step 3-3 타깃 섹터 점수화 모델 (skill workflow.md) --------------------
# 스킬 원본은 6항목 20점 만점이나, '공시·뉴스 모멘텀'(0–3)은 섹터 단위 데이터
# 소스가 아직 없어 제외 — 5항목 17점 만점. verdict 는 비율 컷오프라 항목이
# 추가돼도 기준이 유지된다 (>=70% OW, >=40% N, 미만 UW).
_ETF_CANON = {  # ETF -> regime.preferredSectors 명칭
    "XLK": "Tech", "SMH": "Tech", "XLC": "Tech",
    "XLV": "Healthcare", "XBI": "Healthcare",
    "XLF": "Financials", "XLE": "Energy", "XLI": "Industrials",
    "XLP": "Staples", "XLY": "Discretionary", "XLB": "Materials",
    "XLRE": "Real Estate", "XLU": "Utilities",
}
_SUB_ETFS = {"SMH", "XBI", "XLC"}          # 상위 섹터에서 파생된 서브섹터 ETF
_GROWTH_ETFS = {"XLK", "SMH", "XLY", "XLC", "XBI"}
_DEFENSIVE_ETFS = {"XLU", "XLP", "XLV"}


def score_sectors(sectors, regime, sentiment_score, benchmark, etf_pes):
    """섹터별 결정론적 스코어카드. 반환: {etf: {score,maxScore,verdict,parts[]}}"""
    prefer = set(regime.get("preferredSectors") or [])
    pes = [v for v in etf_pes.values() if v]
    pe_median = sorted(pes)[len(pes) // 2] if pes else None
    spy3m = (benchmark or {}).get("ret3M")
    out = {}
    for s in sectors:
        tk = s["etf"]
        parts = []

        # ① 매크로 적합도 (0–5): 국면 선호 섹터 여부
        canon = _ETF_CANON.get(tk, "")
        if canon in prefer:
            m = 4 if tk in _SUB_ETFS else 5
            m_note = f"{regime.get('regime','')} 국면 선호 섹터" + (" (서브섹터)" if tk in _SUB_ETFS else "")
        else:
            m, m_note = 2, "국면 선호 섹터 아님 (중립)"
        parts.append({"key": "macro", "label": "매크로 적합도", "score": m, "max": 5, "note": m_note})

        # ② 센티먼트 정합성 (0–3): F&G 구간 x 성장/방어/시클리컬 성격
        kind = "growth" if tk in _GROWTH_ETFS else ("defensive" if tk in _DEFENSIVE_ETFS else "cyclical")
        if sentiment_score is None:
            sc, sc_note = 0, "센티먼트 미가용"
        elif sentiment_score >= 56:
            sc = {"growth": 3, "cyclical": 2, "defensive": 1}[kind]
            sc_note = f"Greed 구간({sentiment_score}) — 성장 섹터 우위"
        elif sentiment_score >= 45:
            sc, sc_note = 2, f"Neutral 구간({sentiment_score})"
        else:
            sc = {"defensive": 3, "cyclical": 2, "growth": 1}[kind]
            sc_note = f"Fear 구간({sentiment_score}) — 방어 섹터 우위"
        parts.append({"key": "sentiment", "label": "센티먼트 정합성", "score": sc, "max": 3, "note": sc_note})

        # ③ 3M 모멘텀 vs SPY (0–3)
        r3 = s.get("ret3M")
        if r3 is None or spy3m is None:
            mo, mo_note = 0, "수익률 미가용"
        else:
            ex = r3 - spy3m
            mo = 3 if ex > 5 else (2 if ex > 0 else (1 if ex > -5 else 0))
            mo_note = f"SPY 대비 {ex:+.1f}%p"
        parts.append({"key": "momentum", "label": "3M 모멘텀 (vs SPY)", "score": mo, "max": 3, "note": mo_note})

        # ④ 밸류에이션 매력도 (0–3): ETF trailing PE vs 섹터 중앙값
        pe = etf_pes.get(tk)
        if pe is None or pe_median is None:
            va, va_note = 1, "ETF PE 미가용 (중립 1점)"
        else:
            ratio = pe / pe_median
            va = 3 if ratio < 0.85 else (2 if ratio < 1.0 else (1 if ratio < 1.15 else 0))
            va_note = f"PE {pe} vs 중앙값 {pe_median} ({ratio:.2f}배)"
        parts.append({"key": "valuation", "label": "밸류에이션 매력도", "score": va, "max": 3, "note": va_note})

        # ⑤ 이익성장 모멘텀 근사 (0–3): 최근 3M vs 직전 3M 수익률 가속도
        r6 = s.get("ret6M")
        if r3 is None or r6 is None:
            gr, gr_note = 0, "수익률 미가용"
        else:
            accel = r3 - (r6 - r3)
            gr = 3 if accel > 3 else (2 if accel > 0 else (1 if accel > -3 else 0))
            gr_note = f"3M 가속도 {accel:+.1f}%p"
        parts.append({"key": "growth", "label": "이익성장 모멘텀", "score": gr, "max": 3, "note": gr_note})

        total = sum(p["score"] for p in parts)
        max_score = sum(p["max"] for p in parts)
        pct = total / max_score if max_score else 0
        verdict = "OW" if pct >= 0.70 else ("N" if pct >= 0.40 else "UW")
        out[tk] = {"etf": tk, "name": s["name"], "score": total, "maxScore": max_score,
                   "verdict": verdict, "parts": parts,
                   "source": "결정론적 산출 (skill Step 3-3 점수화 모델, 공시·뉴스 항목 제외)"}
    return out


# ----------------------------------------------------------------------------
# Phase 4) Theme stocks (from briefing flow[]) — catalysts + valuation
# ----------------------------------------------------------------------------
def _yf_valuation(code, market):
    """Best-effort valuation/momentum/target via yfinance. Empty on error.
    skill Phase 4(퀄리티 스코어 입력) + Phase 5(목표주가·상승여력) 항목 수집."""
    suffix = ".KS" if market == "KOSPI" else ".KQ"
    try:
        tkr = yf.Ticker(f"{code}{suffix}")
        info = tkr.info or {}
        def _num(x, nd=1):
            return round(x, nd) if isinstance(x, (int, float)) else None
        def _pct(x):
            return round(x * 100, 1) if isinstance(x, (int, float)) else None

        out = {
            "per": _num(info.get("trailingPE")),
            "pbr": _num(info.get("priceToBook"), 2),
            "roe": _pct(info.get("returnOnEquity")),
            "revGrowth": _pct(info.get("revenueGrowth")),
            "beta": _num(info.get("beta"), 2),
            "debtToEquity": _num(info.get("debtToEquity")),  # yfinance 는 % 단위
            "marketCap": info.get("marketCap") if isinstance(info.get("marketCap"), (int, float)) else None,
        }

        # 3M 수익률 (Q점수 모멘텀 입력) — 일봉 히스토리에서 직접 계산
        price = None
        try:
            close = tkr.history(period="4mo")["Close"].dropna()
            if len(close) >= 2:
                price = float(close.iloc[-1])
                base = float(close.iloc[-63]) if len(close) > 63 else float(close.iloc[0])
                if base:
                    out["ret3M"] = round((price / base - 1) * 100, 2)
        except Exception as e:
            _warn(f"yf history {code}: {e}")
        if price is None:
            p = info.get("regularMarketPrice") or info.get("currentPrice")
            price = float(p) if isinstance(p, (int, float)) else None
        if price:
            out["price"] = int(round(price))   # 네이버 목표주가 괴리 계산·표시용

        # 52주 고점 대비 이격 (하방 여유 참고)
        hi = info.get("fiftyTwoWeekHigh")
        if price and isinstance(hi, (int, float)) and hi:
            out["from52WHigh"] = round((price / hi - 1) * 100, 1)

        # Phase 5 — 애널리스트 컨센서스 목표주가 -> 상승여력.
        # 의견 2건 미만이면 신뢰도가 낮아 미표시 (skill Step 5-2 검증 취지).
        tgt = info.get("targetMeanPrice")
        n_op = info.get("numberOfAnalystOpinions")
        out["analystCount"] = n_op if isinstance(n_op, int) else None
        if (price and isinstance(tgt, (int, float)) and tgt
                and isinstance(n_op, int) and n_op >= 2):
            out["targetUpside"] = round((tgt / price - 1) * 100, 1)

        return _validate_valuation(out)
    except Exception as e:
        _warn(f"yf valuation {code}: {e}")
        return {}


def _validate_valuation(v):
    """skill Step 5-2 데이터 검증 — 이상치는 None 처리하고 사유를 남긴다."""
    warns = []
    checks = [
        ("per", lambda x: 0 < x <= 500, "PER 이상치"),
        ("estPer", lambda x: 0 < x <= 500, "추정PER 이상치"),
        ("industryPer", lambda x: 0 < x <= 500, "업종PER 이상치"),
        ("pbr", lambda x: 0 < x <= 50, "PBR 이상치"),
        ("roe", lambda x: -100 <= x <= 100, "ROE 이상치"),
        ("debtToEquity", lambda x: 0 <= x <= 2000, "부채비율 이상치"),
        ("targetUpside", lambda x: -90 <= x <= 200, "목표가 괴리 이상치"),
    ]
    for key, ok, label in checks:
        x = v.get(key)
        if x is not None and not ok(x):
            warns.append(f"{label}({x}) 제외")
            v[key] = None
    if warns:
        v["dataWarnings"] = (v.get("dataWarnings") or []) + warns
    return v


def _merge_naver_valuation(entry, code):
    """Yahoo 가 한국 종목에 못 주는 밸류에이션을 네이버 증권으로 보완한다.

    PER·PBR·동일업종 PER·추정PER 은 네이버가 정본(Yahoo 는 KRX trailing EPS
    미제공으로 항상 결측 — 2026-07 확인), roe·revGrowth 등 나머지는 기존
    yfinance 값을 유지. 목표가 괴리는 Yahoo 애널리스트 컨센서스를 1순위로
    유지하고 없을 때만 네이버 목표주가로 계산한다."""
    try:
        nv = sources.naver_valuation(code)
    except Exception as e:
        _warn(f"naver valuation {code}: {e}")
        nv = {}
    if not nv:
        entry["dataWarnings"] = (entry.get("dataWarnings") or []) + ["네이버 밸류에이션 조회 실패"]
        return entry

    for k in ("per", "pbr", "estPer", "industryPer", "industryChangePct", "dividendYield"):
        if nv.get(k) is not None:
            entry[k] = nv[k]

    if entry.get("targetUpside") is not None:
        n = entry.get("analystCount")
        entry["targetSource"] = f"야후 애널리스트 {n}명 평균" if n else "야후 컨센서스"
    elif entry.get("price") and nv.get("targetPrice"):
        entry["targetUpside"] = round((nv["targetPrice"] / entry["price"] - 1) * 100, 1)
        entry["targetPrice"] = int(nv["targetPrice"])
        entry["targetSource"] = "네이버 컨센서스"

    return _validate_valuation(entry)   # 네이버 값 포함 재검증


def _lin(x, lo, hi):
    """x 를 [lo, hi] -> [0, 100] 으로 선형 매핑 (lo>hi 역방향 허용, 클램프)."""
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))


def quality_score(v):
    """skill Step 4-3 퀄리티 스코어 (0–100) — 모멘텀 30% + 밸류에이션 25%
    + 성장성 25% + 재무건전성 20%. 결측 항목은 가중치 재배분하되,
    가용 가중치 50% 미만이면 미산출(None).
    밸류에이션 = 절대 PER + 업종 상대 PER(있으면) + PBR 의 균등 평균 —
    반도체처럼 업종 전체가 고PER 일 때 절대 PER 만으로 생기는 왜곡을 줄인다."""
    comps = {}
    if v.get("ret3M") is not None:
        comps["momentum"] = _lin(v["ret3M"], -20, 30)
    val_parts = []
    if v.get("per") is not None:
        val_parts.append(_lin(v["per"], 40, 5))
        if v.get("industryPer"):
            # 업종 대비 0.5배 이하 만점 ~ 2.0배 이상 0점
            val_parts.append(_lin(v["per"] / v["industryPer"], 2.0, 0.5))
    if v.get("pbr") is not None:
        val_parts.append(_lin(v["pbr"], 8, 0.5))
    if val_parts:
        comps["valuation"] = sum(val_parts) / len(val_parts)
    if v.get("revGrowth") is not None:
        comps["growth"] = _lin(v["revGrowth"], -10, 40)
    health_parts = []
    if v.get("debtToEquity") is not None:
        health_parts.append(_lin(v["debtToEquity"], 200, 0))
    if v.get("roe") is not None:
        health_parts.append(_lin(v["roe"], -10, 30))
    if health_parts:
        comps["health"] = sum(health_parts) / len(health_parts)

    weights = {"momentum": 0.30, "valuation": 0.25, "growth": 0.25, "health": 0.20}
    avail = sum(weights[k] for k in comps)
    if avail < 0.5:
        return None
    return round(sum(weights[k] * s for k, s in comps.items()) / avail)


def _theme_stock_seeds(f, max_per_theme):
    """브리핑 flow 항목에서 (이름, 코드) 시드를 뽑는다.
    신 스키마(2026-07-03~)는 krStocks[{code,name,changePct}], 구 스키마는 krNames[]."""
    seeds = []
    for s in (f.get("krStocks") or [])[:max_per_theme]:
        if s.get("name"):
            seeds.append({"name": s["name"], "code": s.get("code")})
    if not seeds:
        for nm in (f.get("krNames") or [])[:max_per_theme]:
            seeds.append({"name": nm, "code": None})
    return seeds


def theme_stocks(flow, resolve_fn, max_per_theme=4, direction="up"):
    """Phase 4: briefing flow[]/downFlow[] 의 국내 종목 -> valuation + DART 촉매.
    resolve_fn(name)->{code,name,market}|None (railway_server.resolve or a local map)."""
    themes = []
    for f in (flow or []):
        stocks = []
        for seed in _theme_stock_seeds(f, max_per_theme):
            hit = None
            if seed["code"]:
                hit = (resolve_fn(seed["code"]) if resolve_fn else None) \
                      or {"code": seed["code"], "name": seed["name"], "market": "KOSPI"}
            elif resolve_fn:
                hit = resolve_fn(seed["name"])
            if not hit:
                stocks.append({"code": None, "name": seed["name"], "market": None,
                               "note": "종목코드 미해결"})
                continue
            code, market = hit["code"], hit.get("market", "KOSPI")
            entry = {"code": code, "name": hit.get("name", seed["name"]), "market": market}
            entry.update(_yf_valuation(code, market))
            _merge_naver_valuation(entry, code)   # PER·PBR·업종PER·목표가 폴백
            entry["qScore"] = quality_score(entry)
            # latest DART disclosure as catalyst evidence (deterministic)
            try:
                corp = sources.dart_corp_code(code)
                if corp:
                    disc = sources.dart_disclosures(corp, days=60, page_count=5)
                    if disc:
                        entry["recentFiling"] = {
                            "date": disc[0].get("date", ""),
                            "title": disc[0].get("title", ""),
                            "url": disc[0].get("url", ""),
                        }
            except Exception as e:
                _warn(f"dart catalyst {code}: {e}")
            stocks.append(entry)
        themes.append({
            "usTheme": f.get("usTheme", ""),
            "usSymbols": f.get("usSymbols", []) or [s.get("symbol") for s in (f.get("usStocks") or [])],
            "krTheme": f.get("krTheme", ""),
            "rationale": f.get("rationale", ""),
            "direction": direction,
            "etf": BRIEFING_THEME_ETF.get(f.get("usTheme", "")),
            "industryReports": f.get("industryReports") or [],   # 브리핑이 부착한 산업 리포트
            "stocks": stocks,
        })
    return themes


# ----------------------------------------------------------------------------
# Phase 5) LLM synthesis
# ----------------------------------------------------------------------------
SYSTEM = (
    "당신은 글로벌 주식 섹터·테마를 분석하는 퀀트 리서치 애널리스트입니다. "
    "제공된 RAW 데이터(FRED 매크로, CNN Fear&Greed 센티먼트, 섹터 ETF 퍼포먼스, "
    "브리핑 도출 테마·종목)를 종합해 탑다운(매크로→센티먼트→섹터→종목) 분석을 "
    "작성하세요.\n"
    "규칙:\n"
    "- 모든 판단은 제공된 RAW 데이터에 근거할 것. 데이터에 없는 수치를 지어내지 말 것.\n"
    "- 매크로 국면과 센티먼트가 가리키는 방향의 정합성을 명시할 것.\n"
    "- 타깃 섹터의 OW/N/UW 판정은 RAW 의 sectorScores(결정론적 스코어카드)의 verdict 를\n"
    "  그대로 따를 것. 당신의 역할은 판정을 바꾸는 것이 아니라 점수 구성(매크로 적합도·\n"
    "  센티먼트·모멘텀·밸류·성장)을 근거로 rationale 한두 문장을 서술하는 것이다.\n"
    "- 종목 서술 시 qScore(퀄리티 스코어)·targetUpside(목표가 괴리)가 있으면 근거로 활용할 것.\n"
    "- 각 테마 종목에 대해 투자 포인트와 리스크를 균형 있게 서술할 것.\n"
    "- 테마의 direction 이 'down'(미국 급락 테마)이면 매수 관점이 아니라 **약세 주의 관점**으로:\n"
    "  summary 는 국내 파급 경로·경계 포인트를, 종목 point 는 '이 종목이 왜 영향권인지'를,\n"
    "  risk 는 하방 시나리오를 서술할 것. 급락 테마 종목을 매수 추천처럼 쓰지 말 것.\n"
    "- Extreme Greed 구간이면 리스크 관리를 강조할 것.\n"
    "- 출력은 지정된 JSON 스키마를 엄격히 따를 것. 한국어로 작성."
)

_STR = {"type": "string"}


def _arr(items):
    return {"type": "array", "items": items}


def _obj(props, required=None):
    return {"type": "object", "properties": props,
            "required": required or list(props.keys())}


SECTOR_SCHEMA = _obj({
    "regimeSummary": _STR,          # 매크로 국면 2~3문장
    "sentimentSummary": _STR,       # 센티먼트 해석 2~3문장
    "targetSectors": _arr(_obj({
        "name": _STR, "etf": _STR, "rationale": _STR,
        "recommend": _STR,          # OW/N/UW
    })),
    "themes": _arr(_obj({
        "krTheme": _STR,
        "summary": _STR,            # 테마 종합
        "stocks": _arr(_obj({
            "name": _STR, "point": _STR, "risk": _STR,
        })),
    })),
    "strategy": _STR,               # 종합 전략 방향
    "risks": _arr(_STR),            # 매크로/섹터/종목 리스크 3~5개
})


# ----------------------------------------------------------------------------
# Phase 4b) 증권사 산업 리포트 요약 (전용 LLM 패스)
#   테마별로 부착된 산업 리포트의 본문(네이버 상세)을 읽어 2~3문장으로 요약한다.
#   대형 종합 콜(analyze_sectors Phase 5)과 분리 — URL 중복 제거로 토큰을
#   아끼고, 요약 실패가 종합 분석을 흔들지 않도록 독립적으로 best-effort 동작.
# ----------------------------------------------------------------------------
_REPORT_BODY_MIN = 250     # 이보다 짧으면 티저(본문은 PDF) — 요약하지 않는다
_REPORT_BODY_MAX = 2000

REPORT_SUMMARY_SYSTEM = (
    "당신은 증권사 산업분석 리포트를 요약하는 리서치 에디터입니다. "
    "각 리포트 본문(body)을 읽고 핵심을 한국어 2~3문장으로 요약하세요.\n"
    "규칙:\n"
    "- 본문에 실제로 있는 내용만 쓸 것. 없는 수치·전망을 지어내지 말 것.\n"
    "- 업종 전망·핵심 논거·수혜(또는 피해) 포인트 중심으로 압축할 것.\n"
    "- '이 리포트는' 같은 군말 없이 내용부터 서술할 것.\n"
    "- 입력의 각 리포트 id 를 그대로 echo 하고, 모든 리포트를 빠짐없이 요약할 것.\n"
    "- 출력은 {\"items\":[{\"id\":<문자열>,\"summary\":<문자열>}]} 스키마를 엄격히 따를 것."
)

REPORT_SUMMARY_SCHEMA = _obj({
    "items": _arr(_obj({"id": _STR, "summary": _STR})),
})


def _collect_report_bodies(themes):
    """테마들의 industryReports 를 URL 기준 중복 제거하고 본문을 조회한다.
    반환: dict {url: {"title","body"}} — 본문이 _REPORT_BODY_MIN 이상인 것만
    (티저성 짧은 본문은 요약 근거가 부족해 제외)."""
    bodies = {}
    for t in themes:
        for r in (t.get("industryReports") or []):
            url = r.get("url")
            if not url or url in bodies:
                continue
            try:
                body = sources.naver_industry_detail(url, max_chars=_REPORT_BODY_MAX)
            except Exception as e:
                _warn(f"industry detail {url}: {e}")
                body = ""
            if len(body) >= _REPORT_BODY_MIN:
                bodies[url] = {"title": r.get("title", ""), "body": body}
    return bodies


def summarize_industry_reports(themes):
    """전용 LLM 패스 — 고유 산업 리포트를 2~3문장으로 요약해 각 리포트 dict 에
    llmSummary 를 부착한다. LLM 미설정/본문 없음/실패 시 무동작(best-effort)."""
    if not llm.configured():
        _warn("LLM 미설정 — 산업 리포트 요약 생략")
        return
    bodies = _collect_report_bodies(themes)
    if not bodies:
        _warn("요약할 산업 리포트 본문 없음 (티저/미수집)")
        return
    urls = list(bodies.keys())
    items = [{"id": str(i), "title": bodies[u]["title"], "body": bodies[u]["body"]}
             for i, u in enumerate(urls)]
    user = "리포트 목록(JSON):\n" + json.dumps({"reports": items}, ensure_ascii=False)
    try:
        out = llm.generate_json(REPORT_SUMMARY_SYSTEM, user,
                                max_tokens=2048, schema=REPORT_SUMMARY_SCHEMA)
    except Exception as e:
        _warn(f"산업 리포트 요약 LLM 실패: {e}")
        return

    by_url = {}
    for it in (out.get("items") or []):
        try:
            idx = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        s = (it.get("summary") or "").strip()
        if s and 0 <= idx < len(urls):
            by_url[urls[idx]] = s

    n = 0
    for t in themes:
        for r in (t.get("industryReports") or []):
            s = by_url.get(r.get("url"))
            if s:
                r["llmSummary"] = s
                n += 1
    _warn(f"산업 리포트 요약: 본문 {len(bodies)}건 → LLM 요약 {len(by_url)}건 (테마 부착 {n})")


def _build_raw(briefing, macro, regime, sentiment, sectors, themes, sector_scores=None):
    return {
        "asof": briefing.get("date", ""),
        "macro": {sid: {k: v for k, v in m.items() if k in ("name", "latest", "prev", "unit")}
                  for sid, m in macro.items()},
        "regime": regime,
        "sentiment": {k: sentiment.get(k) for k in ("score", "rating", "prevClose", "prevWeek", "prevMonth")},
        "sectors": [{k: s.get(k) for k in ("etf", "name", "ret1W", "ret1M", "ret3M", "ret6M", "retYTD")}
                    for s in sectors],
        "sectorScores": [{"etf": sc["etf"], "name": sc["name"], "score": sc["score"],
                          "maxScore": sc["maxScore"], "verdict": sc["verdict"],
                          "parts": [{"label": p["label"], "score": p["score"],
                                     "max": p["max"], "note": p["note"]}
                                    for p in sc["parts"]]}
                         for sc in (sector_scores or {}).values()],
        "themes": [{"usTheme": t["usTheme"], "usSymbols": t["usSymbols"],
                    "krTheme": t["krTheme"], "rationale": t["rationale"],
                    "direction": t.get("direction", "up"),
                    "industryReports": [{k: r.get(k) for k in ("category", "title", "broker", "date", "llmSummary")}
                                        for r in (t.get("industryReports") or [])],
                    "etf": t.get("etf"), "etfReturns": t.get("etfReturns"),
                    "stocks": [{k: s.get(k) for k in ("name", "market", "per", "estPer",
                                                      "industryPer", "pbr", "roe",
                                                      "revGrowth", "qScore", "targetUpside",
                                                      "beta", "from52WHigh", "recentFiling")}
                               for s in t["stocks"]]}
                   for t in themes],
    }


def analyze_sectors(briefing, resolve_fn=None):
    """End-to-end 5-phase sector analysis. `briefing` is the loaded
    public/briefing/latest.json (needs .flow / .date). Returns a dict matching
    the sector_analysis.json schema. Degrades gracefully at each phase."""
    date = briefing.get("date") or datetime.datetime.now(KST).strftime("%Y-%m-%d")

    # Phase 1-4: collect RAW (each is best-effort)
    macro = fetch_macro()
    regime = classify_regime(macro)
    sentiment = fetch_fear_greed()
    sentiment["strategyHint"] = interpret_sentiment(sentiment.get("score"))
    sectors, benchmark = fetch_sector_etfs()
    etf_pes = fetch_etf_pes([s["etf"] for s in sectors])
    sector_scores = score_sectors(sectors, regime, sentiment.get("score"), benchmark, etf_pes)
    # 급등(flow) + 급락(downFlow) 6개 테마 전부 분석 — 대시보드 테마 클릭 심층분석용.
    themes = (theme_stocks(briefing.get("flow", []), resolve_fn, direction="up")
              + theme_stocks(briefing.get("downFlow", []), resolve_fn, direction="down"))

    # 테마별로 해당 섹터 ETF 의 phase 3 수익률 + 12개 중 3M 모멘텀 순위 + 스코어카드.
    etf_rows = {s["etf"]: s for s in sectors}
    ranked = [s["etf"] for s in sectors if s.get("ret3M") is not None]
    for t in themes:
        row = etf_rows.get(t.get("etf"))
        if row:
            t["etfReturns"] = {k: row.get(k) for k in
                               ("ret1W", "ret1M", "ret3M", "ret6M", "retYTD")}
            t["etfRank"] = (ranked.index(t["etf"]) + 1) if t["etf"] in ranked else None
            t["etfRankOf"] = len(ranked)
        if t.get("etf") in sector_scores:
            t["scorecard"] = sector_scores[t["etf"]]

    # Phase 4b: 산업 리포트 전용 요약 패스 (llmSummary 를 industryReports 에 부착)
    try:
        summarize_industry_reports(themes)
    except Exception as e:
        _warn(f"산업 리포트 요약 패스 실패: {e}")

    # Phase 5: LLM synthesis (optional — mechanical passthrough if unavailable)
    synth, model_used = {}, None
    if llm.configured():
        try:
            raw = _build_raw(briefing, macro, regime, sentiment, sectors, themes, sector_scores)
            user = ("RAW 데이터(JSON):\n"
                    + json.dumps(raw, ensure_ascii=False))
            synth, model_used = llm.generate_json(
                SYSTEM, user, max_tokens=6144, schema=SECTOR_SCHEMA, return_model=True)
        except Exception as e:
            _warn(f"LLM 종합 실패: {e} — 기계적 결과만 반환")
    else:
        _warn("LLM 미설정 — 기계적 결과만 반환")

    # Assemble output. LLM fields overlay the deterministic data.
    sources_used = [
        "FRED (api.stlouisfed.org) — " + ", ".join(s for s, _, _ in FRED_SERIES),
        "Yahoo Finance — 섹터 ETF 13종+SPY 벤치마크, ETF PE, 테마 종목 재무·목표주가 컨센서스",
        sentiment.get("source", ""),
        "DART OpenAPI — 테마 종목 최근 공시",
    ]
    return {
        "date": date,
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "generatedBy": model_used or "mechanical",
        "macro": {
            "regime": regime["regime"],
            "regimeReason": regime["regimeReason"],
            "preferredSectors": regime["preferredSectors"],
            "summary": synth.get("regimeSummary", ""),
            "indicators": [
                {"name": m["name"], "latest": m["latest"], "prev": m["prev"],
                 "unit": m["unit"], "source": m["source"]}
                for m in macro.values()
            ],
        },
        "sentiment": {
            "score": sentiment.get("score"),
            "rating": sentiment.get("rating"),
            "prevClose": sentiment.get("prevClose"),
            "prevWeek": sentiment.get("prevWeek"),
            "prevMonth": sentiment.get("prevMonth"),
            "components": sentiment.get("components", []),
            "strategyHint": sentiment.get("strategyHint", ""),
            "summary": synth.get("sentimentSummary", ""),
            "note": sentiment.get("note", ""),
            "source": sentiment.get("source", ""),
        },
        "sectors": sectors,
        "sectorScores": list(sector_scores.values()),
        "targetSectors": _override_verdicts(synth.get("targetSectors", []), sector_scores),
        "themes": _merge_theme_synth(themes, synth.get("themes", [])),
        "strategy": synth.get("strategy", ""),
        "risks": synth.get("risks", []),
        "sources": [s for s in sources_used if s],
        "disclaimer": "본 리포트는 퀀트 모델 기반 참고 자료이며 투자 권유가 아닙니다. "
                      "최종 투자 판단은 자격을 갖춘 전문가와 검토하십시오.",
    }


def _override_verdicts(target_sectors, sector_scores):
    """LLM targetSectors 의 recommend 를 결정론적 스코어카드 verdict 로 강제 통일.
    (LLM 은 rationale 서술만 담당 — 판정 환각 방지)"""
    out = []
    for t in target_sectors:
        sc = sector_scores.get(t.get("etf"))
        if sc:
            t = {**t, "recommend": sc["verdict"],
                 "score": sc["score"], "maxScore": sc["maxScore"]}
        out.append(t)
    return out


def _merge_theme_synth(themes, synth_themes):
    """Attach LLM point/risk to the deterministic theme-stock rows (matched by name)."""
    by_theme = {t.get("krTheme", ""): t for t in synth_themes}
    by_stock = {}
    for t in synth_themes:
        for s in (t.get("stocks") or []):
            by_stock[s.get("name", "")] = s
    out = []
    for t in themes:
        st = by_theme.get(t["krTheme"], {})
        stocks = []
        for s in t["stocks"]:
            syn = by_stock.get(s.get("name", ""), {})
            stocks.append({**s, "point": syn.get("point", ""), "risk": syn.get("risk", "")})
        out.append({**t, "summary": st.get("summary", ""), "stocks": stocks})
    return out
