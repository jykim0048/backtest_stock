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
import re
import sys
import json
import time
import datetime
from concurrent.futures import ThreadPoolExecutor

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
THEMES_PER_SECTOR = int(os.environ.get("BRIEFING_THEMES_PER_SECTOR", "3"))  # 섹터당 관련 테마 상한
# 반대방향 테마 슬롯: 섹터 방향과 반대로 |등락|이 이 값(%) 이상인 종목이 있으면
# 그 종목과 가장 연관된 테마 1개를 마지막 슬롯에 노출 (통신 +3.39% 내 프리티 -24% 사례)
THEME_COUNTER_MIN_CHG = float(os.environ.get("BRIEFING_COUNTER_MIN_CHG", "5"))
# 섹터 상위 종목 당일 뉴스 부착: 테마명이 아닌 '실제 촉매'(AI데이터센터·수주·정책 등)로
# 섹터 강세/약세를 서술하게 하는 근거. 섹터당 상위 N종목 × 종목당 M헤드라인.
SECTOR_NEWS_TOP = int(os.environ.get("BRIEFING_SECTOR_NEWS_TOP", "5"))
SECTOR_NEWS_PER = int(os.environ.get("BRIEFING_SECTOR_NEWS_PER", "3"))
# 섹터 상위 종목 당일 가집계(외국인/기관/연기금 순매수 대금) — KIS 허브 /flow 로 수집해
# 섹터 카드 알약·LLM 수급 근거로 사용. 상위 N종목만.
SECTOR_NETBUY_TOP = int(os.environ.get("BRIEFING_SECTOR_NETBUY_TOP", "5"))
# ── 기여도 가중 스코어링(2026-07-15 13:41 회차 시뮬레이션으로 확정) ──────────────
# 지수는 시총가중이므로 '오늘 이 섹터를 움직인' 테마 = 시총×당일등락 기여가 큰 종목이
# 속한 테마다(오락·문화 -1.63% 의 동인이 파라다이스·GKL 폭락인데 개수 기준으론 카지노가
# 8위라 안 보이던 문제). 구조항(겹침·명칭·키워드)에 기여항을 합산한다.
THEME_CONTRIB_W   = float(os.environ.get("BRIEFING_CONTRIB_W", "40"))    # 기여항 가중(40×share)
THEME_CONTRIB_CAP = float(os.environ.get("BRIEFING_CONTRIB_CAP", "0.20"))  # 종목당 기여 상한(지배종목 도배 방지)
THEME_CONTRIB_MIN_CHG = float(os.environ.get("BRIEFING_CONTRIB_MIN_CHG", "0.5"))  # 기여항 활성 최소 |섹터 등락%|
THEME_MAX_SECTORS = int(os.environ.get("BRIEFING_THEME_MAX_SECTORS", "2"))  # 테마당 최대 표시 섹터 수(소프트 캡)
THEME_DETAIL_TOP = int(os.environ.get("BRIEFING_THEME_DETAIL_TOP", "20"))   # (폴백용) 구성종목 로드 테마 수(|등락| 상위)
THEME_MAP_PATH = os.path.join(ROOT, "public", "assets", "theme_map.json")   # 주 1회 재생성(theme_map.yml)
SECTORS_PER_SIDE = int(os.environ.get("BRIEFING_SECTORS", "3"))    # 상승/하락 각 N개 섹터
CATALYSTS_PER_DIR = int(os.environ.get("BRIEFING_CAT_PER_DIR", "10"))  # 촉매 방향별 상한(상방/중립/하방)

# KIS 허브(vi_limit_monitor) /status — KOSPI 산업별 업종 지수 등락률(KRX 분류) 공급원.
# 허브가 장중 REST 로 FHPUP02140000 을 폴링해 Redis→/status 로 노출한다(대시보드 VI 탭과 동일 서비스).
KIS_HUB_URL = os.environ.get(
    "KIS_HUB_URL", "https://tradingstrategies-production-09d4.up.railway.app")
# 관련주 시세 — 대시보드와 동일한 backteststock /api/prices (KIS 허브 /prices 는 앱키 부재로 빈 응답)
PRICES_API_BASE = os.environ.get(
    "PRICES_API_BASE", "https://backteststock-production.up.railway.app")


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
            # 테마 매칭용 전체 코드 — KOSPI 시총상위 12 + 같은 업종의 KOSDAQ 시총상위 12.
            # 표시(stocks)는 KOSPI 만(섹터 등락률이 KOSPI 산업별 지수라 지수 정합 유지),
            # 매칭은 양 시장으로 넓혀 코스닥 중심 테마(장비·바이오 등)와의 종목 겹침을 확보.
            entry = _sector_entry(s["name"], sec_map)
            members = (((entry.get("stocks") or []) + (entry.get("kosdaqStocks") or []))
                       if entry else [])
            s["_matchCodes"] = [x["code"] for x in members]
            # 기여도 가중용 시총(맵 재생성분부터 cap 필드 존재 — 없으면 기여항 자동 비활성)
            s["_matchCaps"] = {x["code"]: x.get("cap") or 0 for x in members}
    return heat, ups, downs


