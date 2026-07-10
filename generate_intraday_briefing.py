#!/usr/bin/env python3
"""장중 시황 브리핑 생성 — 모닝브리핑의 '장중 실시간' 대칭 버전.

모닝브리핑이 미국장 기반 예습이라면, 이 스크립트는 한국장이 실제로 어떻게
흘러가는지 30분 회차로 복습한다 (intraday_screener.yml 스텝으로 실행):

  1) 한국 급등/급락 테마 (네이버 테마 랭킹 — 각 3개, 동일 비중)
  2) 테마별 주요 밸류체인 (테마 구성종목 등락 상위 + 편입 사유)
  3) 장중 국내 촉매 (DART 당일 촉매성 공시 + '특징주' 뉴스)
  4) LLM 시황 종합 (브리핑 3~4문장 · 테마별 1줄 · 촉매 선별 요약)

산출:
  public/intraday_briefing.json                    (최신 회차 — 대시보드)
  public/reports/intraday_briefing/<date>.json     (회차 누적 rounds[] 아카이브)

LLM 실패 시에도 기계적 수집분(테마·밸류체인·공시·뉴스)만으로 완결된 JSON 을
산출한다(graceful degradation — 모닝브리핑과 동일 원칙).
"""
import os
import sys
import json
import datetime

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from analysis import sources
import llm

KST = datetime.timezone(datetime.timedelta(hours=9))

OUT_PATH = os.environ.get("INTRADAY_BRIEFING_OUT") or os.path.join(
    ROOT, "public", "intraday_briefing.json")
ARCHIVE_DIR = os.path.join(ROOT, "public", "reports", "intraday_briefing")
THEMES_PER_SIDE = int(os.environ.get("BRIEFING_THEMES", "3"))     # 급등/급락 각 N개
STOCKS_PER_THEME = int(os.environ.get("BRIEFING_VC_STOCKS", "3"))  # 테마 구성종목 표기 수(방향 상위)
THEMES_PER_SECTOR = int(os.environ.get("BRIEFING_THEMES_PER_SECTOR", "2"))  # 섹터당 관련 테마 상한
THEME_DETAIL_TOP = int(os.environ.get("BRIEFING_THEME_DETAIL_TOP", "20"))   # 구성종목까지 로드해 매칭할 테마 수(|등락| 상위)
SECTORS_PER_SIDE = int(os.environ.get("BRIEFING_SECTORS", "3"))    # 상승/하락 각 N개 섹터
CATALYSTS_PER_DIR = int(os.environ.get("BRIEFING_CAT_PER_DIR", "10"))  # 촉매 방향별 상한(상방/중립/하방)

# KIS 허브(vi_limit_monitor) /status — KOSPI 산업별 업종 지수 등락률(KRX 분류) 공급원.
# 허브가 장중 REST 로 FHPUP02140000 을 폴링해 Redis→/status 로 노출한다(대시보드 VI 탭과 동일 서비스).
KIS_HUB_URL = os.environ.get(
    "KIS_HUB_URL", "https://tradingstrategies-production-09d4.up.railway.app")


def _warn(msg):
    print(f"[intraday-briefing] {msg}", file=sys.stderr)


def _is_final_round(now):
    """마감 시황 여부 — 명시 플래그(BRIEFING_FINAL=1, closing_briefing.yml) 또는
    장 마감(15:30 KST) 이후 실행이면 True."""
    if os.environ.get("BRIEFING_FINAL") == "1":
        return True
    return (now.hour, now.minute) >= (15, 30)


def _load_prev_rounds(date_str, limit=12):
    """당일 아카이브의 이전 회차들 → 마감 시황용 다이제스트(회차별 시황 불릿 + 촉매 종목).
    아카이브 없으면 [] (첫 거래일/아카이브 실패 시 graceful)."""
    path = os.path.join(ARCHIVE_DIR, f"{date_str}.json")
    try:
        with open(path, encoding="utf-8") as f:
            rounds = (json.load(f) or {}).get("rounds") or []
    except Exception:
        return []
    dir_kr = {"bullish": "상방", "bearish": "하방"}
    out = []
    for r in rounds[-limit:]:
        asof = (r.get("asof") or "").split(" ")
        out.append({
            "time": asof[1] if len(asof) > 1 else "",
            "briefing": r.get("briefing") or [],
            "catalysts": [f"{c.get('stock')}({dir_kr.get(c.get('direction'), '중립')})"
                          for c in (r.get("catalysts") or []) if c.get("stock")],
        })
    return out


