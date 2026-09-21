"""
REST data sources for the daily Deep Research batch.

Each function returns RAW data (no interpretation). The LLM step
(generate_analysis.py) turns this raw data into the analysis schema.

All functions degrade gracefully: on any error they return an empty
result and log to stderr, so one failing API never kills the batch.

Env vars (GitHub Actions secrets):
  DART_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, TAVILY_API_KEY
"""
import io
import os
import re
import sys
import json
import zipfile
import datetime
import threading
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor

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

UA = {"User-Agent": "Mozilla/5.0 (quant-antigravity batch)"}


def _warn(msg):
    print(f"[sources] {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------
# 1) Peer quotes (yfinance) — deterministic, no key needed
# ----------------------------------------------------------------------------
def _ticker_frame(df, ticker):
    """Return a single-level OHLCV frame for `ticker`, handling yfinance MultiIndex."""
    if df is None:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        if ticker in df.columns.get_level_values(0):
            return df[ticker]                       # ticker-major (group_by='ticker')
        flat = df.copy()
        flat.columns = flat.columns.get_level_values(0)
        return flat                                  # field-major: flatten to fields
    return df


def get_peer_quotes(peers):
    """peers: list of {name, ticker, note}. Returns same list with price/changePct filled."""
    out = []
    tickers = [p["ticker"] for p in peers]
    try:
        df = yf.download(tickers, period="2d", group_by="ticker",
                         progress=False, threads=True, auto_adjust=True, timeout=15)
    except Exception as e:
        _warn(f"yfinance download failed: {e}")
        df = None

    for p in peers:
        item = {"name": p["name"], "ticker": p["ticker"],
                "price": "N/A", "changePct": 0, "note": p.get("note", "")}
        try:
            tdf = _ticker_frame(df, p["ticker"])
            closes = tdf["Close"].dropna() if (tdf is not None and "Close" in tdf.columns) else None
            if closes is not None and len(closes) >= 1:
                close = float(closes.iloc[-1])
                prev = float(closes.iloc[-2]) if len(closes) >= 2 else close
                item["price"] = round(close, 2)
                item["changePct"] = round(((close - prev) / prev) * 100, 2) if prev else 0
        except Exception as e:
            _warn(f"peer quote failed for {p['ticker']}: {e}")
        out.append(item)
    return out


# ----------------------------------------------------------------------------
# 2) Naver Search API (news, cafearticle)
# ----------------------------------------------------------------------------
def naver_search(kind, query, display=8):
    """kind: 'news' | 'cafearticle'. Returns list of {title, link, description, date}."""
    cid = os.environ.get("NAVER_CLIENT_ID")
    secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        _warn("NAVER_CLIENT_ID/SECRET missing")
        return []
    try:
        r = requests.get(
            f"https://openapi.naver.com/v1/search/{kind}.json",
            headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
            params={"query": query, "display": display, "sort": "date"},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        return [{
            "title": _strip_tags(it.get("title", "")),
            "link": it.get("originallink") or it.get("link", ""),
            "description": _strip_tags(it.get("description", "")),
            "date": it.get("pubDate", ""),
        } for it in items]
    except Exception as e:
        _warn(f"naver_search({kind}, {query}) failed: {e}")
        return []


def _strip_tags(s):
    return (s.replace("<b>", "").replace("</b>", "")
            .replace("&quot;", '"').replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))


# ----------------------------------------------------------------------------
# 2b) Naver Finance 종목토론방 (HTML scrape — not in the official Search API)
#     Encoding is auto-detected (now UTF-8; was EUC-KR historically). Datacenter/CI
#     IPs may be blocked, so this degrades to [] (cafearticle search is the fallback).
# ----------------------------------------------------------------------------
_BOARD_API = "https://stock.naver.com/api/community/discussion/posts"
_BOARD_PAGE = 20           # 구형 게시판 1페이지 = 20글 — pages 인자와 같은 분량을 1콜로


def naver_board(code, pages=1, limit=15):
    """종목토론방 글 → [{title, url, date, views, agree, disagree, comments}], 공감수
    내림차순(노이즈/낚시 글을 가라앉히기 위함). 실패 시 [] 반환(배치 중단 방지).

    2026-09-21 신규 웹 JSON 으로 전환 — 구형 finance.naver.com/item/board.naver 가
    stock.naver.com/domestic/stock/<code>/discussion SPA 로 리다이렉트되어 HTML 파서가
    항상 0건(딥리서치 board_deep 결손). 번들 규약: itemCode·discussionType·isHolderOnly·
    excludesItemNews·isItemNewsOnly·pageSize **전부 필요** — 빠지면 전 종목 글이 섞여
    온다(프로브 실측). 목록 응답엔 조회수가 없어 화면과 같이 글 ID 묶음 API
    (posts/reactions?postIds=)로 viewCount·공감/비공감(최신값)을 채운다 — 실패 시
    목록값 유지, views 는 0."""
    try:
        r = requests.get(_BOARD_API,
                         params={"itemCode": code, "discussionType": "domesticStock",
                                 "isHolderOnly": "false", "excludesItemNews": "false",
                                 "isItemNewsOnly": "false",
                                 "pageSize": min(100, _BOARD_PAGE * max(1, pages))},
                         headers={**UA, "Referer":
                                  f"https://stock.naver.com/domestic/stock/{code}/discussion"},
                         timeout=15)
        r.raise_for_status()
        posts = (r.json() or {}).get("posts") or []
    except Exception as e:
        _warn(f"naver_board({code}) failed: {e}")
        return []

    def _i(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    out, by_id = [], {}
    for p in posts:
        # 방어: 파라미터 규약이 바뀌어 다른 종목 글이 섞이면 버린다(프로브 실측 사례)
        if str(p.get("itemCode") or code) != code or p.get("replyDepth"):
            continue
        pid = str(p.get("id") or "")
        row = {
            "title": str(p.get("title") or "").strip(),
            "url": f"https://stock.naver.com/domestic/stock/{code}/discussion/{pid}" if pid else "",
            "date": str(p.get("writtenAt") or "").replace("T", " ")[:16],
            "views": 0,
            "agree": _i(p.get("recommendCount")),
            "disagree": _i(p.get("notRecommendCount")),
            "comments": _i(p.get("commentCount")),
        }
        out.append(row)
        if pid:
            by_id[pid] = row

    # 조회수·반응 — 글 ID 50개씩 묶음 조회(화면과 동일 경로)
    ids = list(by_id)
    for i in range(0, len(ids), 50):
        try:
            r = requests.get(f"{_BOARD_API}/reactions",
                             params={"postIds": ",".join(ids[i:i + 50])},
                             headers={**UA, "Referer":
                                      f"https://stock.naver.com/domestic/stock/{code}/discussion"},
                             timeout=15)
            r.raise_for_status()
            for rx in r.json() or []:
                row = by_id.get(str(rx.get("postId") or ""))
                if row is not None:
                    row["views"] = _i(rx.get("viewCount"))
                    row["agree"] = _i(rx.get("recommendCount", row["agree"]))
                    row["disagree"] = _i(rx.get("notRecommendCount", row["disagree"]))
        except Exception as e:
            _warn(f"naver_board({code}) reactions failed: {e}")
            break

    out.sort(key=lambda x: x.get("agree", 0), reverse=True)
    return out[:limit]


# ----------------------------------------------------------------------------
# 2c/2d) Naver 증권사 리서치 (종목분석·산업분석) — m.stock.naver.com 모바일 JSON API
#     (2026-09-18) finance.naver.com/research/*.naver HTML 페이지가 stock.naver.com
#     SPA 로 302 리다이렉트돼 옛 셀렉터(a[href*="_read.naver"], td.view_cnt)가 항상
#     0건 매칭 — requests 는 리다이렉트를 따라가 200 을 받으므로 예외·경고 없이
#     빈 리스트만 돌아왔다(모닝브리핑 industryReports 가 2026-06 이후 전일 []).
#     같은 데이터를 주는 모바일 JSON API 로 전환(naver_investor_trend 와 같은 호스트 —
#     Actions 해외 IP 접근성 검증 완료, .github/naver_flow_probe_result.json):
#       종목 목록: /api/research/stock/<code>?page&pageSize      (previewContent 포함)
#       종목 상세: /api/research/company/<researchId>             (opinion·goalPrice·content·attachUrl)
#       산업 목록: /api/research/industry?page&pageSize           (category = 옛 업종명 표기와 동일)
#       산업 상세: /api/research/industry/<researchId>            (content·attachUrl)
#     url 은 데스크톱 상세(stock.naver.com/research/<kind>/<id>)로 두고, pdfUrl 은
#     상세(attachUrl)를 조회한 건에만 채운다(목록 API 에는 첨부가 없음).
# ----------------------------------------------------------------------------
_MSTOCK_RESEARCH = "https://m.stock.naver.com/api/research"
_RESEARCH_PAGE   = 200          # 산업 목록 페이지 크기(≈2주치) — 서버 상한 300 이상 확인


def _research_id(url):
    """리서치 URL → researchId 문자열. 신형(.../industry/46132, .../company/96231)과
    구형(finance.naver.com/...?nid=46132 — 아카이브에 남은 URL, nid == researchId) 모두 수용."""
    m = re.search(r"(?:nid=|/(?:industry|company)/)(\d+)", str(url or ""))
    return m.group(1) if m else ""


def _research_text(html_body, max_chars):
    """리서치 본문 HTML(content) → 태그 제거·공백 정규화 텍스트 (max_chars 로 절단)."""
    import html as _html
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", " ", str(html_body or ""), flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(_html.unescape(text).split())[:max_chars]


def _research_json(path, params=None, timeout=15):
    r = requests.get(f"{_MSTOCK_RESEARCH}/{path}", params=params, headers=UA, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _research_row(it):
    """목록 API 공통 필드 → (date, title, broker, researchId). 날짜 형식이 아니면 date=""."""
    d = str(it.get("writeDate") or "").strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        d = ""
    return (d, (it.get("title") or "").strip(), (it.get("brokerName") or "").strip(),
            str(it.get("researchId") or "").strip())


def naver_research(code, days=30, limit=6, detail_top=3):
    """증권사 종목분석 리포트 목록(+상위 detail_top 건 상세)을 수집한다.

    Returns list of {title, broker, date, url, pdfUrl, targetPrice, opinion,
    summary} (최신순, days 일 이내). targetPrice/opinion/pdfUrl 은 상세를 조회한
    상위 건에만 채워진다(summary 는 목록의 previewContent 로 전 건 채우고, 상세
    조회 건은 본문으로 교체). 실패 시 [] (배치 중단 방지).
    """
    kst = datetime.timezone(datetime.timedelta(hours=9))
    cutoff = (datetime.datetime.now(kst).date() - datetime.timedelta(days=days)).isoformat()
    try:
        rows = _research_json(f"stock/{code}", {"page": 1, "pageSize": max(limit, 20)})
    except Exception as e:
        _warn(f"naver_research({code}) list failed: {e}")
        return []

    out = []
    for it in (rows if isinstance(rows, list) else []):
        d, title, broker, rid = _research_row(it)
        if not d or not rid or d < cutoff:
            continue                 # 목록은 최신순이지만 안전하게 전 건 필터
        out.append({
            "title": title,
            "broker": broker,
            "date": d,
            "url": f"https://stock.naver.com/research/company/{rid}",
            "pdfUrl": "",
            "targetPrice": None, "opinion": "",
            "summary": " ".join(str(it.get("previewContent") or "").split())[:500],
        })
        if len(out) >= limit:
            break

    # 상위 detail_top 건만 상세 조회(요청 수 절약): 목표주가·투자의견·본문·PDF.
    # 상세는 서로 독립이라 병렬 수집(2026-07-22 온디맨드 속도 개선 — 순차 15s×3 제거).
    def _detail(rpt):
        try:
            rc = (_research_json(f"company/{_research_id(rpt['url'])}") or {}).get("researchContent") or {}
            goal = str(rc.get("goalPrice") or "").replace(",", "").strip()
            if goal.isdigit() and int(goal) > 0:
                rpt["targetPrice"] = int(goal)
            if (rc.get("opinion") or "").strip():
                rpt["opinion"] = rc["opinion"].strip()
            if rc.get("attachUrl"):
                rpt["pdfUrl"] = rc["attachUrl"]
            body = _research_text(rc.get("content"), 500)
            if body:
                rpt["summary"] = body
        except Exception as e:
            _warn(f"naver_research detail({rpt.get('url','')}) failed: {e}")

    top = out[:detail_top]
    if len(top) > 1:
        with ThreadPoolExecutor(max_workers=len(top)) as ex:
            list(ex.map(_detail, top))
    elif top:
        _detail(top[0])
    return out


def naver_industry_research(days=30, max_pages=3):
    """산업분석 리포트 최근 목록을 수집한다 (업종 필터 없이 전체 → 호출측에서 매칭).

    Returns list of {category, title, broker, date, url, pdfUrl} (최신순,
    days 일 이내). 실패 시 [] (배치 중단 방지). 상세 본문·PDF 는 요청 수 절약을
    위해 별도 함수(naver_industry_detail)로 필요한 건만 조회한다.
    """
    kst = datetime.timezone(datetime.timedelta(hours=9))
    cutoff = (datetime.datetime.now(kst).date() - datetime.timedelta(days=days)).isoformat()
    out = []
    for page in range(1, max_pages + 1):
        try:
            rows = _research_json("industry", {"page": page, "pageSize": _RESEARCH_PAGE})
        except Exception as e:
            _warn(f"naver_industry_research(p{page}) failed: {e}")
            break
        if not isinstance(rows, list) or not rows:
            break
        stop = False
        for it in rows:
            d, title, broker, rid = _research_row(it)
            if not d or not rid:
                continue
            if d < cutoff:
                stop = True              # 목록은 최신순 — 컷오프 지나면 다음 페이지 불필요
                break
            out.append({
                "category": (it.get("category") or "").strip(),
                "title": title,
                "broker": broker,
                "date": d,
                "url": f"https://stock.naver.com/research/industry/{rid}",
                "pdfUrl": "",
            })
        if stop or len(rows) < _RESEARCH_PAGE:
            break
    return out


def naver_industry_detail(url, max_chars=400):
    """산업분석 리포트 상세의 요약 본문 텍스트. 실패 시 ""."""
    rid = _research_id(url)
    if not rid:
        _warn(f"naver_industry_detail({url}): researchId 추출 실패")
        return ""
    try:
        rc = (_research_json(f"industry/{rid}") or {}).get("researchContent") or {}
        return _research_text(rc.get("content"), max_chars)
    except Exception as e:
        _warn(f"naver_industry_detail({url}) failed: {e}")
    return ""


# ----------------------------------------------------------------------------
# 2f) Naver 종목 밸류에이션 — m.stock.naver.com 모바일 JSON(integration)
#     Yahoo Finance 가 KRX 종목의 trailing EPS 를 제공하지 않아 PER/PBR 이 항상
#     결측 — 네이버가 정본. 2026-09-21 전환: 구형 finance.naver.com/item/main.naver 가
#     stock.naver.com/domestic/stock/<code>/price SPA 로 리다이렉트되어 #_per 등
#     셀렉터가 전부 사라졌다(값이 모두 None 인 dict 를 돌려줘 '조회 실패' 경고도 안 뜨고
#     추정PER·배당·목표가 폴백이 조용히 결측). integration 은 totalInfos[{code,key,
#     value}]·consensusInfo{recommMean, priceTargetMean} 를 준다(프로브 실측).
#     동일업종 PER·등락률은 새 API 에 없어 None — 업종 PER 은 FnGuide 3순위가 보완.
# ----------------------------------------------------------------------------
_INTEGRATION_API = "https://m.stock.naver.com/api/stock/{code}/integration"


def _vnum(s):
    """'12.25배' / '22,292원' / '0.61%' / '-3.2배' / 'N/A' → float|None"""
    t = re.sub(r"[^\d.\-]", "", str(s or ""))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def _opinion_label(score):
    """네이버 투자의견 점수(1~5, 증권사 평균) → 라벨. 새 API 엔 라벨 텍스트가 없어
    점수 구간으로 복원 — 표기는 신규 웹 컨센서스 막대(적극매도·매도·중립·매수·
    적극매수, 2026-09-21 사용자 제공 화면: 4.00=매수)와 동일."""
    if score is None:
        return None
    return ("적극매수" if score >= 4.5 else "매수" if score >= 3.5 else
            "중립" if score >= 2.5 else "매도" if score >= 1.5 else "적극매도")


def naver_valuation(code):
    """네이버 종목 밸류에이션 지표.

    Returns {per, eps, estPer, estEps, pbr, bps, dividendYield, industryPer,
    industryChangePct, opinionScore, opinionLabel, targetPrice, high52w, low52w}
    — 값이 없거나 N/A(적자 등)면 None. 요청 실패·응답 비정상 시 {} (배치 중단 방지 +
    호출부 '조회 실패' 경고가 뜨도록). per·eps 는 trailing, estPer·estEps 는 증권사
    추정 평균(컨센서스). 52주 고저는 수정주가(…Adjusted) 우선."""
    try:
        r = requests.get(_INTEGRATION_API.format(code=code), headers=UA, timeout=15)
        r.raise_for_status()
        d = r.json() or {}
    except Exception as e:
        _warn(f"naver_valuation({code}) failed: {e}")
        return {}
    info = {str(x.get("code")): x.get("value") for x in (d.get("totalInfos") or [])
            if isinstance(x, dict)}
    if not info:
        _warn(f"naver_valuation({code}): totalInfos 없음")
        return {}
    cns = d.get("consensusInfo") or {}
    score = _vnum(cns.get("recommMean"))

    def pick(*keys):
        for k in keys:
            v = _vnum(info.get(k))
            if v is not None:
                return v
        return None

    return {
        "per": pick("per"), "eps": pick("eps"),
        "estPer": pick("cnsPer"), "estEps": pick("cnsEps"),
        "pbr": pick("pbr"), "bps": pick("bps"),
        "dividendYield": pick("dividendYieldRatio"),
        "industryPer": None, "industryChangePct": None,
        "opinionScore": score, "opinionLabel": _opinion_label(score),
        "targetPrice": _vnum(cns.get("priceTargetMean")),
        "high52w": pick("highPriceOf52WeeksAdjusted", "highPriceOf52Weeks"),
        "low52w": pick("lowPriceOf52WeeksAdjusted", "lowPriceOf52Weeks"),
    }


# ----------------------------------------------------------------------------
# 2g) COMP FNGUIDE 밸류에이션 보완 (wcomp.fnguide.com — HTML 내장 JSON 파싱)
#     네이버·야후가 못 채우는 칸 전용 3순위 소스:
#       ① 코스닥 스몰캡 ROE·매출액증가율 (FinanceRatio, yfinance 미제공 케이스)
#       ② 적자기업 12M 선행 PER (Invest 상단 칩 — 트레일링 PER 이 'N/A'일 때)
#       ③ 업종 PER·PBR 잔여 결측
#     페이지가 서버렌더링 HTML 안에 JSON 변수(rtoAccumulate 등)를 내장하는
#     구조라 DOM 테이블 파싱보다 견고하다. (2026-07-07 구조 실측 기준)
# ----------------------------------------------------------------------------
_FNGUIDE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://wcomp.fnguide.com/",
}


def _fnguide_num(t):
    """'23.85' / '157.07' / '-' / 'N/A' / '1,234' -> float|None"""
    if t is None:
        return None
    t = str(t).strip().replace(",", "").replace("%", "")
    if t in ("", "-", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def fnguide_invest(code):
    """Invest 페이지 상단 지표 칩 — {per, fwdPer, industryPer, pbr}.
    각 칩은 <button id="h_..">라벨</button></li><li> 값 < 구조. 실패 시 {}."""
    out = {"per": None, "fwdPer": None, "industryPer": None, "pbr": None}
    try:
        r = requests.get("https://wcomp.fnguide.com/CompanyInfo/Invest",
                         params={"c_id": "AA", "menu_type": "01", "cmp_cd": code},
                         headers=_FNGUIDE_HEADERS, timeout=15)
        r.raise_for_status()
        for cid, key in (("h_per", "per"), ("h_12m", "fwdPer"),
                         ("h_u_per", "industryPer"), ("h_pbr", "pbr")):
            m = re.search(r'id="' + cid + r'"[^>]*>.*?</button>\s*</li>\s*<li>\s*([^<]+)<',
                          r.text, re.S)
            if m:
                out[key] = _fnguide_num(m.group(1))
    except Exception as e:
        _warn(f"fnguide invest({code}) failed: {e}")
    return out if any(v is not None for v in out.values()) else {}


# FinanceRatio 행 이름(NM, strip 후) -> 반환 키. 성장·수익성·안정성 지표는
# 야후·네이버가 못 주는 FnGuide 전용 (Q점수 3단계 — Growth/Quality 보강).
_FNGUIDE_RATIO_WANT = {
    "ROE": "roe",
    "매출액증가율": "revGrowth",
    "영업이익증가율": "opGrowth",
    "EPS증가율": "epsGrowth",
    "영업이익률": "opMargin",
    "이자보상배율": "interestCoverage",
}


def fnguide_ratios(code):
    """FinanceRatio 페이지 — 내장 JSON 변수 rtoAccumulate(연간 재무비율 테이블).
    header 가 컬럼 CD(VAL1~5)와 연도(YYMM, 마지막은 '최근분기')를 매핑하므로
    가장 오른쪽(최신) 비결측 값을 취한다. 반환 키는 _FNGUIDE_RATIO_WANT 값들,
    실패 시 {}."""
    out = {}
    try:
        r = requests.get("https://wcomp.fnguide.com/CompanyInfo/FinanceRatio",
                         params={"c_id": "AA", "menu_type": "01", "cmp_cd": code},
                         headers=_FNGUIDE_HEADERS, timeout=15)
        r.raise_for_status()
        m = re.search(r'rtoAccumulate\s*[:=]\s*(\{.*?\})\s*,\s*rtoAccumulate3Month',
                      r.text, re.S)
        if m:
            d = json.loads(m.group(1))
            cols = [h.get("CD") for h in (d.get("header") or []) if h.get("CD")] \
                   or [f"VAL{i}" for i in range(1, 6)]
            want = dict(_FNGUIDE_RATIO_WANT)
            for row in d.get("data") or []:
                key = want.pop((row.get("NM") or "").strip(), None)
                if not key:
                    continue
                for c in reversed(cols):   # 최신 컬럼(최근분기) 우선
                    v = _fnguide_num(row.get(c))
                    if v is not None:
                        out[key] = v
                        break
                if not want:
                    break
    except Exception as e:
        _warn(f"fnguide ratio({code}) failed: {e}")
    return out


def fnguide_valuation(code):
    """Invest + FinanceRatio 통합 (하위호환 래퍼). 두 페이지 모두 실패하면 {}."""
    out = {**fnguide_invest(code), **fnguide_ratios(code)}
    return out if any(v is not None for v in out.values()) else {}


# ----------------------------------------------------------------------------
# 2h) COMP FNGUIDE 요약리포트 (증권사 종목분석 리포트 보완 소스)
#     네이버 리서치 목록이 종목에 따라 수 주씩 비는 사례(예: 한화시스템 —
#     네이버 최신 5월 vs FnGuide 7월)가 있어 요약리포트로 보완한다.
#     페이지(Report/ReportSummary)는 스켈레톤이고 데이터는 JSON 엔드포인트
#     /Report/getRptSmrSummary 가 준다. (2026-07-08 구조 실측 기준)
#     행 필드: DT, RPT_TITLE, COMMENT(불릿 요약), BRK_NM_KOR(증권사),
#     RECOMM_NM(투자의견), TARGET_PRC, CMP_CD — 목표주가·의견이 전 행에 채워짐.
# ----------------------------------------------------------------------------
def fnguide_research(code, days=60, limit=8):
    """FnGuide 요약리포트를 naver_research 와 같은 스키마로 파싱한다.

    Returns list of {title, broker, date, url, pdfUrl, targetPrice, opinion,
    summary, source} (최신순, days 일 이내). 원문 PDF 는 제공되지 않아 url 은
    종목 스코프의 CompanyInfo/Consensus 페이지(리포트 요약 섹션 포함)로 연결한다
    — Report/ReportSummary 는 URL 의 cmp_cd 를 읽지 않아 전체 목록으로 열리므로
    쓰지 않는다(2026-07-08 검색 스크립트 실측). 실패 시 [] (배치 중단 방지).
    """
    kst = datetime.timezone(datetime.timedelta(hours=9))
    today = datetime.datetime.now(kst).date()
    try:
        r = requests.get(
            "https://wcomp.fnguide.com/Report/getRptSmrSummary",
            params={"search_typ": "cmp",     # 종목코드/명 검색
                    "sdt": (today - datetime.timedelta(days=days)).strftime("%Y%m%d"),
                    "edt": today.strftime("%Y%m%d"),
                    "search": code, "order_col": 0, "order_typ": "D"},
            headers={**_FNGUIDE_HEADERS,
                     "Referer": "https://wcomp.fnguide.com/Report/ReportSummary"},
            timeout=15,
        )
        r.raise_for_status()
        rows = ((r.json() or {}).get("dataset") or {}).get("data") or []
    except Exception as e:
        _warn(f"fnguide_research({code}) failed: {e}")
        return []

    page_url = f"https://wcomp.fnguide.com/CompanyInfo/Consensus?cmp_cd={code}"
    out = []
    for row in rows:
        if str(row.get("CMP_CD") or "").strip() != str(code):
            continue                      # 코드/명 혼합 검색 — 동명 종목 방어
        try:
            d = datetime.datetime.strptime((row.get("DT") or "").strip(),
                                           "%Y/%m/%d").date()
        except ValueError:
            continue
        # COMMENT 는 "▶ 불릿" 줄들 — 접두 기호를 떼고 한 줄 요약으로 합친다.
        lines = [ln.strip().lstrip("▶■·•-").strip()
                 for ln in (row.get("COMMENT") or "").splitlines()]
        tp = _fnguide_num(row.get("TARGET_PRC"))
        out.append({
            "title": (row.get("RPT_TITLE") or "").strip(),
            "broker": (row.get("BRK_NM_KOR") or "").strip(),
            "date": d.strftime("%Y-%m-%d"),
            "url": page_url,
            "pdfUrl": "",
            "targetPrice": int(tp) if tp else None,
            "opinion": (row.get("RECOMM_NM") or "").strip(),
            "summary": " / ".join(ln for ln in lines if ln)[:500],
            "source": "fnguide",   # 프론트 출처 라벨용 (네이버 항목은 필드 없음)
        })
        if len(out) >= limit:
            break
    return out


def combined_research(code, days=60, limit=8):
    """증권사 종목분석 리포트 통합 수집 — 네이버 리서치 + FnGuide 요약리포트.

    같은 리포트가 양쪽에 있으면 (날짜, 증권사) 키로 중복 제거하고, 원문/PDF
    링크가 있는 네이버 항목을 우선하되 상세 미조회로 비어 있는 목표주가·의견·
    요약은 FnGuide 값으로 채운다. 최신순 정렬 후 limit 건 반환.
    """
    # 네이버·FnGuide 는 독립 소스 — 병렬 수집(2026-07-22 온디맨드 속도 개선)
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_n = ex.submit(naver_research, code, days=days, limit=limit)
        f_f = ex.submit(fnguide_research, code, days=days, limit=limit)
        try:
            naver = f_n.result()
        except Exception as e:
            _warn(f"combined_research naver({code}) failed: {e}")
            naver = []
        try:
            fng = f_f.result()
        except Exception as e:
            _warn(f"combined_research fnguide({code}) failed: {e}")
            fng = []

    def _key(r):
        return (r.get("date", ""), (r.get("broker") or "").replace(" ", ""))

    by_key = {}
    for n in naver:
        by_key.setdefault(_key(n), n)
    for f in fng:
        n = by_key.get(_key(f))
        if n:                              # 동일 리포트 — 네이버 빈 칸 보강
            if not n.get("targetPrice"):
                n["targetPrice"] = f.get("targetPrice")
            if not n.get("opinion"):
                n["opinion"] = f.get("opinion")
            if not n.get("summary"):
                n["summary"] = f.get("summary")
        else:
            by_key[_key(f)] = f
    merged = sorted(by_key.values(), key=lambda r: r.get("date", ""), reverse=True)
    return merged[:limit]


# ----------------------------------------------------------------------------
# 2i) 네이버 투자자별 매매동향 (Q점수 Flow 팩터 — 외국인·기관 순매수)
#     m.stock.naver.com 모바일 JSON API. Actions 해외 IP 접근성은
#     .github/naver_flow_probe_result.json 으로 검증됨(2026-07-08, KRX 와 달리
#     차단 없음). (구 폴백 후보 finance.naver.com/item/frgn.naver 는 2026-09 신규 웹
#     개편으로 데이터 표가 사라져 폐기 — 2026-09-21 프로브 실측)
# ----------------------------------------------------------------------------
def naver_investor_trend(code, days=20):
    """일별 투자자 순매수(주) 목록 — 최신순 [{date, foreigner, organ}].

    foreignerPureBuyQuant 등은 "+7,870,568" 형태 문자열이라 int 로 정규화.
    실패 시 [] (배치 중단 방지)."""
    try:
        r = requests.get(
            f"https://m.stock.naver.com/api/stock/{code}/trend",
            params={"pageSize": max(days, 5), "page": 1},
            headers=UA, timeout=15)
        r.raise_for_status()
        rows = r.json() or []
        if not isinstance(rows, list):
            rows = []
    except Exception as e:
        _warn(f"naver_investor_trend({code}) failed: {e}")
        return []

    def _qty(s):
        try:
            return int(str(s or "0").replace(",", "").replace("+", ""))
        except ValueError:
            return 0

    return [{"date": (row.get("bizdate") or "").strip(),
             "foreigner": _qty(row.get("foreignerPureBuyQuant")),
             "organ": _qty(row.get("organPureBuyQuant"))}
            for row in rows[:days]]


# ----------------------------------------------------------------------------
# 2j) 네이버 테마 랭킹 / 테마 구성종목 / 급등급락 랭킹 (장중 시황판 입력)
#     generate_intraday_briefing 이 사용. 2026-09-21 모바일 JSON 으로 전환 —
#     구형 theme.naver·sise_group_detail.naver 가 신규 웹(stock.naver.com/market/
#     stock/kr/theme/…) SPA 로 리다이렉트되어 HTML 파서가 0행(9/14·9/21 theme_map.yml
#     연속 실패). m.stock.naver.com/api/stocks/theme 는 Actions IP 에서 정상(프로브 실측).
# ----------------------------------------------------------------------------
_THEME_API = "https://m.stock.naver.com/api/stocks/theme"
_THEME_PAGE = 100          # 목록·상세 1콜 행 수(전체 테마 ~264 → 3콜)


def _fpct(s):
    """'20.09' / '-1.34' / '+3.2' → float, 실패 None."""
    try:
        return float(str(s).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def naver_theme_ranking(max_pages=8):
    """테마 랭킹 전체 → [{no, name, changePct, leaders}] (등락률 내림차순, API 정렬).

    모바일 JSON groups[{no, name, changeRate, totalCount, rise/fall/steadyCount}].
    구형 HTML 이 주던 주도주 2종목(leaders)은 목록 API 에 없어 빈 리스트 — 매칭은
    정적 테마맵(theme_map.json) 구성종목이 1순위라 영향은 보조 자격(lead_ov)뿐.
    no 는 종전과 같이 문자열(테마맵 키와 일치). 실패한 페이지에서 중단(부분 반환)."""
    themes, seen = [], set()
    for page in range(1, max_pages + 1):
        try:
            r = requests.get(_THEME_API, params={"page": page, "pageSize": _THEME_PAGE},
                             headers=UA, timeout=15)
            r.raise_for_status()
            d = r.json() or {}
        except Exception as e:
            _warn(f"naver_theme_ranking p{page} failed: {e}")
            break
        new = 0
        for g in d.get("groups") or []:
            no = str(g.get("no") or "")
            pct = _fpct(g.get("changeRate"))
            if not no or no in seen or pct is None:
                continue
            seen.add(no)
            themes.append({"no": no, "name": str(g.get("name") or "").strip(),
                           "changePct": pct, "leaders": []})
            new += 1
        if not new or len(themes) >= int(d.get("totalCount") or 0):
            break
    return themes


def naver_theme_stocks(theme_no, limit=10):
    """테마 상세 구성종목 → [{code, name, changePct, reason}] (API 응답 순서 그대로).

    모바일 JSON stocks[{itemCode, stockName, fluctuationsRatio, …}] +
    themeItemInfoMap{code: '테마 편입 사유'}. limit 이 한 페이지를 넘으면 이어 받는다."""
    out, reasons = [], {}
    page = 1
    while len(out) < limit:
        try:
            r = requests.get(f"{_THEME_API}/{theme_no}",
                             params={"page": page, "pageSize": min(_THEME_PAGE, limit)},
                             headers=UA, timeout=15)
            r.raise_for_status()
            d = r.json() or {}
        except Exception as e:
            _warn(f"naver_theme_stocks({theme_no}) p{page} failed: {e}")
            break
        reasons.update(d.get("themeItemInfoMap") or {})
        rows = d.get("stocks") or []
        for x in rows:
            code = str(x.get("itemCode") or "")
            if not re.fullmatch(r"\d{6}", code):
                continue
            out.append({"code": code, "name": str(x.get("stockName") or "").strip(),
                        "changePct": _fpct(x.get("fluctuationsRatio")), "reason": ""})
            if len(out) >= limit:
                break
        if not rows or len(out) >= int(d.get("totalCount") or 0):
            break
        page += 1
    for x in out:
        x["reason"] = str(reasons.get(x["code"]) or "").strip()
    return out


def naver_stock_ranking(direction="up", market="KOSPI", limit=20):
    """급등/급락 개별종목 랭킹 (m.stock.naver.com 모바일 JSON).
    direction: 'up'|'down'. 반환: [{code, name, changePct, price}]."""
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stocks/{direction}/{market}",
                         params={"page": 1, "pageSize": limit}, headers=UA, timeout=15)
        r.raise_for_status()
        d = r.json()
        rows = (d.get("stocks") if isinstance(d, dict) else d) or []
    except Exception as e:
        _warn(f"naver_stock_ranking({direction},{market}) failed: {e}")
        return []

    def _fnum(s):
        try:
            return float(str(s or "").replace(",", "").replace("+", ""))
        except ValueError:
            return None

    out = []
    for it in rows[:limit]:
        code = (it.get("itemCode") or "").strip()
        if not code:
            continue
        out.append({"code": code, "name": (it.get("stockName") or "").strip(),
                    "changePct": _fnum(it.get("fluctuationsRatio")),
                    "price": _fnum(it.get("closePrice"))})
    return out


def naver_market_indicators():
    """네이버 시장지표(finance.naver.com/marketindex) — 환율·국제환율·유가·금·국내금리.

    반환: {"exchange": [...], "world": [...], "commodities": [...], "rates": [...]}
    - exchange: 환전 고시 환율(미국 USD·유럽연합 EUR·일본 JPY(100엔)·중국 CNY)
    - world:    국제환율(달러인덱스·엔/달러·달러/유로 등) — 원화 무관 글로벌 통화 지표
    각 항목 {name, value, change, direction('up'|'down'|'same')}. 실패 시 {}."""
    try:
        r = requests.get("https://finance.naver.com/marketindex/",
                         headers=UA, timeout=15)
        r.raise_for_status()
        html = r.content.decode("euc-kr", errors="replace")
    except Exception as e:
        _warn(f"naver_market_indicators failed: {e}")
        return {}

    def _fnum(s):
        try:
            return float(str(s).replace(",", "").strip())
        except ValueError:
            return None

    # 환율/유가·금 리스트(li 구조): blind 이름 + head_info point_up|dn + value/change
    def _parse_list(section_id):
        i = html.find(f'id="{section_id}"')
        if i < 0:
            return []
        chunk = html[i:html.find("</ul>", i)]
        out = []
        for m in re.finditer(
                r'<span class="blind">([^<]+)</span></h3>.*?'
                r'head_info(?:\s+point_(up|dn))?[^>]*>.*?'
                r'<span class="value">([^<]+)</span>.*?'
                r'<span class="change">\s*([^<]+?)\s*</span>', chunk, re.S):
            name, direction, value, change = m.groups()
            out.append({"name": name.strip(),
                        "value": _fnum(value),
                        "change": _fnum(change),
                        "direction": {"up": "up", "dn": "down"}.get(direction, "same")})
        return out

    # 국내시장금리 테이블: tr class=up|down|same, 이름 span, 금리 td, 등락 td
    rates = []
    j = html.find("국내시장금리")
    if j >= 0:
        chunk = html[j:html.find("</table>", j)]
        for m in re.finditer(
                r'<tr class="(up|down|same)">.*?<span>([^<]+)</span></a></th>\s*'
                r'<td>([\d.,\-]+)</td>\s*<td>.*?([\d.,]+)\s*</td>', chunk, re.S):
            direction, name, value, change = m.groups()
            rates.append({"name": name.strip(), "value": _fnum(value),
                          "change": _fnum(change), "direction": direction})

    out = {"exchange": _parse_list("exchangeList"),
           "world": _parse_list("worldExchangeList"),   # 국제환율: 달러인덱스·엔/달러 등
           "commodities": _parse_list("oilGoldList"),
           "rates": rates}
    return out if any(out.values()) else {}


# 투자자 코드(investorGubun) → 금액(원). 신규 웹 번들 코드표(2026-09-21 프로브):
# 1000 금융투자 · 2000 보험 · 3000 투신 · 3100 사모 · 4000 은행 · 5000 기타금융 ·
# 6000 연기금등 · 7000 국가·지자체 · 7100 기타법인 · 8000 개인 · 9000 외국인 ·
# 9001 기타외국인. 응답엔 기관계(9999)가 없어 1000~7000 합으로 만든다(모바일 일별
# 총계 institutionalValue 와 대조 일치), 외국인 = 9000+9001, 투신(사모) = 3000+3100.
_INV_TIME_API = "https://stock.naver.com/api/domestic/market/trend/time"
_INST_CODES = ("1000", "2000", "3000", "3100", "4000", "5000", "6000", "7000")
_INV_PAGE = 200            # 상한(300 은 400 응답) — 하루 ~380분이라 2콜이면 전량


def _inv_time_row(item):
    """content[] 1행 → 종전 스키마 행(억원 반올림). 핵심 3주체 결손이면 None."""
    amt = {}
    for a in item.get("netAmounts") or []:
        try:
            amt[str(a.get("investorGubun"))] = float(a.get("diffValue"))
        except (TypeError, ValueError):
            continue
    t = str(item.get("time") or "")
    if len(t) < 4 or "8000" not in amt or "9000" not in amt:
        return None

    def eok(*codes):
        vals = [amt[c] for c in codes if c in amt]
        return round(sum(vals) / 1e8) if vals else None
    return {"time": f"{t[:2]}:{t[2:4]}",
            "individual": eok("8000"), "foreign": eok("9000", "9001"),
            "institution": eok(*_INST_CODES),
            # 기관 세부(대시보드 기관 알약 토글)·기타법인(기관 옆 알약) — 종전 키 유지
            "finInv": eok("1000"), "insur": eok("2000"), "trust": eok("3000", "3100"),
            "pension": eok("6000"), "etc": eok("7100")}


def naver_investor_timeline(market="KOSPI", pages=6):
    """투자자별 매매 동향 시간별(1분) '당일 누적' 순매수(억원) — 신규 웹 JSON API
    stock.naver.com/api/domestic/market/trend/time (화면: /market/stock/kr/trend/trader).

    2026-09-21 전환 — 구형 iframe(finance.naver.com/sise/investorDealTrendTime.naver)이
    9/18 부터 410 Gone(폐지)이라 장중 시황 수급 차트·기관 세부·기타법인 알약이 빠졌다.
    파라미터: tradeType=KRX · marketType=KOSPI|KOSDAQ · bizdate · startIdx(페이지 번호) ·
    pageSize(≤200). 응답은 최신순 content[{bizdate, time 'HHMMSS', netAmounts[]}].

    반환(종전과 동일 스키마): [{time 'HH:MM', individual, foreign, institution,
    finInv, insur, trust, pension, etc}] 시간 오름차순. pages = 최대 페이지 수(호출부가
    증분 회차엔 6, 첫 회차엔 40 을 준다 — 200행 페이지라 2콜 안에 끝난다). 실패 시
    수집분까지만 반환(graceful)."""
    mkt = "KOSDAQ" if str(market).upper() == "KOSDAQ" else "KOSPI"
    bizdate = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y%m%d")
    out = {}
    for page in range(max(1, pages)):
        try:
            r = requests.get(_INV_TIME_API,
                             params={"tradeType": "KRX", "marketType": mkt,
                                     "bizdate": bizdate, "startIdx": page,
                                     "pageSize": _INV_PAGE},
                             headers=dict(UA, Referer="https://stock.naver.com/market/"
                                                      "stock/kr/trend/trader"),
                             timeout=15)
            r.raise_for_status()
            d = r.json() or {}
        except Exception as e:
            _warn(f"naver_investor_timeline({market}) p{page} failed: {e}")
            break
        content = d.get("content") or []
        for item in content:
            if str(item.get("bizdate") or bizdate) != bizdate:
                continue                    # 장 시작 전엔 전 거래일 행이 올 수 있음 — 제외
            row = _inv_time_row(item)
            if row and row["time"] not in out:
                out[row["time"]] = row
        total = int(d.get("totalElements") or 0)
        if not content or (page + 1) * _INV_PAGE >= total:
            break
    return sorted(out.values(), key=lambda x: x["time"])


def naver_stock_flow(code):
    """종목별 최신 집계일 외국인·기관 순매수 수량(주) — m.stock.naver.com trend API
    (Actions IP 접근성은 naver_flow_probe 로 검증 완료). 장중엔 당일 잠정치가 아직
    없을 수 있어 date(집계일)를 함께 반환한다. 실패 시 {} (graceful)."""
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/trend",
                         params={"pageSize": 1, "page": 1}, headers=UA, timeout=10)
        r.raise_for_status()
        rows = r.json()
        row = rows[0] if isinstance(rows, list) and rows else {}
    except Exception as e:
        _warn(f"naver_stock_flow({code}) failed: {e}")
        return {}

    def _i(v):
        try:
            return int(str(v).replace(",", ""))
        except (ValueError, TypeError):
            return None
    frn = _i(row.get("foreignerPureBuyQuant"))
    org = None
    for k in ("organPureBuyQuant", "organizationPureBuyQuant"):
        if k in row:
            org = _i(row.get(k))
            break
    if frn is None and org is None:
        return {}
    out = {"date": str(row.get("bizdate") or "")}
    if frn is not None:
        out["foreign"] = frn
    if org is not None:
        out["institution"] = org
    return out


def naver_investor_trend_full(code, rows=20):
    """일자별 외국인/기관/개인 순매매량(주)·외국인 보유율 — m.stock.naver.com trend API.
    KIS 일별 대금(FHPTJ04160001)이 장중 시간제한(00:00~15:40)으로 비는 구간을 메운다
    (전일까지 확정, 당일치는 장 마감 후 반영). 최신순 [{date, close, rate, frgn, orgn,
    prsn, holdRatio}]. 수량 필드는 "+1,799,843" 형식(부호 포함). 실패 시 [] (graceful).

    주의: 동명의 naver_investor_trend(위, Q점수 Flow 팩터용 {foreigner, organ})와
    별개 함수 — 2026-07-22 이름 충돌로 후자 정의가 덮여 sector.py 의 Flow 팩터가
    TypeError(days= 키워드)로 조용히 미산출되던 사고를 _full 접미사로 분리 복구."""
    try:
        r = requests.get(f"https://m.stock.naver.com/api/stock/{code}/trend",
                         params={"pageSize": rows, "page": 1}, headers=UA, timeout=10)
        r.raise_for_status()
        items = r.json()
        if not isinstance(items, list):
            return []
    except Exception as e:
        _warn(f"naver_investor_trend({code}) failed: {e}")
        return []

    def _i(v):
        try:
            return int(str(v).replace(",", ""))
        except (ValueError, TypeError):
            return None

    out = []
    for it in items[:rows]:
        row = {"date": str(it.get("bizdate") or ""),
               "frgn": _i(it.get("foreignerPureBuyQuant")),
               "orgn": _i(it.get("organPureBuyQuant")),
               "prsn": _i(it.get("individualPureBuyQuant"))}
        try:
            row["holdRatio"] = float(str(it.get("foreignerHoldRatio") or "").rstrip("%"))
        except ValueError:
            row["holdRatio"] = None
        close = _i(it.get("closePrice"))
        chg = _i(it.get("compareToPreviousClosePrice")) or 0   # 부호 포함(실측)
        if close:
            row["close"] = close
            prev = close - chg
            row["rate"] = round(chg / prev * 100, 2) if prev else 0
        if row["date"] and (row["frgn"] is not None or row["orgn"] is not None):
            out.append(row)
    return out


def naver_index_investors():
    """코스피/코스닥 당일 투자자별 순매수 금액(억원) — 네이버 증권홈 '오늘의 증시'
    와 동일 수치. 반환: {kospi: {individual, foreign, institution}, kosdaq: {...}}
    (양수=순매수, 음수=순매도). 실패한 시장은 생략."""
    def _fnum(s):
        try:
            return float(str(s or "").replace(",", "").replace("+", ""))
        except ValueError:
            return None
    out = {}
    for key, sym in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        try:
            r = requests.get(f"https://m.stock.naver.com/api/index/{sym}/trend",
                             headers=UA, timeout=10)
            r.raise_for_status()
            d = r.json() or {}
            row = d[0] if isinstance(d, list) and d else d
            vals = {"individual": _fnum(row.get("personalValue")),
                    "foreign": _fnum(row.get("foreignValue")),
                    "institution": _fnum(row.get("institutionalValue"))}
            if any(v is not None for v in vals.values()):
                out[key] = vals
        except Exception as e:
            _warn(f"naver_index_investors({sym}) failed: {e}")
    return out


# 장중 촉매성 공시 제목 키워드 (전체 공시에서 시장영향 큰 유형만 추린다)
DART_CATALYST_KEYWORDS = (
    "공급계약", "단일판매", "유상증자", "무상증자", "합병", "분할", "소송",
    "특허", "임상", "품목허가", "자기주식", "전환사채", "신주인수권",
    "최대주주", "경영권", "영업정지", "파산", "회생", "투자판단", "조회공시",
    "생산재개", "생산중단", "화재", "수주",
)


def dart_today_disclosures(limit=40, keywords=DART_CATALYST_KEYWORDS):
    """당일 전체 상장사 공시(list.json, corp 미지정) 최신순 → 촉매성 필터.

    반환: [{corp, stockCode, market, title, url}] (최신순 limit 건).
    DART list 는 접수 '날짜'만 주므로 시각 필드는 없다 — 순서(rcept_no desc)로
    최신성을 보장. keywords=None 이면 필터 없이 전부."""
    key = _dart_key()
    if not key:
        return []
    today = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y%m%d")
    out = []
    try:
        for page in (1, 2):
            r = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": key, "bgn_de": today, "end_de": today,
                        "page_no": page, "page_count": 100,
                        "sort": "date", "sort_mth": "desc"},
                timeout=20,
            )
            data = r.json()
            if data.get("status") != "000":
                break
            for it in data.get("list") or []:
                cls = it.get("corp_cls")
                if cls not in ("Y", "K"):          # 상장사(유가/코스닥)만
                    continue
                title = (it.get("report_nm") or "").strip()
                if keywords and not any(k in title for k in keywords):
                    continue
                out.append({
                    "corp": (it.get("corp_name") or "").strip(),
                    "stockCode": (it.get("stock_code") or "").strip(),
                    "market": "KOSPI" if cls == "Y" else "KOSDAQ",
                    "title": title,
                    "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it.get('rcept_no')}",
                })
                if len(out) >= limit:
                    return out
            if int(data.get("total_page") or 1) <= page:
                break
    except Exception as e:
        _warn(f"dart_today_disclosures failed: {e}")
    return out


