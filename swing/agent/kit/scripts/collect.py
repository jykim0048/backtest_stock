"""수집 오케스트레이터 — 종목 하나의 원자료를 병렬로 모아 JSON 5개 + manifest 로 저장한다.

LLM 을 호출하지 않는다. backtest_stock `generate_analysis.analyze_stock` 의 수집 그래프에서
Gemini 단계를 뺀 것이며, 소스 함수는 이 폴더의 sources_kr.py·social_kr.py(그 레포에서 이식)를 쓴다.

출력 (runs/<code>/<date>/):
  01_price.json            OHLCV 스냅샷 + 지표 13종 (indicators.py)        → Market Analyst
  02_fundamentals.json     밸류에이션(네이버·FnGuide) + DART 분기재무 + 대량보유 + 컨센서스 → Fundamentals Analyst
  03_news_community.json   네이버 뉴스·해외 뉴스·카페·종토방·증권사 리포트·peer 여론   → News / Sentiment Analyst
  04_flow.json             KIS 허브(/flow) + 네이버 일자별 매매동향                 → Flow Analyst
  05_disclosures.json      DART 최근 공시 + 촉매 키워드 플래그                      → News Analyst
  manifest.json            종목 정보·각 수집 레그 상태/소요시간/오류

사용:  python collect.py 삼성전자 [--date YYYY-MM-DD] [--no-social] [--full-dart]
환경:  DART_API_KEY NAVER_CLIENT_ID NAVER_CLIENT_SECRET (선택 TAVILY_API_KEY BRAVE_API_KEY FLOW_API_BASE)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

from _common import ASSETS_DIR, MEMORY_DIR as SKILL_MEMORY_DIR, load_env, read_json, run_dir, today_kst, warn, write_json

load_env()                                                # <skill>/.env → 환경변수
import dart_fin                                           # noqa: E402
import indicators                                         # noqa: E402
import social_kr as social                                # noqa: E402
import sources_kr as sources                              # noqa: E402
from resolve import candidates, resolve                   # noqa: E402

FLOW_API_BASE = os.environ.get("FLOW_API_BASE", "https://tradingstrategies-production-09d4.up.railway.app")

CATALYST_KW = getattr(sources, "DART_CATALYST_KEYWORDS", (
    "공급계약", "단일판매", "유상증자", "무상증자", "합병", "분할", "소송", "특허", "임상", "품목허가",
    "자기주식", "전환사채", "신주인수권", "최대주주", "경영권", "영업정지", "파산", "회생", "투자판단",
    "조회공시", "생산재개", "생산중단", "화재", "수주"))
_BULL_KW = ("공급계약", "단일판매", "수주", "무상증자", "자기주식", "자사주", "품목허가", "특허")
_BEAR_KW = ("유상증자", "전환사채", "신주인수권", "교환사채", "감자", "소송", "횡령", "배임",
            "영업정지", "파산", "회생", "생산중단", "화재")


def _hub_flow(code: str, rows: int = 30, timeout: int = 15) -> dict:
    r = requests.get(f"{FLOW_API_BASE}/flow", params={"code": code, "rows": rows}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "success":
        raise RuntimeError(f"hub /flow status={d.get('status')}")
    keep = ("daily", "shorts", "loans", "asof", "detail")          # 가집계 랭킹(rank)은 쓰지 않는다(2026-09-22)
    return {k: d[k] for k in keep if k in d}


def _flag_disclosures(items: list[dict]) -> list[dict]:
    out = []
    for it in items:
        t = it.get("title") or ""
        flags = [k for k in CATALYST_KW if k in t]
        tone = "bull" if any(k in t for k in _BULL_KW) else ("bear" if any(k in t for k in _BEAR_KW) else "neutral")
        if "정정" in t:
            tone = "correction"
        out.append({**it, "catalyst_keywords": flags, "tone": tone})
    return out


def _consensus(reports: list[dict]) -> dict:
    tps = [r["targetPrice"] for r in reports if isinstance(r.get("targetPrice"), (int, float)) and r["targetPrice"] > 0]
    ops = [(r.get("opinion") or "").strip() for r in reports if r.get("opinion")]
    return {"n_reports": len(reports), "n_target": len(tps),
            "target_mean": round(sum(tps) / len(tps)) if tps else None,
            "target_max": max(tps) if tps else None, "target_min": min(tps) if tps else None,
            "opinions": {o: ops.count(o) for o in sorted(set(ops))}}


def collect(stock: dict, date: str, out: Path, social_on: bool = True, full_dart: bool = False) -> dict:
    code, name, ticker, corp = stock["code"], stock["name"], stock["ticker"], stock.get("corp_code")
    status, dur, errors = {}, {}, {}

    def timed(label, fn, *a, **kw):
        def run():
            t = time.perf_counter()
            try:
                return fn(*a, **kw)
            finally:
                dur[label] = round(time.perf_counter() - t, 2)
        return run

    def res(fut, label, default):
        try:
            v = fut.result()
            status[label] = "ok" if v else "empty"
            return v if v is not None else default
        except Exception as e:  # noqa: BLE001
            status[label] = "unavailable"
            errors[label] = str(e)[:300]
            warn(f"{label} 실패: {e}")
            return default

    # 해외 맥락: yfinance 프로필(영문명·업종) → peer 3단계 폴백 + 종목 직접 미국 심볼
    profile = sources.yahoo_profile(ticker)
    peer_cfg = read_json(ASSETS_DIR / "peers.json", {}) or {}
    industry_cfg = read_json(ASSETS_DIR / "industry_peers.json", {}) or {}
    dynamic_cfg = read_json(SKILL_MEMORY_DIR / "peers_dynamic.json", {}) or {}   # Claude 제안·yfinance 검증(peers_resolve.py)
    peers_static, peer_source = sources.select_peers(code, profile.get("industryKey"), peer_cfg, industry_cfg, dynamic_cfg=dynamic_cfg)

    with ThreadPoolExecutor(max_workers=10) as pool:
        sub = lambda label, fn, *a, **kw: pool.submit(timed(label, fn, *a, **kw))  # noqa: E731
        f_price = sub("price", indicators.run, ticker, date, out)
        f_news = sub("naver_news", sources.naver_search, "news", name, display=10)
        f_osn = sub("overseas_news", sources.peer_news, peers_static, top=4, per_peer=4, max_items=12) if peers_static else None
        f_cafe = sub("naver_cafe", sources.naver_search, "cafearticle", name, display=6)
        f_board = sub("naver_board", sources.naver_board, code, pages=2)
        f_rsch = sub("research", sources.combined_research, code, days=60)
        f_valn = sub("valuation_naver", sources.naver_valuation, code)
        f_valf = sub("valuation_fnguide", sources.fnguide_valuation, code)
        f_trend = sub("naver_trend", sources.naver_investor_trend_full, code, rows=20)
        f_flow = sub("hub_flow", _hub_flow, code)
        f_disc = sub("dart_disclosures", sources.dart_disclosures, corp, days=120, page_count=15) if corp else None
        f_hold = sub("dart_major_holders", sources.dart_major_holders, corp) if corp else None
        f_fin = sub("dart_financials", dart_fin.collect, corp, 5) if corp else None
        f_quotes = sub("peer_quotes", sources.get_peer_quotes, peers_static) if peers_static else None
        f_st = sub("peer_stocktwits", social.stocktwits_for_peers, peers_static, top=2, fresh_days=7) if (peers_static and social_on) else None
        f_rd = sub("reddit", social.reddit_for_company, profile.get("keywords"), peers_static, limit=5, date=date) if social_on else None

        price = res(f_price, "price", {})
        naver_news = res(f_news, "naver_news", [])
        overseas = res(f_osn, "overseas_news", []) if f_osn else []
        cafe = res(f_cafe, "naver_cafe", [])
        board = res(f_board, "naver_board", [])
        research = res(f_rsch, "research", [])
        val_naver = res(f_valn, "valuation_naver", {})
        val_fng = res(f_valf, "valuation_fnguide", {})
        trend = res(f_trend, "naver_trend", [])
        hub = res(f_flow, "hub_flow", {})
        disc = res(f_disc, "dart_disclosures", []) if f_disc else []
        holders = res(f_hold, "dart_major_holders", []) if f_hold else []
        fin_pair = res(f_fin, "dart_financials", None) if f_fin else None
        quotes = res(f_quotes, "peer_quotes", []) if f_quotes else []
        st = res(f_st, "peer_stocktwits", []) if f_st else []
        rd = res(f_rd, "reddit", {"posts": [], "status": "skipped"}) if f_rd else {"posts": [], "status": "skipped"}

    # 소셜 레그는 반환 dict 의 status 가 진실(빈 dict 가 아니라도 unavailable 일 수 있음)
    if f_rd:
        status["reddit"] = rd.get("status") or status.get("reddit")
    else:
        status["reddit"] = "skipped(--no-social)" if not social_on else "skipped"
    if not peers_static:
        status["peer_quotes"] = status["peer_stocktwits"] = status["overseas_news"] = "skipped(no peers)"
    # 공매도·대차 시총대비(%) — yfinance 시가총액 기준
    if hub and profile.get("marketCap"):
        hub = sources.annotate_flow_mcap(hub, profile.get("marketCap"))

    if not corp:
        status["dart_disclosures"] = status["dart_major_holders"] = status["dart_financials"] = "skipped(no corp_code)"
    fin_core, fin_raw = (fin_pair if isinstance(fin_pair, tuple) else ({}, []))
    if fin_pair and fin_core.get("status") != "ok":
        # 분기 확장 실패 시 기존 연간 6계정 폴백
        try:
            legacy = sources.dart_financials(corp)
            fin_core = {**fin_core, "legacy_annual": legacy}
            status["dart_financials"] = "fallback(legacy)" if legacy else status.get("dart_financials", "empty")
        except Exception as e:  # noqa: BLE001
            errors["dart_financials_legacy"] = str(e)[:200]
    if full_dart and fin_raw:
        write_json(out / "dart_raw.json", fin_raw)

    write_json(out / "02_fundamentals.json", {
        "stock": stock, "date": date,
        "valuation": {"naver": val_naver, "fnguide": val_fng,
                      "note": "per/eps 트레일링, estPer/estEps 컨센서스, 업종PER 비교용. 현재 시점 스냅샷(과거 빈티지 없음)."},
        "dart_financials": fin_core,
        "major_holders": holders,
        "research_consensus": _consensus(research),
    })
    write_json(out / "03_news_community.json", {
        "stock": stock, "date": date,
        "naver_news": naver_news, "overseas_news": overseas,
        "overseas_note": "해외 뉴스 = 해외 peer 종목의 야후 티커 뉴스(상위 4개 peer, 각 4건). 항목의 peer 가 출처 종목, relevance=company 는 그 peer 회사명 매치, context 는 섹터·시황.",
        "naver_cafe": cafe, "naver_board": board,
        "research_reports": research,
        "peers": {"items": quotes, "source": peer_source,
                  "industry": {"sector": profile.get("sector"), "industry": profile.get("industry"), "industryKey": profile.get("industryKey")},
                  "stocktwits": st,
                  "reddit": rd.get("posts") or [],
                  "status": {"stocktwits": [{"ticker": r.get("ticker"), "status": r.get("status")} for r in st],
                             "reddit": rd.get("status"), "reddit_queried": rd.get("queried") or [], "reddit_cached": rd.get("cached") or []},
                  "note": "source=curated 는 peers.json 큐레이션, dynamic 은 Claude 제안·yfinance 검증(memory/peers_dynamic.json), industry-default 는 yfinance 업종 기반 미국 대표주(직접 경쟁 아닐 수 있음 — 섹터 맥락으로만). reddit 글의 scope=company 는 영문 회사명 검색, peer 는 1순위 peer 티커 검색. 수집 실패(unavailable)는 '언급 없음'이 아니다. empty 만 침묵으로 해석."},
    })
    write_json(out / "04_flow.json", {
        "stock": stock, "date": date,
        "hub": hub, "hub_status": status.get("hub_flow"),
        "naver_trend": trend,
        "note": ("hub.daily = KIS 일별 투자자 순매수 대금(백만원, 15:40 이후 확정). "
                 "shorts = 공매도 일별(pbmn 억원·거래대금 비중 pbmnRlim%·시총대비 mcapPct%), shorts_20d_mcapPct = 20일 누적 공매도 대금÷시총. "
                 "loans = 대차 일별(잔고 rmndAmt 억원·시총대비 rmndMcapPct%·증감). marketCap_eok = 시가총액(억원, yfinance). "
                 "naver_trend = 일자별 외국인/기관/개인 순매매 수량(주)·외국인 보유율%."),
    })
    write_json(out / "05_disclosures.json", {
        "stock": stock, "date": date,
        "disclosures": _flag_disclosures(disc),
        "note": "DART list 는 접수 날짜만 제공(시각 없음). tone 은 제목 키워드 기반 1차 분류일 뿐 판단이 아님.",
    })
    manifest = {
        "stock": stock, "date": date, "run_dir": str(out),
        "files": ["01_price.json", "02_fundamentals.json", "03_news_community.json", "04_flow.json", "05_disclosures.json"],
        "legs": {k: {"status": status.get(k), "seconds": dur.get(k), "error": errors.get(k)}
                 for k in sorted(set(status) | set(dur))},
        "env": {k: bool(os.environ.get(k)) for k in ("DART_API_KEY", "NAVER_CLIENT_ID", "NAVER_CLIENT_SECRET", "TAVILY_API_KEY", "BRAVE_API_KEY")},
        "flow_api_base": FLOW_API_BASE,
    }
    write_json(out / "manifest.json", manifest)
    return manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="+", help="종목명 또는 6자리 코드")
    ap.add_argument("--date", default=today_kst())
    ap.add_argument("--out", default=None, help="출력 폴더(기본 runs/<code>/<date>)")
    ap.add_argument("--no-social", action="store_true", help="StockTwits/Reddit 생략")
    ap.add_argument("--full-dart", action="store_true", help="DART 전 계정 원본도 저장")
    a = ap.parse_args(argv)
    q = " ".join(a.query)
    stock = resolve(q)
    if not stock:
        print(f"종목을 찾지 못했습니다: {q}", file=sys.stderr)
        for c in candidates(q):
            print(f"  후보: {c['name']} ({c['code']})", file=sys.stderr)
        return 2
    out = Path(a.out) if a.out else run_dir(stock["code"], a.date)
    m = collect(stock, a.date, out, social_on=not a.no_social, full_dart=a.full_dart)
    summary = {k: v["status"] for k, v in m["legs"].items()}
    print(json.dumps({"stock": stock, "run_dir": m["run_dir"], "legs": summary}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
