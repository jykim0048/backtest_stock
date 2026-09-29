"""한국 주식 원자료 수집 함수 — backtest_stock `analysis/sources.py` 에서 이 스킬이 쓰는 부분만 이식.

원본: jykim0048/backtest_stock analysis/sources.py @ 9fe9e71b (2026-09-22).
이식 원칙: 함수 시그니처·반환 형태·실패 시 빈 값 반환(배치 중단 없음)을 그대로 유지한다.
원본이 갱신되면(네이버 API 규약 변경 등) 같은 이름의 함수를 다시 옮겨 오면 된다.

바뀐 점:
  - DART 고유번호 정적 맵 경로가 `<skill>/assets/dart_corp_map.json` 이다.
  - 대시보드 전용 함수(테마·시장지표·업종 리서치·시장 전체 공시 스캔 등)는 제외.

환경변수: DART_API_KEY, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET (선택 TAVILY_API_KEY, BRAVE_API_KEY)
"""
from __future__ import annotations

import datetime
import io
import json
import os
import re
import sys
import threading
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import requests
import yfinance as yf

UA = {"User-Agent": "Mozilla/5.0 (quant-antigravity batch)"}
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"


def _warn(msg):
    print(f"[sources] {msg}", file=sys.stderr, flush=True)


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
# 2b) 네이버 종목토론방 — stock.naver.com 신규 웹 JSON (2026-09-21 전환)
# ----------------------------------------------------------------------------
_BOARD_API = "https://stock.naver.com/api/community/discussion/posts"
_BOARD_PAGE = 20           # 구형 게시판 1페이지 = 20글 — pages 인자와 같은 분량을 1콜로


def naver_board(code, pages=1, limit=15):
    """종목토론방 글 → [{title, url, date, views, agree, disagree, comments}], 공감수
    내림차순(노이즈/낚시 글을 가라앉히기 위함). 실패 시 [] 반환(배치 중단 방지).

    번들 규약: itemCode·discussionType·isHolderOnly·excludesItemNews·isItemNewsOnly·pageSize
    **전부 필요** — 빠지면 전 종목 글이 섞여 온다(프로브 실측). 목록 응답엔 조회수가 없어
    글 ID 묶음 API(posts/reactions?postIds=)로 viewCount·공감/비공감(최신값)을 채운다."""
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
# 2c) 네이버 증권사 리서치 (종목분석) — m.stock.naver.com 모바일 JSON API
#       종목 목록: /api/research/stock/<code>?page&pageSize      (previewContent 포함)
#       종목 상세: /api/research/company/<researchId>             (opinion·goalPrice·content·attachUrl)
# ----------------------------------------------------------------------------
_MSTOCK_RESEARCH = "https://m.stock.naver.com/api/research"


def _research_id(url):
    """리서치 URL → researchId 문자열. 신형(.../company/96231)과 구형(?nid=46132) 모두 수용."""
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
    상위 건에만 채워진다. 실패 시 [] (배치 중단 방지).
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


# ----------------------------------------------------------------------------
# 2e) 네이버 밸류에이션 — 모바일 integration JSON + 신규 웹 detail JSON (2026-09-21/22 전환)
# ----------------------------------------------------------------------------
_INTEGRATION_API = "https://m.stock.naver.com/api/stock/{code}/integration"
_DETAIL_API = "https://stock.naver.com/api/domestic/detail/{code}/detail"


def _same_industry(code):
    """(동일업종 PER, 동일업종 등락률%) — 실패 시 (None, None)."""
    try:
        r = requests.get(_DETAIL_API.format(code=code), params={"codeType": "KRX"},
                         headers={**UA, "Referer":
                                  f"https://stock.naver.com/domestic/stock/{code}/price"},
                         timeout=15)
        r.raise_for_status()
        d = r.json() or {}
    except Exception as e:
        _warn(f"naver_valuation({code}) 동일업종 조회 실패: {e}")
        return None, None
    per = _vnum(d.get("sameIndustryPer"))
    return (round(per, 2) if per is not None else None), _vnum(d.get("sameIndustryChangeRate"))