# ----------------------------------------------------------------------------
# 3) Tavily Search API (overseas news, reddit)
# ----------------------------------------------------------------------------
def tavily_search(query, max_results=5, include_domains=None):
    key = os.environ.get("TAVILY_API_KEY")
    if not key:
        _warn("TAVILY_API_KEY missing")
        return []
    try:
        payload = {"api_key": key, "query": query, "max_results": max_results,
                   "search_depth": "basic"}
        if include_domains:
            payload["include_domains"] = include_domains
        # 25→12s (2026-07-22): 병렬화 후 wall time 은 최장 leg 가 결정 — 폴백 포함
        # 웹검색 leg 상한을 50s→24s 로 억제 (Tavily 정상 응답은 수 초 내)
        r = requests.post("https://api.tavily.com/search", json=payload, timeout=12)
        r.raise_for_status()
        return [{
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "content": it.get("content", ""),
        } for it in r.json().get("results", [])]
    except Exception as e:
        _warn(f"tavily_search({query}) failed: {e}")
        return []


def brave_search(query, max_results=5, include_domains=None):
    """Brave Web Search (Tavily 한도초과 432 시 폴백). 반환 형태는 tavily_search 와 동일.

    무료 티어: 2,000 쿼리/월, 1 req/s. include_domains 는 쿼리에 site: 로 반영한다.
    """
    key = os.environ.get("BRAVE_API_KEY")
    if not key:
        _warn("BRAVE_API_KEY missing")
        return []
    q = f"{query} site:{include_domains[0]}" if include_domains else query
    try:
        r = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": q, "count": min(max_results, 20)},
            headers={"Accept": "application/json", "X-Subscription-Token": key},
            timeout=12,   # 25→12s (2026-07-22) — tavily 와 동일 사유
        )
        r.raise_for_status()
        results = ((r.json().get("web") or {}).get("results")) or []
        return [{
            "title": it.get("title", ""),
            "url": it.get("url", ""),
            "content": it.get("description", ""),
        } for it in results[:max_results]]
    except Exception as e:
        _warn(f"brave_search({query}) failed: {e}")
        return []