# ----------------------------------------------------------------------------
# Phase 1) 수집
# ----------------------------------------------------------------------------
def fetch_indices():
    """코스피/코스닥 지수 — railway_server._naver_indices 와 동일 소스.
    closePrice 는 장전엔 전일 종가, 장중·마감 후엔 당일 (준)실시간/종가."""
    def _num(s):
        try:
            return float(str(s or "").replace(",", "").replace("+", ""))
        except (ValueError, AttributeError):
            return None
    out = {}
    for key, sym in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        try:
            r = requests.get(f"https://m.stock.naver.com/api/index/{sym}/basic",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            d = r.json()
            price, rate = _num(d.get("closePrice")), _num(d.get("fluctuationsRatio"))
            if price is not None:
                out[key] = {"price": price, "rate": rate if rate is not None else 0.0}
        except Exception as e:
            _warn(f"index {sym}: {e}")
    return out


KRX_SECTOR_MAP_PATH = os.path.join(ROOT, "public", "assets", "krx_sector_map.json")
STOCKS_PER_SECTOR = int(os.environ.get("BRIEFING_SEC_STOCKS", "5"))  # 섹터당 관련주


def _norm_sector(name):
    """업종명 정규화 — build_krx_sector_map.norm_sector 와 동일 규칙(공백·중점·괄호 제거)."""
    import re
    return re.sub(r"[\s·・()]", "", str(name or "")).strip()


def _load_krx_sector_map():
    """KRX 업종분류 매핑(정규화 업종명 → 시총 상위 종목) 로드. 없으면 {}."""
    try:
        with open(KRX_SECTOR_MAP_PATH, encoding="utf-8") as f:
            return (json.load(f) or {}).get("sectors") or {}
    except Exception as e:
        _warn(f"KRX 섹터맵 로드 실패: {e}")
        return {}


def _sector_entry(sector_name, sec_map):
    """KIS 업종명 → KRX 매핑 엔트리(시총 상위 12종목 보유). 정규화 동일키 우선, 없으면
    접두 매칭('의료·정밀기' ⊂ '의료·정밀기기' 같은 표기차 흡수). 미매칭 시 None."""
    key = _norm_sector(sector_name)
    entry = sec_map.get(key)
    if entry is None:
        for k, v in sec_map.items():          # 접두 매칭(짧은 쪽이 긴 쪽의 접두)
            if k.startswith(key) or key.startswith(k):
                entry = v
                break
    return entry


def _related_for(sector_name, sec_map):
    """KIS 업종명 → 표시용 관련주(시총 상위 STOCKS_PER_SECTOR개). 미매칭 시 []."""
    entry = _sector_entry(sector_name, sec_map)
    if not entry:
        return []
    return [{"code": s["code"], "name": s["name"]}
            for s in (entry.get("stocks") or [])[:STOCKS_PER_SECTOR]]


def fetch_sectors():
    """KIS 허브 /status 에서 KOSPI 산업별 업종(KRX 분류)을 받아 섹터 히트/상하위를 만든다.

    반환: (heat, ups, downs)
      heat  = 등락률 내림차순 전체 [{name, changePct, index}]
      ups   = 상위 SECTORS_PER_SIDE (급등 섹터), downs = 하위 SECTORS_PER_SIDE (급락 섹터)
              각 섹터엔 KRX 업종분류 기준 관련주(시총 상위)를 stocks[] 로 부착.
    허브 미배포/장외/응답부재 시 ([], [], []) — 대시보드가 섹터 섹션을 숨긴다(graceful).
    """
    try:
        r = requests.get(f"{KIS_HUB_URL}/status", timeout=10)
        r.raise_for_status()
        raw = (r.json() or {}).get("sectors") or []
    except Exception as e:
        _warn(f"KIS 허브 섹터 조회 실패: {e}")
        return [], [], []
    heat = [{"name": s.get("name"), "changePct": s.get("changePct"),
             "index": s.get("index")}
            for s in raw if s.get("name") and s.get("changePct") is not None
            # 산업 업종만 — '코스피 200 기후변화지수'·'코스피200제외 코스피지수' 같은
            # 합성 지수 시리즈는 섹터 히트/급등락 선정에서 제외
            and "코스피" not in s.get("name")]
    heat.sort(key=lambda s: s["changePct"], reverse=True)
    ups = [dict(s) for s in heat[:SECTORS_PER_SIDE]]
    downs = [dict(s) for s in (heat[-SECTORS_PER_SIDE:][::-1]
                               if len(heat) >= SECTORS_PER_SIDE else [])]
    sec_map = _load_krx_sector_map()
    if sec_map:
        for s in ups + downs:
            s["stocks"] = _related_for(s["name"], sec_map)
            # 테마 매칭용 전체 코드(시총 상위 12) — 표시용 4개보다 넓게 잡아 매칭률을 높인다.
            entry = _sector_entry(s["name"], sec_map)
            s["_matchCodes"] = [x["code"] for x in (entry.get("stocks") or [])] if entry else []
    return heat, ups, downs


# KIS 업종(KRX 산업 분류) → 관련 네이버 테마 매칭용 키워드 폴백. 대장주 코드 겹침이
# 우선이고, 겹침이 없을 때 테마명에 아래 키워드가 있으면 매칭한다(주요 KOSPI 섹터 커버).
_SECTOR_THEME_KW = {
    "전기·전자":     ["반도체", "HBM", "반도체장비", "반도체소재", "IT부품", "카메라", "광통신"],
    "운송장비·부품": ["자동차", "자동차부품", "전기차", "타이어", "2차전지"],
    "화학":         ["2차전지", "전지소재", "화학", "정유", "태양광"],
    "운송·창고":     ["항공", "해운", "물류", "택배"],
    "건설":         ["건설", "건설기계", "시멘트", "리모델링"],
    "제약":         ["제약", "바이오", "비만", "신약", "임상"],
    "보험":         ["보험"],
    "증권":         ["증권", "STO", "토큰증권"],   # 토큰증권은 '증권' 접미 합성어라 접두 매칭에 안 걸림 — 명시
    "은행":         ["은행", "금융지주"],
    "전기·가스":     ["전력", "원자력", "도시가스", "풍력", "태양광", "수소"],
    "통신":         ["통신", "5G"],
    "금속":         ["철강", "비철금속", "아연", "구리"],
    "음식료·담배":   ["음식료", "주류", "사료", "담배"],
    "유통":         ["유통", "화장품", "면세점", "홈쇼핑"],
    "기계·장비":     ["기계", "조선", "방산", "로봇", "원전"],
    "의료·정밀기기": ["의료기기", "진단", "치과"],
    "비금속":       ["시멘트", "유리"],
    "종이·목재":     ["제지", "골판지"],
    "섬유·의류":     ["의류", "패션", "섬유"],
    "오락·문화":     ["엔터", "미디어", "게임", "영화"],
    "IT 서비스":     ["소프트웨어", "인터넷", "AI", "보안", "결제"],
}


_THEME_STOCKS_CACHE = {}   # theme_no → 상세 구성종목 (회차 내 1회 fetch)


def _theme_detail(theme, up):
    """매칭된 네이버 테마 → {name, changePct, stocks}. stocks 는 섹터 방향(up/down) 상위."""
    if theme["no"] not in _THEME_STOCKS_CACHE:
        _THEME_STOCKS_CACHE[theme["no"]] = sources.naver_theme_stocks(theme["no"], limit=15)
    stocks = [x for x in _THEME_STOCKS_CACHE[theme["no"]]
              if x.get("changePct") is not None]
    stocks.sort(key=lambda x: -x["changePct"] if up else x["changePct"])
    return {"name": theme["name"], "changePct": theme["changePct"],
            "stocks": [{"code": x["code"], "name": x["name"], "changePct": x["changePct"]}
                       for x in stocks[:STOCKS_PER_THEME]]}


def _name_tokens(name):
    """섹터·테마명 → 2글자 이상 토큰 집합('·／/공백/괄호' 구분). 명칭 유사 매칭용."""
    import re
    return {t for t in re.split(r"[\s·・/()]+", str(name or "")) if len(t) >= 2}


def _kw_hit(kws, theme_name):
    """키워드 맵 매칭 — 테마명 '토큰'의 접두/동등 비교. 부분문자열 매칭이 아니라서
    '반도체' ⊂ '반도체장비'(접두)는 잡되 '통신' ⊂ '광통신'(접미 합성어)은 배제한다
    (2026-07-10 광통신 테마가 통신 섹터에 오분류된 원인 수정)."""
    toks = _name_tokens(theme_name)
    return any(t == k or t.startswith(k) for k in kws for t in toks)


def _match_themes(sector, ranking, used):
    """섹터 → 관련 네이버 테마 후보를 점수순으로 반환(방향 무관, 전체 랭킹에서).

    점수(높을수록 관련): ① 테마 주도주(leaders)가 섹터 KRX 관련주(시총 상위 12)에
    겹침 — 겹침당 +4  ② 명칭 유사(섹터명·테마명 토큰 교집합, 예: 섬유·의류 ↔ 패션/의류)
    +3  ③ 키워드 맵(토큰 접두) +2. 동점은 |등락률| 큰 테마 우선.

    대표(1번째) 테마는 최고점이면 되지만, 2번째부터는 '대장주 1종목 단독 겹침'을
    배제한다 — 조광피혁(섬유·의류 ↔ 부동산자산주)처럼 종목 하나가 이질 테마에 걸친
    경우라 보조 테마로는 신뢰 부족(2026-07-10 실측). 명칭 일치·큐레이션 키워드는
    사람이 검증한 연관이라 단독으로도 보조 테마 자격이 있다(STO↔증권 사례).
    이미 다른 섹터에 배정된 테마(used)는 제외. 상한 THEMES_PER_SECTOR."""
    codes = set(sector.get("_matchCodes") or
                [s.get("code") for s in (sector.get("stocks") or [])])
    s_toks = _name_tokens(sector.get("name"))
    kws = _SECTOR_THEME_KW.get(sector.get("name"), [])
    scored = []
    for t in ranking:
        if t["no"] in used:
            continue
        # 점수용 겹침은 _codes(주도주 ∪ 구성종목, attach 에서 확장) 기준 — 주도주(2종목)
        # 스냅샷은 회차마다 바뀌어 연결이 끊긴다(케이씨텍↔HBM, 2026-07-10 12:10 실측).
        t_codes = t.get("_codes") or {L.get("code") for L in (t.get("leaders") or [])}
        overlap = len(codes & t_codes)
        lead_ov = sum(1 for L in (t.get("leaders") or []) if L.get("code") in codes)
        name_sim = bool(s_toks & _name_tokens(t["name"]))
        kw = _kw_hit(kws, t["name"])
        score = 4 * overlap + 3 * name_sim + 2 * kw
        if score > 0:
            # 보조 테마 자격은 '주도주' 겹침≥2 또는 명칭/키워드 — 구성종목 겹침은 점수
            # (대표 선정)에만 쓴다. 구성종목 기준으로 풀면 우발적 교차 편입(조광피혁·
            # SG세계물산 ∈ 부동산자산주)이 보조 테마로 되살아난다(회귀 실측).
            ok_secondary = lead_ov >= 2 or name_sim or kw
            scored.append((score, abs(t.get("changePct") or 0), t, ok_secondary))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked = [t for _, _, t, _ in scored[:1]] \
        + [t for _, _, t, ok in scored[1:] if ok]
    return picked[:THEMES_PER_SECTOR]


def attach_sector_themes(sectors_up, sectors_down):
    """각 KIS 섹터(급등/급락 상위)에 관련 네이버 테마들 + 종목을 부착(sector['themes']).

    모닝브리핑의 '미국 섹터 → 국내 밸류체인' 흐름을 장중판으로. 테마 풀을 상승/하락으로
    가르지 않고 전체 랭킹에서 고른다 — 강세장엔 '급락 섹터'(상대 약세)도 양수 등락이라
    관련 테마가 상승권에 있는 게 정상이었고, 방향 풀 분리가 미검출의 주원인이었다
    (2026-07-10 통신·종이·목재·섬유·의류 실측). 섹터당 최대 THEMES_PER_SECTOR개."""
    ranking = sources.naver_theme_ranking()
    if not ranking:
        return
    # 매칭 코드 확장: 기본은 주도주(2종목), |등락률| 상위 THEME_DETAIL_TOP 테마는
    # 구성종목(15개)까지 로드해 합집합 — 주도주 스냅샷 교체로 섹터 연결이 끊기는
    # 문제 보완(케이씨텍이 HBM 주도주에서 밀리자 매칭 소실, 2026-07-10 실측).
    # 상세 페이지는 회차 내 캐시(_THEME_STOCKS_CACHE)라 _theme_detail 과 중복 fetch 없음.
    for t in ranking:
        t["_codes"] = {L.get("code") for L in (t.get("leaders") or []) if L.get("code")}
    by_abs = sorted(ranking, key=lambda t: -abs(t.get("changePct") or 0))
    for t in by_abs[:THEME_DETAIL_TOP]:
        if t["no"] not in _THEME_STOCKS_CACHE:
            _THEME_STOCKS_CACHE[t["no"]] = sources.naver_theme_stocks(t["no"], limit=15)
        t["_codes"] |= {x.get("code") for x in _THEME_STOCKS_CACHE[t["no"]] if x.get("code")}
    used = set()
    # 등락률 순위 앞 섹터(급등1→3, 급락1→3)가 테마를 먼저 가져간다(중복 배정 방지).
    for sector, up in ([(s, True) for s in sectors_up]
                       + [(s, False) for s in sectors_down]):
        themes = _match_themes(sector, ranking, used)
        if themes:
            used.update(t["no"] for t in themes)
            sector["themes"] = [_theme_detail(t, up) for t in themes]
        sector.pop("_matchCodes", None)     # 내부 매칭용 — 산출 JSON 에서 제거


def fetch_movers():
    """급등/급락 개별종목 (LLM 재료 — 테마 밖 단독 급등주 포착용)."""
    up, down = [], []
    for market in ("KOSPI", "KOSDAQ"):
        up += sources.naver_stock_ranking("up", market, limit=10)
        down += sources.naver_stock_ranking("down", market, limit=10)
    up.sort(key=lambda s: -(s["changePct"] or 0))
    down.sort(key=lambda s: (s["changePct"] or 0))
    return up[:10], down[:10]


def fetch_news(display=20):
    """'특징주' 뉴스 — 장중 급등락 사유가 실시간으로 잡히는 대표 검색어.
    가능하면 오늘(KST) 기사만 남긴다(pubDate 파싱 실패 시 무필터)."""
    items = sources.naver_search("news", "특징주", display=display)
    today = datetime.datetime.now(KST).strftime("%d %b %Y")   # RFC822 "09 Jul 2026"
    todays = [it for it in items if today in (it.get("date") or "")]
    return todays if todays else items


def fetch_fx_news(display=15):
    """환율 관련 뉴스 — 원/달러 환율 급변 사유·전망을 잡는 검색어 2종(중복 제거).
    가능하면 오늘(KST) 기사만 남긴다(pubDate 파싱 실패 시 무필터)."""
    items, seen = [], set()
    for q in ("원달러 환율", "환율"):
        for it in sources.naver_search("news", q, display=display):
            link = (it.get("link") or it.get("title") or "").strip()
            if link and link not in seen:
                seen.add(link)
                items.append(it)
    today = datetime.datetime.now(KST).strftime("%d %b %Y")
    todays = [it for it in items if today in (it.get("date") or "")]
    return (todays if todays else items)[:12]


# ----------------------------------------------------------------------------
# Phase 2) LLM 종합
# ----------------------------------------------------------------------------
SYSTEM = (
    "당신은 한국 주식시장 장중 시황을 요약하는 마켓 애널리스트입니다. "
    "제공된 RAW(지수, 투자자별 순매수 금액, 환율·금리·원자재 시장지표, 급등/급락 테마와 "
    "구성종목·편입사유, 급등/급락 개별종목, 당일 공시, 특징주 뉴스)만 근거로 장중 시황 "
    "브리핑을 작성하세요.\n"
    "규칙:\n"
    "- 데이터에 없는 수치·사실을 지어내지 말 것. 등락률 등 수치는 입력값을 인용할 것.\n"
    "- briefing 은 3~5개의 완결된 문장(각각이 대시보드 불릿 하나): ① 지수 흐름과 수급 주체"
    "(investors — 기관/외국인/개인 순매수, 단위 억원)를 함께, ② 환율·금리·유가(marketIndicators)가 "
    "시장에 주는 함의, ③ 주도 섹터(sectorsUp)와 동인, ④ 약세 섹터(sectorsDown), ⑤ 관전 포인트 순으로.\n"
    "- catalysts 는 입력 공시(id: d0,d1,..)·뉴스(id: n0,n1,..) 중 시장 영향이 큰 것을 '종목 단위'로 "
    "정리한다(방향별 — 상방·중립·하방 각각 최대 10건 — 서로 다른 종목을 최대한 많이 포괄):\n"
    "  · 한국(KOSPI/KOSDAQ) 상장 종목만 포함하고 해외 종목·지수·ETF(예: 테슬라·엔비디아 등)는 제외한다.\n"
    "  · 같은 종목이 공시·뉴스에 중복 등장하면 반드시 하나로 합쳐 한 줄로 요약할 것(같은 종목 중복 금지).\n"
    "  · 각 항목 필드: id(대표 출처 1개만, 공시가 있으면 공시 우선), stock(종목명), "
    "market(한국 종목이면 반드시 KOSPI 또는 KOSDAQ 로 채울 것 — 비우지 말 것; 해외 종목은 애초에 넣지 말 것), "
    "direction(그 촉매가 주가에 주는 방향 — 상방=bullish|중립=neutral|하방=bearish), summary(핵심 촉매 한 문장).\n"
    "  · 뉴스는 corp 필드가 없으니 제목·본문에서 종목명을 추출해 stock 에 넣을 것.\n"
    "- fxBullets 는 환율 관련 1~3개 불릿(fxNews·marketIndicators.exchange/world 근거): "
    "원/달러·달러인덱스·엔/달러 흐름과 그 배경(뉴스 근거), 국내 증시(수출주·환율 민감주)에 주는 "
    "함의를 담을 것. 근거 데이터가 전혀 없으면 빈 배열.\n"
    "- 한국어. 출력은 지정된 JSON 스키마를 엄격히 따를 것."
)

# 마감 시황(장 마감 후 마지막 회차) 전용 추가 지침 — previousRounds 를 반영해 하루를 종합
FINAL_ADDENDUM = (
    "\n- 이번 회차는 장 마감 후 작성하는 '마감 시황'이다. previousRounds(당일 이전 회차별 "
    "시황 불릿과 촉매 종목 요약)를 반드시 반영해 하루 전체 흐름을 종합하라: ① 오전→오후 "
    "지수·수급 흐름의 변화, ② 주도 섹터·테마의 지속/교체, ③ 장중 부각됐던 촉매의 결말"
    "(상한 유지/되돌림 등), ④ 다음 거래일 관전 포인트. briefing 은 5~7문장으로 확장하라."
)

_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "briefing": {"type": "array", "items": _STR},
        "fxBullets": {"type": "array", "items": _STR},
        "catalysts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": _STR, "stock": _STR, "market": _STR,
                "direction": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
                "summary": _STR},
            "required": ["id", "stock", "direction", "summary"]}},
    },
    "required": ["briefing", "catalysts"],
}


