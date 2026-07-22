#!/usr/bin/env python3
"""미국 야간(정규장) SEC 공시 촉매 수집 → public/us_catalysts.json

모닝브리프의 미국 정보(지수·섹터·무버 수치)에 '무슨 일이 있었는지'를 보태는
장중 시황 촉매의 미국판. us_night_catalysts.yml 이 미국 정규장(KST 22:47~04:47)
동안 30분 간격으로 실행한다(Railway _scheduler 가 dispatch).

동작:
  1. 유니버스 = screener.SECTOR_US_STOCKS + analysis/peers.json 의 미국 상장 티커
     (접미사 없는 심볼만 — .T/.TW/.HK 등 비미국 상장은 SEC 미대상). 약 50~70개.
  2. SEC 공식 ticker→CIK 맵(company_tickers.json, 무료)으로 CIK 집합 구성.
  3. edgartools get_current_filings 로 최신 8-K/6-K 피드를 받아 유니버스 교집합 중
     '이번 세션에서 아직 못 본'(seen accession) 신규분만 추린다.
  4. 신규 0건이면 즉시 종료(LLM·커밋 없음 — 비용 절감 핵심).
  5. 신규 있으면 yfinance 로 당일 등락률 보강 → LLM 1콜로 한국어 촉매 요약·방향
     판정(스키마 강제, 내용 미상 공시는 유형·주가 반응만 근거로 서술) → 세션 파일에
     append(accession 중복 제거).
세션 = 미국 거래일(KST 17시 이후 시작 날짜). 새 세션 첫 회차에서 파일을 리셋한다.
실패 내성: 모든 단계 graceful — 기존 파일 보존(econ_calendar 패턴).

Env: SEC_IDENTITY("이름 이메일" — SEC fair-use 식별, 키 아님), GEMINI_API_KEY 등 LLM 체인.
의존성: edgartools 는 이 워크플로에서만 설치(requirements.txt 미포함 — pandas 드리프트 격리).
"""
import os
import sys
import json
import datetime

import requests

import llm
from screener import SECTOR_US_STOCKS

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "public", "us_catalysts.json")
PEERS_PATH = os.path.join(ROOT, "analysis", "peers.json")
KST = datetime.timezone(datetime.timedelta(hours=9))

SEC_IDENTITY = os.environ.get("SEC_IDENTITY", "QuantAntigravity cyb1100@gmail.com")
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
- 입력 필드: ticker, company, form, items(8-K 항목 코드, 없을 수 있음), changePct(당일 등락률).
- 공시 '본문'은 제공되지 않는다 — 내용을 추정해 지어내지 말고, 공시 유형(form·items)과
  당일 주가 반응(changePct)만 근거로 담백하게 서술한다.
  예: "8-K(실적 관련 항목) 제출, 주가 +3.2% 반응 — 한국 반도체 밸류체인 심리에 우호적".
- direction: 그 공시·주가 반응이 시사하는 방향(bullish|neutral|bearish). 근거가 약하면 neutral.
- summary 끝에 가능하면 한국 증시 연관 한 마디(밸류체인·경쟁사 관점).
출력은 정확히 이 JSON 만:
{"catalysts": [{"ticker": "...", "stock": "회사명", "direction": "bullish|neutral|bearish",
               "summary": "한국어 1-2문장"}]}"""

CAT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"catalysts": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {"ticker": {"type": "string"}, "stock": {"type": "string"},
                       "direction": {"type": "string"}, "summary": {"type": "string"}},
        "required": ["ticker", "stock", "direction", "summary"]}}},
    "required": ["catalysts"],
}


def summarize(filings):
    """LLM 1콜 — 실패 시 기계적 폴백(공시 유형 문구)."""
    view = [{k: f.get(k) for k in ("ticker", "company", "form", "items", "changePct")}
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

    uni = build_universe()
    _log(f"유니버스 {len(uni)}티커")
    try:
        ciks = cik_map(uni)
    except Exception as e:
        _log(f"CIK 맵 실패 — 이번 회차 스킵: {e}")
        return
    _log(f"CIK 매핑 {len(ciks)}/{len(uni)}")

    seen = set(state.get("seen") or [])
    new = scan_new_filings(ciks, seen)
    if not new:
        # 신규 없음 — 세션 리셋이 있었던 회차만 빈 상태를 기록(그 외엔 파일 무변경 → 커밋 0)
        if reset:
            state["asof"] = now.strftime("%Y-%m-%d %H:%M KST")
            with open(OUT_PATH, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=1)
        _log("신규 공시 0건 — 종료")
        return

    _log(f"신규 공시 {len(new)}건: " + ", ".join(f"{f['ticker']}({f['form']})" for f in new))
    new = attach_rates(new)
    cats = summarize(new)

    state["catalysts"] = (state.get("catalysts") or []) + cats
    state["seen"] = sorted(seen | {f["accession"] for f in new})
    state["asof"] = now.strftime("%Y-%m-%d %H:%M KST")
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    _log(f"저장: 누적 {len(state['catalysts'])}건 ({OUT_PATH})")


if __name__ == "__main__":
    main()
