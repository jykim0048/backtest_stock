#!/usr/bin/env python3
"""
Morning briefing generator — 전일 미국장 리뷰 + 당일 한국시장 프리뷰.

장전 스크리너(screener.py, pre 모드)가 이미 만들어 둔 선정 산출물
  public/reports/selection/YYYY-MM-DD.json
을 입력으로 받는다. 이 파일은 usMarket(미국 지수·섹터·급등 특징주)과
picks(종목별 촉매·근거), marketView(3~4문장 요약)를 이미 담고 있으므로,
브리핑 생성기는 **추가 네트워크 수집 없이** 그 자료를 재가공·심화한다.
(로컬 SSL 제약과 무관 — yfinance/DART 를 다시 호출하지 않는다.)

핵심 부가가치는 **미국 movers/섹터 → 국내 테마 밸류체인 매핑**과 스탠스 판정으로,
이를 위해 전용 LLM 콜 1회를 쓴다. LLM 이 없거나 실패하면 기계적 폴백으로
브리핑을 만들어 **절대 파이프라인을 막지 않는다**(screener 와 같은 철학).

출력(public/briefing/):
  YYYY-MM-DD.json  — 날짜별 아카이브(대시보드 과거 조회)
  latest.json      — 최신본(대시보드 기본 로드)
  index.json       — 사용 가능한 날짜 목록(최신순)

Env:
  BRIEFING_SELECTION  입력 selection JSON 경로(기본: 오늘자, 없으면 최신 selection)
  BRIEFING_OUT_DIR    출력 디렉토리(기본 public/briefing)
  BRIEFING_DATE       대상 날짜 YYYY-MM-DD(기본 KST 오늘)
  LLM_CHAIN + 키      llm.py 참고. 미설정 시 기계적 폴백.
"""
import os
import re
import sys
import json
import glob
import datetime

import llm

ROOT          = os.path.dirname(os.path.abspath(__file__))
SELECTION_DIR = os.path.join(ROOT, "public", "reports", "selection")
OUT_DIR       = os.environ.get("BRIEFING_OUT_DIR") or os.path.join(ROOT, "public", "briefing")
if not os.path.isabs(OUT_DIR):
    OUT_DIR = os.path.join(ROOT, OUT_DIR)

KST        = datetime.timezone(datetime.timedelta(hours=9))
MAX_TOKENS = 3000
US_CATALYSTS_PATH = os.path.join(ROOT, "public", "us_catalysts.json")


