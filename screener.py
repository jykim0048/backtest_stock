#!/usr/bin/env python3
"""
Daily pre-market stock screener — picks today's 급등 예상 종목 (surge candidates).

Runs BEFORE generate_report.py (~08:00 KST, 장 시작 전). Process:

  1. 전일 미국시장 분석     — yfinance: 미국 지수 + 섹터 ETF 전일 등락
  2. 유니버스 + 기계적 1차 필터 — FinanceDataReader KRX 스냅샷에서
       KOSPI 시총 top200 + KOSDAQ 시총 top150 (코스피200/코스닥150 근사)을 잡고,
       거래대금회전율·등락률·거래대금으로 상위 N 후보를 추린다.
  3. 뉴스/공시 보강         — 후보별 네이버 뉴스 + DART 공시 (analysis/sources.py 재사용)
  4. LLM 최종 선정          — Claude가 미국장 + 뉴스/공시 + 모멘텀을 종합해 최종 N종목 선정

Outputs:
  watchlist.json                            — 선정 종목 (code/name/market). 기존 파이프라인이 소비.
  public/reports/selection/YYYY-MM-DD.json  — 선정 근거 (시장관 + 종목별 사유).

Graceful degradation: 어느 단계가 실패해도 기계적 점수 상위로 watchlist를 채워
generate_report.py 가 항상 돌 수 있게 한다 (딥리서치 배치와 동일한 철학).

정확한 코스피200/코스닥150 멤버십은 KRX 로그인이 필요해, 여기서는 시가총액 상위
근사를 쓴다. data/index_constituents.json (KOSPI200/KOSDAQ150 코드 배열)이 있으면
그 정확한 명단을 우선 사용한다.

Env (GitHub Actions secrets): ANTHROPIC_API_KEY, NAVER_CLIENT_ID/SECRET, DART_API_KEY, TAVILY_API_KEY
"""
import os
import re
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

from analysis import sources

ROOT          = os.path.dirname(os.path.abspath(__file__))
WATCHLIST     = os.path.join(ROOT, "watchlist.json")
CONSTITUENTS  = os.path.join(ROOT, "data", "index_constituents.json")
SELECTION_DIR = os.path.join(ROOT, "public", "reports", "selection")

KST = datetime.timezone(datetime.timedelta(hours=9))

# ---- tunables -------------------------------------------------------------
N_FINAL      = int(os.environ.get("SCREEN_N_FINAL", "6"))    # 최종 선정 종목 수
N_SHORTLIST  = int(os.environ.get("SCREEN_N_SHORTLIST", "30"))  # 기계 필터 통과 후보 수
KOSPI_TOP    = 200   # 코스피200 근사 (시총 상위)
KOSDAQ_TOP   = 150   # 코스닥150 근사 (시총 상위)
ENRICH_WORKERS = int(os.environ.get("SCREEN_ENRICH_WORKERS", "8"))
NEWS_FETCH   = 30   # 시간창 필터 전, 최신순으로 넉넉히 받아올 뉴스 건수 (Naver display)
NEWS_MAX     = 8    # 시간창(전일 장마감~실행시각) 통과 후 후보당 최대 뉴스 수

MODEL      = "claude-sonnet-4-6"
MAX_TOKENS = 3000

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