def _norm(s):
    return "".join(ch for ch in str(s or "") if ch not in " \t·ㆍ・")


def synthesize(indices, investors, indicators, sectors_up, sectors_down,
               movers_up, movers_down, disclosures, news, fx_news,
               prev_rounds=None):
    # 시장지표는 핵심만 추려 LLM 에 전달 (COFIX 등 저관련 항목 제외)
    core_ind = {}
    if indicators:
        core_ind = {
            "exchange": [x for x in indicators.get("exchange", [])
                         if any(k in x["name"] for k in ("USD", "JPY", "EUR"))],
            "world": [x for x in indicators.get("world", [])
                      # 네이버 국제환율 라벨: 달러인덱스, '달러/일본 엔'(=USD/JPY, 엔/달러 환율)
                      if any(k in x["name"] for k in ("달러인덱스", "일본 엔"))],
            "rates": [x for x in indicators.get("rates", [])
                      if any(k in x["name"] for k in ("CD", "국고채", "회사채"))],
            "commodities": [x for x in indicators.get("commodities", [])
                            if any(k in x["name"] for k in ("WTI", "국제 금"))],
        }
    raw = {
        "indices": indices,
        "investors": investors,          # 억원 단위 순매수 (개인/외국인/기관)
        "marketIndicators": core_ind,
        "sectorsUp": [{"name": s["name"], "changePct": s["changePct"],
                       "themes": [t["name"] for t in (s.get("themes") or [])],
                       "stocks": [x["name"] for x in (s.get("stocks") or [])]}
                      for s in sectors_up],
        "sectorsDown": [{"name": s["name"], "changePct": s["changePct"],
                         "themes": [t["name"] for t in (s.get("themes") or [])],
                         "stocks": [x["name"] for x in (s.get("stocks") or [])]}
                        for s in sectors_down],
        "moversUp": [{"name": s["name"], "changePct": s["changePct"]} for s in movers_up],
        "moversDown": [{"name": s["name"], "changePct": s["changePct"]} for s in movers_down],
        "disclosures": [{"id": f"d{i}", "corp": d["corp"], "title": d["title"]}
                        for i, d in enumerate(disclosures)],
        "news": [{"id": f"n{i}", "title": n["title"],
                  "description": (n.get("description") or "")[:100]}
                 for i, n in enumerate(news)],
        "fxNews": [{"title": n["title"],
                    "description": (n.get("description") or "")[:120]}
                   for n in fx_news],
    }
    system = SYSTEM
    if prev_rounds:
        raw["previousRounds"] = prev_rounds     # 마감 시황 — 당일 회차별 다이제스트
        system = SYSTEM + FINAL_ADDENDUM
    user = "RAW 데이터(JSON):\n" + json.dumps(raw, ensure_ascii=False)
    synth, model = {}, None
    if llm.configured():
        try:
            synth, model = llm.generate_json(system, user, max_tokens=4096,
                                             schema=SCHEMA, return_model=True)
        except Exception as e:
            _warn(f"LLM 종합 실패: {e} — 기계적 결과만 산출")
    else:
        _warn("LLM 미설정 — 기계적 결과만 산출")

    # 촉매: 종목 단위(중복 통합) + 방향(상방/중립/하방). id → 원문 URL 매칭(공시 dN / 뉴스 nN).
    # 방향별 상한 10건(상방·중립·하방 각각) — 한 방향이 표를 독점하지 않도록.
    catalysts, seen, dir_count = [], set(), {"bullish": 0, "neutral": 0, "bearish": 0}
    for c in (synth.get("catalysts") or []):
        cid = str(c.get("id") or "")
        stock = (c.get("stock") or "").strip()
        summary = (c.get("summary") or "").strip()
        direction = str(c.get("direction") or "neutral").lower()
        if direction not in ("bullish", "neutral", "bearish"):
            direction = "neutral"
        market = (c.get("market") or "").strip()
        try:
            idx = int(cid[1:])
        except (ValueError, IndexError):
            idx = -1
        url, kind = "", ""
        if cid.startswith("d") and 0 <= idx < len(disclosures):
            url, kind = disclosures[idx].get("url", ""), "disclosure"
            market = disclosures[idx].get("market") or market   # 공시 기록의 시장(KOSPI/KOSDAQ) 우선
            if not stock:
                stock = (disclosures[idx].get("corp") or "").strip()
        elif cid.startswith("n") and 0 <= idx < len(news):
            url, kind = news[idx].get("link", ""), "news"
        if not stock or not summary:
            continue
        if market not in ("KOSPI", "KOSDAQ"):   # 한국 상장 종목만 — 해외 종목/지수는 제외
            continue
        key = _norm(stock)                 # LLM 이 또 중복 내도 종목 단위로 한 번 더 방어
        if key in seen:
            continue
        if dir_count[direction] >= CATALYSTS_PER_DIR:   # 방향별 상한
            continue
        seen.add(key)
        dir_count[direction] += 1
        catalysts.append({"stock": stock, "market": market, "direction": direction,
                          "summary": summary, "url": url, "kind": kind})

    # 환율 브리핑 불릿 — LLM 요약 우선, 실패/미설정 시 오늘 환율 뉴스 헤드라인으로 폴백.
    fx_bullets = [b.strip() for b in (synth.get("fxBullets") or []) if b and b.strip()]
    if not fx_bullets:
        fx_bullets = [n["title"].strip() for n in fx_news[:3]
                      if (n.get("title") or "").strip()]

    return [b.strip() for b in (synth.get("briefing") or []) if b and b.strip()], \
        fx_bullets, catalysts, model