def web_search(query, max_results=5, include_domains=None):
    """웹 검색 통합 진입점: Tavily 우선, 빈 결과/한도초과(432)·오류 시 Brave 폴백."""
    res = tavily_search(query, max_results, include_domains)
    if res:
        return res
    return brave_search(query, max_results, include_domains)


# ----------------------------------------------------------------------------
# 3b) Reddit public JSON search (keyless). Tavily is the PRIMARY source in the
#     caller — Reddit 403s from datacenter/CI IPs, so this is a residential-only
#     best-effort fallback.
# ----------------------------------------------------------------------------
REDDIT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_REDDIT_BLOCKED_WARNED = False


def reddit_search(query, max_results=5, period="year"):
    """Search Reddit posts via the public search.json endpoint. No key required.

    Returns list of {title, url, subreddit, score, num_comments, content}.
    Datacenter/CI IPs are blocked (403) by Reddit; on any failure this returns []
    so the caller falls back to Tavily. Block warnings are logged once per process.
    """
    try:
        r = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "sort": "relevance", "t": period,
                    "limit": max_results, "type": "link"},
            headers={"User-Agent": REDDIT_UA},
            timeout=20,
        )
        r.raise_for_status()
        children = r.json().get("data", {}).get("children", [])
        out = []
        for c in children:
            d = c.get("data", {})
            out.append({
                "title": d.get("title", ""),
                "url": "https://www.reddit.com" + d.get("permalink", ""),
                "subreddit": d.get("subreddit_name_prefixed") or ("r/" + d.get("subreddit", "")),
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "content": (d.get("selftext", "") or "")[:500],
            })
        return out
    except Exception as e:
        global _REDDIT_BLOCKED_WARNED
        if not _REDDIT_BLOCKED_WARNED:
            _warn(f"reddit_search blocked/failed ({e}); relying on Tavily for Reddit "
                  f"(further reddit warnings suppressed)")
            _REDDIT_BLOCKED_WARNED = True
        return []