def _warn(msg):
    print(f"[screener] {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------
# 1) 전일 미국시장 분석
# ----------------------------------------------------------------------------
def us_market_brief():
    """yfinance 로 미국 지수/섹터 ETF 전일 등락률을 계산해 반환."""
    import yfinance as yf

    items = US_INDICES + US_SECTORS
    tickers = [x["ticker"] for x in items]
    try:
        df = yf.download(tickers, period="5d", group_by="ticker",
                         progress=False, threads=True, auto_adjust=True)
    except Exception as e:
        _warn(f"US market download failed: {e}")
        return {"indices": [], "sectors": [], "asof": ""}

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

    return {
        "indices": pack(US_INDICES),
        "sectors": sorted(pack(US_SECTORS), key=lambda d: d["changePct"], reverse=True),
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d"),
    }


# ----------------------------------------------------------------------------
# 2) 유니버스 (코스피200 + 코스닥150 근사) + 기계적 1차 필터
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


def load_universe():
    """FinanceDataReader KRX 스냅샷에서 유니버스 DataFrame을 만든다.

    columns: Code, Name, Market, Close, ChagesRatio, Amount, Volume, Marcap
    """
    import FinanceDataReader as fdr

    df = fdr.StockListing("KRX")
    df = df[df["Market"].isin(["KOSPI", "KOSDAQ"])].copy()
    for col in ("Close", "ChagesRatio", "Amount", "Volume", "Marcap"):
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df = df.dropna(subset=["Marcap", "Amount"])
    df["Code"] = df["Code"].astype(str).str.zfill(6)

    explicit = _load_constituents()
    if explicit:
        uni = df[df["Code"].isin(explicit)]
        if len(uni) >= 100:                      # sanity: file looked usable
            _warn(f"universe from index_constituents.json: {len(uni)} stocks")
            return uni.reset_index(drop=True)
        _warn("index_constituents.json too small; falling back to market-cap proxy")

    # 시가총액 상위 근사: 코스피 top200 + 코스닥 top150
    kospi  = df[df["Market"] == "KOSPI"].nlargest(KOSPI_TOP, "Marcap")
    kosdaq = df[df["Market"] == "KOSDAQ"].nlargest(KOSDAQ_TOP, "Marcap")
    uni = pd.concat([kospi, kosdaq]).reset_index(drop=True)
    _warn(f"universe (market-cap proxy): KOSPI {len(kospi)} + KOSDAQ {len(kosdaq)} = {len(uni)}")
    return uni


def _zscore(s):
    s = s.astype(float)
    std = s.std()
    return (s - s.mean()) / std if std and not np.isnan(std) else s * 0.0


def mechanical_funnel(uni):
    """거래대금회전율·등락률·거래대금을 종합 점수화해 상위 N_SHORTLIST 후보 반환."""
    df = uni.copy()
    df["turnover"] = df["Amount"] / df["Marcap"]            # 거래대금회전율 (규모 대비 관심)
    df["score"] = (
        _zscore(df["turnover"]) * 1.0 +                     # 비정상적 관심
        _zscore(df["ChagesRatio"]) * 1.0 +                  # 전일 모멘텀
        _zscore(np.log1p(df["Amount"].clip(lower=0))) * 0.5  # 유동성 가점
    )
    df = df.sort_values("score", ascending=False).head(N_SHORTLIST)
    cols = ["Code", "Name", "Market", "Close", "ChagesRatio", "Amount", "turnover", "Marcap", "score"]
    return df[cols].reset_index(drop=True)


# ----------------------------------------------------------------------------
# 3) 뉴스/공시 보강
# ----------------------------------------------------------------------------
def _recent_disclosures(code, days=7):
    corp = sources.dart_corp_code(code)
    if not corp:
        return []
    today = datetime.date.today()
    cutoff = today - datetime.timedelta(days=days)
    out = []
    for d in sources.dart_disclosures(corp, days=days, page_count=10):
        try:
            dt = datetime.date.fromisoformat(d.get("date", ""))
        except Exception:
            dt = None
        if dt is None or dt >= cutoff:
            out.append({"date": d.get("date", ""), "title": d.get("title", "")})
    return out[:5]


def overnight_cutoff(now=None):
    """뉴스 수집 시작 시각 = 직전 거래일 15:30 KST (전일 장 마감).

    주말은 금요일로 당긴다(공휴일은 미반영 — 보수적으로 최근 평일 마감을 사용).
    스크리너는 장전(08:00 KST)에 돌므로 '전일' = 직전 거래일을 의미한다.
    """
    now = now or datetime.datetime.now(KST)
    d = now.date() - datetime.timedelta(days=1)
    while d.weekday() >= 5:                       # 토(5)/일(6) 건너뜀
        d -= datetime.timedelta(days=1)
    return datetime.datetime.combine(d, datetime.time(15, 30), tzinfo=KST)


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


def _enrich_one(row, cutoff):
    name, code = row["Name"], row["Code"]
    news = []
    for n in sources.naver_search("news", name, display=NEWS_FETCH):  # 최신순
        dt = _parse_pubdate(n.get("date", ""))
        if dt is not None and dt >= cutoff:       # 전일 장마감 이후만
            news.append({"title": n["title"], "date": n.get("date", "")})
        if len(news) >= NEWS_MAX:
            break
    return code, {"news": news, "disclosures": _recent_disclosures(code)}


def enrich_candidates(shortlist):
    """후보별 '전일 장마감~실행시각' 뉴스 + 최근 공시를 병렬 수집. 실패는 빈 값으로 흡수."""
    cutoff = overnight_cutoff()
    _warn(f"news window: {cutoff.strftime('%Y-%m-%d %H:%M')} KST ~ now")
    enriched = {}
    rows = shortlist.to_dict("records")
    workers = max(1, min(ENRICH_WORKERS, len(rows)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for code, data in ex.map(lambda r: _enrich_one(r, cutoff), rows):
            enriched[code] = data
    return enriched


# ----------------------------------------------------------------------------
# 4) LLM 최종 선정
# ----------------------------------------------------------------------------
SYSTEM = """\
너는 한국 주식 데이 트레이딩 애널리스트다. 장 시작 전, 주어진 데이터로 '오늘 급등 가능성이
높은' 한국 종목 {n}개를 선정한다.

입력:
- us_market: 전일 미국 지수/섹터 ETF 등락 (어떤 섹터가 강했는지 → 국내 동조 섹터 추론에 사용)
- candidates: 코스피200/코스닥150 유니버스에서 전일 거래대금회전율·등락률로 1차 선별된 후보.
  각 후보의 전일 등락률(changePct), 거래대금(amountKRW), 최근 뉴스 제목, 최근 DART 공시 포함.

선정 원칙:
- 전일 미국시장에서 강했던 섹터와 연관된 국내 종목, 의미 있는 뉴스/공시 촉매가 있는 종목,
  거래대금·모멘텀이 살아있는 종목을 우선한다.
- 단순 대형주 추종이 아니라 '오늘 추가 상승 여력'에 집중한다.
- 근거 없는 종목은 넣지 마라. 후보 목록 안에서만 고른다(코드/이름 그대로).

반드시 아래 스키마와 정확히 동일한 JSON만 출력한다. 마크다운 펜스/설명 금지.
{
  "marketView": "전일 미국장 요약과 오늘 국내 시장 관점 3-4문장 (한국어)",
  "picks": [
    {"code": "6자리", "name": "종목명", "market": "KOSPI|KOSDAQ",
     "reason": "선정 사유 1-2문장 (미국장/뉴스/공시/모멘텀 근거 명시)",
     "catalyst": "핵심 촉매 한 줄"}
  ]
}
picks 는 정확히 {n}개."""


def _extract_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


def llm_select(us_brief, shortlist, enriched):
    """Claude 로 최종 N종목 선정. 실패 시 None 반환(호출부에서 fallback)."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        _warn("ANTHROPIC_API_KEY missing; using mechanical fallback")
        return None
    try:
        from anthropic import Anthropic
    except Exception as e:
        _warn(f"anthropic import failed: {e}")
        return None

    candidates = []
    for r in shortlist.to_dict("records"):
        code = r["Code"]
        e = enriched.get(code, {})
        candidates.append({
            "code": code,
            "name": r["Name"],
            "market": r["Market"],
            "changePct": round(float(r["ChagesRatio"]), 2),
            "amountKRW": int(r["Amount"]),
            "turnover": round(float(r["turnover"]), 4),
            "news": [n["title"] for n in e.get("news", [])][:5],
            "disclosures": [d["title"] for d in e.get("disclosures", [])][:5],
        })

    user = json.dumps({"us_market": us_brief, "candidates": candidates},
                      ensure_ascii=False)
    system = SYSTEM.replace("{n}", str(N_FINAL))
    try:
        client = Anthropic()
        resp = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        result = _extract_json(text)
    except Exception as e:
        _warn(f"LLM selection failed: {e}")
        return None

    # 후보 목록 안의 종목만 신뢰 (환각 방지)
    valid = {r["Code"]: r for r in shortlist.to_dict("records")}
    picks = []
    for p in result.get("picks", []):
        code = str(p.get("code", "")).zfill(6)
        if code in valid and not any(code == x["code"] for x in picks):
            picks.append({
                "code": code,
                "name": valid[code]["Name"],
                "market": valid[code]["Market"],
                "reason": p.get("reason", ""),
                "catalyst": p.get("catalyst", ""),
            })
    if not picks:
        return None
    return {"marketView": result.get("marketView", ""), "picks": picks[:N_FINAL]}


def fallback_select(shortlist):
    """LLM 불가 시 기계 점수 상위 N_FINAL 종목으로 선정."""
    picks = []
    for r in shortlist.head(N_FINAL).to_dict("records"):
        picks.append({
            "code": r["Code"], "name": r["Name"], "market": r["Market"],
            "reason": (f"전일 등락률 {float(r['ChagesRatio']):+.2f}%, "
                       f"거래대금 {int(r['Amount']):,}원 — 거래대금회전율·모멘텀 상위."),
            "catalyst": "기계적 스크리닝 상위 (뉴스/LLM 미적용)",
        })
    return {"marketView": "LLM 선정을 사용할 수 없어 기계적 점수 상위 종목으로 대체했습니다.",
            "picks": picks}


# ----------------------------------------------------------------------------
# 출력
# ----------------------------------------------------------------------------
def write_outputs(selection, us_brief):
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    picks = selection["picks"]

    watchlist = [{"code": p["code"], "name": p["name"], "market": p["market"]} for p in picks]
    with open(WATCHLIST, "w", encoding="utf-8") as f:
        json.dump(watchlist, f, ensure_ascii=False, indent=2)
    print(f"  Updated watchlist : {WATCHLIST} ({len(watchlist)} stocks)")

    os.makedirs(SELECTION_DIR, exist_ok=True)
    selection_path = os.path.join(SELECTION_DIR, f"{today}.json")
    payload = {
        "date": today,
        "usMarket": us_brief,
        "marketView": selection.get("marketView", ""),
        "picks": picks,
    }
    with open(selection_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  Saved selection   : {selection_path}")


def main():
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    print(f"=== Pre-market screener ({today}) ===")

    print("1) 전일 미국시장 분석...")
    us_brief = us_market_brief()
    if us_brief.get("sectors"):
        top = us_brief["sectors"][0]
        print(f"   미국 섹터 1위: {top['name']} {top['changePct']:+.2f}%")

    print("2) 유니버스 + 기계적 1차 필터...")
    uni = load_universe()
    shortlist = mechanical_funnel(uni)
    print(f"   후보 {len(shortlist)}종목 (상위 5): "
          + ", ".join(f"{r['Name']}({float(r['ChagesRatio']):+.1f}%)"
                      for r in shortlist.head(5).to_dict("records")))

    print("3) 뉴스/공시 보강...")
    try:
        enriched = enrich_candidates(shortlist)
    except Exception as e:
        _warn(f"enrichment failed: {e}")
        enriched = {}

    print("4) LLM 최종 선정...")
    selection = llm_select(us_brief, shortlist, enriched) or fallback_select(shortlist)

    write_outputs(selection, us_brief)
    print("   선정 종목: " + ", ".join(f"{p['name']}({p['code']})" for p in selection["picks"]))
    print("=== Done ===")


if __name__ == "__main__":
    main()