# KIS 업종(KRX 산업 분류) → 관련 네이버 테마 매칭용 키워드 폴백. 대장주 코드 겹침이
# 우선이고, 겹침이 없을 때 테마명에 아래 키워드가 있으면 매칭한다(주요 KOSPI 섹터 커버).
_SECTOR_THEME_KW = {
    "전기·전자":     ["반도체", "HBM", "반도체장비", "반도체소재", "IT부품", "카메라", "광통신"],
    "운송장비·부품": ["자동차", "자동차부품", "전기차", "타이어", "2차전지"],
    "화학":         ["2차전지", "전지소재", "화학", "정유", "태양광", "화장품"],  # 화장품사는 KRX 화학 분류
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


def _load_theme_codes():
    """정적 테마맵 → {theme_no: 구성종목 코드 set}. 없거나 로드 실패 시 {} (폴백 전환)."""
    try:
        with open(THEME_MAP_PATH, encoding="utf-8") as f:
            themes = (json.load(f) or {}).get("themes") or {}
        return {no: {s.get("code") for s in (e.get("stocks") or []) if s.get("code")}
                for no, e in themes.items()}
    except Exception as e:
        _warn(f"테마맵 로드 실패: {e}")
        return {}


def _theme_detail(theme, up):
    """매칭된 네이버 테마 → {name, changePct, stocks, reasons}.
    stocks 는 섹터 방향(up/down) 상위, reasons 는 그 종목들의 '테마 편입 사유'(네이버
    상세 페이지 info_txt) 상위 2건 — LLM 이 섹터 등락 동인을 서술할 근거."""
    if theme["no"] not in _THEME_STOCKS_CACHE:
        _THEME_STOCKS_CACHE[theme["no"]] = sources.naver_theme_stocks(theme["no"], limit=15)
    stocks = [x for x in _THEME_STOCKS_CACHE[theme["no"]]
              if x.get("changePct") is not None]
    stocks.sort(key=lambda x: -x["changePct"] if up else x["changePct"])
    reasons = []
    for x in stocks:
        rs = (x.get("reason") or "").strip()
        if rs and rs not in reasons:
            reasons.append(rs[:90])
        if len(reasons) >= 2:
            break
    return {"name": theme["name"], "changePct": theme["changePct"],
            "stocks": [{"code": x["code"], "name": x["name"], "changePct": x["changePct"]}
                       for x in stocks[:STOCKS_PER_THEME]],
            "reasons": reasons}


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


def _fetch_match_rates(codes):
    """매칭코드 전체의 당일 등락률(%) — 네이버 폴링 API 배치(50종목/1콜, KIS 무관).

    반환 {code: rate(부호 포함)}. 실패 청크는 생략(해당 종목 기여 0 처리) — 전체 실패
    시 빈 dict 이고 기여항이 자동 비활성돼 구조항만으로 동작한다(graceful)."""
    out = {}
    for i in range(0, len(codes), 50):
        chunk = codes[i:i + 50]
        try:
            r = requests.get(
                "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:"
                + ",".join(chunk),
                headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            for a in ((r.json() or {}).get("result") or {}).get("areas") or []:
                for it in a.get("datas") or []:
                    cd, cr = str(it.get("cd") or ""), it.get("cr")
                    if cd and cr is not None:
                        sign = -1 if str(it.get("rf")) in ("4", "5") else 1  # rf 4/5 = 하락
                        out[cd] = sign * float(cr)
        except Exception as e:
            _warn(f"매칭코드 등락률 조회 실패(청크 생략): {e}")
    return out


def _contrib_shares(sector, rates):
    """섹터 방향 기여 share {code: 0~1} — 시총 × 당일등락 중 섹터 방향과 같은 기여만.

    종목당 기여는 총기여의 THEME_CONTRIB_CAP(기본 20%)로 캡 — 삼성전자급 지배종목이
    든 테마가 도배되는 것 방지. |섹터 등락| < THEME_CONTRIB_MIN_CHG(기본 0.5%)이거나
    시총(cap)·등락률이 없으면 None → 기여항 비활성(구조항만, 노이즈 증폭 방지)."""
    chg = sector.get("changePct") or 0
    if abs(chg) < THEME_CONTRIB_MIN_CHG:
        return None
    caps = sector.get("_matchCaps") or {}
    d = 1 if chg >= 0 else -1
    raw = {c: max(0.0, d * (caps.get(c) or 0) * (rates.get(c) or 0.0)) for c in caps}
    tot = sum(raw.values())
    if tot <= 0:
        return None
    capped = {c: min(v, THEME_CONTRIB_CAP * tot) for c, v in raw.items()}
    denom = sum(capped.values()) or 1.0
    return {c: v / denom for c, v in capped.items()}


def _match_themes(sector, ranking, assigned, shares):
    """섹터 → 관련 네이버 테마 후보를 점수순으로 반환(방향 무관, 전체 랭킹에서).

    점수 = 구조항 + 기여항 (2026-07-15 13:41 회차 시뮬레이션으로 확정):
      구조항: ① 구성종목 겹침당 +4  ② 명칭 유사(토큰 교집합) +3  ③ 키워드 맵(접두) +2
      기여항: THEME_CONTRIB_W × (겹친 종목들의 섹터 방향 기여 share 합, 종목당 20% 캡)
              — '오늘 이 섹터를 실제로 움직인' 테마를 끌어올린다(오락·문화 하락일의
              카지노, 기계·장비 급등일의 반도체 장비). shares=None 이면 구조항만.
    동점은 |테마 등락률| 큰 쪽 우선. 선정 후 '노출 순서'는 섹터 기여 share
    내림차순으로 재정렬한다(2026-07-16 — 오늘 섹터를 움직인 테마가 위로).

    대표(1번째) 테마는 최고점이면 되지만, 2번째부터는 '대장주 1종목 단독 겹침'을
    배제한다 — 조광피혁(섬유·의류 ↔ 부동산자산주)처럼 종목 하나가 이질 테마에 걸친
    경우라 보조 테마로는 신뢰 부족(2026-07-10 실측). 명칭 일치·큐레이션 키워드·
    기여 share 10%+ 는 검증된 연관으로 단독 자격 인정.

    선점제(used) → 소프트 캡: 같은 테마를 최대 THEME_MAX_SECTORS(기본 2)개 섹터까지
    허용 — 광역 업종(제조)이 진짜 연관 테마를 흡수하던 문제(케이씨텍↔반도체 장비,
    2026-07-14) 해결 + 반도체 랠리일의 전 섹터 도배는 캡으로 방지."""
    codes = set(sector.get("_matchCodes") or
                [s.get("code") for s in (sector.get("stocks") or [])])
    s_toks = _name_tokens(sector.get("name"))
    kws = _SECTOR_THEME_KW.get(sector.get("name"), [])
    scored = []
    for t in ranking:
        if assigned.get(t["no"], 0) >= THEME_MAX_SECTORS:
            continue
        # 지수형·유사지수형 테마(밸류업·코스피200·MSCI·'대표주' 모음·S7 등)는 대형주
        # 다수 포함이라 겹침·기여가 높게 나오지만 섹터 등락의 '동인' 설명으론 무의미 —
        # 후보 제외. ('대표주|S7'은 기여도 가중 도입 시뮬에서 도배 실측, 2026-07-15)
        if re.search(r"지수|코스피|밸류업|MSCI|대표주|S7", t["name"] or ""):
            continue
        # 점수용 겹침은 _codes(주도주 ∪ 구성종목, attach 에서 확장) 기준 — 주도주(2종목)
        # 스냅샷은 회차마다 바뀌어 연결이 끊긴다(케이씨텍↔HBM, 2026-07-10 12:10 실측).
        t_codes = t.get("_codes") or {L.get("code") for L in (t.get("leaders") or [])}
        overlap = len(codes & t_codes)
        lead_ov = sum(1 for L in (t.get("leaders") or []) if L.get("code") in codes)
        name_sim = bool(s_toks & _name_tokens(t["name"]))
        kw = _kw_hit(kws, t["name"])
        share = sum(shares.get(c, 0.0) for c in (codes & t_codes)) if shares else 0.0
        score = 4 * overlap + 3 * name_sim + 2 * kw + THEME_CONTRIB_W * share
        if score > 0.5:
            # 보조 테마 자격: 주도주 겹침≥2 / 명칭 / 키워드 / 구성종목 겹침≥4 /
            # 기여 share≥10%. 겹침 소수의 우발 교차 편입은 차단하되(조광피혁 사례),
            # 겹침 4+(화학∩화장품 사례)와 유의미한 지수 기여는 진짜 연관으로 인정.
            ok_secondary = (lead_ov >= 2 or name_sim or kw or overlap >= 4
                            or share >= 0.10)
            scored.append((score, abs(t.get("changePct") or 0), t, ok_secondary, share))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked = [(t, sh) for _, _, t, _, sh in scored[:1]] \
        + [(t, sh) for _, _, t, ok, sh in scored[1:] if ok]
    picked = picked[:THEMES_PER_SECTOR]
    # 노출 순서는 '오늘 섹터를 움직인' 기여 share 내림차순 — 선정(점수순)과 분리
    # (2026-07-16). 파이썬 정렬은 안정적이라 share 동률(0 포함)은 점수순 유지.
    picked.sort(key=lambda x: -x[1])
    return [t for t, _ in picked]


def _counter_theme(sector, ranking, assigned, rates):
    """섹터 방향과 반대로 |등락|이 가장 큰 종목(≥ THEME_COUNTER_MIN_CHG%)의 연관 테마.

    통신 +3.39% 안의 프리티 -24%(2026-07-16 실측)처럼 유의미한 역방향 급등락은
    정방향 기여 스코어링(_contrib_shares 가 섹터 방향 기여만 집계)에 전혀 안 잡힌다 —
    그 종목이 속한 테마 1개를 마지막 슬롯에 노출해 상승·하락 동인을 함께 보인다.
    연관성 순위: ① 그 종목이 테마 주도주 ② 구조항 점수(겹침4·명칭3·키워드2)
    ③ 테마 등락이 종목과 같은 방향 ④ |테마 등락| 큰 쪽.
    반환 None = 미발동(임계 미달·테마 미소속·지수형/소프트캡 제외)."""
    chg = sector.get("changePct") or 0
    d = 1 if chg >= 0 else -1
    codes = set(sector.get("_matchCodes") or
                [s.get("code") for s in (sector.get("stocks") or [])])
    counter_rates = {c: r for c, r in rates.items() if c in codes and d * r < 0}
    if not counter_rates:
        return None
    top = max(counter_rates, key=lambda c: abs(counter_rates[c]))
    if abs(counter_rates[top]) < THEME_COUNTER_MIN_CHG:
        return None
    s_toks = _name_tokens(sector.get("name"))
    kws = _SECTOR_THEME_KW.get(sector.get("name"), [])
    cand = []
    for t in ranking:
        if assigned.get(t["no"], 0) >= THEME_MAX_SECTORS:
            continue
        if re.search(r"지수|코스피|밸류업|MSCI|대표주|S7", t["name"] or ""):
            continue
        t_codes = t.get("_codes") or set()
        if top not in t_codes:
            continue
        struct = (4 * len(codes & t_codes)
                  + 3 * bool(s_toks & _name_tokens(t["name"]))
                  + 2 * _kw_hit(kws, t["name"]))
        is_leader = any(L.get("code") == top for L in (t.get("leaders") or []))
        dir_match = counter_rates[top] * (t.get("changePct") or 0) > 0
        cand.append((is_leader, struct, dir_match, abs(t.get("changePct") or 0), t))
    if not cand:
        return None
    cand.sort(key=lambda x: (-x[0], -x[1], -x[2], -x[3]))
    return cand[0][4]


def attach_sector_themes(sectors_up, sectors_down):
    """각 KIS 섹터(급등/급락 상위)에 관련 네이버 테마들 + 종목을 부착(sector['themes']).

    모닝브리핑의 '미국 섹터 → 국내 밸류체인' 흐름을 장중판으로. 테마 풀을 상승/하락으로
    가르지 않고 전체 랭킹에서 고른다 — 강세장엔 '급락 섹터'(상대 약세)도 양수 등락이라
    관련 테마가 상승권에 있는 게 정상이었고, 방향 풀 분리가 미검출의 주원인이었다
    (2026-07-10 통신·종이·목재·섬유·의류 실측). 섹터당 최대 THEMES_PER_SECTOR개."""
    ranking = sources.naver_theme_ranking()
    if not ranking:
        return
    # 매칭 코드: 정적 테마맵(theme_map.json — 전체 테마 × 전체 구성종목, 주 1회 재생성)
    # 우선. 주도주(2종목) 스냅샷·회차 시점 상세 fetch 는 교체·순위 변동으로 연결이
    # 끊긴다(케이씨텍↔HBM, 2026-07-10 실측). 맵이 없으면 기존 방식 폴백:
    # 주도주 + |등락률| 상위 THEME_DETAIL_TOP 테마의 구성종목 fetch(회차 내 캐시).
    theme_codes = _load_theme_codes()
    for t in ranking:
        t["_codes"] = {L.get("code") for L in (t.get("leaders") or []) if L.get("code")}
        t["_codes"] |= theme_codes.get(t["no"], set())
    if not theme_codes:
        _warn("테마맵 미존재 — 주도주+상세 fetch 폴백 매칭")
        by_abs = sorted(ranking, key=lambda t: -abs(t.get("changePct") or 0))
        for t in by_abs[:THEME_DETAIL_TOP]:
            if t["no"] not in _THEME_STOCKS_CACHE:
                _THEME_STOCKS_CACHE[t["no"]] = sources.naver_theme_stocks(t["no"], limit=15)
            t["_codes"] |= {x.get("code") for x in _THEME_STOCKS_CACHE[t["no"]] if x.get("code")}
    # 기여도 가중용 당일 등락률 — 전 섹터 매칭코드 일괄(네이버 폴링 배치, KIS 무관)
    all_codes = sorted({c for s in sectors_up + sectors_down
                        for c in (s.get("_matchCodes") or [])})
    rates = _fetch_match_rates(all_codes) if all_codes else {}
    assigned = {}   # theme_no → 배정된 섹터 수 (소프트 캡 THEME_MAX_SECTORS)
    # 등락률 순위 앞 섹터(급등1→3, 급락1→3)부터 배정 — 소프트 캡 도달 시 뒤 순번 제외.
    for sector, up in ([(s, True) for s in sectors_up]
                       + [(s, False) for s in sectors_down]):
        shares = _contrib_shares(sector, rates)
        themes = _match_themes(sector, ranking, assigned, shares)
        # 반대방향 슬롯: 역방향 |등락|≥THEME_COUNTER_MIN_CHG% 종목의 연관 테마를
        # 마지막 슬롯으로 (정방향 기여순 상위는 유지, 상한 THEMES_PER_SECTOR 불변).
        counter = _counter_theme(sector, ranking, assigned, rates)
        if counter and any(t["no"] == counter["no"] for t in themes):
            counter = None                    # 이미 정방향 선정에 포함 — 중복 방지
        if counter:
            themes = themes[:max(0, THEMES_PER_SECTOR - 1)] + [counter]
        if themes:
            for t in themes:
                assigned[t["no"]] = assigned.get(t["no"], 0) + 1
            # 반대방향 테마는 구성종목 표기를 역방향 상위로 정렬(_theme_detail 방향 반전)
            sector["themes"] = [
                _theme_detail(t, (not up) if (counter and t["no"] == counter["no"]) else up)
                for t in themes]
        # _matchCodes/_matchCaps 정리는 enrich_sector_stocks(마지막 소비처 — 대표주
        # 기여도 정렬에 cap 사용)가 담당한다.


def fetch_investor_series():
    """코스피/코스닥 1분 투자자 누적 순매수 시계열 — 직전 회차 산출물 이월 + 증분 병합.

    직전 intraday_briefing.json 의 investorSeries(당일분)를 이어받고, 마지막 저장 시각
    이후 페이지만 새로 파싱한다(회차 간격 30분 ≈ 3페이지 + 여유). 당일 첫 수집이면
    전량(최대 40페이지). 반환: {kospi: [...], kosdaq: [...]}."""
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    prev = {}
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            old = json.load(f) or {}
        if old.get("date") == today:
            prev = old.get("investorSeries") or {}
    except Exception:
        pass
    series = {}
    for key, market in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        rows = {r["time"]: r for r in (prev.get(key) or []) if isinstance(r, dict)}
        pages = 6 if rows else 40
        for r in sources.naver_investor_timeline(market, pages=pages):
            rows[r["time"]] = r
        series[key] = sorted(rows.values(), key=lambda x: x["time"])
    return series


def _mins(hm):
    try:
        h, m = str(hm).split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return 0


def _flow_trend(series):
    """시장×주체별 수급 요약(LLM 용) — now 현재 누적(억원), chg30m 최근 30분 변화량,
    flipAt 현재 부호로 전환된 시각(당일 내내 같은 부호면 생략). 원시 시계열 대신
    이 요약만 LLM 에 넘긴다(토큰 절약 + 해석 오류 방지)."""
    out = {}
    sign = lambda v: (v > 0) - (v < 0)
    for mk, rows in (series or {}).items():
        m = {}
        for who in ("individual", "foreign", "institution"):
            vals = [(r["time"], r.get(who)) for r in rows if r.get(who) is not None]
            if not vals:
                continue
            now_t, now_v = vals[-1]
            base_v = next((v for t, v in reversed(vals)
                           if _mins(now_t) - _mins(t) >= 30), vals[0][1])
            d = {"now": now_v, "chg30m": now_v - base_v}
            cur = sign(now_v)
            if cur != 0:
                flip_at = None
                for i in range(len(vals) - 1, -1, -1):
                    if sign(vals[i][1]) != cur:
                        flip_at = vals[i + 1][0] if i + 1 < len(vals) else None
                        break
                if flip_at:
                    d["flipAt"] = flip_at    # 이 시각부터 현재 부호(순매수/순매도) 유지
            m[who] = d
        if m:
            out[mk] = m
    return out


def _fmt_investors(investors):
    """투자자 순매수 금액(억원)을 LLM 이 '그대로 인용'할 미리 완성된 문자열로 — LLM 이 큰
    억원 수치를 조 변환하며 자릿수를 흘리는 오류(외국인 -46,494억을 -4,649억으로 ÷10 표기,
    2026-07-28 사용자 지적) 방지. |v|>=1만억이면 조 병기. 반환: {kospi: "개인 +…억 · …"}."""
    order = (("개인", "individual"), ("외국인", "foreign"), ("기관", "institution"))
    out = {}
    for mk, iv in (investors or {}).items():
        parts = []
        for label, k in order:
            v = (iv or {}).get(k)
            if v is None:
                continue
            jo = f"(={v / 10000:+.1f}조)" if abs(v) >= 10000 else ""
            parts.append(f"{label} {v:+,.0f}억{jo}")
        if parts:
            out[mk] = " · ".join(parts)
    return out


def enrich_sector_stocks(sectors_up, sectors_down):
    """급등/급락 섹터 관련주에 ① 실시간 등락률(KIS 허브 /prices, 1회 일괄)과
    ② 최신 집계일 외국인·기관 순매수 수량(네이버 trend, 종목당 1회)을 부착.
    LLM 이 '어느 종목이, 누가 사서' 섹터를 움직이는지 서술할 근거. 실패 필드는 생략."""
    stocks = [x for s in sectors_up + sectors_down for x in (s.get("stocks") or [])]
    codes = list(dict.fromkeys(x["code"] for x in stocks if x.get("code")))
    if codes:
        prices = {}
        try:
            # 시세는 backteststock /api/prices (대시보드와 동일 소스). KIS 허브(모니터 서비스)
            # /prices 는 그 서비스에 KIS 앱키가 없어 항상 빈 응답(2026-07-13 실측) — 사용 금지.
            r = requests.get(f"{PRICES_API_BASE}/api/prices",
                             params={"codes": ",".join(codes)}, timeout=20)
            prices = (r.json() or {}).get("stocks") or {}
        except Exception as e:
            _warn(f"관련주 시세 조회 실패: {e}")
        # 종목당 1콜 순차(30~60콜 × ~0.5s)가 수집 단계의 주요 꼬리 — TP8 병렬
        # (attach_sector_stock_news 와 동일 패턴, naver_stock_flow 는 실패 시 {} graceful).
        with ThreadPoolExecutor(max_workers=8) as pool:
            flows = dict(zip(codes, pool.map(sources.naver_stock_flow, codes)))
        n_p, n_f = 0, 0
        for x in stocks:
            p = prices.get(x.get("code")) or {}
            if p.get("rate") is not None:
                x["changePct"] = p["rate"]
                n_p += 1
            f = flows.get(x.get("code")) or {}
            if f:
                x["flow"] = f          # {date, foreign, institution} — 순매수 수량(주)
                n_f += 1
        print(f"  섹터 관련주 보강: 시세 {n_p}/{len(stocks)}, 수급 {n_f}/{len(stocks)}")

    # 표시 순서 = 시총순 고정(2026-07-30 사용자 결정 — 종목 자리가 회차마다 바뀌지 않게).
    # 단 '오늘의 동인' 정보(기여도 d×cap×chg, 2026-07-20 비금속 사례로 도입)는 잃지 않도록
    # 종목별 기여도를 내부 필드 _contrib 로 남긴다 — attach_sector_stock_news 가 뉴스 수집
    # 대상(상위 N)을 기여순으로 골라 LLM 의 '왜 움직였나' 근거는 유지(표시/뉴스근거 분리).
    # cap 없는 구맵은 d×chg 폴백·순서는 맵 순서(=시총순) 그대로, 등락률 없는 종목은 0.
    for s in sectors_up + sectors_down:
        d = 1 if (s.get("changePct") or 0) >= 0 else -1
        caps = s.get("_matchCaps") or {}
        has_cap = any(caps.get(x.get("code")) for x in (s.get("stocks") or []))
        for x in (s.get("stocks") or []):
            chg = x.get("changePct")
            x["_contrib"] = 0.0 if chg is None else (
                d * ((caps.get(x.get("code")) or 0) if has_cap else 1.0) * chg)
        if has_cap:
            (s.get("stocks") or []).sort(
                key=lambda x: -(caps.get(x.get("code")) or 0))
        s.pop("_matchCodes", None)     # 내부 매칭·정렬용 — 산출 JSON 에서 제거
        s.pop("_matchCaps", None)


def attach_sector_stock_news(sectors_up, sectors_down):
    """섹터 상위 종목(기여순)의 '오늘' 뉴스 헤드라인을 부착 — LLM 이 네이버 테마명이
    아니라 실제 촉매(예: AI 데이터센터 인프라·수주·정책)로 섹터 강세/약세를 서술하게
    하는 근거. enrich_sector_stocks(기여순 정렬) 이후에 호출. 상위 SECTOR_NEWS_TOP
    종목만·종목당 SECTOR_NEWS_PER 건, 병렬·오늘자 우선·graceful(실패 필드 생략).

    테마명은 '무엇'(어느 테마 소속)이지 '왜'(오늘의 촉매)가 아니라, 테마명만 보면
    'GTX·원자력' 처럼 소속 테마로 원인을 오독한다(2026-07-23 건설 AI데이터센터 사례).
    당일 뉴스 헤드라인에 실제 촉매가 있어 이를 LLM 입력에 직접 넣는다(추가 LLM 콜 없음)."""
    today = datetime.datetime.now(KST).strftime("%d %b %Y")   # RFC822 "23 Jul 2026"
    # 같은 종목이 여러 섹터 top 에 별개 '객체'로 들어간다(예: SK하이닉스 = 전기·전자
    # + 제조) — 조회는 코드당 1회로 dedup 하되, 결과는 모든 객체에 부착해야 한다.
    # (기존엔 첫 객체에만 부착 → 두 번째 섹터에서 데이터 누락, 2026-07-24 실측)
    by_code = {}
    for s in sectors_up + sectors_down:
        stocks = s.get("stocks") or []
        # 뉴스 대상 = 기여도(_contrib) 상위 — 표시 배열은 시총순 고정이라(2026-07-30)
        # '오늘 섹터를 움직인' 종목의 뉴스가 밀리지 않게 별도 선정. _contrib 없으면
        # (enrich 실패 등) 0 → 배열 앞 종목(시총 상위) 폴백.
        for x in sorted(stocks, key=lambda x: -(x.get("_contrib") or 0))[:SECTOR_NEWS_TOP]:
            code = x.get("code")
            if code and x.get("name"):
                by_code.setdefault(code, []).append(x)
        for x in stocks:
            x.pop("_contrib", None)    # 내부 선정용 — 산출 JSON 누출 방지
    if not by_code:
        return

    def _one(xs):
        try:
            items = sources.naver_search("news", xs[0]["name"], display=6)
            todays = [it for it in items if today in (it.get("date") or "")] or items
            heads = []
            for it in todays[:SECTOR_NEWS_PER]:
                t = re.sub(r"<[^>]+>", "", it.get("title") or "").strip()
                if t:
                    heads.append(t)
            return xs, heads
        except Exception:
            return xs, []

    n = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for xs, heads in pool.map(_one, by_code.values()):
            if heads:
                for x in xs:
                    x["news"] = heads
                n += 1
    print(f"  섹터 관련주 뉴스: {n}/{len(by_code)}종목 헤드라인 부착")


def attach_sector_stock_netbuy(sectors_up, sectors_down):
    """섹터 상위 종목(기여순)에 당일 매매종목 가집계(잠정) — 외국인/기관/연기금 순매수
    대금(백만원, 부호) — 를 KIS 허브 /flow 에서 병렬 부착. 섹터를 '누가 사서/팔아'
    움직이는지 수급 주체를 알약·LLM 근거로 노출. enrich_sector_stocks(기여순 정렬) 이후 호출.

    함정: /flow 의 rank 는 종목이 KIS 순매수/순매도 상위 리스트(대략 top 30)에 들 때만
    채워진다 — 미랭크 종목은 가집계 금액이 없어 netBuy 미부착(프론트가 합계에서 제외·표에 –)."""
    # 같은 종목이 여러 섹터 top 에 별개 '객체'로 들어간다 — 조회는 코드당 1회로
    # dedup 하되 결과는 모든 객체에 부착(첫 객체만 부착하면 두 번째 섹터가 – 로 비는
    # 버그: 제조의 SK하이닉스·삼성전자가 전기·전자에만 반영되던 2026-07-24 실측).
    by_code = {}
    for s in sectors_up + sectors_down:
        for x in (s.get("stocks") or [])[:SECTOR_NETBUY_TOP]:
            code = x.get("code")
            if code:
                by_code.setdefault(code, []).append(x)
    if not by_code:
        return
    today_c = datetime.datetime.now(KST).strftime("%Y%m%d")

    def _one(item):
        code, xs = item
        try:
            r = requests.get(f"{KIS_HUB_URL}/flow", params={"code": code}, timeout=12)
            r.raise_for_status()
            d = r.json()
            if d.get("status") != "success":
                return xs, None, False
            # 장 마감(15:40, KIS FHPTJ04160001 시간제한 해제) 후엔 허브가 daily 에 일별
            # '확정' 수급을 실어준다 — 당일 확정 행이 있으면 가집계(잠정)보다 우선
            # (사용자 요청 2026-07-31: 마감 회차는 최종 집계 반영). 단위·필드 동일(백만원,
            # frgn/orgn/fund). 장중·허브 구버전·미집계 시 daily 빈 배열 → 가집계 폴백.
            d0 = (d.get("daily") or [None])[0]
            if d0 and str(d0.get("date")) == today_c:
                return xs, {"frgn": d0.get("frgn"), "orgn": d0.get("orgn"),
                            "fund": d0.get("fund")}, True
            rank = d.get("rank") or []
            if rank:
                r0 = rank[0]     # frgn/orgn/fund 는 종목 순매수 대금(리스트 무관 동일 값)
                return xs, {"frgn": r0.get("frgn"), "orgn": r0.get("orgn"),
                            "fund": r0.get("fund")}, False
            return xs, None, False
        except Exception:
            return xs, None, False

    n, n_final = 0, 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        for xs, nb, is_final in pool.map(_one, by_code.items()):
            if nb and any(v is not None for v in nb.values()):
                for x in xs:
                    x["netBuy"] = nb
                    x["_nbFinal"] = is_final
                n += 1
                n_final += 1 if is_final else 0
    # 섹터 단위 확정 라벨 — 부착된 종목이 전부 확정일 때만(혼재 시 가집계 표기 유지)
    for s in sectors_up + sectors_down:
        got = [x for x in (s.get("stocks") or []) if x.get("netBuy")]
        if got and all(x.get("_nbFinal") for x in got):
            s["netBuyFinal"] = True
        for x in (s.get("stocks") or []):
            x.pop("_nbFinal", None)
    print(f"  섹터 가집계(외국인/기관/연기금): {n}/{len(by_code)}종목 부착 (확정 {n_final})")


def _load_us_context():
    """모닝브리핑(public/briefing/latest.json)에서 전일 미국장 컨텍스트 발췌 —
    장중 섹터 랠리가 미국장의 연장인 경우(예: 필라델피아 반도체 급등 → 국내 반도체
    장비주 강세)를 LLM 이 연결할 수 있게 한다. 없으면 {} (graceful)."""
    try:
        with open(os.path.join(ROOT, "public", "briefing", "latest.json"),
                  encoding="utf-8") as f:
            d = json.load(f) or {}
        sectors = ((d.get("usMarket") or {}).get("sectors") or [])
        sec = [{"name": s.get("name"), "changePct": s.get("changePct")}
               for s in sectors if s.get("name")]
        sec.sort(key=lambda s: -(s["changePct"] or 0))
        return {
            "date": d.get("date"),
            "bullets": ((d.get("usReview") or {}).get("bullets") or [])[:4],
            "sectorsUp": sec[:3],
            "sectorsDown": sec[-3:][::-1] if len(sec) >= 3 else [],
        }
    except Exception as e:
        _warn(f"미국장 컨텍스트 로드 실패: {e}")
        return {}


def _load_econ_events(now):
    """경제지표 캘린더(public/econ_calendar.json, 매 회차 fetch_econ_calendar.py 로 갱신)에서
    장중 관점 3구획 발췌 — korReleasedToday(오늘 발표된 한국 지표 결과),
    korTodayUpcoming(오늘 남은 한국 발표 예정 — 예: 12:00 M2 통화공급),
    usTonight(오늘 밤 미국 발표 예정). 없으면 {} (graceful)."""
    try:
        with open(os.path.join(ROOT, "public", "econ_calendar.json"),
                  encoding="utf-8") as f:
            cal = json.load(f) or {}
    except Exception as e:
        _warn(f"econ_calendar.json 로드 실패(경제지표 생략): {e}")
        return {}
    today = now.strftime("%Y-%m-%d")
    tonight_from = f"{today} 15:30"
    tonight_to = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d") + " 08:00"

    def pick(rows, limit=8):
        rows = sorted(rows, key=lambda r: (-(r.get("importance") or 0),
                                           r.get("releaseAtKST") or ""))
        return sorted(rows[:limit], key=lambda r: r.get("releaseAtKST") or "")

    def ok(r):
        # 중요도 1(낮음)도 표시(2026-07-22 사용자 결정) — LLM 입력은 _econ_llm_view 가
        # 중요도 2+ 만 추려 관전포인트 오염을 막는다(표시/LLM 분리).
        return (r.get("importance") or 0) >= 1 and r.get("releaseAtKST")

    now_str = now.strftime("%Y-%m-%d %H:%M")
    ev = {
        "korReleasedToday": pick([r for r in cal.get("released") or []
                                  if ok(r) and r.get("nation") == "KOR"
                                  and r["releaseAtKST"][:10] == today]),
        # 오늘 남은 한국 발표 예정 — 캘린더 파일이 회차마다 갱신돼 발표되면 released 로
        # 넘어가지만, fetch 실패로 파일이 낡았을 수도 있어 현재 시각 이후만 남긴다.
        "korTodayUpcoming": pick([r for r in cal.get("upcoming") or []
                                  if ok(r) and r.get("nation") == "KOR"
                                  and r["releaseAtKST"][:10] == today
                                  and r["releaseAtKST"] >= now_str]),
        "usTonight": pick([r for r in cal.get("upcoming") or []
                           if ok(r) and r.get("nation") == "USA"
                           and tonight_from <= r["releaseAtKST"] <= tonight_to]),
    }
    if not any(ev.values()):
        # 조용한 날 폴백 — 2026-07-20(발표 0건)·07-21(중요도1 1건) 실측: 3구획이 모두
        # 비면 카드가 통째로 사라져 고장처럼 보였다. 향후 48시간 내 예정 지표를
        # '다가오는 지표'로 담아 카드가 항상 정보를 준다.
        soon_to = (now + datetime.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M")
        ev = {"upcomingSoon": pick([r for r in cal.get("upcoming") or []
                                    if ok(r)
                                    and now_str <= r["releaseAtKST"] <= soon_to])}
    # 관세청 수출입 통계(월간 + 수입 10일 잠정) — 발표(수집) 당일만 카드에 표기
    # (2026-07-23 사용자 결정: 상시 이월 표시 → 당일자만). LLM 게이트와 동일 기준.
    trade = cal.get("trade")
    if trade and str(trade.get("asof", ""))[:10] == today:
        ev["trade"] = trade
    return ev if any(ev.values()) else {}


def _load_earnings_events(now):
    """실적발표 캘린더(public/earnings_calendar.json, fetch_earnings_calendar.py 갱신)에서
    장중 관점 2구획 발췌 — releasedToday(오늘 발표 결과)와 upcomingSoon(오늘 발표 예정).
    당일 발표 + '전일 장 마감 후 발표돼 오늘 처음 포착된' 지연 실적(capturedDate==today,
    late 플래그)까지 표기 — 전일 마감후 실적도 익일 주가에 영향을 주므로 하루 이월.
    표시 전용(LLM 미전달 — 환각 차단). 없으면 {}."""
    try:
        with open(os.path.join(ROOT, "public", "earnings_calendar.json"),
                  encoding="utf-8") as f:
            cal = json.load(f) or {}
    except Exception as e:
        _warn(f"earnings_calendar.json 로드 실패(실적 캘린더 생략): {e}")
        return {}
    today = now.strftime("%Y-%m-%d")

    # date==today(당일 발표) + 직전 거래일 마감 후(15:30+) 발표분 이월 — 익일 개장 관점에서
    # 전일 마감후 실적이 오늘 시초에 영향을 주므로 '전일' 칩으로 하루 더 노출한다. 이월 대상 2갈래:
    #   ⓐ capturedDate==today: 전일 마감 회차(15:40) 이후 발표돼 오늘 아침 처음 포착(예: 17:10).
    #   ⓑ date==prev_bd & time>=15:30: 전일 마감 회차가 당일 즉시 포착한 마감후 발표(예: 15:36
    #      우리금융지주 — capturedDate=전일이라 ⓐ에 안 걸려 익일에 누락되던 것, 2026-07-27).
    # 직전 거래일만(월요일은 금요일까지 소급) — 며칠 지난 실적이 '전일'로 쏟아지는 것 방지.
    # 공휴일 연휴는 미반영(드묾). if/elif 라 한 행이 오늘·이월 양쪽에 중복 편입되지 않는다.
    prev_bd = (now - datetime.timedelta(days=3 if now.weekday() == 0 else 1)) \
        .strftime("%Y-%m-%d")
    rel = []
    for r in cal.get("released") or []:
        if r.get("date") == today:
            rel.append(r)
        elif r.get("capturedDate") == today and (r.get("date") or "") >= prev_bd:
            rel.append({**r, "late": True})       # ⓐ 익일 첫 포착 (마감 회차 이후 발표)
        elif r.get("date") == prev_bd and (r.get("time") or "") >= "15:30":
            rel.append({**r, "late": True})       # ⓑ 전일 마감후(15:30+) 발표 — 당일 포착분도 이월
    # 건수 리밋 없음(2026-07-24 사용자 결정) — 상위 8건 컷이 장중 발표 누적 시 정상
    # 발표·전일 이월을 밀어내 사라지게 하던 문제(멀티캠퍼스·두산퓨얼셀류). 유니버스
    # 필터도 도입 안 함(whitelist 누락 재발 우려). 정렬 = 발표시간 순: 발표일 → 공시
    # 접수시각(오름차순, 전일→오늘·오전→오후 시계열). 시각 미상은 날짜 그룹 끝.
    rel.sort(key=lambda r: (r.get("date") or "", r.get("time") or "99:99",
                            r.get("name") or ""))

    up = [u for u in cal.get("upcoming") or [] if (u.get("date") or "") == today]
    up.sort(key=lambda u: -(((u.get("consensus") or {}).get("op")) or 0))

    ev = {}
    if rel:
        ev["releasedToday"] = rel
    if up:
        ev["upcomingSoon"] = up
    return ev


def _econ_llm_view(econ):
    """LLM 입력용 축약 — 수치 재생성 유인을 줄이기 위해 필요한 필드만."""
    def slim(r):
        out = {"name": r.get("name"), "importance": r.get("importance"),
               "releaseAtKST": r.get("releaseAtKST"),
               "previous": r.get("previous"), "forecast": r.get("forecast"),
               "unit": r.get("unit") or r.get("unitScale") or ""}
        if r.get("actual") is not None:
            out["actual"] = r["actual"]
        if r.get("surprise"):
            out["surpriseVerdict"] = r["surprise"].get("verdict")
            out["surpriseVs"] = r["surprise"].get("vs")   # forecast=예상 대비 | previous=이전 대비
        return out
    # 표시는 중요도 1+ 이지만 LLM 에는 2+ 만 — 모기지 재융자지수류 소음 지표가
    # 관전포인트 서술을 오염시키지 않게 표시/LLM 입력을 분리(2026-07-22).
    out = {}
    for k, v in econ.items():
        if not isinstance(v, list):
            continue                      # trade(dict)는 아래에서 별도 처리
        rows = [slim(r) for r in v if (r.get("importance") or 0) >= 2]
        if rows:
            out[k] = rows
    # 관세청 수출입 — 수집 당일(발표일)만 LLM 에 전달(상시 주입은 관전포인트 오염).
    # 카드 표시는 econ["trade"] 원본이 상시 담당.
    t = econ.get("trade")
    if t and str(t.get("asof", ""))[:10] == datetime.datetime.now(KST).strftime("%Y-%m-%d"):
        out["tradeStats"] = {k: t[k] for k in ("monthly", "monthlyItems", "impTenday")
                             if t.get(k)}
    return out


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
    "**모든 출력 문자열(briefing·fxBullets·catalysts.summary 등 전부)은 반드시 한국어로 "
    "작성한다 — 영어 문장 금지(종목명·티커 등 고유명사만 예외).** "
    "제공된 RAW(지수, 투자자별 순매수 금액, 환율·금리·원자재 시장지표, 급등/급락 테마와 "
    "구성종목·편입사유, 급등/급락 개별종목, 당일 공시, 특징주 뉴스)만 근거로 장중 시황 "
    "브리핑을 작성하세요.\n"
    "규칙:\n"
    "- 데이터에 없는 수치·사실을 지어내지 말 것. 등락률 등 수치는 입력값을 인용할 것.\n"
    "- marketIndicators 각 항목의 chg 는 전일대비 '부호 포함' 변화량, trend 는 방향이다. "
    "상승/하락 표현은 반드시 chg 부호·trend 를 그대로 따를 것 — 값의 절대 수준(예: 환율 "
    "1,480원대)이 높다는 이유로 '상승'이라고 쓰지 말 것(chg 가 음수면 반드시 '하락').\n"
    "- briefing 은 3~5개의 완결된 문장(각각이 대시보드 불릿 하나): ① 지수 흐름과 수급 주체"
    "(investors — 기관/외국인/개인 순매수, 단위 억원)를 함께, ② 환율·금리·유가(marketIndicators)가 "
    "시장에 주는 함의, ③ 주도 섹터(sectorsUp), ④ 약세 섹터(sectorsDown), ⑤ 관전 포인트 순으로.\n"
    "  · **수급 금액을 쓸 때는 investorsFmt(시장별로 이미 완성된 억원 표기, 예: '외국인 "
    "-46,494억(=-4.6조)')를 글자 그대로 인용한다. 자릿수를 임의로 줄이거나(÷10 등) 늘리거나 "
    "재계산하지 말 것 — 조 표기는 1조=10,000억(46,494억=약 4.6조).**\n"
    "- ①의 수급은 investorFlow(1분 누적 순매수 요약 — now 현재 누적(억원), chg30m 최근 "
    "30분 변화량, flipAt 그 시각부터 현재 부호 유지)로 방향 '전환·가속'까지 서술할 것 "
    "(예: '외국인은 13:24 순매수 전환 후 매수 폭을 키우는 중'). 전환·변화가 뚜렷할 때만 언급.\n"
    "- ③④는 등락률 나열이 아니라 '동인'을 서술한다: 각 섹터의 stocks(관련주별 changePct 와 "
    "flow — date 집계일의 외국인/기관 순매수 수량(주), 장중엔 전일 확정치일 수 있음)로 어느 "
    "종목이 끌어올리는지/끌어내리는지, 그 '이유(오늘의 촉매)'가 무엇인지를 근거와 함께 명시할 것.\n"
    "  · **원인은 stocks[].newsHeadlines(상위 종목 당일 뉴스)를 최우선 근거로 서술한다.** "
    "themes[].name(네이버 테마명)은 종목의 '소속 분류'일 뿐 '오늘 오른 이유'가 아니다 — "
    "테마명(예: GTX·원자력)만 보고 원인으로 단정하지 말 것. newsHeadlines 에 실제 촉매(예: "
    "AI 데이터센터·데이터센터 인프라·수주·정책·증설)가 있으면 반드시 그 촉매를 원인으로 쓰고, "
    "테마명은 부차적으로만 언급한다. 뉴스 근거가 전혀 없을 때만 themes 의 reasons·수급으로 서술한다.\n"
    "  · netBuy5(섹터 상위 종목의 외국인/기관/연기금 순매수 대금 합, 억원·부호)가 있으면 "
    "'누가 사서/팔아' 섹터가 움직이는지 수급 주체를 서술에 반영할 것(예: '외국인이 순매수하며 "
    "견인' — 양수=순매수, 음수=순매도, 수치 인용). 종목수는 가집계 랭킹에 든 상위 종목 수다.\n"
    "  · usContext(전일 미국장 리뷰·섹터)와 이어지는 흐름인지도 함께 볼 것. "
    "제공 데이터에 없는 원인을 추정해 지어내지 말 것.\n"
    "- catalysts 는 입력 공시(id: d0,d1,..)·뉴스(id: n0,n1,..) 중 시장 영향이 큰 것을 '종목 단위'로 "
    "정리한다(방향별 — 상방·중립·하방 각각 최대 10건 — 서로 다른 종목을 최대한 많이 포괄):\n"
    "  · 한국(KOSPI/KOSDAQ) 상장 종목만 포함하고 해외 종목·지수·ETF(예: 테슬라·엔비디아 등)는 제외한다.\n"
    "  · 같은 종목이 공시·뉴스에 중복 등장하면 반드시 하나로 합쳐 한 줄로 요약할 것(같은 종목 중복 금지).\n"
    "  · 각 항목 필드: id(대표 출처 1개만, 공시가 있으면 공시 우선), stock(종목명), "
    "market(한국 종목이면 반드시 KOSPI 또는 KOSDAQ 로 채울 것 — 비우지 말 것; 해외 종목은 애초에 넣지 말 것), "
    "direction(그 촉매가 주가에 주는 방향 — 상방=bullish|중립=neutral|하방=bearish), summary(핵심 촉매 한 문장).\n"
    "  · 뉴스는 corp 필드가 없으니 제목·본문에서 종목명을 추출해 stock 에 넣을 것.\n"
    "  · stock 은 반드시 실제 상장 '개별 종목명'이어야 한다 — '~테마주'·'~관련주'·업종/테마 "
    "묶음(예: 한동훈 테마주, 2차전지 관련주)은 종목이 아니다. 기사에 대표 개별 종목이 명시된 "
    "경우에만 그 종목명으로 넣고, 개별 종목이 없으면 그 기사는 촉매에서 제외할 것.\n"
    "- econEvents 가 있으면: korReleasedToday(오늘 발표된 한국 경제지표)는 ① 지수·수급 서술의 "
    "배경으로, korTodayUpcoming(오늘 남은 한국 발표 예정)과 usTonight(오늘 밤 미국 발표 예정 지표)은 "
    "⑤ 관전 포인트에 반영할 것(수치는 입력값을 그대로 인용). 당일 지표가 없는 날의 폴백인 "
    "upcomingSoon(향후 48시간 예정 지표)이 있으면 ⑤ 관전 포인트에 다가오는 일정으로 언급할 것. "
    "tradeStats(관세청 수출입 발표 — monthly: 전월 수출/수입/무역수지 백만$ 와 YoY%, "
    "monthlyItems: 품목별 수출 YoY, impTenday: 이달 수입 10일 단위 잠정치)가 있으면 "
    "① 배경 또는 ⑤ 관전 포인트에 반도체 등 품목 흐름과 함께 반영할 것(수치 그대로 인용). "
    "surpriseVerdict(above=상회|inline=부합|below=하회)는 surpriseVs 기준을 구분해 "
    "표현할 것 — surpriseVs=forecast 면 '예상(컨센서스) 상회/하회', surpriseVs=previous 면 반드시 "
    "'이전(직전치) 대비 상회/하회'로 쓰고, 이전값 대비를 '예상치 상회/하회'라고 표현하지 말 것.\n"
    "- fxBullets 는 환율 관련 1~3개 불릿(fxNews·marketIndicators.exchange/world 근거): "
    "원/달러·달러인덱스·엔/달러 흐름과 그 배경(뉴스 근거), 국내 증시(수출주·환율 민감주)에 주는 "
    "함의를 담을 것. 근거 데이터가 전혀 없으면 빈 배열.\n"
    "- regime: 모의투자 엔진이 소비하는 '지금부터 장 마감까지'의 시장 국면 판정. "
    "지수(indices.rate)·투자자 수급(investors, investorFlow 의 방향·전환)·섹터 상하위 분포·"
    "환율/지표(marketIndicators)·촉매(catalysts 방향 분포)를 종합한다. "
    "가이드: 양 지수 -1% 이상 하락 + 외국인 순매도 지속 + 하락 섹터 우위 같은 복수 근거가 "
    "겹치면 stance=risk_off, 반대로 강한 동반 상승·수급 유입이면 risk_on, 그 외 neutral. "
    "confidence 는 근거가 뚜렷하고 상충 신호가 없을 때만 high — risk_off/high 는 보유 포지션 "
    "전량 청산을 유발하므로 **확신이 없으면 반드시 neutral 또는 confidence=low** 를 택한다. "
    "reason 은 판정 근거 한 문장(수치 인용).\n"
    "- 한국어. 출력은 지정된 JSON 스키마를 엄격히 따를 것."
)


def _mechanical_regime(indices):
    """LLM 부재/실패/누락 시 결정적 국면 폴백 — 양 지수 평균 등락률 기준(항상 low)."""
    try:
        chgs = [float((indices.get(k) or {}).get("rate"))
                for k in ("kospi", "kosdaq")
                if (indices.get(k) or {}).get("rate") is not None]
        avg = sum(chgs) / len(chgs) if chgs else 0.0
    except Exception:
        avg = 0.0
    stance = "risk_off" if avg <= -1.0 else ("risk_on" if avg >= 1.0 else "neutral")
    return {"stance": stance, "confidence": "low",
            "reason": f"기계적 판정 — 코스피·코스닥 평균 {avg:+.2f}%"}


# ── regime 후검증(2026-08-04) — v9 크기 게이트의 안전판을 판정 생산지로 이식 ──────
# LLM regime 을 소비하는 쪽이 대시보드 모의투자(전량청산)에 더해 v9 실매매(방향 필터)로
# 늘어나 오판 비용이 커짐 — 가격·수급 결정적 신호로 confidence 강등/stance 승격을 걸어
# 모든 소비자가 공통 수혜를 받게 한다.
REGIME_DEMOTE = os.environ.get("REGIME_DEMOTE", "1") != "0"          # 모순 시 high→low 강등
REGIME_EXTREME_PCT = float(os.environ.get("REGIME_EXTREME_PCT", "2.5"))  # 극단 승격 임계(%)
REGIME_PROMOTE_HIGH = os.environ.get("REGIME_PROMOTE_HIGH", "0") == "1"  # 승격 시 high 부여(기본 OFF)


def _finalize_regime(llm_regime, indices, investors):
    """LLM regime -> 검증·강등·승격·signals 부착한 최종 regime (순수 함수 — 테스트 대상).

    ① 검증: stance enum 밖/누락 → _mechanical_regime 폴백(기존 동작 유지).
    ② 모순 강등(v9 모순 게이트 이식): risk_off/high 인데 양 지수 평균이 상승이거나
       KOSPI (외국인+기관) 순매수가 양수 — 가격·수급이 판정과 반대면 high→low.
       (high 는 모의투자 전량청산·v9 방향 필터 발동 조건 — 강등으로 강제만 막고
        stance 는 유지해 매수 차단 등 저비용 방어는 살린다.) risk_on/high 대칭.
    ③ 극단 승격(v9 ★D안 '가격 권위' 이식): 양 지수 평균 |등락| ≥ REGIME_EXTREME_PCT 면
       LLM 이 뭐라 했든 그 방향 stance 로 승격 — LLM 빈응답·언어드리프트·기계폴백 회차
       방어. 수급이 '적극 반대'(방향과 역부호)면 승격 보류(휩소 방어). confidence 는
       low 유지(REGIME_PROMOTE_HIGH=1 일 때만 high — 전량청산 트리거라 기본 OFF).
    ④ signals: 판정 근거 수치·강등/승격 플래그 동봉(배지 툴팁·v9 로그 공용 관측성)."""
    rg = (llm_regime if str((llm_regime or {}).get("stance"))
          in ("risk_on", "neutral", "risk_off") else _mechanical_regime(indices))
    out = {"stance": rg["stance"],
           "confidence": rg.get("confidence") if rg.get("confidence") in ("high", "low") else "low",
           "reason": str(rg.get("reason") or "")[:200]}
    try:
        chgs = [float((indices.get(k) or {}).get("rate"))
                for k in ("kospi", "kosdaq")
                if (indices.get(k) or {}).get("rate") is not None]
        idx_avg = round(sum(chgs) / len(chgs), 2) if chgs else None
    except Exception:
        idx_avg = None
    iv = (investors or {}).get("kospi") or {}
    f_eok, i_eok = iv.get("foreign"), iv.get("institution")
    flow_sum = (f_eok + i_eok) if (f_eok is not None and i_eok is not None) else None
    demoted = promoted = False
    # ② 모순 강등 — 실측 데이터가 실제로 반대 방향일 때만(결측은 강등 근거 아님)
    if REGIME_DEMOTE and out["confidence"] == "high":
        contra_off = (out["stance"] == "risk_off"
                      and ((idx_avg is not None and idx_avg > 0)
                           or (flow_sum is not None and flow_sum > 0)))
        contra_on = (out["stance"] == "risk_on"
                     and ((idx_avg is not None and idx_avg < 0)
                          or (flow_sum is not None and flow_sum < 0)))
        if contra_off or contra_on:
            out["confidence"] = "low"
            demoted = True
            out["reason"] = (out["reason"] + f" [강등: 지수평균 {idx_avg}% · "
                             f"외인+기관 {flow_sum}억 상충]")[:280]
    # ③ 극단 승격 — 가격 권위. 수급이 방향과 역부호로 '적극 반대'면 보류
    if idx_avg is not None and abs(idx_avg) >= REGIME_EXTREME_PCT:
        want = "risk_off" if idx_avg < 0 else "risk_on"
        flow_contra = (flow_sum is not None
                       and ((want == "risk_off" and flow_sum > 0)
                            or (want == "risk_on" and flow_sum < 0)))
        if out["stance"] != want and not flow_contra:
            out["stance"] = want
            out["confidence"] = "high" if REGIME_PROMOTE_HIGH else "low"
            promoted = True
            out["reason"] = (f"극단 승격 — 지수평균 {idx_avg:+.2f}% "
                             f"(임계 {REGIME_EXTREME_PCT}%). " + out["reason"])[:280]
    out["signals"] = {"idxAvg": idx_avg, "flowSum": flow_sum,
                      "demoted": demoted, "promoted": promoted}
    return out

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
        "briefing": {"type": "array", "minItems": 3, "items": _STR},
        "fxBullets": {"type": "array", "items": _STR},
        "catalysts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": _STR, "stock": _STR, "market": _STR,
                "direction": {"type": "string", "enum": ["bullish", "neutral", "bearish"]},
                "summary": _STR},
            "required": ["id", "stock", "direction", "summary"]}},
        # 모의투자 국면 연동(2026-07-23): LLM 이 판단, 엔진이 결정적 룰로 집행
        "regime": {
            "type": "object",
            "properties": {
                "stance": {"type": "string", "enum": ["risk_on", "neutral", "risk_off"]},
                "confidence": {"type": "string", "enum": ["high", "low"]},
                "reason": _STR},
            "required": ["stance", "confidence", "reason"]},
    },
    "required": ["briefing", "catalysts", "regime"],
}