def _load_us_catalysts(date_str):
    """밤사이 미국 SEC 공시 촉매(us_night_catalysts 회차 누적, 2026-07-22 도입).

    세션 키는 미국 거래일(KST 저녁 시작 날짜) — 월요일 아침엔 금요일 세션이 최신이라
    '오늘-3일'까지 허용하고, 그보다 낡은 파일(수집 중단 등)은 버린다. 없으면 []."""
    try:
        with open(US_CATALYSTS_PATH, encoding="utf-8") as f:
            data = json.load(f) or {}
        floor = (datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
                 - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
        if str(data.get("session") or "") < floor:
            return []
        return data.get("catalysts") or []
    except Exception:
        return []


def _warn(msg):
    print(f"[briefing] {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------
# 입력 로드 — 오늘자 selection, 없으면 가장 최근 selection
# ----------------------------------------------------------------------------
def _load_selection(date_str):
    explicit = os.environ.get("BRIEFING_SELECTION")
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(ROOT, explicit)
        with open(path, encoding="utf-8") as f:
            return json.load(f), path

    today_path = os.path.join(SELECTION_DIR, f"{date_str}.json")
    if os.path.exists(today_path):
        with open(today_path, encoding="utf-8") as f:
            return json.load(f), today_path

    # 오늘자가 아직 없으면(장전 스크리너 미실행) 가장 최근 selection 을 쓴다.
    dated = sorted(glob.glob(os.path.join(SELECTION_DIR, "20*.json")))
    if not dated:
        raise RuntimeError(f"selection JSON 없음: {SELECTION_DIR}")
    with open(dated[-1], encoding="utf-8") as f:
        _warn(f"오늘자 selection 없음 → 최신본 사용: {os.path.basename(dated[-1])}")
        return json.load(f), dated[-1]


# ----------------------------------------------------------------------------
# 경제지표 캘린더 (fetch_econ_calendar.py 산출물) — 브리핑 관점 3분할.
# 수치는 LLM 을 통과시키지 않고 프런트 카드가 econEvents 를 그대로 렌더한다(환각 방지).
# ----------------------------------------------------------------------------
ECON_CALENDAR_PATH = os.path.join(ROOT, "public", "econ_calendar.json")
ECON_MIN_IMPORTANCE = 2      # 시장 영향력 보통 이상만
ECON_MAX_ITEMS = 8           # 구획별 상한


def _load_econ_calendar():
    try:
        with open(ECON_CALENDAR_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _warn(f"econ_calendar.json 로드 실패(경제지표 카드 생략): {e}")
        return {}


def _econ_pick(rows):
    """중요도 우선 상한 적용 후 시간순 정렬."""
    rows = sorted(rows, key=lambda r: (-(r.get("importance") or 0), r.get("releaseAtKST") or ""))
    return sorted(rows[:ECON_MAX_ITEMS], key=lambda r: r.get("releaseAtKST") or "")


def _econ_events(cal, date_str):
    """{usReleased, korToday, usTonight} — 각각 미국장 리뷰 / 한국 프리뷰 반영용.

    usReleased : 전일(월요일 아침이면 금~일) 이후 발표된 미국 지표 — 전일 밤 21:30 + 당일 새벽 FOMC.
    korToday   : 당일 발표 예정 한국 지표.
    usTonight  : 당일 15:30 ~ 익일 08:00 KST 발표 예정 미국 지표(당일 밤 미국장).
    """
    if not cal:
        return {}
    try:
        day = datetime.date.fromisoformat(date_str)
    except ValueError:
        return {}
    lookback = 3 if day.weekday() == 0 else 1
    released_from = f"{day - datetime.timedelta(days=lookback)} 00:00"
    tonight_from  = f"{day} 15:30"
    tonight_to    = f"{day + datetime.timedelta(days=1)} 08:00"

    def ok(r):
        return (r.get("importance") or 0) >= ECON_MIN_IMPORTANCE and r.get("releaseAtKST")

    released = [r for r in cal.get("released") or [] if ok(r)]
    upcoming = [r for r in cal.get("upcoming") or [] if ok(r)]

    ev = {
        "usReleased": _econ_pick([r for r in released
                                  if r.get("nation") == "USA"
                                  and r["releaseAtKST"] >= released_from]),
        "korToday":   _econ_pick([r for r in upcoming
                                  if r.get("nation") == "KOR"
                                  and r["releaseAtKST"][:10] == date_str]),
        "usTonight":  _econ_pick([r for r in upcoming
                                  if r.get("nation") == "USA"
                                  and tonight_from <= r["releaseAtKST"] <= tonight_to]),
    }
    return ev if any(ev.values()) else {}


def _econ_llm_view(econ):
    """LLM 입력용 축약(수치 재생성 유인을 줄이기 위해 필요한 필드만)."""
    def slim(r):
        out = {"name": r.get("name"), "importance": r.get("importance"),
               "releaseAtKST": r.get("releaseAtKST"),
               "previous": r.get("previous"), "forecast": r.get("forecast"),
               "unit": r.get("unit") or r.get("unitScale") or ""}
        if r.get("actual") is not None:
            out["actual"] = r["actual"]
        if r.get("surprise"):
            out["surpriseVerdict"] = r["surprise"].get("verdict")
            out["surpriseVs"] = r["surprise"].get("vs")
        return out
    return {k: [slim(r) for r in v] for k, v in econ.items() if v}


# ----------------------------------------------------------------------------
# 테마 스켈레톤 구성 — 섹터 히트 상하위 3개씩 + 각 테마의 대표 종목(미국/국내).
# 종목·등락률은 전부 selection JSON 에 이미 실려온 실측치이며(screener.py 가 계산),
# LLM/기계적 폴백 어느 경로든 이 스켈레톤은 동일하게 재사용한다(환각 방지 + 결정론적 "3개 이상" 보장).
# ----------------------------------------------------------------------------
_SECTOR_KR_THEME = {
    # 부분일치(substring)로 매칭하므로, 다른 업종 촉매 문구에도 흔히 등장하는 범용 단어는
    # 피한다("AI"→전력/반도체 촉매에도 등장, "장비"→의료·건설장비, "진단"→"실적 진단" 관용구,
    # "지주"→업종 무관 지주회사 구조, "에너지"→이차전지 "에너지밀도" 등, "게임"→"게임체인저" 관용구,
    # "증권"→"출자증권 취득"처럼 업종 무관 공시 유형명에도 등장). "증권사"도 "증권사 리포트/
    # 분석"(애널리스트 커버리지)로 오매칭되므로 _theme_kr_stocks 가 홈섹터 배제 + 금융 이름
    # 게이트로 2차 방어한다.
    "기술":       {"theme": "IT·플랫폼",     "kw": ["플랫폼", "소프트웨어", "클라우드", "인터넷", "게임사", "모바일게임"]},
    "반도체":     {"theme": "반도체 소부장", "kw": ["반도체", "웨이퍼", "파운드리", "소부장", "반도체장비", "공정장비", "패키징"]},
    "헬스케어":   {"theme": "헬스케어",      "kw": ["헬스케어", "의료기기", "체외진단", "진단기기", "병원"]},
    "바이오":     {"theme": "제약·바이오",   "kw": ["바이오", "제약", "임상", "신약", "백신"]},
    "에너지":     {"theme": "정유·에너지",   "kw": ["정유", "신재생에너지", "가스", "석유", "화력", "태양광", "풍력"]},
    "금융":       {"theme": "은행·증권",     "kw": ["금융", "은행", "증권사", "금융지주", "보험"]},
    "산업재":     {"theme": "산업재·기계",   "kw": ["기계", "조선", "건설", "중공업", "플랜트"]},
    "소재":       {"theme": "소재·화학",     "kw": ["화학", "소재", "철강", "이차전지"]},
    "임의소비재": {"theme": "소비재·유통",   "kw": ["유통", "소비재", "레저", "카지노", "호텔", "여행", "의류"]},
}
THEME_STOCKS_MAX = 5


# 금융 테마는 촉매 텍스트("증권사 리포트/분석" 등 애널리스트 커버리지 언급)로 오매칭되기
# 쉬워, 종목 '이름'에 아래 금융 토큰이 있는지로 한 번 더 검증한다(실제 증권/은행/지주는
# 이름에 신호가 있고, S-Oil 같은 정유주는 없다).
_FINANCIAL_NAME_TOKENS = ("증권", "은행", "뱅크", "금융", "보험", "지주", "화재", "생명", "캐피탈", "카드")


def _theme_kr_stocks(sector_name, picks, home_sector=None):
    """국내 밸류체인 종목: 오늘 picks 중 테마 키워드 매칭(우선) + 대표 종목(대체·보강).

    matched(오늘 실제 선정된 촉매 종목)을 우선 배치하고, 3개 이상을 보장하기 위해
    대표 종목 목록으로 보강한다. 전부 changePct(전일 등락률) 있는 것만 남긴다.

    오매칭 방지 2단:
    ① 홈섹터 배제 — 다른 섹터의 대표 종목으로 등록된 코드는 키워드가 맞아도 제외
       (home_sector: {code: 섹터명}, krSectorStocks 역맵). 예: S-Oil(에너지)→금융 차단.
    ② 금융 이름 게이트 — 금융 테마는 종목명에 금융 토큰이 있어야 매칭(커버리지 문구
       오매칭 차단). 대표 목록에 없는 비큐레이션 종목까지 일반적으로 보호한다.
    """
    meta = _SECTOR_KR_THEME.get(sector_name, {"theme": sector_name, "kw": []})
    kws = meta["kw"]
    home_sector = home_sector or {}
    matched = []
    if kws:
        for p in picks:
            name, code = p.get("name"), p.get("code")
            if not name or p.get("changePct") is None:
                continue
            # ① 다른 섹터 대표 종목이면 스킵(홈섹터 우선)
            if home_sector.get(code) and home_sector[code] != sector_name:
                continue
            text = f"{p.get('reason', '')} {p.get('catalyst', '')}"
            if not any(k in text for k in kws):
                continue
            # ② 금융 테마는 종목명에 금융 토큰이 있어야 인정
            if sector_name == "금융" and not any(t in name for t in _FINANCIAL_NAME_TOKENS):
                continue
            matched.append({"code": code, "name": name, "changePct": p["changePct"]})
    return meta["theme"], matched


def _kr_home_sector(sel):
    """krSectorStocks(섹터→대표 종목)를 code→섹터 역맵으로. 키워드 오매칭 배제용."""
    kss = (sel.get("usMarket", {}) or {}).get("krSectorStocks", {}) or {}
    home = {}
    for sector, rows in kss.items():
        for r in (rows or []):
            if r.get("code"):
                home.setdefault(r["code"], sector)
    return home


def _build_theme(sector_obj, sel, home_sector=None):
    name = sector_obj.get("name", "")
    us = sel.get("usMarket", {}) or {}
    us_stocks = list((us.get("sectorStocks", {}) or {}).get(name, []))[:THEME_STOCKS_MAX]
    kr_theme, matched = _theme_kr_stocks(name, sel.get("picks", []), home_sector)
    kr_rep = list((us.get("krSectorStocks", {}) or {}).get(name, []))
    seen = {m["name"] for m in matched}
    kr_stocks = matched + [r for r in kr_rep if r.get("name") not in seen]
    kr_stocks.sort(key=lambda d: abs(d.get("changePct") or 0), reverse=True)
    return {
        "usTheme": name,
        "usChangePct": sector_obj.get("changePct"),
        "usStocks": us_stocks,
        "krTheme": kr_theme,
        "krStocks": kr_stocks[:THEME_STOCKS_MAX],
    }


def _build_themes(sel):
    """섹터 히트맵 상위 3(급등 테마) / 하위 3(급락 테마). sectors 는 이미 changePct 내림차순."""
    sectors = (sel.get("usMarket", {}) or {}).get("sectors", [])
    if len(sectors) < 6:
        return [], []
    up = sectors[:3]
    down = list(reversed(sectors[-3:]))     # 가장 급락한 섹터부터(= up 과 대칭)
    home = _kr_home_sector(sel)             # code→홈섹터 역맵(키워드 오매칭 배제)
    return ([_build_theme(s, sel, home) for s in up],
            [_build_theme(s, sel, home) for s in down])


# ----------------------------------------------------------------------------
# 네이버 산업분석 리포트 → 테마 부착.
# 리포트는 종목이 아니라 '업종' 단위라, 스크리너 미국 섹터명 → 네이버 업종명 매핑으로
# 붙인다. 이 파일의 "추가 네트워크 수집 없음" 원칙의 유일한 예외 — 키 불필요·실패 시
# 빈 배열이라 파이프라인을 막지 않는다.
# ----------------------------------------------------------------------------
_SECTOR_UPJONG = {   # 미국 섹터명(usTheme) -> 네이버 리서치 업종명 후보(복수 허용)
    "기술":       ["인터넷포탈", "IT", "게임", "미디어", "통신", "소프트웨어"],
    "반도체":     ["반도체", "전기전자", "디스플레이"],
    "헬스케어":   ["제약", "바이오", "의료"],
    "바이오":     ["바이오", "제약"],
    "에너지":     ["에너지", "석유화학", "유틸리티"],
    "금융":       ["은행", "증권", "보험"],
    "산업재":     ["기계", "조선", "건설", "운송", "자동차", "방산"],
    "소재":       ["철강금속", "석유화학", "화학", "비철금속"],
    "임의소비재": ["유통", "화장품", "의류", "음식료", "레저", "게임"],
}
THEME_REPORTS_MAX = 3


def _attach_industry_reports(up_themes, down_themes):
    """테마별 industryReports[] 부착 (최근 30일, 최신순 최대 3건).
    각 테마의 최신 1건은 상세 요약(summary)까지 조회해 LLM rationale 근거로 쓴다."""
    themes = up_themes + down_themes
    for t in themes:
        t["industryReports"] = []
    try:
        from analysis import sources
        reports = sources.naver_industry_research(days=30)
    except Exception as e:
        _warn(f"산업 리포트 수집 실패: {e} — 생략")
        return
    if not reports:
        return
    fetched_detail = set()
    for t in themes:
        cats = _SECTOR_UPJONG.get(t.get("usTheme"), [])
        matched = [r for r in reports
                   if any(c in r.get("category", "") or r.get("category", "") in c
                          for c in cats)]
        t["industryReports"] = matched[:THEME_REPORTS_MAX]
        # 테마당 최신 1건 요약 본문 (중복 URL 은 1회만 조회)
        if matched and matched[0]["url"] not in fetched_detail:
            fetched_detail.add(matched[0]["url"])
            matched[0]["summary"] = sources.naver_industry_detail(matched[0]["url"])
    print(f"  Industry rpts  : {len(reports)}건(30일) → 테마별 부착 "
          f"{[len(t['industryReports']) for t in themes]}")


# ----------------------------------------------------------------------------
# LLM 브리핑 (전일 미국장 리뷰 + 당일 한국 프리뷰).
# 테마의 종목·등락률은 이미 결정돼 있으므로(위 _build_themes), LLM 은 종목을
# 새로 고르지 않고 각 테마의 rationale(한 줄 근거)과 서술형 텍스트만 작성한다.
# ----------------------------------------------------------------------------
SYSTEM = """\
너는 한국 주식 데이 트레이딩 데스크의 애널리스트다. 장 시작 전(08시경), 주어진 데이터로
'전일 미국장 리뷰'와 '당일 한국시장 프리뷰'로 구성된 모닝 브리핑을 작성한다.

입력:
- usMarket.indices : 전일 미국 주요 지수/VIX 등락(changePct). 시장 위험선호도 판단용.
- usMarket.sectors : 미국 섹터 ETF 등락(강→약 정렬).
- upThemes   : 섹터 히트맵 **상위 3개**(급등 테마). 각 항목은 usTheme(섹터명)·usChangePct·
  usStocks(그 섹터 대표 미국 종목, 실제 등락률 포함, 3개 이상)·krTheme(연결되는 국내 테마명)·
  krStocks(관련 국내 종목, 전일 등락률 포함, 3개 이상)를 **이미 확정해 담고 있다**.
- downThemes : 섹터 히트맵 **하위 3개**(급락 테마). 구조는 upThemes 와 동일(대칭).
- picks      : 장전 스크리너가 오늘 뽑은 국내 워치리스트(code/name/market/catalyst/reason/changePct).
- marketView : 스크리너가 남긴 간단 시황 코멘트(참고).
- 각 테마의 industryReports : 해당 업종의 최근 30일 증권사 산업분석 리포트
  (category/title/broker/date, 최신 1건은 summary 요약 본문 포함). 없을 수 있다.
- econEvents : 경제지표 캘린더. usReleased(전일 발표된 미국 지표 — actual/forecast/previous 와
  surpriseVerdict: above=상회 / inline=부합 / below=하회), korToday(당일 발표 예정 한국 지표),
  usTonight(오늘 밤 미국 발표 예정). 없을 수 있다.
- usCatalysts : 밤사이 미국 세션 촉매 — kind=sec 는 SEC 공시(한국 연관 미국 종목 한정,
  원문 기반 요약), kind=news 는 시장 뉴스(지정학·매크로·대형주 실적 보도). stock 필드는
  사건의 실제 주체(해외 기업·지수·이슈, 예: CXMT·국제유가), relatedStock 은 그 촉매의
  국내 대표 관련주(매매 관점). 공통 필드 ticker/stock/relatedStock/form/kind/direction/
  summary/changePct. 없을 수 있다.
  **surpriseVs 가 비교 기준이다: forecast 면 '예상(컨센서스) 상회/하회', previous 면 반드시
  '이전(직전치) 대비 상회/하회'로 표현을 구분하고, 이전값 대비를 '예상치 상회/하회'라고 쓰지 말 것.**

**중요: upThemes/downThemes 의 usTheme·usStocks·krTheme·krStocks 는 이미 정해진 사실이다.
너는 종목이나 등락률을 새로 만들거나 바꾸지 않는다. 오직 각 테마별 rationale(한 줄 근거)과
아래 서술형 텍스트만 작성한다.**

작성 원칙:
1) stance: 전일 미국장 위험선호도를 '위험선호|중립|위험회피' 중 하나로 판정한다.
   나스닥·필라델피아 반도체 강세 + VIX 하락 = 위험선호. 반대면 위험회피.
   econEvents.usReleased 에 핵심 지표(CPI·고용·FOMC 등)가 있으면 그 서프라이즈 방향도
   지수·VIX 와 함께 판정 근거로 반영한다.
2) usReview.bullets: 전일 미국장(지수·섹터·주도 테마)을 **3개 이상의 짧은 불릿 문장**으로
   요약한다(불릿 하나 = 한 문장, 문단이 아니라 리스트로 가독성 있게).
   econEvents.usReleased 가 있으면 불릿 중 1개는 그 지표 결과와 시장 반응 해석을 담는다
   (수치는 입력값을 그대로 인용하고 새로 만들지 않는다).
   usCatalysts 가 있으면 불릿 중 1개는 시장 영향이 큰 촉매(SEC 공시·뉴스)를 종목명(또는
   사건명)·주가 반응과 함께 담고, 유의미한 하방/상방 촉매(특히 지정학·매크로 뉴스)는
   stance 판단에도 반영한다. summary 내용 밖의 사실을 지어내지 말 것.
3) upRationale: **upThemes 와 정확히 같은 개수·순서**로, 각 테마별 "왜 미국 이 섹터 강세가
   국내 이 테마로 이어지는지" 한 줄 근거. 주어진 usStocks/krStocks 종목명을 자연스럽게
   인용해도 좋다(새 종목 언급 금지). 테마에 industryReports 가 있으면 그 제목·summary 의
   논거를 근거로 활용하라(리포트에 없는 내용을 지어내지 말 것).
4) downRationale: **downThemes 와 정확히 같은 개수·순서**로, 각 테마별 "왜 미국 이 섹터
   약세가 국내에 부담이 될 수 있는지" 한 줄 근거.
5) krPreview.narrative: 당일 국내 관점 1~2문장. econEvents 의 korToday·usTonight 이 있으면
   그중 중요도 높은 지표를 당일 관전 포인트로 1문장 반영한다. catalystSummary: 전일 장마감 후
   국내 촉매(picks 의 catalyst)를 1~2문장으로 압축.
6) 과장·투자권유 표현을 피하고, 담백한 데스크 코멘트 톤으로 한국어로 쓴다.

반드시 아래 스키마와 정확히 동일한 JSON만 출력한다. 마크다운 펜스/설명 금지.
{
  "stance": "위험선호|중립|위험회피",
  "stanceReason": "지수·VIX 근거 한 줄",
  "usReview": {"bullets": ["문장1", "문장2", "문장3"]},
  "upRationale": ["테마1 근거", "테마2 근거", "테마3 근거"],
  "downRationale": ["테마1 근거", "테마2 근거", "테마3 근거"],
  "krPreview": {
    "narrative": "당일 국내 관점 1~2문장",
    "catalystSummary": "전일 장마감 후 국내 촉매 요약"
  }
}"""

_STR   = {"type": "string"}
_STRLIST = {"type": "array", "items": _STR}
BRIEF_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "stance": _STR,
        "stanceReason": _STR,
        "usReview": {
            "type": "object", "additionalProperties": False,
            "properties": {"bullets": _STRLIST},
            "required": ["bullets"],
        },
        "upRationale": _STRLIST,
        "downRationale": _STRLIST,
        "krPreview": {
            "type": "object", "additionalProperties": False,
            "properties": {"narrative": _STR, "catalystSummary": _STR},
            "required": ["narrative", "catalystSummary"],
        },
    },
    "required": ["stance", "stanceReason", "usReview", "upRationale", "downRationale", "krPreview"],
}


def _llm_input(sel, up_themes, down_themes, econ=None, us_cats=None):
    us = sel.get("usMarket", {}) or {}
    payload = {
        "usMarket": {
            "indices": us.get("indices", []),
            "sectors": us.get("sectors", []),
        },
        "upThemes": up_themes,
        "downThemes": down_themes,
        "picks": [{"name": p.get("name"), "market": p.get("market"),
                   "catalyst": p.get("catalyst", ""), "reason": p.get("reason", ""),
                   "changePct": p.get("changePct")}
                  for p in sel.get("picks", [])],
        "marketView": sel.get("marketView", ""),
    }
    if econ:
        payload["econEvents"] = _econ_llm_view(econ)
    if us_cats:
        payload["usCatalysts"] = [{k: c.get(k) for k in
                                   ("ticker", "stock", "relatedStock", "form", "kind",
                                    "direction", "summary", "changePct")}
                                  for c in us_cats[:15]]
    return json.dumps(payload, ensure_ascii=False)


def llm_briefing(sel, up_themes, down_themes, econ=None, us_cats=None):
    """LLM 으로 stance/usReview/rationale/krPreview 생성. 실패 시 None(호출부에서 기계적 폴백)."""
    if not llm.configured():
        _warn("LLM 미설정 — 기계적 폴백 사용")
        return None, None
    try:
        data, model = llm.generate_json(
            SYSTEM, _llm_input(sel, up_themes, down_themes, econ, us_cats),
            max_tokens=MAX_TOKENS, schema=BRIEF_SCHEMA, return_model=True)
        return data, model
    except Exception as e:
        _warn(f"LLM 브리핑 실패: {e} — 기계적 폴백 사용")
        return None, None


def _merge_rationale(themes, rationales, direction):
    """LLM 이 준 rationale 배열을 테마 스켈레톤에 순서대로 붙인다. 개수가 모자라면 기본 문구로 채운다."""
    out = []
    for i, t in enumerate(themes):
        if i < len(rationales) and rationales[i]:
            r = rationales[i]
        elif direction == "up":
            r = f"미국 {t['usTheme']} 섹터 강세({t['usChangePct']:+.2f}%) → 국내 {t['krTheme']} 동조 기대"
        else:
            r = f"미국 {t['usTheme']} 섹터 약세({t['usChangePct']:+.2f}%) → 국내 {t['krTheme']} 약세 주의"
        out.append(dict(t, rationale=r))
    return out


# ----------------------------------------------------------------------------
# 기계적 폴백 — LLM 없이 selection 자료(+ 이미 구성된 테마 스켈레톤)만으로 브리핑을 구성한다.
# ----------------------------------------------------------------------------
def _pct(items, name):
    for it in items:
        if it.get("name") == name:
            return it.get("changePct")
    return None


def _econ_line(r):
    """지표 1건의 기계적 요약 문장 (예상/이전 대비)."""
    unit = r.get("unit") or ""
    scale = r.get("unitScale") or ""
    sfx = f"{scale}{unit}" if unit != "%" else "%"
    verdict = (r.get("surprise") or {}).get("verdict")
    vs = (r.get("surprise") or {}).get("vs")
    word = {"above": "상회", "inline": "부합", "below": "하회"}.get(verdict)
    base = "예상" if vs == "forecast" else "이전"
    line = f"{r.get('name')} 실제 {r.get('actual')}{sfx}"
    if r.get("forecast") is not None:
        fc = r.get("forecastText") or r["forecast"]   # FF 원문 정밀도 유지("0.0" 등)
        line += f" (예상 {fc}{sfx} · 이전 {r.get('previous')}{sfx})"
    elif r.get("previous") is not None:
        line += f" (이전 {r['previous']}{sfx})"
    if word:
        line += f" — {base} {word}"
    return line


def _mechanical(sel, up_themes, down_themes, econ=None):
    us = sel.get("usMarket", {}) or {}
    indices = us.get("indices", [])
    picks   = sel.get("picks", [])
    mv      = sel.get("marketView", "")

    vix = _pct(indices, "VIX 변동성")
    nas = _pct(indices, "나스닥")
    sox = _pct(indices, "필라델피아 반도체")

    # 스탠스: 나스닥/필라델피아 반도체 상승 + VIX 하락 = 위험선호
    up_signals   = sum(1 for v in (nas, sox) if v is not None and v > 0)
    down_signals = sum(1 for v in (nas, sox) if v is not None and v < 0)
    vix_calm     = (vix is not None and vix < 0)
    if up_signals >= 1 and down_signals == 0 and (vix_calm or vix is None):
        stance = "위험선호"
    elif down_signals >= 1 and not vix_calm:
        stance = "위험회피"
    else:
        stance = "중립"

    def fmt(v):
        return "—" if v is None else f"{v:+.2f}%"
    stance_reason = f"나스닥 {fmt(nas)} · 필라델피아 반도체 {fmt(sox)} · VIX {fmt(vix)}"

    bullets = []
    if nas is not None:
        bullets.append(f"나스닥이 전일 대비 {nas:+.2f}% {'상승' if nas >= 0 else '하락'} 마감했습니다.")
    if sox is not None:
        word = "강세" if sox >= 0 else "약세"
        bullets.append(f"필라델피아 반도체 지수는 {sox:+.2f}%로 반도체 업종 {word}가 두드러졌습니다.")
    if vix is not None:
        word = "위축" if vix >= 0 else "개선"
        bullets.append(f"VIX 변동성 지수가 {vix:+.2f}% {'상승' if vix >= 0 else '하락'}해 투자심리가 {word}됐습니다.")
    if up_themes:
        t = up_themes[0]
        bullets.append(f"섹터별로는 {t['usTheme']}({t['usChangePct']:+.2f}%) 이 가장 강했습니다.")
    if down_themes:
        t = down_themes[0]
        bullets.append(f"반대로 {t['usTheme']}({t['usChangePct']:+.2f}%) 은 가장 부진했습니다.")
    us_released = (econ or {}).get("usReleased") or []
    if us_released:
        top = max(us_released, key=lambda r: r.get("importance") or 0)
        bullets.append(f"경제지표로는 {_econ_line(top)}.")
    if not bullets:
        bullets = [mv or "전일 미국장 데이터 기반 자동 요약입니다."]

    flow      = _merge_rationale(up_themes, [], "up")
    down_flow = _merge_rationale(down_themes, [], "down")

    catalyst = " / ".join(f"{p['name']}: {p.get('catalyst', '')}".strip(": ")
                          for p in picks[:5]) or "특이 촉매 없음"

    return {
        "stance": stance,
        "stanceReason": stance_reason,
        "usReview": {"bullets": bullets},
        "flow": flow,
        "downFlow": down_flow,
        "krPreview": {
            "narrative": "미국 강세 테마·급등주와 국내 밸류체인 동조 흐름을 관찰합니다. "
                         "(LLM 미가동 — 기계적 요약)",
            "catalystSummary": catalyst,
        },
    }


# ----------------------------------------------------------------------------
# 출력
# ----------------------------------------------------------------------------
def write_outputs(payload, date_str):
    os.makedirs(OUT_DIR, exist_ok=True)

    dated = os.path.join(OUT_DIR, f"{date_str}.json")
    with open(dated, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  Saved briefing : {dated}")

    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("  Updated        : latest.json")

    index_path = os.path.join(OUT_DIR, "index.json")
    index = []
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except Exception:
            index = []
    if date_str not in index:
        index.append(date_str)
    index = sorted({d for d in index if re.match(r"^\d{4}-\d{2}-\d{2}$", d)}, reverse=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"  Updated index  : {index_path} ({len(index)} entries)")


def main():
    date_str = os.environ.get("BRIEFING_DATE") or datetime.datetime.now(KST).strftime("%Y-%m-%d")
    print(f"=== Morning briefing ({date_str}) ===")

    sel, sel_path = _load_selection(date_str)
    print(f"  Selection      : {os.path.basename(sel_path)} "
          f"(picks {len(sel.get('picks', []))}, movers {len(sel.get('usMarket', {}).get('movers', []))})")

    up_themes, down_themes = _build_themes(sel)
    print(f"  Themes         : up {[t['usTheme'] for t in up_themes]} "
          f"/ down {[t['usTheme'] for t in down_themes]}")
    _attach_industry_reports(up_themes, down_themes)

    econ = _econ_events(_load_econ_calendar(), date_str)
    if econ:
        print(f"  Econ events    : usReleased {len(econ.get('usReleased', []))} "
              f"/ korToday {len(econ.get('korToday', []))} "
              f"/ usTonight {len(econ.get('usTonight', []))}")

    us_cats = _load_us_catalysts(date_str)
    if us_cats:
        print(f"  US SEC catalysts: {len(us_cats)}건")

    raw, model = llm_briefing(sel, up_themes, down_themes, econ, us_cats)
    if raw is None:
        brief = _mechanical(sel, up_themes, down_themes, econ)
        generated_by = "mechanical"
    else:
        brief = {
            "stance": raw["stance"],
            "stanceReason": raw["stanceReason"],
            "usReview": raw["usReview"],
            "flow": _merge_rationale(up_themes, raw.get("upRationale", []), "up"),
            "downFlow": _merge_rationale(down_themes, raw.get("downRationale", []), "down"),
            "krPreview": raw["krPreview"],
        }
        generated_by = model
    print(f"  Generated by   : {generated_by} · stance={brief.get('stance')}")

    payload = {
        "date": sel.get("date", date_str),
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "sourceAsof": sel.get("asof", ""),
        "generatedBy": generated_by,
        "stance": brief["stance"],
        "stanceReason": brief["stanceReason"],
        "usReview": brief["usReview"],
        "flow": brief.get("flow", []),
        "downFlow": brief.get("downFlow", []),
        "krPreview": brief["krPreview"],
        # 카드·테이블 렌더용 원본 데이터(프런트가 그대로 사용)
        "usMarket": sel.get("usMarket", {}),
        "picks": sel.get("picks", []),
        "marketView": sel.get("marketView", ""),
        "econEvents": econ,   # 경제지표 카드 원본 (없으면 {})
        "usCatalysts": us_cats,   # 밤사이 SEC 공시 촉매(테이블 렌더용 원본, 없으면 [])
    }
    write_outputs(payload, date_str)
    print("=== Done ===")


if __name__ == "__main__":
    main()