# ----------------------------------------------------------------------------
# 4) DART OpenAPI (corp_code, financials, disclosures, major holders)
# ----------------------------------------------------------------------------
_CORP_MAP = None  # stock_code(6) -> corp_code(8), cached per process
_CORP_LOCK = threading.Lock()  # guard the one-time (large) corpCode.xml download

# Bundled static stock_code -> corp_code map (built by tools/build_dart_corp_map.py).
# Serverless (Vercel /api/research) loads this to skip the ~tens-of-MB corpCode.xml
# download on every cold start. Absent → fall back to the live DART download below.
_STATIC_CORP_MAP_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "public", "assets", "dart_corp_map.json")


def _dart_key():
    return os.environ.get("DART_API_KEY")


def _load_static_corp_map():
    """Load the bundled stock_code->corp_code map, or None if missing/empty."""
    try:
        with open(_STATIC_CORP_MAP_PATH, encoding="utf-8") as f:
            m = json.load(f)
        if isinstance(m, dict) and m:
            return {str(k).zfill(6): str(v) for k, v in m.items()}
    except FileNotFoundError:
        return None
    except Exception as e:
        _warn(f"static corp map load failed: {e}")
    return None


def _load_corp_map():
    global _CORP_MAP
    if _CORP_MAP is not None:
        return _CORP_MAP
    with _CORP_LOCK:
        if _CORP_MAP is not None:          # another thread populated it while we waited
            return _CORP_MAP
        # Fast path: bundled static map (no network, no DART key needed) — serverless.
        static = _load_static_corp_map()
        if static is not None:
            _warn(f"corp map: static file ({len(static)} listed codes)")
            _CORP_MAP = static
            return _CORP_MAP
        # Fallback: download the full corpCode.xml from DART (needs DART_API_KEY).
        m = {}
        key = _dart_key()
        if not key:
            _warn("DART_API_KEY missing")
            _CORP_MAP = m
            return _CORP_MAP
        try:
            r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                             params={"crtfc_key": key}, timeout=30)
            r.raise_for_status()
            zf = zipfile.ZipFile(io.BytesIO(r.content))
            xml = zf.read(zf.namelist()[0])
            root = ET.fromstring(xml)
            for el in root.iter("list"):
                stock = (el.findtext("stock_code") or "").strip()
                corp = (el.findtext("corp_code") or "").strip()
                if stock and corp:
                    m[stock] = corp
        except Exception as e:
            _warn(f"DART corpCode load failed: {e}")
        _CORP_MAP = m
        return _CORP_MAP