def _norm(s):
    return "".join(ch for ch in str(s or "") if ch not in " \t·ㆍ・")


def _sector_raw(s):
    """섹터 → LLM raw. 관련주는 등락률·수급(enrich_sector_stocks)까지, 테마는
    구성종목 등락률·편입사유(reasons)까지 — 섹터 등락의 '동인' 서술 근거."""
    def _stock(x):
        d = {"name": x["name"]}
        if x.get("changePct") is not None:
            d["changePct"] = x["changePct"]
        if x.get("flow"):
            d["flow"] = x["flow"]      # {date, foreign, institution} 순매수 수량(주)
        if x.get("news"):
            d["newsHeadlines"] = x["news"]   # 당일 뉴스(상위 종목) — 섹터 '원인' 서술 근거
        if x.get("netBuy"):            # 당일 가집계 순매수 대금(억원) — 수급 주체 근거
            d["netBuyEok"] = {k: round(v / 100, 1) for k, v in x["netBuy"].items()
                              if v is not None}
        return d
    # 섹터 상위 종목 가집계 합계(억원, 데이터 있는 종목만) — LLM 이 '누가 사서/팔아'를 서술
    tops = (s.get("stocks") or [])[:SECTOR_NETBUY_TOP]
    nb = [x["netBuy"] for x in tops if x.get("netBuy")]
    out = {
        "name": s["name"], "changePct": s["changePct"],
        "stocks": [_stock(x) for x in (s.get("stocks") or [])],
        "themes": [{"name": t["name"], "changePct": t.get("changePct"),
                    "stocks": [f"{x['name']} {x.get('changePct')}%"
                               for x in (t.get("stocks") or [])[:3]],
                    "reasons": t.get("reasons") or []}
                   for t in (s.get("themes") or [])],
    }
    if nb:
        out["netBuy5"] = {
            "외국인": round(sum((d.get("frgn") or 0) for d in nb) / 100, 1),
            "기관": round(sum((d.get("orgn") or 0) for d in nb) / 100, 1),
            "연기금": round(sum((d.get("fund") or 0) for d in nb) / 100, 1),
            "종목수": len(nb),
        }
    return out


