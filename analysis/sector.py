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


def _risk_metrics_from_close(close, spy_close=None):
    """스코어카드 v2 리스크 지표 — 이미 내려받은 1Y 일봉에서 추가 비용 없이 계산.
    vol60(60일 실현변동성, 연율화 %), mdd1Y(최대낙폭 %), from52WHigh(고점 이격 %),
    beta(SPY 대비, 1Y 일간수익률 회귀). 계산 불가 항목은 생략."""
    out = {}
    if close is None or len(close) < 30:
        return out
    rets = close.pct_change().dropna()
    if len(rets) >= 60:
        out["vol60"] = round(float(rets.iloc[-60:].std()) * (252 ** 0.5) * 100, 1)
    dd = close / close.cummax() - 1
    out["mdd1Y"] = round(float(dd.min()) * 100, 1)
    out["from52WHigh"] = round((float(close.iloc[-1]) / float(close.max()) - 1) * 100, 1)
    if spy_close is not None and len(spy_close) >= 30:
        srets = spy_close.pct_change().dropna()
        a, b = rets.align(srets, join="inner")
        if len(a) >= 60 and float(b.var()) > 0:
            out["beta"] = round(float(a.cov(b) / b.var()), 2)
    return out


def fetch_sector_etfs():
    """Phase 3: 13 sector ETFs + SPY benchmark, 1Y daily -> period returns
    + 리스크 지표(vol60/mdd1Y/from52WHigh/beta — 스코어카드 v2 입력).
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

    spy_close = _close_of("SPY")
    for tk, name in US_SECTOR_ETFS:
        close = _close_of(tk)
        rets = _returns_from_close(close)
        rows.append({
            "etf": tk, "name": name, **rets,
            **_risk_metrics_from_close(close, spy_close),
            "source": f"Yahoo Finance, {tk}, "
                      + datetime.datetime.now(KST).strftime("%Y-%m-%d")
                      + ("" if rets else " [Data Unavailable]"),
        })
    # sort by 3M momentum (skill scoring proxy), unknowns last
    rows.sort(key=lambda r: (r.get("ret3M") is None, -(r.get("ret3M") or -999)))
    benchmark = _returns_from_close(spy_close)
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


# --- 타깃 섹터 스코어카드 v2 — 퀀트 팩터 8항목, 총점 100점 환산 --------------
# (v1 은 skill Step 3-3 의 5항목 17점.) 내부 루브릭은 항목별 정수 빈(합 23점)
# 이고 노출 총점은 100점 환산(score/maxScore=100, 원점수는 rawScore/rawMax).
# 스윙/모멘텀 목적에 맞춰 모멘텀 계열(상대모멘텀·52주 고점 근접·추세 가속)
# 비중 ~35%. 신규 항목은 전부 이미 내려받는 1Y 일봉·FRED 에서 계산(추가 수집
# 없음). verdict 는 비율 컷오프라 항목이 바뀌어도 기준이 유지된다
# (>=70% OW, >=40% N, 미만 UW).
_ETF_CANON = {  # ETF -> regime.preferredSectors 명칭
    "XLK": "Tech", "SMH": "Tech", "XLC": "Tech",
    "XLV": "Healthcare", "XBI": "Healthcare",
    "XLF": "Financials", "XLE": "Energy", "XLI": "Industrials",
    "XLP": "Staples", "XLY": "Discretionary", "XLB": "Materials",
    "XLRE": "Real Estate", "XLU": "Utilities",
}
_SUB_ETFS = {"SMH", "XBI", "XLC"}          # 상위 섹터에서 파생된 서브섹터 ETF
# 실측 베타 미가용 시 폴백용 정적 분류
_GROWTH_ETFS = {"XLK", "SMH", "XLY", "XLC", "XBI"}
_DEFENSIVE_ETFS = {"XLU", "XLP", "XLV"}


def _beta_kind(s):
    """섹터 성격 분류 — 실측 베타(1Y vs SPY) 우선, 미가용 시 정적 집합 폴백.
    반환: ("high"|"mid"|"low", 근거문구)."""
    b = s.get("beta")
    if b is not None:
        kind = "high" if b >= 1.1 else ("low" if b <= 0.9 else "mid")
        return kind, f"β {b}"
    tk = s.get("etf")
    kind = "high" if tk in _GROWTH_ETFS else ("low" if tk in _DEFENSIVE_ETFS else "mid")
    return kind, "β 미가용 — 정적 분류"


def _tercile(sorted_vals, v):
    """교차단면 3분위 (0=하위 1/3, 1=중간, 2=상위 1/3)."""
    import bisect
    n = len(sorted_vals)
    if not n:
        return 1
    pos = bisect.bisect_left(sorted_vals, v)
    return 0 if pos * 3 < n else (1 if pos * 3 < 2 * n else 2)


def score_sectors(sectors, regime, sentiment_score, benchmark, etf_pes, macro=None):
    """섹터별 결정론적 스코어카드 v2. 반환: {etf: {score,maxScore,verdict,parts[]}}"""
    prefer = set(regime.get("preferredSectors") or [])
    pes = [v for v in etf_pes.values() if v]
    pe_median = sorted(pes)[len(pes) // 2] if pes else None

    # ⑦ 저변동 항목용 교차단면 분포
    vols = sorted(x["vol60"] for x in sectors if x.get("vol60") is not None)
    mdds = sorted(x["mdd1Y"] for x in sectors if x.get("mdd1Y") is not None)
    mdd_median = mdds[len(mdds) // 2] if mdds else None

    # ⑧ 리스크 온/오프 환경 판정 — VIX 수준 + HY 스프레드 방향 (수집만 하고
    # 스코어에 안 쓰던 FRED 지표 활용)
    vix = ((macro or {}).get("VIXCLS") or {}).get("latest")
    hy = (macro or {}).get("BAMLH0A0HYM2") or {}
    hy_up = (hy.get("latest") is not None and hy.get("prev") is not None
             and hy["latest"] > hy["prev"])
    if vix is None and hy.get("latest") is None:
        risk_env, risk_desc = None, "리스크 지표 미가용"
    else:
        signals = int(vix is not None and vix >= 20) + int(hy_up)
        risk_env = ["risk-on", "neutral", "risk-off"][signals]
        risk_desc = (f"VIX {vix}" if vix is not None else "VIX 미가용") \
                    + " · HY " + ("확대" if hy_up else "안정")

    out = {}
    for s in sectors:
        tk = s["etf"]
        parts = []
        kind, kind_note = _beta_kind(s)

        # ① 매크로 적합도 (0–4): 국면 선호 섹터 여부
        canon = _ETF_CANON.get(tk, "")
        if canon in prefer:
            m = 3 if tk in _SUB_ETFS else 4
            m_note = f"{regime.get('regime','')} 국면 선호 섹터" + (" (서브섹터)" if tk in _SUB_ETFS else "")
        else:
            m, m_note = 2, "국면 선호 섹터 아님 (중립)"
        parts.append({"key": "macro", "label": "매크로 적합도", "score": m, "max": 4, "note": m_note})

        # ② 센티먼트 정합성 (0–2): F&G 구간 x 섹터 성격(실측 베타)
        if sentiment_score is None:
            sc, sc_note = 0, "센티먼트 미가용"
        elif sentiment_score >= 56:
            sc = {"high": 2, "mid": 1, "low": 0}[kind]
            sc_note = f"Greed({sentiment_score}) — 고베타 우위 ({kind_note})"
        elif sentiment_score >= 45:
            sc, sc_note = 1, f"Neutral 구간({sentiment_score})"
        else:
            sc = {"low": 2, "mid": 1, "high": 0}[kind]
            sc_note = f"Fear({sentiment_score}) — 저베타 우위 ({kind_note})"
        parts.append({"key": "sentiment", "label": "센티먼트 정합성", "score": sc, "max": 2, "note": sc_note})

        # ③ 상대 모멘텀 합성 (0–4): 1M/3M/6M SPY 대비 초과수익 가중합성 (.25/.5/.25)
        exs = []
        for k, w in (("ret1M", 0.25), ("ret3M", 0.5), ("ret6M", 0.25)):
            r, b = s.get(k), (benchmark or {}).get(k)
            if r is not None and b is not None:
                exs.append((r - b, w))
        if exs:
            comp = sum(e * w for e, w in exs) / sum(w for _, w in exs)
            mo = 4 if comp > 5 else (3 if comp > 2 else (2 if comp > 0 else (1 if comp > -5 else 0)))
            mo_note = f"SPY 대비 합성 {comp:+.1f}%p (1M/3M/6M)"
        else:
            mo, mo_note = 0, "수익률 미가용"
        parts.append({"key": "momentum", "label": "상대 모멘텀 합성", "score": mo, "max": 4, "note": mo_note})

        # ④ 52주 고점 근접도 (0–2): 고점 회복력 (앵커드 모멘텀)
        gap = s.get("from52WHigh")
        if gap is None:
            hi, hi_note = 0, "고점 이격 미가용"
        else:
            hi = 2 if gap >= -5 else (1 if gap >= -15 else 0)
            hi_note = f"52주 고점 대비 {gap:+.1f}%"
        parts.append({"key": "high52w", "label": "52주 고점 근접", "score": hi, "max": 2, "note": hi_note})

        # ⑤ 추세 가속 (0–2): 최근 3M vs 직전 3M 수익률 가속도 (가격 지표)
        r3, r6 = s.get("ret3M"), s.get("ret6M")
        if r3 is None or r6 is None:
            gr, gr_note = 0, "수익률 미가용"
        else:
            accel = r3 - (r6 - r3)
            gr = 2 if accel > 3 else (1 if accel > 0 else 0)
            gr_note = f"3M 가속도 {accel:+.1f}%p"
        parts.append({"key": "accel", "label": "추세 가속", "score": gr, "max": 2, "note": gr_note})

        # ⑥ 밸류에이션 매력도 (0–3): ETF trailing PE vs 섹터 중앙값
        pe = etf_pes.get(tk)
        if pe is None or pe_median is None:
            va, va_note = 1, "ETF PE 미가용 (중립 1점)"
        else:
            ratio = pe / pe_median
            va = 3 if ratio < 0.85 else (2 if ratio < 1.0 else (1 if ratio < 1.15 else 0))
            va_note = f"PE {pe} vs 중앙값 {pe_median} ({ratio:.2f}배)"
        parts.append({"key": "valuation", "label": "밸류에이션 매력도", "score": va, "max": 3, "note": va_note})

        # ⑦ 저변동·하방 방어 (0–3): 실현변동성 3분위(0–2) + MDD 중앙값 대비(+1)
        vol, mdd = s.get("vol60"), s.get("mdd1Y")
        if vol is None:
            lv, lv_note = 1, "변동성 미가용 (중립 1점)"
        else:
            lv = 2 - _tercile(vols, vol)           # 변동성 낮을수록 가점
            lv_note = f"실현변동성 {vol}% (60일 연율)"
            if mdd is not None and mdd_median is not None and mdd > mdd_median:
                lv += 1                            # 낙폭이 중앙값보다 얕음
                lv_note += f" · MDD {mdd}% (중앙값보다 얕음)"
            elif mdd is not None:
                lv_note += f" · MDD {mdd}%"
        parts.append({"key": "lowvol", "label": "저변동·하방 방어", "score": lv, "max": 3, "note": lv_note})

        # ⑧ 리스크 온/오프 정합성 (0–3): 리스크 환경 x 섹터 베타
        if risk_env is None:
            ro, ro_note = 2, risk_desc + " (중립 2점)"
        elif risk_env == "risk-on":
            ro = {"high": 3, "mid": 2, "low": 1}[kind]
            ro_note = f"리스크온 ({risk_desc}) — 고베타 우위 ({kind_note})"
        elif risk_env == "risk-off":
            ro = {"low": 3, "mid": 2, "high": 1}[kind]
            ro_note = f"리스크오프 ({risk_desc}) — 저베타 우위 ({kind_note})"
        else:
            ro, ro_note = 2, f"중립 ({risk_desc})"
        parts.append({"key": "riskonoff", "label": "리스크 온/오프 정합성", "score": ro, "max": 3, "note": ro_note})

        # 총점은 100점 환산으로 노출 (내부 루브릭은 8항목 23점 정수 빈 — raw* 로 보존).
        # verdict 비율 컷(70/40%)과 항목별 score/max 표시는 그대로다.
        raw = sum(p["score"] for p in parts)
        raw_max = sum(p["max"] for p in parts)
        pct = raw / raw_max if raw_max else 0
        verdict = "OW" if pct >= 0.70 else ("N" if pct >= 0.40 else "UW")
        out[tk] = {"etf": tk, "name": s["name"],
                   "score": round(pct * 100), "maxScore": 100,
                   "rawScore": raw, "rawMax": raw_max,
                   "verdict": verdict, "parts": parts,
                   "source": "결정론적 산출 (퀀트 팩터 스코어카드 v2 — 8항목 100점 환산, 모멘텀 계열 35%)"}
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

        # 1M/3M/6M 수익률 + 20일 평균 거래대금 (Q점수 v2 모멘텀·유동성 입력)
        price = None
        try:
            hist = tkr.history(period="1y")
            close = hist["Close"].dropna()
            if len(close) >= 2:
                price = float(close.iloc[-1])
                for key, days in (("ret1M", 21), ("ret3M", 63), ("ret6M", 126)):
                    base = float(close.iloc[-1 - days]) if len(close) > days else None
                    if base:
                        out[key] = round((price / base - 1) * 100, 2)
            tv = (hist["Close"] * hist["Volume"]).dropna()
            if len(tv) >= 5:
                out["tradingValue20"] = int(float(tv.iloc[-20:].mean()))   # 원 단위
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
        ("fwdPer", lambda x: 0 < x <= 500, "선행PER 이상치"),
        ("industryPer", lambda x: 0 < x <= 500, "업종PER 이상치"),
        ("pbr", lambda x: 0 < x <= 50, "PBR 이상치"),
        ("roe", lambda x: -100 <= x <= 100, "ROE 이상치"),
        ("debtToEquity", lambda x: 0 <= x <= 2000, "부채비율 이상치"),
        ("targetUpside", lambda x: -90 <= x <= 200, "목표가 괴리 이상치"),
        ("opMargin", lambda x: -200 <= x <= 100, "영업이익률 이상치"),
        ("opGrowth", lambda x: -100 <= x <= 5000, "영업이익증가율 이상치"),
        ("epsGrowth", lambda x: -100 <= x <= 5000, "EPS증가율 이상치"),
        ("interestCoverage", lambda x: -1000 <= x <= 100000, "이자보상배율 이상치"),
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


def _merge_fnguide_valuation(entry, code):
    """FnGuide 3순위 보완 + 전용 재무비율 수집.

    ① FinanceRatio 는 항상 조회 — 영업이익·EPS 증가율, 영업이익률, 이자보상
      배율은 야후·네이버가 못 주는 FnGuide 전용(Q점수 성장·퀄리티 입력)이고,
      roe·revGrowth 는 결측일 때만 채운다(스몰캡 yfinance 미제공 케이스).
    ② Invest 페이지는 per·pbr·업종PER 결측이 있을 때만 조회 — 적자로
      트레일링 PER 이 없는 종목의 12M 선행 PER 은 fwdPer 별도 필드로만 담고
      per 를 덮지 않는다(트레일링/선행 혼동 방지, 표시·Q점수에서 구분 처리)."""
    try:
        rat = sources.fnguide_ratios(code)
    except Exception as e:
        _warn(f"fnguide ratios {code}: {e}")
        rat = {}
    for k in ("roe", "revGrowth"):                       # 결측 보완만
        if entry.get(k) is None and rat.get(k) is not None:
            entry[k] = rat[k]
    for k in ("opGrowth", "epsGrowth", "opMargin", "interestCoverage"):
        if rat.get(k) is not None:                       # FnGuide 전용 — 항상 채움
            entry[k] = rat[k]

    need = [k for k in ("per", "pbr", "industryPer") if entry.get(k) is None]
    if need:
        try:
            inv = sources.fnguide_invest(code)
        except Exception as e:
            _warn(f"fnguide invest {code}: {e}")
            inv = {}
        for k in need:
            if inv.get(k) is not None:
                entry[k] = inv[k]
        if entry.get("per") is None and inv.get("fwdPer") is not None:
            entry["fwdPer"] = inv["fwdPer"]
    return _validate_valuation(entry)   # FnGuide 값 포함 재검증


def _lin(x, lo, hi):
    """x 를 [lo, hi] -> [0, 100] 으로 선형 매핑 (lo>hi 역방향 허용, 클램프)."""
    if hi == lo:
        return 50.0
    return max(0.0, min(100.0, (x - lo) / (hi - lo) * 100))


_MKT_RETS = None


def _market_returns():
    """KOSPI/KOSDAQ 지수 기간수익률 — Q점수 v2 의 시장상대 모멘텀 입력.
    실행당 1회 조회 캐시. 실패 시 빈 dict (모멘텀은 절대수익률로 폴백)."""
    global _MKT_RETS
    if _MKT_RETS is not None:
        return _MKT_RETS
    out = {}
    try:
        df = yf.download(["^KS11", "^KQ11"], period="1y", interval="1d",
                         group_by="ticker", progress=False, auto_adjust=True)
        for tk, mkt in (("^KS11", "KOSPI"), ("^KQ11", "KOSDAQ")):
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    close = df[tk]["Close"].dropna()
                else:
                    close = df["Close"].dropna()
                rets = _returns_from_close(close)
                if rets:
                    out[mkt] = rets
            except Exception as e:
                _warn(f"market returns {tk}: {e}")
    except Exception as e:
        _warn(f"market returns download: {e}")
    _MKT_RETS = out
    return out


_INVWARN = None


def _invwarn_map():
    """시장경보(투자주의/경고/위험) code -> 라벨 — Q점수 리스크 게이트 입력.
    fetch_investment_warning.py 가 커밋하는 public/data/investment_warning.json
    을 읽는다 (위험 > 경고 > 주의 우선). 실패 시 빈 dict (게이트 미적용)."""
    global _INVWARN
    if _INVWARN is not None:
        return _INVWARN
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "public", "data", "investment_warning.json")
    m = {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        for level, label in (("danger", "투자위험"), ("warning", "투자경고"),
                             ("caution", "투자주의")):
            for it in d.get(level) or []:
                code = str(it.get("code") or "").zfill(6)
                if code != "000000" and code not in m:
                    m[code] = label
    except Exception as e:
        _warn(f"invwarn map: {e}")
    _INVWARN = m
    return m


# 리스크 게이트 — 점수 가산이 아니라 상한 캡/미산출 (관리·경고 종목이 고득점으로
# 보이는 것을 차단). None = Q점수 미산출.
_RISK_CAPS = {"투자위험": None, "투자경고": 40, "투자주의": 60, "저유동성": 50}
_MIN_TRADING_VALUE = 1_000_000_000     # 20일 평균 거래대금 10억원 미만 = 저유동성


def apply_risk_gate(entry):
    """시장경보·유동성 게이트를 entry 에 적용 — riskFlags 부착 + qScore 캡."""
    flags = []
    warn = _invwarn_map().get(str(entry.get("code") or ""))
    if warn:
        flags.append(warn)
    tv = entry.get("tradingValue20")
    if tv is not None and tv < _MIN_TRADING_VALUE:
        flags.append("저유동성")
    if not flags:
        return
    entry["riskFlags"] = flags
    caps = [_RISK_CAPS[f] for f in flags]
    if any(c is None for c in caps):
        entry["qScore"] = None
    elif entry.get("qScore") is not None:
        entry["qScore"] = min(entry["qScore"], min(caps))


def quality_score(v):
    """Q점수 v2 (0–100) — 모멘텀 30 : 성장 20 : 밸류 15 : 퀄리티 15 : 수급 10 :
    센티먼트 5 상대 가중. 결측 팩터는 가중치 재배분하되 가용 가중치 50% 미만이면
    미산출. 반환: {"score": int|None, "parts": {factor: 0-100}} — UI 분해 표시용.

    - 모멘텀: 1M/3M/6M (KOSPI/KOSDAQ 상대 우선, 지수 미가용 시 절대) + 52주 고점 근접
    - 밸류: 절대 PER(적자 시 12M 선행 PER 대체) + 업종 상대 PER + PBR + 목표가 괴리
    - 성장: 매출액·영업이익·EPS 증가율 (영업이익·EPS 는 FnGuide FinanceRatio)
    - 퀄리티: 부채비율 + ROE + 영업이익률 + 이자보상배율 (뒤 둘은 FnGuide)
    - 수급: 외국인·기관 5/20일 순매수 (상장주식수 대비 % — 네이버 trend API)
    - 센티먼트: 최근 60일 증권사 리포트 건수 (결정론적 프록시)"""
    comps = {}

    mparts = []
    for k in ("1M", "3M", "6M"):
        rel, absr = v.get("rel" + k), v.get("ret" + k)
        if rel is not None:
            mparts.append(_lin(rel, -15, 25))
        elif absr is not None:
            mparts.append(_lin(absr, -20, 30))
    if v.get("from52WHigh") is not None:
        mparts.append(_lin(v["from52WHigh"], -40, -3))
    if mparts:
        comps["momentum"] = sum(mparts) / len(mparts)

    val_parts = []
    # 적자 등으로 트레일링 PER 이 없으면 12M 선행 PER(FnGuide 컨센서스)로 대체 평가
    per_eff = v["per"] if v.get("per") is not None else v.get("fwdPer")
    if per_eff is not None:
        val_parts.append(_lin(per_eff, 40, 5))
        if v.get("industryPer"):
            # 업종 대비 0.5배 이하 만점 ~ 2.0배 이상 0점
            val_parts.append(_lin(per_eff / v["industryPer"], 2.0, 0.5))
    if v.get("pbr") is not None:
        val_parts.append(_lin(v["pbr"], 8, 0.5))
    if v.get("targetUpside") is not None:
        val_parts.append(_lin(v["targetUpside"], -10, 40))
    if val_parts:
        comps["valuation"] = sum(val_parts) / len(val_parts)

    growth_parts = []
    if v.get("revGrowth") is not None:
        growth_parts.append(_lin(v["revGrowth"], -10, 40))
    if v.get("opGrowth") is not None:
        growth_parts.append(_lin(v["opGrowth"], -20, 50))
    if v.get("epsGrowth") is not None:
        growth_parts.append(_lin(v["epsGrowth"], -20, 50))
    if growth_parts:
        comps["growth"] = sum(growth_parts) / len(growth_parts)

    health_parts = []
    if v.get("debtToEquity") is not None:
        health_parts.append(_lin(v["debtToEquity"], 200, 0))
    if v.get("roe") is not None:
        health_parts.append(_lin(v["roe"], -10, 30))
    if v.get("opMargin") is not None:
        health_parts.append(_lin(v["opMargin"], -5, 20))
    if v.get("interestCoverage") is not None:
        health_parts.append(_lin(v["interestCoverage"], 0, 10))
    if health_parts:
        comps["health"] = sum(health_parts) / len(health_parts)

    if v.get("researchCount") is not None:
        comps["sentiment"] = _lin(v["researchCount"], 0, 4)

    # 수급: 순매수가 상장주식수 대비 20일 ±1% / 5일 ±0.5% 를 만점/0점 경계로
    flow_parts = []
    for key, lo, hi in (("flowFrgn20", -1.0, 1.0), ("flowInst20", -1.0, 1.0),
                        ("flowFrgn5", -0.5, 0.5), ("flowInst5", -0.5, 0.5)):
        if v.get(key) is not None:
            flow_parts.append(_lin(v[key], lo, hi))
    if flow_parts:
        comps["flow"] = sum(flow_parts) / len(flow_parts)

    weights = {"momentum": 0.30, "growth": 0.20, "valuation": 0.15,
               "health": 0.15, "flow": 0.10, "sentiment": 0.05}
    avail = sum(weights[k] for k in comps)
    if avail < sum(weights.values()) * 0.5:
        return {"score": None, "parts": {k: round(s) for k, s in comps.items()}}
    score = round(sum(weights[k] * s for k, s in comps.items()) / avail)
    return {"score": score, "parts": {k: round(s) for k, s in comps.items()}}


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
            _merge_naver_valuation(entry, code)    # PER·PBR·업종PER·목표가 폴백
            _merge_fnguide_valuation(entry, code)  # 잔여 결측(스몰캡 ROE·적자 PER 등) 3순위 보완
            # 시장상대 모멘텀 — KOSPI/KOSDAQ 지수 대비 초과수익 (v2)
            mkt = _market_returns().get(market) or {}
            for k in ("ret1M", "ret3M", "ret6M"):
                if entry.get(k) is not None and mkt.get(k) is not None:
                    entry["rel" + k[3:]] = round(entry[k] - mkt[k], 2)
            # 센티먼트 프록시 — 최근 60일 증권사 리포트 건수 (네이버+FnGuide)
            try:
                entry["researchCount"] = len(sources.combined_research(code, days=60, limit=8))
            except Exception as e:
                _warn(f"research count {code}: {e}")
            # 수급(Flow) — 외국인·기관 5/20일 순매수를 상장주식수 대비 %로
            # (주식수 ≈ 시총/현재가 — 스케일 무관 강도 지표)
            try:
                trend = sources.naver_investor_trend(code, days=20)
            except Exception as e:
                trend = []
                _warn(f"investor trend {code}: {e}")
            if trend and entry.get("price") and entry.get("marketCap"):
                shares = entry["marketCap"] / entry["price"]
                if shares > 0:
                    for key, grp, n in (("flowFrgn5", "foreigner", 5), ("flowFrgn20", "foreigner", 20),
                                        ("flowInst5", "organ", 5), ("flowInst20", "organ", 20)):
                        entry[key] = round(sum(r[grp] for r in trend[:n]) / shares * 100, 3)
            qs = quality_score(entry)
            entry["qScore"] = qs["score"]
            if qs["parts"]:
                entry["qParts"] = qs["parts"]
            apply_risk_gate(entry)     # 시장경보·저유동성 — 캡/미산출 + riskFlags
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
    "  센티먼트·상대 모멘텀·52주 고점 근접·추세 가속·밸류·저변동/하방 방어·리스크 온오프)을\n"
    "  근거로 rationale 한두 문장을 서술하는 것이다.\n"
    "- 종목 서술 시 qScore(퀄리티 스코어)·targetUpside(목표가 괴리)가 있으면 근거로 활용할 것.\n"
    "- 테마 summary(테마 종합)는 대시보드에서 문장 단위 불릿으로 표시된다 — 반드시 3~4개의\n"
    "  완결된 문장으로 서술할 것 (미국 테마 동향, 국내 파급 경로, 밸류·모멘텀 근거, 관전 포인트 순).\n"
    "  summary 는 입력 rationale 바로 뒤에 이어 표시되므로 rationale 문장을 반복하지 말고\n"
    "  새로운 정보를 더할 것.\n"
    "- regimeSummary·sentimentSummary 는 각각 2~3문장으로 서술할 것.\n"
    "- RAW 의 themes 배열에 있는 **모든 테마**와 각 테마의 **모든 종목**을 빠짐없이 서술할 것.\n"
    "  응답이 길어져도 뒤쪽 테마·종목을 생략하지 말 것. krTheme 과 종목 name 은 입력 표기\n"
    "  그대로 echo 할 것 (병합 매칭 키).\n"
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
        "sectors": [{k: s.get(k) for k in ("etf", "name", "ret1W", "ret1M", "ret3M", "ret6M", "retYTD",
                                           "vol60", "mdd1Y", "from52WHigh", "beta")}
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
                                                      "fwdPer",
                                                      "industryPer", "pbr", "roe",
                                                      "revGrowth", "qScore", "targetUpside",
                                                      "beta", "from52WHigh", "recentFiling",
                                                      "riskFlags", "researchCount",
                                                      "flowFrgn20", "flowInst20",
                                                      "opMargin", "epsGrowth")}
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
    sector_scores = score_sectors(sectors, regime, sentiment.get("score"), benchmark, etf_pes, macro)
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
            # 한국어로 6개 테마 × 4종목의 summary/point/risk 를 전부 쓰면 6144 토큰을
            # 초과해 JSON 이 잘리거나(파싱 실패 → 체인 이탈) 모델이 뒤쪽 테마를 통째로
            # 생략했다(테마 종합 간헐 공백의 2차 원인). 여유 있게 늘린다.
            synth, model_used = llm.generate_json(
                SYSTEM, user, max_tokens=16384, schema=SECTOR_SCHEMA, return_model=True)
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


def _norm_key(s):
    """테마/종목명 병합 매칭용 정규화 — LLM 이 echo 하며 넣거나 뺀 공백·가운뎃점류
    표기 차이 때문에 exact 매칭이 깨져 summary/point 가 비던 것을 방지한다."""
    return "".join(ch for ch in str(s or "") if ch not in " \t·ㆍ・")


def _merge_theme_synth(themes, synth_themes):
    """Attach LLM point/risk to the deterministic theme-stock rows (matched by name)."""
    by_theme = {}
    by_stock = {}
    for t in synth_themes:
        k = _norm_key(t.get("krTheme", ""))
        if k:
            by_theme[k] = t
        for s in (t.get("stocks") or []):
            sk = _norm_key(s.get("name", ""))
            if sk:
                by_stock[sk] = s
    out = []
    for t in themes:
        st = by_theme.get(_norm_key(t["krTheme"]), {})
        stocks = []
        for s in t["stocks"]:
            syn = by_stock.get(_norm_key(s.get("name", "")), {})
            stocks.append({**s, "point": syn.get("point", ""), "risk": syn.get("risk", "")})
        out.append({**t, "summary": st.get("summary", ""), "stocks": stocks})
    return out