# ----------------------------------------------------------------------------
# Phase 3) 산출 + 아카이브
# ----------------------------------------------------------------------------
def main():
    print("=== Intraday Market Briefing ===")
    now = datetime.datetime.now(KST)
    indices = fetch_indices()
    investors = sources.naver_index_investors()          # 기관/외인/개인 순매수(억원)
    indicators = sources.naver_market_indicators()       # 환율·금리·유가·금
    sector_heat, sectors_up, sectors_down = fetch_sectors()   # KIS 업종(KRX 분류) 섹터 히트
    attach_sector_themes(sectors_up, sectors_down)            # 섹터 → 관련 네이버 테마·종목
    movers_up, movers_down = fetch_movers()
    disclosures = sources.dart_today_disclosures(limit=40)
    news = fetch_news()
    fx_news = fetch_fx_news()                             # 환율 관련 뉴스(원달러·환율)
    _tm = sum(len(s.get("themes") or []) for s in sectors_up + sectors_down)
    print(f"  수집: 섹터 {len(sector_heat)}개(↑{len(sectors_up)}/↓{len(sectors_down)}, 테마매칭 {_tm}), "
          f"공시 {len(disclosures)}, 뉴스 {len(news)}, 환율뉴스 {len(fx_news)}, "
          f"지표 {sum(len(v) for v in indicators.values()) if indicators else 0}, "
          f"수급 {len(investors)}시장")

    # 마감 시황(장 마감 후 마지막 회차): 당일 이전 회차 다이제스트를 LLM 에 함께 넘겨
    # 하루 전체 흐름(오전→오후 변화, 촉매 결말)을 종합하게 한다.
    final = _is_final_round(now)
    prev_rounds = _load_prev_rounds(now.strftime("%Y-%m-%d")) if final else []
    if final:
        print(f"  마감 시황 모드 — 이전 회차 {len(prev_rounds)}건 반영")

    briefing, fx_bullets, catalysts, model = synthesize(
        indices, investors, indicators, sectors_up, sectors_down,
        movers_up, movers_down, disclosures, news, fx_news,
        prev_rounds=prev_rounds)

    out = {
        "date": now.strftime("%Y-%m-%d"),
        "asof": now.strftime("%Y-%m-%d %H:%M KST"),
        "final": final,                 # 마감 시황 여부 (대시보드 배지)
        "generatedBy": model or "mechanical",
        "indices": indices,
        "investors": investors,
        "indicators": indicators,
        "briefing": briefing,
        "fxBullets": fx_bullets,        # 환율 브리핑(LLM 요약 또는 헤드라인 폴백)
        "fxNews": fx_news[:8],          # 환율 관련 뉴스 원문(대시보드 접기)
        "sectorHeat": sector_heat,      # KIS KOSPI 산업별 업종(KRX 분류), 등락 내림차순
        "sectorsUp": sectors_up,        # 급등 상위 섹터 + KRX 관련주 + 매칭 네이버 테마(theme)
        "sectorsDown": sectors_down,    # 급락 상위 섹터
        "catalysts": catalysts,
        "disclosures": disclosures[:15],
        "news": news[:10],
        "disclaimer": "네이버 금융·DART·네이버뉴스 기반 자동 생성 시황 — 투자 판단 참고용.",
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  Updated: {OUT_PATH} (model={out['generatedBy']}, "
          f"briefing {len(briefing)}문장, 환율 {len(fx_bullets)}불릿, 촉매 {len(catalysts)}건)")

    # 회차 누적 아카이브 — 장 마감 후 하루 흐름 복기용
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    arch_path = os.path.join(ARCHIVE_DIR, f"{out['date']}.json")
    arch = {"date": out["date"], "rounds": []}
    if os.path.exists(arch_path):
        try:
            with open(arch_path, encoding="utf-8") as f:
                arch = json.load(f)
        except Exception:
            pass
    if not isinstance(arch.get("rounds"), list):
        arch["rounds"] = []
    arch["rounds"].append(out)
    with open(arch_path, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False, indent=2)
    print(f"  Archive: {arch_path} ({len(arch['rounds'])} rounds)")
    print("=== Done ===")


if __name__ == "__main__":
    main()