def dart_corp_code(stock_code):
    return _load_corp_map().get(stock_code)


def dart_financials(corp_code, year=None):
    """Annual single-company key accounts for the most recent available year."""
    key = _dart_key()
    if not (key and corp_code):
        return {}
    year = year or (datetime.date.today().year - 1)
    for y in (year, year - 1):  # fall back one year if not yet filed
        try:
            r = requests.get(
                "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json",
                params={"crtfc_key": key, "corp_code": corp_code,
                        "bsns_year": str(y), "reprt_code": "11011", "fs_div": "CFS"},
                timeout=20,
            )
            data = r.json()
            if data.get("status") == "000" and data.get("list"):
                wanted = {"매출액", "영업이익", "당기순이익", "자산총계", "부채총계", "자본총계"}
                rows = {}
                for it in data["list"]:
                    nm = it.get("account_nm")
                    if nm in wanted and nm not in rows:
                        rows[nm] = {
                            "thstrm": it.get("thstrm_amount"),
                            "frmtrm": it.get("frmtrm_amount"),
                        }
                if rows:
                    return {"year": y, "accounts": rows}
        except Exception as e:
            _warn(f"dart_financials({corp_code}, {y}) failed: {e}")
    return {}


def dart_disclosures(corp_code, days=120, page_count=15):
    key = _dart_key()
    if not (key and corp_code):
        return []
    end = datetime.date.today()
    bgn = end - datetime.timedelta(days=days)
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/list.json",
            params={"crtfc_key": key, "corp_code": corp_code,
                    "bgn_de": bgn.strftime("%Y%m%d"), "end_de": end.strftime("%Y%m%d"),
                    "page_count": page_count},
            timeout=20,
        )
        data = r.json()
        if data.get("status") != "000":
            return []
        out = []
        for it in data.get("list", []):       # DART returns newest first
            rno = it.get("rcept_no", "")
            out.append({
                "date": _fmt_date(it.get("rcept_dt", "")),
                "title": it.get("report_nm", ""),
                "filer": it.get("flr_nm", ""),
                "rcept_no": rno,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}" if rno else "",
            })
        return out
    except Exception as e:
        _warn(f"dart_disclosures({corp_code}) failed: {e}")
        return []