def _vnum(s):
    """'12.25배' / '22,292원' / '0.61%' / '-3.2배' / 'N/A' → float|None"""
    t = re.sub(r"[^\d.\-]", "", str(s or ""))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def _opinion_label(score):
    """네이버 투자의견 점수(1~5, 증권사 평균) → 라벨(적극매도·매도·중립·매수·적극매수)."""
    if score is None:
        return None
    return ("적극매수" if score >= 4.5 else "매수" if score >= 3.5 else
            "중립" if score >= 2.5 else "매도" if score >= 1.5 else "적극매도")


def naver_valuation(code):
    """네이버 종목 밸류에이션 지표.

    Returns {per, eps, estPer, estEps, pbr, bps, dividendYield, industryPer,
    industryChangePct, opinionScore, opinionLabel, targetPrice, high52w, low52w}
    — 값이 없거나 N/A(적자 등)면 None. 요청 실패·응답 비정상 시 {}.
    per·eps 는 trailing, estPer·estEps 는 증권사 추정 평균(컨센서스). 52주 고저는 수정주가 우선."""
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

    ind_per, ind_chg = _same_industry(code)
    return {
        "per": pick("per"), "eps": pick("eps"),
        "estPer": pick("cnsPer"), "estEps": pick("cnsEps"),
        "pbr": pick("pbr"), "bps": pick("bps"),
        "dividendYield": pick("dividendYieldRatio"),
        "industryPer": ind_per, "industryChangePct": ind_chg,
        "opinionScore": score, "opinionLabel": _opinion_label(score),
        "targetPrice": _vnum(cns.get("priceTargetMean")),
        "high52w": pick("highPriceOf52WeeksAdjusted", "highPriceOf52Weeks"),
        "low52w": pick("lowPriceOf52WeeksAdjusted", "lowPriceOf52Weeks"),
    }


# ----------------------------------------------------------------------------
# 2g) COMP FNGUIDE 밸류에이션 보완 (wcomp.fnguide.com — HTML 내장 JSON 파싱)
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
    """Invest 페이지 상단 지표 칩 — {per, fwdPer, industryPer, pbr}. 실패 시 {}."""
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