def _ind_llm_view(x):
    """시장지표 항목 → LLM 입력용 정규화. 원본은 change 가 '절대값' + direction 분리
    형태라 LLM 이 양수만 보고 "+8.5원 상승"으로 오독하는 사고가 잦았다(2026-07-20
    환율 ▼8.5 를 상승으로) — 부호를 결정적으로 복원(chg)하고 방향 단어(trend)를
    동봉해 모호성을 없앤다. 타일 UI 는 원본 direction 을 쓰므로 영향 없음."""
    sign = -1 if x.get("direction") == "down" else 1
    chg = x.get("change")
    return {"name": x.get("name"), "value": x.get("value"),
            "chg": (sign * chg) if isinstance(chg, (int, float)) else None,
            "trend": {"up": "상승", "down": "하락"}.get(x.get("direction"), "보합")}


def synthesize(indices, investors, indicators, sectors_up, sectors_down,
               movers_up, movers_down, disclosures, news, fx_news,
               prev_rounds=None, us_context=None, investor_flow=None,
               econ_events=None):
    # 시장지표는 핵심만 추려 LLM 에 전달 (COFIX 등 저관련 항목 제외).
    core_ind = {}
    if indicators:
        core_ind = {
            "exchange": [_ind_llm_view(x) for x in indicators.get("exchange", [])
                         if any(k in x["name"] for k in ("USD", "JPY", "EUR"))],
            "world": [_ind_llm_view(x) for x in indicators.get("world", [])
                      # 네이버 국제환율 라벨: 달러인덱스, '달러/일본 엔'(=USD/JPY, 엔/달러 환율)
                      if any(k in x["name"] for k in ("달러인덱스", "일본 엔"))],
            "rates": [_ind_llm_view(x) for x in indicators.get("rates", [])
                      if any(k in x["name"] for k in ("CD", "국고채", "회사채"))],
            "commodities": [_ind_llm_view(x) for x in indicators.get("commodities", [])
                            if any(k in x["name"] for k in ("WTI", "국제 금"))],
        }
    raw = {
        "indices": indices,
        "investors": investors,          # 억원 단위 순매수 (개인/외국인/기관)
        "investorsFmt": _fmt_investors(investors),   # LLM 이 그대로 인용할 억원 표기(자릿수 오류 방지)
        "marketIndicators": core_ind,
        "sectorsUp": [_sector_raw(s) for s in sectors_up],
        "sectorsDown": [_sector_raw(s) for s in sectors_down],
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
    if us_context:
        raw["usContext"] = us_context           # 전일 미국장 리뷰·섹터 (모닝브리핑 발췌)
    if investor_flow:
        raw["investorFlow"] = investor_flow     # 1분 수급 시계열 요약(전환·가속)
    if econ_events:
        raw["econEvents"] = _econ_llm_view(econ_events)   # 오늘 한국 지표 결과 + 오늘 밤 미국 예정
    system = SYSTEM
    if prev_rounds:
        raw["previousRounds"] = prev_rounds     # 마감 시황 — 당일 회차별 다이제스트
        system = SYSTEM + FINAL_ADDENDUM
    user = "RAW 데이터(JSON):\n" + json.dumps(raw, ensure_ascii=False)
    # max_tokens 는 Gemini 3 의 thinking 토큰과 출력이 나눠 쓰는 예산 — 마감 회차
    # (previousRounds 12건 + 5~7문장 확장)에서 4096 이 잘려 빈/불완전 JSON 이 나온
    # 사례(2026-07-13)가 있어 8192 로 상향. 파싱은 됐지만 briefing 이 빈 응답
    # (잘린 출력이 우연히 유효 JSON)은 실패로 간주하고 1회 재시도한다.
    synth, model = {}, None
    if llm.configured():
        for attempt in (1, 2):
            try:
                synth, model = llm.generate_json(system, user, max_tokens=8192,
                                                 schema=SCHEMA, return_model=True)
            except Exception as e:
                _warn(f"LLM 종합 실패: {e} — 기계적 결과만 산출")
                break
            retry_word = "재시도" if attempt == 1 else "기계적 결과만 산출"
            if not synth.get("briefing"):
                _warn(f"{model} 응답에 briefing 없음 — {retry_word}")
                synth = {}
                continue
            # 언어 검증: 경량 폴백 모델(flash-lite)이 낮은 확률로 프롬프트의 한국어
            # 지시를 무시하고 영어로 출력한 실측(2026-07-21 12:45) — 한글보다 영문이
            # 많으면 언어 드리프트로 간주하고 재생성한다(산출물에 영어 회차 방지).
            txt = " ".join(str(x) for x in
                           list(synth.get("briefing") or []) + list(synth.get("fxBullets") or []))
            han = len(re.findall(r"[가-힣]", txt))
            lat = len(re.findall(r"[A-Za-z]", txt))
            if han < lat:
                _warn(f"{model} 출력 언어 드리프트(한글 {han} < 영문 {lat}자) — {retry_word}")
                synth = {}
                continue
            break
    else:
        _warn("LLM 미설정 — 기계적 결과만 산출")

    # 촉매: 종목 단위(중복 통합) + 방향(상방/중립/하방). id → 원문 URL 매칭(공시 dN / 뉴스 nN).
    # 방향별 상한 10건(상방·중립·하방 각각) — 한 방향이 표를 독점하지 않도록.
    catalysts, seen, dir_count = [], set(), {"bullish": 0, "neutral": 0, "bearish": 0}
    for c in (synth.get("catalysts") or []):
        cid = str(c.get("id") or "")
        stock = (c.get("stock") or "").strip()
        # 결정적 필터: 테마 묶음명은 종목이 아님 — 프롬프트 금지에도 LLM 이 넣은 사례
        # ('한동훈 테마주' 2026-07-23) 재발을 구조적으로 차단.
        if re.search(r"(테마주|관련주|수혜주|테마)$", stock):
            continue
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

    # 시황 불릿 — 재시도까지 실패하면 수집 수치로 기계적 요약(대시보드 공백 방지)
    briefing = [b.strip() for b in (synth.get("briefing") or []) if b and b.strip()]
    if not briefing:
        briefing = _mechanical_briefing(indices, investors, sectors_up, sectors_down,
                                        bool(prev_rounds))
    return briefing, fx_bullets, catalysts, model, synth.get("regime") or {}


def _mechanical_briefing(indices, investors, sectors_up, sectors_down, final):
    """LLM 전 링크 실패/빈 응답 시 폴백 — 수집 데이터만으로 최소 시황 불릿 구성."""
    out = []
    seg = []
    for nm, k in (("코스피", "kospi"), ("코스닥", "kosdaq")):
        v = (indices or {}).get(k) or {}
        if isinstance(v.get("price"), (int, float)) and v.get("rate") is not None:
            seg.append(f"{nm} {v['price']:,.2f}({v['rate']:+.2f}%)")
    if seg:
        out.append(" · ".join(seg) + (" 마감." if final else " 진행 중."))
    iv = (investors or {}).get("kospi") or {}
    if iv.get("individual") is not None:
        out.append("코스피 수급(억원): 개인 {:+,.0f} · 외국인 {:+,.0f} · 기관 {:+,.0f}.".format(
            iv.get("individual") or 0, iv.get("foreign") or 0, iv.get("institution") or 0))
    for label, secs in (("상승 섹터", sectors_up), ("하락 섹터", sectors_down)):
        names = [f"{s['name']}({s['changePct']:+.2f}%)" for s in (secs or [])[:3]
                 if s.get("name") and s.get("changePct") is not None]
        if names:
            out.append(f"{label}: " + " · ".join(names))
    if out:
        out.append("※ 이번 회차는 LLM 요약 실패로 수치 요약만 표시됩니다.")
    return out


# ----------------------------------------------------------------------------
# Phase 3) 산출 + 아카이브
# ----------------------------------------------------------------------------
def main():
    print("=== Intraday Market Briefing ===")
    now = datetime.datetime.now(KST)
    _t_collect = time.time()

    def _timed(label, fn, *a):
        t0 = time.time()
        try:
            return fn(*a)
        finally:
            print(f"  [timing] {label} {time.time() - t0:.1f}s")

    # 수집 병렬화(2026-07-27): 원래 13개 수집 호출이 전부 직렬이라 수집 단계만 3~5분 —
    # 상호 파일/상태 공유가 없는 9건을 동시 실행한다(각자 내부 예외 처리·graceful empty,
    # sources 함수들은 attach_* 의 기존 TP8 로 이미 동시 사용 실적 있음). 워커 6 =
    # 네이버/DART 레이트리밋 보수 고려. 섹터 체인(themes→enrich→news∥netbuy)만
    # fetch_sectors 결과에 순서 의존이라, sectors 완료 즉시 메인 스레드에서 이어 돌린다
    # (남은 독립 수집과 자연히 겹침). news/netbuy 는 상호 독립이라 마지막에 동시 실행.
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {
            "indices":     pool.submit(_timed, "indices", fetch_indices),
            "investors":   pool.submit(_timed, "index_investors", sources.naver_index_investors),
            "indicators":  pool.submit(_timed, "market_indicators", sources.naver_market_indicators),
            "sectors":     pool.submit(_timed, "sectors", fetch_sectors),
            "movers":      pool.submit(_timed, "movers", fetch_movers),
            "disclosures": pool.submit(_timed, "disclosures",
                                       lambda: sources.dart_today_disclosures(limit=40)),
            "news":        pool.submit(_timed, "news", fetch_news),
            "fx_news":     pool.submit(_timed, "fx_news", fetch_fx_news),
            "inv_series":  pool.submit(_timed, "investor_series", fetch_investor_series),
        }
        sector_heat, sectors_up, sectors_down = futs["sectors"].result()  # KIS 업종 섹터 히트
        _timed("sector_themes", attach_sector_themes, sectors_up, sectors_down)   # 섹터→테마·종목
        _timed("sector_enrich", enrich_sector_stocks, sectors_up, sectors_down)   # 관련주 등락·수급
        f_news = pool.submit(_timed, "sector_news",
                             attach_sector_stock_news, sectors_up, sectors_down)  # 상위 종목 뉴스
        f_netbuy = pool.submit(_timed, "sector_netbuy",
                               attach_sector_stock_netbuy, sectors_up, sectors_down)  # 가집계
        f_news.result()
        f_netbuy.result()
        indices = futs["indices"].result()
        investors = futs["investors"].result()            # 기관/외인/개인 순매수(억원)
        indicators = futs["indicators"].result()          # 환율·금리·유가·금
        movers_up, movers_down = futs["movers"].result()
        disclosures = futs["disclosures"].result()
        news = futs["news"].result()
        fx_news = futs["fx_news"].result()                # 환율 관련 뉴스(원달러·환율)
        investor_series = futs["inv_series"].result()     # 1분 투자자 누적 순매수(이월+증분)
    investor_flow = _flow_trend(investor_series)          # LLM 용 요약(전환·가속)
    print(f"  [timing] collect-total {time.time() - _t_collect:.1f}s")
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

    econ_events = _load_econ_events(now)
    earnings_events = _load_earnings_events(now)
    if earnings_events:
        print(f"  실적발표: 결과 {len(earnings_events.get('releasedToday') or earnings_events.get('releasedRecent') or [])}건, "
              f"예정 {len(earnings_events.get('upcomingSoon', []))}건")
    if econ_events:
        print(f"  경제지표: 한국 발표 {len(econ_events.get('korReleasedToday', []))}건, "
              f"한국 예정 {len(econ_events.get('korTodayUpcoming', []))}건, "
              f"오늘 밤 미국 예정 {len(econ_events.get('usTonight', []))}건")

    _t_synth = time.time()
    briefing, fx_bullets, catalysts, model, llm_regime = synthesize(
        indices, investors, indicators, sectors_up, sectors_down,
        movers_up, movers_down, disclosures, news, fx_news,
        prev_rounds=prev_rounds, us_context=_load_us_context(),
        investor_flow=investor_flow, econ_events=econ_events)
    print(f"  [timing] synthesize {time.time() - _t_synth:.1f}s")

    out = {
        "date": now.strftime("%Y-%m-%d"),
        "asof": now.strftime("%Y-%m-%d %H:%M KST"),
        "final": final,                 # 마감 시황 여부 (대시보드 배지)
        "generatedBy": model or "mechanical",
        # 모의투자·v9 국면(2026-07-23, 후검증 2026-08-04): LLM 판정을 검증·강등·승격
        # (_finalize_regime) 거쳐 채택 — 무효/누락이면 기계적 폴백 후 동일 후검증.
        "regime": _finalize_regime(llm_regime, indices, investors),
        "indices": indices,
        "investors": investors,
        "indicators": indicators,
        "investorSeries": investor_series,   # 1분 누적 순매수(억원) — 지수 카드 차트
        "briefing": briefing,
        "fxBullets": fx_bullets,        # 환율 브리핑(LLM 요약 또는 헤드라인 폴백)
        "fxNews": fx_news[:8],          # 환율 관련 뉴스 원문(대시보드 접기)
        "sectorHeat": sector_heat,      # KIS KOSPI 산업별 업종(KRX 분류), 등락 내림차순
        "sectorsUp": sectors_up,        # 급등 상위 섹터 + KRX 관련주 + 매칭 네이버 테마(theme)
        "sectorsDown": sectors_down,    # 급락 상위 섹터
        "catalysts": catalysts,
        "econEvents": econ_events,      # 오늘 한국 지표 결과 + 오늘 밤 미국 예정 (경제지표 카드)
        "earningsEvents": earnings_events,  # 실적발표 결과·예정 (표시 전용 — LLM 미전달)
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
    # 1분 시계열은 아카이브 비대 방지를 위해 마감 회차에만 보존(복기용) — 장중 회차는
    # 최신 파일(intraday_briefing.json)에서 다음 회차가 이월한다.
    arch_entry = dict(out)
    if not final:
        arch_entry.pop("investorSeries", None)
    arch["rounds"].append(arch_entry)
    with open(arch_path, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False, indent=2)
    print(f"  Archive: {arch_path} ({len(arch['rounds'])} rounds)")
    print("=== Done ===")


if __name__ == "__main__":
    main()
