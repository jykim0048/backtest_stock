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
import yfinance as yf

from analysis import sources

ROOT          = os.path.dirname(os.path.abspath(__file__))
WATCHLIST     = os.path.join(ROOT, "watchlist.json")
CONSTITUENTS  = os.path.join(ROOT, "data", "index_constituents.json")
SELECTION_DIR = os.path.join(ROOT, "public", "reports", "selection")

KST = datetime.timezone(datetime.timedelta(hours=9))

# ---- tunables -------------------------------------------------------------
N_FINAL        = int(os.environ.get("SCREEN_N_FINAL", "6"))        # 최종 선정 종목 수
PRICE_BACKUP   = int(os.environ.get("SCREEN_PRICE_BACKUP", "15"))  # ⓑ 가격 보조 후보 수
MAX_CANDIDATES = int(os.environ.get("SCREEN_MAX_CANDIDATES", "40"))  # LLM에 넘길 후보 상한
KOSPI_TOP      = 200   # 코스피200 근사 (시총 상위)
KOSDAQ_TOP     = 150   # 코스닥150 근사 (시총 상위)
ENRICH_WORKERS = int(os.environ.get("SCREEN_ENRICH_WORKERS", "8"))
NEWS_FETCH     = 30    # 시간창 필터 전, 최신순으로 받아올 뉴스 건수 (Naver display)
NEWS_MAX       = 8     # 시간창 통과 후 후보당 최대 뉴스 수
US_MOVERS_TOP  = 20    # 미국 급등 특징주 상위 N (시총 하한 없음)

MODEL      = "claude-sonnet-4-6"
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
# 1) 전일 미국시장 분석 (지수 + 섹터 + 급등 특징주)
# ----------------------------------------------------------------------------
def us_market_brief():
    items = US_INDICES + US_SECTORS
    tickers = [x["ticker"] for x in items]
    try:
        df = yf.download(tickers, period="5d", group_by="ticker",
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

    return {
        "indices": pack(US_INDICES) if df is not None else [],
        "sectors": sorted(pack(US_SECTORS), key=lambda d: d["changePct"], reverse=True) if df is not None else [],
        "movers": us_movers(),
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d"),
    }


def us_movers(top=US_MOVERS_TOP):
    """미국 당일 급등 특징주 (yfinance predefined screener 'day_gainers'). 시총 하한 없음.

    야후 비공식 엔드포인트라 실패하면 빈 리스트로 흡수한다.
    """
    try:
        r = yf.screen("day_gainers")
        quotes = r.get("quotes", []) if isinstance(r, dict) else []
    except Exception as e:
        _warn(f"day_gainers screen failed: {e}")
        return []
    movers = []
    for q in quotes:
        sym = q.get("symbol")
        pct = q.get("regularMarketChangePercent")
        if sym and pct is not None:
            movers.append({
                "symbol": sym,
                "name": q.get("shortName") or q.get("longName") or sym,
                "changePct": round(float(pct), 2),
            })
    movers.sort(key=lambda d: d["changePct"], reverse=True)
    return movers[:top]


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
    bgn = overnight_cutoff().strftime("%Y%m%d")            # 직전 거래일
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
def build_pool(uni, disclosure_map):
    scored = mechanical_score(uni)
    by_code = {r["Code"]: r for r in scored.to_dict("records")}

    pool, seen = [], set()

    # ⓐ 공시 촉매 종목 — 우선. 동률 시 가격 점수 순.
    disc_codes = [c for c in disclosure_map if c in by_code]
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
    """후보별 '전일 장마감~실행시각' 네이버 뉴스를 병렬 수집해 각 row에 붙인다."""
    cutoff = overnight_cutoff()
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
너는 한국 주식 데이 트레이딩 애널리스트다. 장 시작 전, 주어진 데이터로 '오늘 급등 가능성이
높은' 한국 종목 {n}개를 선정한다.

입력:
- us_market: 전일 미국 지수/섹터 ETF 등락 + 급등 특징주(movers). 어떤 섹터·테마가 강했는지,
  어떤 미국 종목이 급등했는지를 보고 '국내 동조/밸류체인 연결' 종목을 추론하는 데 쓴다.
- candidates: 코스피200/코스닥150 유니버스에서 1차 선별된 후보. 각 후보는
  source('공시'=촉매 공시 보유 / '가격'=거래대금·모멘텀 상위), 전일 등락률(changePct),
  거래대금(amountKRW), 전일 장마감 이후 공시(disclosures)와 뉴스(news)를 포함한다.

선정 원칙(중요도 순):
1) 의미 있는 '촉매 공시'(공급계약·수주·실적·임상/허가·투자·자사주 등)가 있는 종목을 최우선.
2) 전일 미국시장에서 강했던 섹터·급등 특징주와 테마/밸류체인이 연결된 종목.
3) 우호적 뉴스 흐름이 있는 종목.
4) 거래대금·모멘텀(가격)은 위 촉매의 '시장 반응 강도' 확인용으로만 쓴다.
- 근거(공시/뉴스/미국 테마) 없는 단순 가격 상승 종목은 넣지 마라. 후보 목록 안에서만 고른다.

반드시 아래 스키마와 정확히 동일한 JSON만 출력한다. 마크다운 펜스/설명 금지.
{
  "marketView": "전일 미국장(지수·섹터·특징주) 요약과 오늘 국내 시장 관점 3-4문장 (한국어)",
  "picks": [
    {"code": "6자리", "name": "종목명", "market": "KOSPI|KOSDAQ",
     "reason": "선정 사유 1-2문장 (공시/미국테마/뉴스 근거 명시)",
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


def llm_select(us_brief, pool):
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

    valid = {r["Code"]: r for r in pool}          # 후보 목록 안의 종목만 신뢰 (환각 방지)
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
        })
    return {"marketView": "LLM 선정을 사용할 수 없어 후보 점수 상위 종목으로 대체했습니다.",
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
    selection = llm_select(us_brief, pool) or fallback_select(pool)

    write_outputs(selection, us_brief)
    print("   선정 종목: " + ", ".join(f"{p['name']}({p['code']})" for p in selection["picks"]))
    print("=== Done ===")


if __name__ == "__main__":
    main()