# FinanceRatio 행 이름(NM, strip 후) -> 반환 키.
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
    가장 오른쪽(최신) 비결측 값을 취한다. 실패 시 {}."""
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
    """Invest + FinanceRatio 통합. 두 페이지 모두 실패하면 {}."""
    out = {**fnguide_invest(code), **fnguide_ratios(code)}
    return out if any(v is not None for v in out.values()) else {}


# ----------------------------------------------------------------------------
# 2h) COMP FNGUIDE 요약리포트 (증권사 종목분석 리포트 보완 소스)
# ----------------------------------------------------------------------------
def fnguide_research(code, days=60, limit=8):
    """FnGuide 요약리포트를 naver_research 와 같은 스키마로 파싱한다. 실패 시 []."""
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
            "source": "fnguide",
        })
        if len(out) >= limit:
            break
    return out


def combined_research(code, days=60, limit=8):
    """증권사 종목분석 리포트 통합 수집 — 네이버 리서치 + FnGuide 요약리포트.
    (날짜, 증권사) 키로 중복 제거, 네이버 항목 우선하되 빈 칸은 FnGuide 값으로 보강. 최신순."""
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
# 2i) 네이버 일자별 투자자 매매동향 — m.stock.naver.com trend API
# ----------------------------------------------------------------------------
def naver_investor_trend_full(code, rows=20):
    """일자별 외국인/기관/개인 순매매량(주)·외국인 보유율. 최신순
    [{date, close, rate, frgn, orgn, prsn, holdRatio}]. 실패 시 []."""
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


# ----------------------------------------------------------------------------
# 3) 웹 검색 (해외 뉴스) — Tavily 우선, Brave 폴백
# ----------------------------------------------------------------------------
DART_CATALYST_KEYWORDS = (
    "공급계약", "단일판매", "유상증자", "무상증자", "합병", "분할", "소송",
    "특허", "임상", "품목허가", "자기주식", "전환사채", "신주인수권",
    "최대주주", "경영권", "영업정지", "파산", "회생", "투자판단", "조회공시",
    "생산재개", "생산중단", "화재", "수주",
)


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
    """Brave Web Search (Tavily 한도초과 시 폴백). 무료 티어 2,000 쿼리/월, 1 req/s."""
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
            timeout=12,
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
    """웹 검색 통합 진입점: Tavily 우선, 빈 결과/한도초과·오류 시 Brave 폴백."""
    res = tavily_search(query, max_results, include_domains)
    if res:
        return res
    return brave_search(query, max_results, include_domains)


# ----------------------------------------------------------------------------
# 3b) 해외 뉴스 — yfinance(야후 티커 뉴스) 기본 + 웹 검색 보조
# ----------------------------------------------------------------------------
_CORP_SUFFIX_RE = re.compile(
    r"\b(co\.?,?\s*ltd\.?|ltd\.?|inc\.?|corp(oration)?\.?|company|holdings?|group|plc|limited|"
    r"sa|ag|nv|kk|co)\b[\s.,]*$", re.I)


def _yahoo_item_fields(n: dict) -> dict:
    """야후 뉴스 항목을 평탄화. 신형(content 중첩)·구형(평면) 스키마 모두 처리."""
    c = n.get("content") if isinstance(n.get("content"), dict) else n
    title = (c.get("title") or "").strip()
    url = ""
    for k in ("canonicalUrl", "clickThroughUrl"):
        v = c.get(k)
        if isinstance(v, dict) and v.get("url"):
            url = v["url"]
            break
    url = url or c.get("link") or n.get("link") or ""
    prov = c.get("provider")
    provider = prov.get("displayName") if isinstance(prov, dict) else (c.get("publisher") or "")
    date = c.get("pubDate") or c.get("displayTime") or ""
    if not date and n.get("providerPublishTime"):
        try:
            date = datetime.datetime.fromtimestamp(int(n["providerPublishTime"]), datetime.timezone.utc) \
                .strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:  # noqa: BLE001
            date = ""
    content = (c.get("summary") or c.get("description") or "").strip()
    return {"title": title, "url": url, "content": content, "date": date, "provider": provider or ""}


def normalize_yahoo_news(raw: list, keywords=(), max_items: int = 10, context_max: int = 5) -> list:
    """순수 함수(오프라인 테스트 대상).

    - keywords(회사 영문명 토큰)가 제목·요약에 있으면 relevance="company", 아니면 "context"(섹터·시황 기사).
    - company 를 먼저, 각 그룹은 날짜 내림차순. context 는 최대 context_max 건, 전체 max_items 건.
    - url 기준 중복 제거. 제목 없는 항목 제외.
    """
    kws = [k.lower() for k in (keywords or []) if k and len(k) >= 3]
    seen, company, context = set(), [], []
    for n in raw or []:
        if not isinstance(n, dict):
            continue
        it = _yahoo_item_fields(n)
        if not it["title"]:
            continue
        key = it["url"] or it["title"]
        if key in seen:
            continue
        seen.add(key)
        txt = f"{it['title']} {it['content']}".lower()
        it["source"] = "yahoo"
        if kws and any(k in txt for k in kws):
            it["relevance"] = "company"
            company.append(it)
        else:
            it["relevance"] = "context"
            context.append(it)
    company.sort(key=lambda x: x["date"], reverse=True)
    context.sort(key=lambda x: x["date"], reverse=True)
    # 섹터·시황(context) 자리를 최대 3건 예약한 뒤 company 를 채우고, 남는 자리를 context 로 메운다(상한 context_max).
    reserve = min(3, len(context), context_max)
    taken = company[: max(0, max_items - reserve)]
    room = min(context_max, max(0, max_items - len(taken)))
    return taken + context[:room]


def company_keywords_from_name(name: str) -> list:
    """'Samsung Electronics Co., Ltd.' → ['samsung electronics', 'samsung']. 짧은 토큰(<3자)은 제외."""
    if not name:
        return []
    base = _CORP_SUFFIX_RE.sub("", name.strip()).strip(" .,")
    base = _CORP_SUFFIX_RE.sub("", base).strip(" .,")   # 'Co., Ltd.' 같이 두 겹인 경우
    toks = [t for t in re.split(r"[\s,]+", base) if t]
    out = []
    if base:
        out.append(base.lower())
    if toks and len(toks[0]) >= 3 and toks[0].lower() not in out:
        out.append(toks[0].lower())
    return out


_YF_PROFILE: dict = {}
_YF_PROFILE_LOCK = threading.Lock()


def yahoo_profile(ticker: str) -> dict:
    """yfinance info 요약(프로세스 캐시, 호출 1회 ~1초). 실패해도 빈 값으로 돌려준다.

    {"ticker", "shortName", "longName", "sector", "industry", "industryKey", "keywords": [영문 회사명 토큰]}
    keywords 는 해외 뉴스 relevance 판정과 Reddit 회사명 검색에 쓴다.
    """
    with _YF_PROFILE_LOCK:
        if ticker in _YF_PROFILE:
            return dict(_YF_PROFILE[ticker])
    try:
        info = yf.Ticker(ticker).info or {}
    except Exception as e:  # noqa: BLE001
        _warn(f"yahoo info({ticker}) failed: {e}")
        info = {}
    kws = []
    # longName 우선: shortName 은 'SamsungElec' 처럼 축약형이라 Reddit 검색어로 부적합(2026-09-22 실측)
    for k in ("longName", "shortName", "displayName"):
        for w in company_keywords_from_name(info.get(k) or ""):
            if w not in kws:
                kws.append(w)
    prof = {"ticker": ticker, "shortName": info.get("shortName") or "", "longName": info.get("longName") or "",
            "sector": info.get("sector") or "", "industry": info.get("industry") or "",
            "industryKey": info.get("industryKey") or "", "keywords": kws,
            "marketCap": info.get("marketCap"), "sharesOutstanding": info.get("sharesOutstanding")}
    with _YF_PROFILE_LOCK:
        _YF_PROFILE[ticker] = prof
    return dict(prof)


def yahoo_company_keywords(ticker: str) -> list:
    """yfinance info 의 shortName/longName 에서 키워드를 만든다. 실패 시 [] (필터 없이 전부 context 취급)."""
    return yahoo_profile(ticker)["keywords"]


def select_peers(code: str, industry_key: str, peers_cfg: dict, industry_cfg: dict, dynamic_cfg: dict | None = None) -> tuple:
    """peer 4단계 폴백(순수 함수). → (peer_list, source)  source ∈ curated | dynamic | industry-default | none.

    ① peers.json 큐레이션 → ② memory/peers_dynamic.json(Claude 제안 + yfinance 검증, `peers_resolve.py propose`)
    → ③ yfinance industryKey 로 industry_peers.json → ④ 없음. backtest_stock `resolve_peers`(정적 → LLM 제안·검증) 이식.
    industry-default 는 '같은 업종의 미국 대표주'일 뿐 직접 경쟁사가 아닐 수 있어 source 를 함께 돌려준다.
    """
    cur = [p for p in ((peers_cfg or {}).get(code) or []) if isinstance(p, dict) and p.get("ticker")]
    if cur:
        return cur, "curated"
    dyn_entry = (dynamic_cfg or {}).get(code) or {}
    dyn = [p for p in (dyn_entry.get("peers") or []) if isinstance(p, dict) and p.get("ticker")] if isinstance(dyn_entry, dict) else []
    if dyn:
        return [dict(p) for p in dyn], "dynamic"
    ind = [p for p in ((industry_cfg or {}).get(industry_key or "") or []) if isinstance(p, dict) and p.get("ticker")]
    if ind:
        return [dict(p) for p in ind], "industry-default"
    return [], "none"


def yahoo_news(ticker: str, keywords=None, count: int = 20, max_items: int = 10, context_max: int = 5) -> list:
    """야후 파이낸스 티커 뉴스(키·쿼터 없음). 비공식 스크래핑이라 실패 시 [] 를 돌려주고 경고만 남긴다."""
    try:
        raw = yf.Ticker(ticker).get_news(count=count)
    except Exception as e:  # noqa: BLE001
        _warn(f"yahoo_news({ticker}) failed: {e}")
        return []
    if keywords is None:
        keywords = yahoo_company_keywords(ticker)
    return normalize_yahoo_news(raw, keywords, max_items=max_items, context_max=context_max)


def merge_peer_news(lists: list, max_items: int = 12) -> list:
    """peer 별 뉴스 목록을 합친다(순수). url 기준 중복 제거 → 날짜 내림차순 → 상한."""
    seen, out = set(), []
    for items in lists or []:
        for it in items or []:
            if not isinstance(it, dict) or not it.get("title"):
                continue
            key = it.get("url") or it["title"]
            if key in seen:
                continue
            seen.add(key)
            out.append(it)
    out.sort(key=lambda x: x.get("date") or "", reverse=True)
    return out[:max_items]


def peer_news(peers: list, top: int = 4, per_peer: int = 4, max_items: int = 12) -> list:
    """해외 뉴스 레그 = **해외 peer 종목 뉴스**(2026-09-22 결정). 한국 종목 자체의 해외 기사는 수집하지 않는다.

    상위 top 개 peer 의 야후 티커 뉴스를 per_peer 건씩 받아 합친다. 각 항목에 peer(name·ticker)를 붙이고
    relevance 는 그 peer 회사명 매치 기준(company = peer 직접 기사, context = 섹터·시황).
    """
    lists = []
    for p in (peers or [])[:top]:
        tk = (p.get("ticker") or "").strip()
        if not tk:
            continue
        kws = company_keywords_from_name(p.get("name") or "")
        items = yahoo_news(tk, keywords=kws, count=15, max_items=per_peer, context_max=1)
        for it in items:
            it["peer"] = {"name": p.get("name") or tk, "ticker": tk}
        lists.append(items)
    return merge_peer_news(lists, max_items=max_items)


def annotate_flow_mcap(hub: dict, market_cap_krw) -> dict:
    """공매도·대차 행에 시가총액 대비 비중(%)을 붙인다(순수). backtest_stock 주간 브리핑 정의와 같다:
    시총대비(%) = 공매도 대금 ÷ 시가총액, 대차잔고 금액 ÷ 시가총액. 시총이 없으면 그대로 돌려준다."""
    hub = dict(hub or {})
    try:
        mcap_eok = float(market_cap_krw) / 1e8
    except (TypeError, ValueError):
        return hub
    if mcap_eok <= 0:
        return hub
    hub["marketCap_eok"] = round(mcap_eok, 1)
    shorts = []
    for r in hub.get("shorts") or []:
        r = dict(r)
        v = r.get("pbmn")
        r["mcapPct"] = round(float(v) / mcap_eok * 100, 4) if isinstance(v, (int, float)) else None
        shorts.append(r)
    hub["shorts"] = shorts
    tot20 = sum(float(r["pbmn"]) for r in shorts[:20] if isinstance(r.get("pbmn"), (int, float)))
    hub["shorts_20d_mcapPct"] = round(tot20 / mcap_eok * 100, 3)
    loans = []
    for r in hub.get("loans") or []:
        r = dict(r)
        v = r.get("rmndAmt")
        r["rmndMcapPct"] = round(float(v) / mcap_eok * 100, 2) if isinstance(v, (int, float)) else None
        loans.append(r)
    hub["loans"] = loans
    return hub


# ----------------------------------------------------------------------------
# 4) DART OpenAPI (corp_code, financials, disclosures, major holders)
# ----------------------------------------------------------------------------
_CORP_MAP = None  # stock_code(6) -> corp_code(8), cached per process
_CORP_LOCK = threading.Lock()
_STATIC_CORP_MAP_PATH = ASSETS_DIR / "dart_corp_map.json"


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
        if _CORP_MAP is not None:
            return _CORP_MAP
        static = _load_static_corp_map()
        if static is not None:
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
    return _load_corp_map().get(str(stock_code).zfill(6))


def dart_financials(corp_code, year=None):
    """연간 주요 6계정(레거시). 분기·전 계정은 dart_fin.py 를 쓴다. 실패 시 {}."""
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