def dart_market_disclosures(bgn_de, end_de, corp_cls, max_pages=20):
    """corp_code 없이 시장 전체 공시를 날짜·시장구분으로 스캔한다 (per-stock fan-out 회피).

    corp_cls: 'Y'(유가증권/KOSPI) | 'K'(코스닥) | 'N'(코넥스) | 'E'(기타).
    Returns list of {date, stock_code, corp_name, title, rcept_no, url}, 최신순.
    """
    key = _dart_key()
    if not key:
        _warn("DART_API_KEY missing")
        return []
    out = []
    for page in range(1, max_pages + 1):
        try:
            r = requests.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": key, "bgn_de": bgn_de, "end_de": end_de,
                        "corp_cls": corp_cls, "page_count": 100, "page_no": page},
                timeout=20,
            )
            data = r.json()
        except Exception as e:
            _warn(f"dart_market_disclosures({corp_cls}, p{page}) failed: {e}")
            break
        if data.get("status") != "000":
            break
        for it in data.get("list", []):
            rno = it.get("rcept_no", "")
            out.append({
                "date": _fmt_date(it.get("rcept_dt", "")),
                "stock_code": (it.get("stock_code") or "").strip(),
                "corp_name": it.get("corp_name", ""),
                "title": it.get("report_nm", ""),
                "rcept_no": rno,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rno}" if rno else "",
            })
        if page >= int(data.get("total_page", 1) or 1):
            break
    return out


def dart_major_holders(corp_code):
    """대량보유 상황(5% rule) — recent ownership changes."""
    key = _dart_key()
    if not (key and corp_code):
        return []
    try:
        r = requests.get(
            "https://opendart.fss.or.kr/api/majorstock.json",
            params={"crtfc_key": key, "corp_code": corp_code},
            timeout=20,
        )
        data = r.json()
        if data.get("status") != "000":
            return []
        out = []
        for it in data.get("list", [])[:8]:
            out.append({
                "date": _fmt_date(it.get("rcept_dt", "")),
                "holder": it.get("repror", ""),
                "ratio": it.get("stkrt", ""),
                "reason": it.get("report_resn", ""),
            })
        return out
    except Exception as e:
        _warn(f"dart_major_holders({corp_code}) failed: {e}")
        return []


def _fmt_date(yyyymmdd):
    s = (yyyymmdd or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s
