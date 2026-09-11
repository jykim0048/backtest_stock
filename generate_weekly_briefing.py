#!/usr/bin/env python3
"""주간 브리핑 — 모닝브리핑 + 장중 시황(마감 회차)을 일별로 압축해 LLM 이 주간 종합.

매 거래일 마감 후(16:10 KST 디스패치) 그 주 월요일~당일 데이터를 다시 합성해
같은 키(그 주 월요일 날짜)에 upsert 한다 — 주중에는 '이번 주 현재까지', 금요일
실행분이 그 주의 최종본. 모의투자/regime 관련 내용은 다루지 않는다(시장 데이터만).

입력 (Railway 서버 HTTP — dual-write/컷오버와 무관하게 항상 최신):
  briefing/<date>.json                    모닝브리핑 (미국장 리뷰 + 당일 프리뷰)
  reports/intraday_briefing/<date>.json   장중 시황 — final(마감) 회차만 사용

출력:
  public/reports/weekly_briefing/<월요일>.json   (아카이브 — DB kind='weekly_briefing')
  public/weekly_briefing.json                    (최신 스냅샷)
  산출 구조: {weekStart, weekEnd, asof, days:[일별 결정적 집계], synthesis:{LLM}}
  LLM 실패 시에도 days(지수·수급·섹터 테이블)는 유지하고 synthesis 만 비운다.

사용: python generate_weekly_briefing.py [--date YYYY-MM-DD] [--no-llm] [--local]
  --date   기준일(기본 오늘 KST). 그 날이 속한 주(월~기준일)를 합성
  --no-llm LLM 생략 (결정적 집계만 — 로컬 구조 검증용)
  --local  서버 대신 로컬 public/ 파일에서 읽기
"""
import os
import re
import sys
import json
import datetime
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import llm

KST = datetime.timezone(datetime.timedelta(hours=9))
BASE = os.environ.get("DASHBOARD_BASE", "https://backteststock-production.up.railway.app")
OUT_DIR = os.path.join(ROOT, "public", "reports", "weekly_briefing")
SNAP = os.path.join(ROOT, "public", "weekly_briefing.json")

USE_LOCAL = "--local" in sys.argv


def _fetch(rel):
    """로컬 public/ 우선(CI 체크아웃 = 최신 커밋, finalize 패치 반영), 서버 폴백.

    서버 폴백은 로컬에 없는 과거분(컷오버 후 등)용. --local 은 서버 폴백도 끈다."""
    try:
        with open(os.path.join(ROOT, "public", rel), encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        if USE_LOCAL:
            return None
    try:
        req = urllib.request.Request(f"{BASE}/{rel}?t={int(datetime.datetime.now().timestamp())}",
                                     headers={"User-Agent": "weekly-briefing"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as ex:
        print(f"[weekly] fetch {rel} 실패: {ex}", file=sys.stderr)
        return None


def _week_dates(base_date):
    """base_date 가 속한 주의 월요일~base_date (주말이면 그 주 금요일까지)."""
    if base_date.weekday() >= 5:                       # 토/일 → 그 주 금요일 기준
        base_date -= datetime.timedelta(days=base_date.weekday() - 4)
    monday = base_date - datetime.timedelta(days=base_date.weekday())
    return [monday + datetime.timedelta(days=i)
            for i in range((base_date - monday).days + 1)]


# ---------------------------------------------------------------------------
# 일별 압축 추출 — LLM 프롬프트에 넣을 2~3KB 요약 (regime/모의투자 제외)
# ---------------------------------------------------------------------------
def _period_dates(base_date, period="week"):
    """기간 거래일 후보 — week: 그 주 월~기준일(_week_dates), month: 그 달 1일~기준일의
    평일(주말이면 직전 금요일까지). 휴장일은 하위 단계가 데이터 부재로 자연 스킵."""
    if period != "month":
        return _week_dates(base_date)
    if base_date.weekday() >= 5:
        base_date -= datetime.timedelta(days=base_date.weekday() - 4)
    first = base_date.replace(day=1)
    return [first + datetime.timedelta(days=i)
            for i in range((base_date - first).days + 1)
            if (first + datetime.timedelta(days=i)).weekday() < 5]


def _compound(pcts):
    """일별 등락률(%) 리스트 → 기간 복리 누적(%). 월간은 단순 합산 오차가 커 복리."""
    acc = 1.0
    for p in pcts:
        acc *= 1.0 + float(p) / 100.0
    return round((acc - 1.0) * 100.0, 2)


def _us_period_returns(days):
    """미국 지수 기간 수익률(%) — 레벨 기반: 첫 관측의 전일 종가(price/(1+chg))를
    기준가로, 마지막 관측 레벨과 비교. 레벨 결측 지수는 일별 등락 복리 폴백."""
    obs = {}
    for d in days:
        for i in (d.get("usIndices") or []):
            nm = i.get("name")
            if nm and isinstance(i.get("changePct"), (int, float)):
                obs.setdefault(nm, []).append(i)
    out = {}
    for nm, xs in obs.items():
        p0, c0, pl = xs[0].get("price"), xs[0].get("changePct"), xs[-1].get("price")
        if all(isinstance(v, (int, float)) for v in (p0, c0, pl)) and p0 and c0 > -100:
            base = p0 / (1.0 + c0 / 100.0)
            out[nm] = round((pl / base - 1.0) * 100.0, 2)
        else:
            out[nm] = _compound(x["changePct"] for x in xs)
    return out


def _compact_day(d):
    """월간 LLM 입력용 일별 축약 — 22거래일 원문(일 3~5KB)은 과대하므로 수치·핵심
    시황 3줄·경제지표만. 서사는 그 달 주간 합성 아카이브(_period_syntheses)가 담당."""
    return {"date": d["date"], "stance": d.get("stance"),
            "indices": d.get("indices") or {}, "investors": d.get("investors") or {},
            "sectorsUp": d.get("sectorsUp") or [], "sectorsDown": d.get("sectorsDown") or [],
            "briefing": (d.get("briefing") or [])[:3],
            "usEcon": d.get("usEcon") or [], "koEcon": d.get("koEcon") or []}


def _period_syntheses(dates):
    """기간 안 각 주의 주간 브리핑 synthesis 요약(월간 서사 입력) — 주차 월요일 키."""
    mondays = sorted({(dt - datetime.timedelta(days=dt.weekday())).isoformat() for dt in dates})
    out = []
    for mk in mondays:
        w = _fetch(f"reports/weekly_briefing/{mk}.json") or {}
        syn = w.get("synthesis") or {}
        if syn:
            out.append({"weekStart": w.get("weekStart") or mk, "weekEnd": w.get("weekEnd"),
                        "headline": syn.get("headline"),
                        "weekNarrative": syn.get("weekNarrative") or [],
                        "sectorRotation": syn.get("sectorRotation") or [],
                        "weeklyComment": syn.get("weeklyComment") or []})
    return out


def _econ_row(e, nation):
    """경제지표 행 축약 — 주간 브리핑 표기/LLM 입력 공용."""
    r = {k: e.get(k) for k in ("name", "importance", "actual", "forecast",
                               "previous", "unit", "unitScale", "surprise")
         if e.get(k) is not None}
    r["nation"] = nation
    return r


def _day_summary(date_str):
    morning = _fetch(f"briefing/{date_str}.json")
    intraday = _fetch(f"reports/intraday_briefing/{date_str}.json")
    if morning is None and intraday is None:
        return None                                     # 휴장/데이터 없음

    day = {"date": date_str}

    if morning:
        day["stance"] = morning.get("stance")               # 모닝브리핑 스탠스(논조 기준점)
        day["stanceReason"] = morning.get("stanceReason")
        day["usReview"] = (morning.get("usReview") or {}).get("bullets") or []
        day["krPreview"] = (morning.get("krPreview") or {}).get("narrative") or ""
        us = morning.get("usMarket") or {}
        day["usIndices"] = [{"name": i.get("name"), "changePct": i.get("changePct"),
                             "price": i.get("price")}          # 월간 레벨 기반 수익률용
                            for i in (us.get("indices") or [])[:5]]
        # 미국 섹터 히트(섹터 ETF 일별 등락) — 주간 누적 상위/하위 섹터 집계 입력
        day["usSectors"] = [{"name": s.get("name"), "changePct": s.get("changePct")}
                            for s in (us.get("sectors") or [])
                            if s.get("name") and isinstance(s.get("changePct"), (int, float))]
        day["usCatalystsTop"] = [
            {"stock": c.get("stock"), "direction": c.get("direction"),
             "summary": (c.get("summary") or "")[:120]}
            for c in (morning.get("usCatalysts") or [])[:5]]
        # 전일 미국 경제지표 — '시장에 의미있게 반영된' 지표만: 모닝브리핑 usReview
        # 불릿에 지표명이 언급된 것(별점 무관 — 2026-09-08 사용자 요청 개편, 예: 9/7
        # 실업률·비농업고용). 언급 매칭 실패 시 별점(★3+) 폴백. actual 없는 행 제외.
        rev_txt = "".join(day["usReview"]).replace(" ", "")
        released = [e for e in ((morning.get("econEvents") or {}).get("usReleased") or [])
                    if e.get("actual") is not None]
        picked = [e for e in released
                  if (e.get("name") or "").replace(" ", "") in rev_txt]
        if not picked:
            picked = [e for e in released if (e.get("importance") or 0) >= 3]
        day["usEcon"] = [_econ_row(e, "USA") for e in picked]

    closing = None
    for r in reversed((intraday or {}).get("rounds") or []):
        if r.get("final"):
            closing = r
            break
    if closing is None and (intraday or {}).get("rounds"):
        rs = [r for r in intraday["rounds"] if not r.get("scoring")]
        closing = rs[-1] if rs else None                # 마감 회차 결손 시 마지막 시황 회차
    # 16:00 촉매 스코어 회차 — 타임라인 종목 행은 결정적 선별(코스피 ★4↑ / 코스닥 ★5)
    for r in reversed((intraday or {}).get("rounds") or []):
        if r.get("scoring"):
            picks = []
            for c in (r.get("catalysts") or []):
                sc, mk = c.get("score"), c.get("market")
                if sc is None:
                    continue
                if (mk == "KOSPI" and sc >= 4) or (mk == "KOSDAQ" and sc == 5):
                    picks.append({"stock": c.get("stock"), "market": mk, "star": sc,
                                  "changePct": c.get("changePct"),
                                  "event": (c.get("reason") or c.get("summary") or "")[:90]})
            if picks:
                picks.sort(key=lambda x: (-(x["star"]), -(x.get("changePct") or 0)))
                day["timelineStocks"] = picks
            break
    if closing:
        day["indices"] = closing.get("indices") or {}
        day["investors"] = closing.get("investors") or {}
        day["briefing"] = (closing.get("briefing") or [])[:8]
        day["sectorsUp"] = [{"name": s.get("name"), "changePct": s.get("changePct")}
                            for s in (closing.get("sectorsUp") or [])[:3]]
        day["sectorsDown"] = [{"name": s.get("name"), "changePct": s.get("changePct")}
                              for s in (closing.get("sectorsDown") or [])[:3]]
        day["catalysts"] = [
            {"stock": c.get("stock"), "market": c.get("market"),
             "direction": c.get("direction"), "summary": (c.get("summary") or "")[:120]}
            for c in (closing.get("catalysts") or [])[:8]]
        day["disclosures"] = [
            {"corp": x.get("corp"), "title": (x.get("title") or "")[:60]}
            for x in (closing.get("disclosures") or [])[:4]]
    # 당일 발표 한국 경제지표 — 장중 회차 econEvents.korReleasedToday(값 확정분)에서
    # ★3 이상만 (한국은 시황 불릿 언급이 드물어 별점 기준 유지). 뒤 회차부터 탐색.
    for r in reversed((intraday or {}).get("rounds") or []):
        kr = [e for e in ((r.get("econEvents") or {}).get("korReleasedToday") or [])
              if (e.get("importance") or 0) >= 3 and e.get("actual") is not None]
        if kr:
            day["koEcon"] = [_econ_row(e, "KOR") for e in kr]
            break
    ft = _day_flow_top(date_str)
    if ft:
        day["flowTop"] = ft
    return day


def _day_flow_top(date_str):
    """당일 확정 순매수 주도주(수급) — netbuy_rank 스냅샷 외인+기관 합산 상위 3."""
    try:
        with open(os.path.join(ROOT, "public", "reports", "netbuy_rank",
                               f"{date_str}.json"), encoding="utf-8") as f:
            rank = json.load(f)
    except OSError:
        rank = _fetch(f"reports/netbuy_rank/{date_str}.json")
    if not rank:
        return None
    fin = rank.get("final") or {}
    best, seen = [], set()
    for rows in (rank.get("lists") or {}).values():
        for r in rows or []:
            c = r.get("code")
            if not c or c in seen:
                continue
            seen.add(c)
            src = fin.get(c) or r
            best.append((float(src.get("frgn") or 0) + float(src.get("orgn") or 0),
                         r.get("name")))
    best.sort(reverse=True)
    return [{"name": n, "netBuyEok": round(a / 100)} for a, n in best[:3]]


FLOW_RANK_URL = os.environ.get(
    "FLOW_RANK_URL",
    "https://tradingstrategies-production-09d4.up.railway.app/flow-rank")
RANK_DIR = os.path.join(ROOT, "public", "reports", "netbuy_rank")


BREADTH_DIR = os.path.join(ROOT, "public", "reports", "breadth")

# 신고가 근접에서 ETF/ETN 제외(2026-09-08 사용자 요청) — KIS near-new-highlow 는
# 상장 전체 대상이라 브랜드 접두(KODEX 등)·액티브/레버리지류 상품이 섞여 나온다.
_ETF_RE = re.compile(
    r"^(KODEX|TIGER|SOL|KBSTAR|RISE|ACE|PLUS|HANARO|KOSEF|ARIRANG|KIWOOM|UNICORN|"
    r"마이다스|에셋플러스|TIMEFOLIO|KoAct|WON|BNK|HK|DAISHIN343)\b"
    r"|액티브|레버리지|인버스|ETN", re.IGNORECASE)


def _is_etf_name(name):
    return bool(_ETF_RE.search(str(name or "")))


_SEC_LOOKUP = None


def _sector_of_name(name, code=None):
    """종목명(→krx_companies 코드) → 업종(krx_code_sector 전 종목 맵). 우선주는
    보통주 코드 폴백. 촉매 타임라인 섹터 컬럼용(2026-09-09) — 미해석은 None."""
    global _SEC_LOOKUP
    if _SEC_LOOKUP is None:
        code_sec, name_code = {}, {}
        try:
            with open(os.path.join(ROOT, "public", "assets", "krx_code_sector.json"),
                      encoding="utf-8") as f:
                code_sec = dict((json.load(f) or {}).get("map") or {})
        except Exception:
            pass
        try:
            with open(os.path.join(ROOT, "public", "assets", "krx_companies.json"),
                      encoding="utf-8") as f:
                for e in json.load(f):
                    nm = (e.get("name") or "").strip()
                    c = str(e.get("code") or "").zfill(6)
                    if nm and c != "000000" and nm not in name_code:
                        name_code[nm] = c
        except Exception:
            pass
        _SEC_LOOKUP = (code_sec, name_code)
    code_sec, name_code = _SEC_LOOKUP
    c = str(code).zfill(6) if code else name_code.get(str(name or "").strip())
    if not c:
        return None
    return code_sec.get(c) or (code_sec.get(c[:5] + "0") if c[5] != "0" else None)


def _code_of_name(name):
    """종목명 → 코드 (krx_companies) — 스크리닝 Join Key(§5). 미등재는 None."""
    _sector_of_name("__init__")            # 룩업 캐시 초기화 겸용
    return _SEC_LOOKUP[1].get(str(name or "").strip())


def _norm_sec(s):
    """섹터명 정규화 — sectorFlow(KIS 업종지수명)와 krx_code_sector 값 매칭용."""
    return re.sub(r"[\s·・()]", "", str(s or ""))


# ── 섹터 시그널 종목 관찰 4-Matrix (2026-09-09 V1) ────────────────────────────
# Top-down: ①섹터x수급 신호(동반강세/수급유입/수급이탈/동반약세) → ②소속 종목
# → ③수급·공매도·대차·촉매 Evidence 결합 → ④동방향 종목 Top5. 결과는 JSON 에
# 저장해 웹/엑셀이 동일 결과를 렌더(§28). 예측 아님 — 관찰 우선순위 산정.
_MATRIX_SUB = {"동반강세": "추세 지속형 상방 관찰", "수급유입": "초기 유입형 상방 관찰",
               "수급이탈": "상승 후 약화 관찰", "동반약세": "약세 지속형 하방 관찰"}
# 신호 섹터 시총 상위 보강 수(2026-09-11) — 0 이면 보강 끔(구 유니버스)
SCREEN_SECTOR_TOPN = int(os.environ.get("SCREEN_SECTOR_TOPN", "10") or 0)
# 칸(신호)별 최대 표시 종목 수 — 3→5(2026-09-11 사용자 요청). 웹/엑셀은 저장값 전부 렌더
SCREEN_MATRIX_TOPK = int(os.environ.get("SCREEN_MATRIX_TOPK", "5") or 5)
_SEC_TOP = None


def _sector_top_caps():
    """정규화 업종명 -> [(code, name)] 시총 내림차순(KOSPI+KOSDAQ 합산, krx_sector_map)."""
    global _SEC_TOP
    if _SEC_TOP is None:
        _SEC_TOP = {}
        try:
            with open(os.path.join(ROOT, "public", "assets", "krx_sector_map.json"),
                      encoding="utf-8") as f:
                sm = json.load(f) or {}
            for key, s in (sm.get("sectors") or {}).items():
                xs = [x for x in (s.get("stocks") or []) + (s.get("kosdaqStocks") or [])
                      if x.get("code")]
                xs.sort(key=lambda x: -(x.get("cap") or 0))
                _SEC_TOP[_norm_sec(s.get("name") or key)] = [
                    (str(x["code"]).zfill(6), x.get("name")) for x in xs]
        except Exception as ex:
            print(f"[weekly] krx_sector_map 로드 실패(섹터 보강 생략): {ex}", file=sys.stderr)
    return _SEC_TOP


def build_sector_screen(sector_rows, netbuy_cum, short_loan, stock_rows, flow_fill=None):
    """스크리닝 결과 {matrix, debug, universeNote}. 규칙(V1):
    - Evidence 축: 외인/기관(핵심), 대차 증감, 공매도 상위 등재(약한 부정).
      연기금은 기관계에 포함되어 점수 미가산(§7 중복 방지) — 표시·디버그만.
      촉매는 방향 분류 데이터가 없어 Neutral(§10) — 표시·디버그만.
    - 결측은 축 자체를 건너뜀(0 취급 금지 §32), Main 후보는 서로 다른 축
      2개 이상 동방향(§33), Matrix 방향과 역행 종목은 debug 에 보존(§14).
    - 유니버스 보강(2026-09-11): A) 신호 섹터마다 시총 상위 SCREEN_SECTOR_TOPN
      편입, B) 신호 섹터 종목의 결손 축(외인·기관·대차)을 flow_fill(codes) —
      _flow_week 주간 누적 — 으로 채움. 기존 값은 보존(순매수 카드 수치 일치)."""
    states = {}
    for r in sector_rows or []:
        if r.get("signal") in _MATRIX_SUB:
            states[_norm_sec(r.get("name"))] = (r.get("name"), r.get("signal"))

    stocks = {}
    by_name = {}            # 이름 → 코드 (공매도·대차 랭킹은 이름만 옴 — '현대차'처럼
                            # _code_of_name 미해석 시 별도 행으로 쪼개지던 문제, 2026-09-11)

    def feat(name, code=None):
        code = ((str(code).zfill(6) if code else None) or _code_of_name(name)
                or by_name.get(name))
        key = code or f"n:{name}"
        e = stocks.setdefault(key, {"code": code, "name": name})
        if code:
            by_name.setdefault(name, code)
        return e

    nc = netbuy_cum or {}
    for grp in ("top", "bottom"):
        for x in ((nc.get("total") or {}).get(grp) or []):
            e = feat(x.get("name"), x.get("code"))
            for src, dst, div in (("frgn", "frgn", 100), ("orgn", "orgn", 100),
                                  ("prsn", "prsn", 100), ("shortSum", "shortSum", 1),
                                  ("loanAmt", "loanAmt", 1), ("loanChg", "loanChg", 1)):
                v = x.get(src)
                if v is not None and dst not in e:
                    e[dst] = round(v / div, 1)          # 수급 백만원→억
        for inv, key in (("frgn", "frgn"), ("orgn", "orgn"), ("fund", "fund"),
                         ("prsn", "prsn")):
            for x in ((nc.get(inv) or {}).get(grp) or []):
                e = feat(x.get("name"), x.get("code"))
                if x.get("amt") is not None and key not in e:
                    e[key] = round(x["amt"] / 100, 1)
    sl = short_loan or {}
    for x in (sl.get("shortTop") or []):
        e = feat(x.get("name"))
        e["shortListed"] = True
        if x.get("amt") is not None and "shortSum" not in e:
            e["shortSum"] = round(x["amt"], 1)
    for lst in ("loanUp", "loanDown"):
        for x in (sl.get(lst) or []):
            e = feat(x.get("name"))
            if x.get("chg") is not None and "loanChg" not in e:
                e["loanChg"] = round(x["chg"], 1)
            if x.get("amt") is not None and "loanAmt" not in e:
                e["loanAmt"] = round(x["amt"], 1)
    for t in stock_rows or []:                          # 촉매 — 종목당 1회(§30)
        if not t.get("stock"):
            continue
        e = feat(t["stock"])
        if (t.get("star") or 0) >= (e.get("catStar") or 0):
            e["catStar"] = t.get("star") or 0
            e["catText"] = t.get("event") or ""

    # A. 신호 섹터 시총 상위 편입 — 순매수 상위 30 등재 중심 유니버스는 대형 반도체주
    # 쏠림(9/10: 68종목 중 전기·전자 25)이라 수급 규모가 작은 섹터(전기·가스·부동산
    # 등)는 0종목 → 수급유입/수급이탈 칸이 구조적으로 비었다.
    for e in stocks.values():
        e.setdefault("src", "list")
    n_added = 0
    if SCREEN_SECTOR_TOPN > 0 and states:
        tops = _sector_top_caps()
        have = {e.get("name") for e in stocks.values()}
        for nsec, (sname, _sig) in states.items():
            for code, name in (tops.get(nsec) or [])[:SCREEN_SECTOR_TOPN]:
                if code in stocks or name in have:
                    continue
                e = feat(name, code)
                e.update({"src": "sector", "secHint": sname})
                have.add(name)
                n_added += 1
    for e in stocks.values():
        e["sector"] = _sector_of_name(e.get("name"), e.get("code")) or e.get("secHint")

    # B. 결손 축 채우기 — 투자자별 목록·공매도·촉매로만 들어온 종목은 외인/기관 중
    # 한 축뿐이라(9/10: 유니버스 60%가 축 ≤1) '동방향 2축' 판정이 원천 불가였다.
    # 신호 섹터 종목만 /flow 주간 누적으로 빈 축을 채운다(기존 값 보존).
    n_fill = 0
    need = [e["code"] for e in stocks.values()
            if e.get("code") and e.get("sector") and _norm_sec(e["sector"]) in states
            and any(not isinstance(e.get(k), (int, float)) for k in ("frgn", "orgn", "loanChg"))]
    if flow_fill and need:
        try:
            fl = flow_fill(need) or {}
        except Exception as ex:
            print(f"[weekly] 스크리닝 결손 축 채우기 실패(기존 유니버스로 진행): {ex}",
                  file=sys.stderr)
            fl = {}
        for e in stocks.values():
            f = fl.get(e.get("code"))
            if not f:
                continue
            got = False
            for k, div in (("frgn", 100), ("orgn", 100), ("prsn", 100),      # 백만원→억
                           ("shortSum", 1), ("loanAmt", 1), ("loanChg", 1)):
                if not isinstance(e.get(k), (int, float)) and isinstance(f.get(k), (int, float)):
                    e[k] = round(f[k] / div, 1)
                    got = True
            if got:
                e["filled"] = True
                n_fill += 1

    def fmt_eok(v):
        return f"{'+' if v > 0 else ''}{round(v):,}억"

    matrix = {k: [] for k in _MATRIX_SUB}
    debug = []
    for key, e in stocks.items():
        sec = e.get("sector")
        st = states.get(_norm_sec(sec)) if sec else None
        sig = st[1] if st else None
        pos, neg = [], []                               # (축, 문구)
        if isinstance(e.get("frgn"), (int, float)) and e["frgn"]:
            (pos if e["frgn"] > 0 else neg).append(
                ("frgn", f"외국인 {fmt_eok(e['frgn'])} {'순매수' if e['frgn'] > 0 else '순매도'}"))
        if isinstance(e.get("orgn"), (int, float)) and e["orgn"]:
            (pos if e["orgn"] > 0 else neg).append(
                ("orgn", f"기관 {fmt_eok(e['orgn'])} {'순매수' if e['orgn'] > 0 else '순매도'}"))
        if isinstance(e.get("loanChg"), (int, float)) and e["loanChg"]:
            (pos if e["loanChg"] < 0 else neg).append(
                ("loan", f"대차잔고 {fmt_eok(e['loanChg'])} {'감소' if e['loanChg'] < 0 else '증가'}"))
        if e.get("shortListed"):
            neg.append(("short", "공매도 누적 상위 등재"))
        score = len(pos) - len(neg)
        e.update({"score": score, "posAxes": [a for a, _ in pos],
                  "negAxes": [a for a, _ in neg]})
        picked, reason = False, ""
        if sig in ("동반강세", "수급유입") and score > 0 and len(pos) >= 2:
            picked = True
        elif sig in ("수급이탈", "동반약세") and score < 0 and len(neg) >= 2:
            picked = True
        if picked:
            ev = pos if score > 0 else neg
            reason = (", ".join(p for _, p in ev[:4])
                      + f" — {st[0]}({sig}) 섹터 {_MATRIX_SUB[sig]}")
            matrix[sig].append({
                "code": e.get("code"), "name": e["name"], "sector": st[0],
                "sectorSignal": sig, "score": score, "reason": reason,
                "evidence": {k: e.get(k) for k in
                             ("frgn", "orgn", "fund", "prsn", "shortSum",
                              "loanAmt", "loanChg", "catStar", "catText")}})
        debug.append({"code": e.get("code"), "name": e["name"], "sector": sec,
                      "sectorSignal": sig,
                      "frgn": e.get("frgn"), "orgn": e.get("orgn"),
                      "fund": e.get("fund"), "prsn": e.get("prsn"),
                      "shortSum": e.get("shortSum"), "loanAmt": e.get("loanAmt"),
                      "loanChg": e.get("loanChg"),
                      "catStar": e.get("catStar"), "catText": e.get("catText"),
                      "catDirection": None,             # V1: 방향 데이터 없음(§10)
                      "posEvidence": len(pos), "negEvidence": len(neg),
                      "score": score, "matrix": sig,
                      "picked": picked, "reason": reason,
                      "src": e.get("src"), "filled": bool(e.get("filled"))})

    def mag(x):                                         # 동점 시 외인+기관 규모 큰 순
        ev = x["evidence"]
        return abs((ev.get("frgn") or 0) + (ev.get("orgn") or 0))
    # 정렬·Top N: 상방=점수(수급유입은 축 수 우선) 내림, 하방=점수 오름(§13, §15)
    matrix["동반강세"].sort(key=lambda x: (-x["score"], -mag(x)))
    matrix["수급유입"].sort(key=lambda x: (-x["score"], -mag(x)))   # 점수=동방향 축 수(V1)
    matrix["수급이탈"].sort(key=lambda x: (x["score"], -mag(x)))
    matrix["동반약세"].sort(key=lambda x: (x["score"], -mag(x)))
    for k in matrix:
        matrix[k] = matrix[k][:SCREEN_MATRIX_TOPK]
    return {"matrix": matrix, "debug": debug,
            "sub": _MATRIX_SUB,
            "coverage": {"added": n_added, "filled": n_fill, "topN": SCREEN_SECTOR_TOPN},
            "universeNote": "현재 확보된 주간 브리핑 데이터 유니버스(순매수 상위 30 등재 · "
                            "공매도/대차 랭킹 · 주간 촉매 · 신호 섹터 시총 상위"
                            f"{' ' + str(SCREEN_SECTOR_TOPN) if SCREEN_SECTOR_TOPN else ''}) "
                            "기준 — 전 시장 스크리닝 아님"}


# ── 섹터 x 수급 매트릭스 V1 신호 (2026-09-09) ─────────────────────────────────
# 미래 예측이 아닌 '가격 추세 x 외인·기관 수급'의 상태 진단. 매수/매도/예상 등
# 예측성 표현 금지, 기존 1W 단독 '과열주의' 로직 폐기. 신호는 생성기에서 한 번만
# 계산해 JSON 에 저장 — 웹/엑셀은 저장값을 그대로 렌더(판정 불일치 방지).
def classify_price_trend(ytd, r3m, r1m, r1w):
    """4개 기간(YTD/3M/1M/1W) 부호 중 3개 이상 동일 방향 → STRONG/WEAK, 그 외
    MIXED. 결측(None)이 하나라도 있으면 None(임의로 0 취급 금지 — V1 §18)."""
    vals = [ytd, r3m, r1m, r1w]
    if any(not isinstance(v, (int, float)) for v in vals):
        return None
    pos = sum(1 for v in vals if v > 0)
    neg = sum(1 for v in vals if v < 0)
    if pos >= 3:
        return "PRICE_STRONG"
    if neg >= 3:
        return "PRICE_WEAK"
    return "PRICE_MIXED"


# 섹터 신호 수급 중립 구간(2026-09-11 사용자 요청 — 상대 기준): 섹터마다 기간 직전
# 최근 N주(달력 주)의 외인+기관 주간 합 절대값을 거래일 수로 나눈 1일 평균 = 평소
# 규모. 이번 기간 |외인+기관| < RATIO x 평소 1일 규모 x 기간 거래일 수 → 혼조.
# 섹터 규모 차(전기·전자 수만억 vs 종이·목재 수억)를 절대 금액 기준 없이 흡수.
SECTOR_NEUTRAL_RATIO = float(os.environ.get("SECTOR_FLOW_NEUTRAL_RATIO", "0.3") or 0)
SECTOR_NEUTRAL_WEEKS = 8            # 기준선 주 수(허브 daily 최대 40거래일 창 안)
SECTOR_NEUTRAL_MIN_WEEKS = 3        # 이보다 적으면 기준선 없음 → 부호만(구 동작)
SECTOR_FLOW_DAYS = 40               # /sector-flow 요청 창(허브 상한) — 기준선·1W 기준 종가


def _flow_baseline(daily, period_start):
    """평소 수급 규모(억/거래일) — period_start(YYYYMMDD) 이전 행을 달력 주로 묶어
    주간 |외인+기관| / 그 주 거래일 수의 평균(최근 SECTOR_NEUTRAL_WEEKS 주).
    주 수 < SECTOR_NEUTRAL_MIN_WEEKS 면 None."""
    wk = {}
    for r in daily or []:
        dd = str(r.get("date") or "")
        if len(dd) != 8 or dd >= period_start:
            continue
        if not any(r.get(k) for k in ("frgn", "orgn", "prsn", "chgPct")):
            continue                        # 빈 행(휴장·미확정)
        key = datetime.date(int(dd[:4]), int(dd[4:6]), int(dd[6:])).isocalendar()[:2]
        a = wk.setdefault(key, [0.0, 0])
        a[0] += float(r.get("frgn") or 0.0) + float(r.get("orgn") or 0.0)
        a[1] += 1
    keys = sorted(wk)[-SECTOR_NEUTRAL_WEEKS:]
    if len(keys) < SECTOR_NEUTRAL_MIN_WEEKS:
        return None
    return sum(abs(wk[k][0]) / wk[k][1] for k in keys) / len(keys) / 100   # 백만원→억


def _period_close_return(daily, want):
    """기간 등락 시점 대 시점(2026-09-11) — (기간 내 마지막 종가 / 기간 직전 마지막
    종가 − 1) x 100. 종가 결측(허브 구버전·필드 부재=0)·기준 행이 창 밖이면 None."""
    rows = sorted((str(r.get("date") or ""), float(r.get("close") or 0.0))
                  for r in daily or [])
    start = min(want)
    cur = [c for dd, c in rows if dd in want and c > 0]
    base = [c for dd, c in rows if dd < start and c > 0]
    if not cur or not base:
        return None
    return round((cur[-1] / base[-1] - 1) * 100, 2)


def calculate_core_flow(frgn, orgn):
    """핵심 수급 = 외인+기관 주간 순매수 합 (연기금·개인은 표시만, 판정 제외)."""
    if not (isinstance(frgn, (int, float)) and isinstance(orgn, (int, float))):
        return None
    return frgn + orgn


def classify_sector_signal(ytd, r3m, r1m, r1w, frgn, orgn, neutral=None):
    """최종 신호: 동반강세/수급이탈/수급유입/동반약세 + 혼조 + 데이터부족.
    수급 0(FLOW_NEUTRAL)은 강제 분류하지 않고 혼조로 표기(실데이터상 희박).
    neutral(억, 2026-09-11): |외인+기관| 이 이 값 미만이면 '수급 미미'로 혼조 —
    섹터별 평소 수급 규모 대비 상대 기준(_flow_baseline). None 이면 부호만."""
    trend = classify_price_trend(ytd, r3m, r1m, r1w)
    core = calculate_core_flow(frgn, orgn)
    if trend is None or core is None:
        return "데이터부족"
    if trend == "PRICE_MIXED" or core == 0 or (neutral and abs(core) < neutral):
        return "혼조"
    if trend == "PRICE_STRONG":
        return "동반강세" if core > 0 else "수급이탈"
    return "수급유입" if core > 0 else "동반약세"


def _snapshot_breadth(today):
    """허브 /breadth(등락 종목수 + 신고가 근접)를 당일 키로 아카이브 — 주간 ADR
    추이·신고가 섹터 그룹핑 입력(2026-09-08 Phase 2⑥). 장전(값 전부 0)이나 실패
    시 저장하지 않는다(16:10 실행 전제 = 당일 마감 값)."""
    base = FLOW_RANK_URL.rsplit("/", 1)[0]
    try:
        req = urllib.request.Request(f"{base}/breadth", headers={"User-Agent": "weekly-briefing"})
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8"))
        counts = d.get("counts") or {}
        ks = counts.get("kospi") or {}
        if not (ks.get("up") or ks.get("down")):      # 장전/휴장 보호 — 0이면 스킵
            print("[weekly] breadth 값 없음(장전/휴장?) — 스냅샷 생략", file=sys.stderr)
            return
    except Exception as ex:
        print(f"[weekly] breadth 조회 실패(스냅샷 생략): {ex}", file=sys.stderr)
        return
    os.makedirs(BREADTH_DIR, exist_ok=True)
    snap = {"date": today, "asof": d.get("asof"),
            "counts": counts,
            "newHighs": [x for x in (d.get("newHighs") or [])
                         if not _is_etf_name(x.get("name"))]}
    if d.get("nearDiag"):       # 허브 구간 분할 조회 진단(2026-09-11) — 30건 잘림 사후 확인용
        snap["nearDiag"] = d["nearDiag"]
    with open(os.path.join(BREADTH_DIR, f"{today}.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    idx_path = os.path.join(BREADTH_DIR, "index.json")
    try:
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
    except OSError:
        idx = []
    if today not in idx:
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(sorted(set(idx) | {today}, reverse=True), f, indent=1)
    print(f"[weekly] breadth 스냅샷 저장: {today} (신고가근접 {len(snap['newHighs'])}종목)")


def _ticker_of_codes(codes):
    """krx_companies.json 에서 code→yfinance 티커. 미등재 코드는 제외."""
    out = {}
    try:
        with open(os.path.join(ROOT, "public", "assets", "krx_companies.json"),
                  encoding="utf-8") as f:
            for e in json.load(f):
                c = str(e.get("code", "")).zfill(6)
                if c in codes and e.get("ticker"):
                    out[c] = e["ticker"]
    except Exception as ex:
        print(f"[weekly] krx_companies 로드 실패: {ex}", file=sys.stderr)
    return out


def _week_cum_codes(codes, week_start, fetch_closes=None):
    """코드별 '주간 누적' 등락률(%) — 전주 마지막 종가 대비 최신 종가.
    _week_cum_map(이름 기반)과 동일 산식, 신고가 카드용 코드 직접 해석. fail-open."""
    tick_of = _ticker_of_codes(set(codes))
    if not tick_of:
        return {}
    fetch_closes = fetch_closes or _yf_closes
    try:
        closes = fetch_closes(sorted(set(tick_of.values())))
    except Exception as ex:
        print(f"[weekly] 신고가 주간 등락률 조회 실패: {ex}", file=sys.stderr)
        return {}
    out = {}
    for code, t in tick_of.items():
        rows_t = closes.get(t) or []
        base = None
        for dt, cl in rows_t:
            if dt < week_start:
                base = cl
        if base and rows_t and rows_t[-1][0] >= week_start:
            out[code] = round((rows_t[-1][1] / base - 1.0) * 100, 2)
    # 진단(2026-09-08): 신고가 종목 주간등락이 전부 0.0 — 원본 종가 샘플을 남겨
    # yfinance 데이터 문제(정지 종목/스테일)인지 산식 문제인지 판별한다.
    for code, t in list(tick_of.items())[:3]:
        print(f"[weekly] cum 샘플 {code}({t}): {(closes.get(t) or [])[-4:]} -> {out.get(code)}",
              file=sys.stderr)
    return out


_FLOW_RAW: dict = {}      # "code:rows" -> /flow 응답 — 1런 1회 왕복(메모이즈, 2026-09-09)
# daily 거래일 수 — 허브 flow_payload 기본 5. 주간은 5로 충분, 월간·과거 백필은
# 환경변수로 확장(FLOW_ROWS=30 등, 2026-09-10 P1). 공매도·대차는 허브가 max(rows,20).
FLOW_ROWS = int(os.environ.get("FLOW_ROWS", "5") or 5)


def _flow_raw(code, rows=None):
    """허브 /flow 응답 프로세스 메모이즈 — 백필·공매도대차·신고가·타임라인이
    같은 종목을 중복 왕복하지 않게 한다(마감 후 단일 런이라 스테일 없음).
    rows = daily 거래일 수(허브 기본 5, 월간·과거 백필은 25~45 — 2026-09-10 P1).
    실패는 캐시하지 않고 예외 전파(호출측 fail-open 유지)."""
    rows = rows or FLOW_ROWS
    key = f"{code}:{rows}"
    if key in _FLOW_RAW:
        return _FLOW_RAW[key]
    base = FLOW_RANK_URL.rsplit("/", 1)[0]
    req = urllib.request.Request(f"{base}/flow?code={code}&rows={rows}",
                                 headers={"User-Agent": "weekly-briefing"})
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode("utf-8"))
    _FLOW_RAW[key] = d
    return d


_TMARK = {}               # 구간 타이머 상태


def _tmark(label=None):
    """런타임 구간 계측 — 직전 구간 경과를 [weekly][t] 로 출력하고 새 구간 시작."""
    import time as _time
    now = _time.monotonic()
    if _TMARK.get("label") is not None:
        print(f"[weekly][t] {_TMARK['label']}: {now - _TMARK['t']:.1f}s", flush=True)
    _TMARK.update(t=now, label=label)


def _flow_week(codes, dates, fetch=None):
    """코드별 '주간 누적' 확정 수급 — 허브 /flow daily(FHPTJ04160001)에서 이번 주
    날짜 행을 합산. 반환 {code: {frgn,orgn,prsn(백만원), shortSum(억),
    loanAmt,loanChg(억), weekChgPct(%)}}. weekChgPct 는 같은 응답의 일자별
    종가로 계산(전주 마지막 종가 대비 — yfinance _week_cum_codes 와 동일 산식,
    2026-09-09 KIS 전환). 실패 종목은 제외(fail-open — UI 는 '—' 표시)."""
    from concurrent.futures import ThreadPoolExecutor
    want = {dt.strftime("%Y%m%d") for dt in dates}
    fetch = fetch or _flow_raw          # 메모이즈 공유(중복 왕복 제거, 2026-09-09)

    def _one(code):
        try:
            d = fetch(code)
            acc, got = {"frgn": 0.0, "orgn": 0.0, "prsn": 0.0}, False
            for row in (d.get("daily") or []):
                if str(row.get("date")) in want:
                    got = True
                    for k in acc:
                        acc[k] += float(row.get(k) or 0.0)
            # 장중(FHPTJ04160001 15:40 전 차단)엔 daily 가 비어도 공매도·대차(별도 TR,
            # T+1 공개)는 응답하므로 부분 결과를 낸다(2026-09-10 장중 실행 공백 해소)
            wk_start = min(want)
            short = sum(float(r0.get("pbmn") or 0.0) for r0 in (d.get("shorts") or [])
                        if str(r0.get("date")) in want)
            loans_all = sorted((str(r0.get("date")), r0) for r0 in (d.get("loans") or []))
            loans = [x for x in loans_all if x[0] in want]
            prior = [x for x in loans_all if x[0] < wk_start]
            out = {k: round(v) for k, v in acc.items()} if got else {}
            if short:
                out["shortSum"] = round(short, 1)
            if loans:
                last = float(loans[-1][1].get("rmndAmt") or 0.0)
                out["loanAmt"] = round(last, 1)
                # 증감 기준 = 전주 마지막 잔고(2026-09-09, 월요일 단일 행도 증감
                # 산출) — 시계열(~20거래일)에 전주 행이 없으면 주중 첫 행 폴백
                base0 = (float(prior[-1][1].get("rmndAmt") or 0.0) if prior
                         else float(loans[0][1].get("rmndAmt") or 0.0))
                out["loanChg"] = round(last - base0, 1)
            # 주간 등락률 — 주중 최신 종가 / 전주 마지막 종가 (기준 종가가 daily
            # 범위(~20거래일) 밖이거나 0(신규상장 등)이면 결손 유지)
            closes = sorted((str(r0.get("date")), float(r0.get("close") or 0.0))
                            for r0 in (d.get("daily") or []))
            in_wk = [c for dd, c in closes if dd in want and c > 0]
            base = [c for dd, c in closes if dd < wk_start and c > 0]
            if in_wk and base:
                out["weekChgPct"] = round((in_wk[-1] / base[-1] - 1) * 100, 2)
            if not out:
                return code, None
            return code, out
        except Exception:
            return code, None

    out = {}
    with ThreadPoolExecutor(max_workers=6) as tp:
        for code, v in tp.map(_one, codes):
            if v:
                out[code] = v
    return out


def _breadth_weekly(dates):
    """주간 ADR 추이 + 최신일 신고가 근접 섹터 그룹핑."""
    rows, latest = [], None
    for dt in dates:
        d = _fetch(f"reports/breadth/{dt.isoformat()}.json")
        if not d:
            continue
        c = d.get("counts") or {}
        rows.append({"date": dt.isoformat(),
                     "kospi": {k: (c.get("kospi") or {}).get(k) for k in ("up", "down", "upLimit")},
                     "kosdaq": {k: (c.get("kosdaq") or {}).get(k) for k in ("up", "down", "upLimit")}})
        latest = d
    if not rows:
        return None
    out = {"days": rows}
    # ETF 제외 — 스냅샷 단계에서도 거르지만, 필터 도입 전 저장분(9/8 등) 방어
    highs = [x for x in ((latest or {}).get("newHighs") or [])
             if not _is_etf_name(x.get("name"))]
    if highs:
        # 섹터 그룹핑 — krx_sector_map (보통주 폴백)
        sec = {}
        try:
            with open(os.path.join(ROOT, "public", "assets", "krx_sector_map.json"),
                      encoding="utf-8") as f:
                sm = json.load(f) or {}
            for name, s in (sm.get("sectors") or {}).items():
                for x in (s.get("stocks") or []) + (s.get("kosdaqStocks") or []):
                    code = str(x.get("code") or "").zfill(6)
                    if code and code not in sec:
                        sec[code] = name
        except Exception:
            pass
        groups = {}
        for hgh in highs[:40]:
            code = (hgh.get("code") or "").zfill(6)
            nm = sec.get(code) or (sec.get(code[:5] + "0") if code and code[5] != "0" else None) or "기타"
            groups.setdefault(nm, []).append(hgh.get("name"))
        # 종목 상세 — UI 카드 표(순매수 상위/하위와 동일 컬럼)용, 2026-09-08.
        # 주간 누적 수급(/flow daily 합산)과 주간 등락률(yfinance)을 부착 — 실패 시
        # 해당 값만 결손(UI '—'), 카드 자체는 유지.
        mkt = {}
        try:
            with open(os.path.join(ROOT, "public", "assets", "krx_companies.json"),
                      encoding="utf-8") as f:
                for c0 in json.load(f):
                    code0 = str(c0.get("code") or "").zfill(6)
                    raw = c0.get("market") or ""
                    mkt[code0] = "KOSDAQ" if "코스닥" in raw else "KOSPI"
        except Exception:
            pass
        stocks = [{**{k: x.get(k) for k in ("code", "name", "chgPct", "nearRate")},
                   "market": mkt.get(str(x.get("code") or "").zfill(6))}
                  for x in highs[:30]]
        codes = [str(s.get("code") or "").zfill(6) for s in stocks]
        flow = _flow_week(codes, dates)
        # 폴백(2026-09-10): /flow 결손 키(장중 daily 차단 → 순매수 3종 등)는 아카이브
        # 누적(등재 종목만)으로 채우고, 그래도 없으면 아래 이월 로직으로
        acc_fb = None
        for s, code in zip(stocks, codes):
            s.update(flow.get(code) or {})
            if s.get("frgn") is None or s.get("shortSum") is None:
                if acc_fb is None:
                    acc_fb = _archive_acc(dates)[0]
                if code in acc_fb:
                    for k, v in _archive_flow_row(acc_fb[code]).items():
                        if s.get(k) is None:
                            s[k] = v
        # 주간 등락률은 KIS(/flow 종가) 우선(2026-09-09) — 결손 종목만 yfinance 폴백
        need_cum = [c for s, c in zip(stocks, codes) if s.get("weekChgPct") is None]
        cum = _week_cum_codes(need_cum, dates[0].isoformat()) if need_cum else {}
        for s, code in zip(stocks, codes):
            if s.get("weekChgPct") is None and code in cum:
                s["weekChgPct"] = cum[code]
        # 장중 재실행 보호(2026-09-09 실측): /flow daily(FHPTJ04160001)는 15:40 이전
        # 차단이라 장중 dispatch 는 수급이 통째로 비고, 그대로 저장하면 직전(마감 후)
        # 산출물의 값을 '지운다'. 결손 종목은 기존 스냅샷 값을 이월 — 마감 후 정규
        # 실행이 최신값으로 자연 갱신한다.
        try:
            with open(SNAP, encoding="utf-8") as f:
                prev = {str(p.get("code") or "").zfill(6): p
                        for p in (((json.load(f).get("breadth") or {})
                                   .get("newHighs") or {}).get("stocks") or [])}
        except Exception:
            prev = {}
        n_carry = 0
        for s, code in zip(stocks, codes):
            p = prev.get(code)
            if not p:
                continue
            carried = False
            for k in ("frgn", "orgn", "prsn", "shortSum", "loanAmt", "loanChg",
                      "weekChgPct"):
                if s.get(k) is None and p.get(k) is not None:
                    s[k] = p[k]
                    carried = True
            n_carry += 1 if carried else 0
        n_wc = sum(1 for s in stocks if s.get("weekChgPct") is not None)
        print(f"[weekly] 신고가 카드 보강: 수급 {len(flow)}/{len(codes)} · "
              f"주간등락 {n_wc}/{len(codes)} (yf 폴백 {len(cum)}) · 이월 {n_carry}종목")
        out["newHighs"] = {"date": rows[-1]["date"], "count": len(highs),
                           "stocks": stocks,
                           "groups": sorted(({"sector": k, "stocks": v[:6]}
                                             for k, v in groups.items()),
                                            key=lambda g: -len(g["stocks"]))[:8]}
    return out


def _short_loan_weekly(dates, flow=None):
    """공매도 주간 누적·대차잔고 주간 증감 상/하위 — 유니버스는 그 주 netbuy_rank
    등재 종목 전체, 값은 /flow shorts·loans 시계열 주간 직접 계산(_flow_week
    재사용, 2026-09-09). 등재일 값만 누적하던 구 방식의 미등재일 공매도 누락·
    잔고 스테일을 제거. 반환 구조 {shortTop, loanUp, loanDown}는 기존과 동일.
    같은 런의 백필·신고가 카드와 겹치는 종목은 허브 10분 캐시로 무료."""
    uni = {}
    for dt in dates:
        d2 = _fetch(f"reports/netbuy_rank/{dt.isoformat()}.json")
        if not d2:
            continue
        for rows in (d2.get("lists") or {}).values():
            for r in rows or []:
                if r.get("code"):
                    uni.setdefault(r["code"], r.get("name"))
    if not uni:
        return None
    fw = dict(flow) if flow is not None else _flow_week(sorted(uni), dates)
    # 폴백(2026-09-10): /flow 결손 종목은 아카이브 누적(등재일 근사)으로 — 장중 daily
    # 차단은 _flow_week 가 공매도·대차만 부분 반환해 대부분 커버, 허브 장애 시 전량
    acc, n_fb = None, 0
    for c in uni:
        if c in fw:
            continue
        if acc is None:
            acc = _archive_acc(dates)[0]
        if c in acc:
            fw[c] = _archive_flow_row(acc[c])
            n_fb += 1
    ent = [{"name": uni[c], **v} for c, v in fw.items() if c in uni]
    if not ent:
        return None
    short_top = sorted([e for e in ent if e.get("shortSum", 0) > 0],
                       key=lambda x: -x["shortSum"])[:10]
    loan_up = sorted([e for e in ent if (e.get("loanChg") or 0) > 0],
                     key=lambda x: -x["loanChg"])[:10]
    loan_dn = sorted([e for e in ent if (e.get("loanChg") or 0) < 0],
                     key=lambda x: x["loanChg"])[:10]
    print(f"[weekly] 공매도·대차 주간(flow): 유니버스 {len(uni)} · 수급응답 {len(ent)}"
          f"(아카이브 폴백 {n_fb}) · 공매도 {len(short_top)} · 대차증가 {len(loan_up)} · "
          f"대차감소 {len(loan_dn)}")
    return {
        "basis": "archive" if n_fb and n_fb >= len(ent) else ("mixed" if n_fb else "flow"),
        "shortTop": [{"name": e["name"], "amt": round(e["shortSum"])} for e in short_top],
        "loanUp": [{"name": e["name"], "chg": e["loanChg"], "amt": e.get("loanAmt")} for e in loan_up],
        "loanDown": [{"name": e["name"], "chg": e["loanChg"], "amt": e.get("loanAmt")} for e in loan_dn],
    }


def _snapshot_netbuy_rank(today):
    """KIS 허브 /flow-rank(외인·기관 순매수 상위, 연기금 금액 포함)를 당일 키로
    아카이브 — 라이브 전용이던 랭킹을 일자별 DB 누적으로 전환(2026-09-07).
    reports/netbuy_rank/<date>.json 은 classify 가 제네릭 처리하므로 dual-write 로
    자동 업서트된다. 실패 시 기존 파일 유지(주말/휴장은 호출측에서 스킵)."""
    try:
        req = urllib.request.Request(FLOW_RANK_URL, headers={"User-Agent": "weekly-briefing"})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode("utf-8"))
        lists = d.get("lists") or {}
        if not any(lists.values()):
            raise ValueError("빈 랭킹 응답")
    except Exception as ex:
        print(f"[weekly] flow-rank 스냅샷 실패(기존 유지): {ex}", file=sys.stderr)
        return
    snap = {"date": today, "asof": d.get("asof"), "lists": lists}
    _enrich_final(snap)
    os.makedirs(RANK_DIR, exist_ok=True)
    with open(os.path.join(RANK_DIR, f"{today}.json"), "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    idx_path = os.path.join(RANK_DIR, "index.json")
    try:
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
    except OSError:
        idx = []
    if today not in idx:
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(sorted(set(idx) | {today}, reverse=True), f, indent=1)
    print(f"[weekly] netbuy_rank 스냅샷 저장: {today} (asof {d.get('asof')})")


def _enrich_final(snap):
    """가집계 랭킹 스냅샷에 종목별 일별 '확정' 순매수를 병합 (snap['final']).

    허브 /flow 의 daily(FHPTJ04160001 확정, 15:40+ 반영)를 랭킹 등재 종목마다
    조회해 스냅샷 날짜와 일치하는 행만 채택 — 가집계와 확정의 괴리(예: 2026-09-07
    SK하이닉스 외인 가집계 9,575억 vs 확정 1.73조)를 보정한다. 단위는 동일(백만원).
    부분 실패는 그 종목만 가집계 유지(무해). 16:10 실행 전제(확정 반영 이후)."""
    from concurrent.futures import ThreadPoolExecutor
    codes = []
    for rows in (snap.get("lists") or {}).values():
        for r in rows or []:
            c = r.get("code")
            if c and c not in codes:
                codes.append(c)
    if not codes:
        return
    want = snap["date"].replace("-", "")

    def _one(code):
        try:
            d = _flow_raw(code)         # 메모이즈 공유 — 백필·주간 단계가 재사용
            out = None
            for row in (d.get("daily") or []):
                if str(row.get("date")) == want:
                    out = {k: row.get(k) for k in ("prsn", "frgn", "orgn", "fund",
                                                   "scrt", "insu", "ivtr", "pe")}
                    break
            if out is not None:
                # 같은 응답의 공매도(pbmn, 억원)·대차잔고(rmndAmt 억원/rmndChg 주) 당일분 병합
                for row in (d.get("shorts") or []):
                    if str(row.get("date")) == want:
                        out["shortAmt"] = row.get("pbmn")
                        break
                loans = d.get("loans") or []          # 최신순 시계열
                for i, row in enumerate(loans):
                    if str(row.get("date")) == want:
                        out["loanAmt"] = row.get("rmndAmt")
                        out["loanChg"] = row.get("rmndChg")
                        if i + 1 < len(loans):        # 직전 영업일 잔고 — 주간 증감
                            out["loanPrev"] = loans[i + 1].get("rmndAmt")   # 계산용(억)
                        break
                return code, out
        except Exception:
            pass
        return code, None

    final = {}
    with ThreadPoolExecutor(max_workers=6) as tp:
        for code, v in tp.map(_one, codes):
            if v:
                final[code] = v
    snap["final"] = final
    print(f"[weekly] 확정 순매수 병합: {len(final)}/{len(codes)}종목")


def _backfill_netbuy_detail(dates):
    """이번 주 netbuy_rank 아카이브 final 에 기관 세분(scrt/insu/ivtr/pe) 소급 기입.

    FHPTJ04160001 은 한 콜에 종목당 ~20거래일 시계열을 반환하므로(허브 /flow
    daily), 세분 값이 없는 (날짜, 종목)만 모아 종목당 /flow 1콜로 그 주 전 일자를
    한 번에 채운다(2026-09-09). 확정 병합이 통째로 빠졌던 날의 final 도 이때 함께
    생성된다(개인 포함 보정). 2026-09-10 확장: shortAmt 결손과 T+1 공개인 대차
    (직전 거래일 파일만)도 같은 /flow 응답으로 보정 → 아카이브 폴백 품질 확보.
    정착 후엔 직전 거래일 대차 보정 정도만 남는다. 실패 종목은 건너뜀."""
    from concurrent.futures import ThreadPoolExecutor
    KEYS = ("prsn", "frgn", "orgn", "fund", "scrt", "insu", "ivtr", "pe")
    today = datetime.datetime.now(KST).date()
    # 대차는 T+1 공개 — '오늘 이전 가장 최근 거래일' 파일만 대차 결손 대상(무한 재조회 방지)
    loan_day = max((dt for dt in dates if dt < today), default=None)
    snaps, pending = {}, {}             # date_iso -> snap dict / code -> {date_iso,...}
    for dt in dates:
        di = dt.isoformat()
        try:
            with open(os.path.join(RANK_DIR, f"{di}.json"), encoding="utf-8") as f:
                d = json.load(f)
        except OSError:
            continue
        final = d.setdefault("final", {})
        codes = set()
        for rows in (d.get("lists") or {}).values():
            for r in rows or []:
                if r.get("code"):
                    codes.add(r["code"])
        need = set()
        for c in codes:
            e = final.get(c) or {}
            if e.get("scrt") is None or e.get("shortAmt") is None \
                    or (dt == loan_day and e.get("loanAmt") is None):
                need.add(c)
        if need:
            snaps[di] = d
            for c in need:
                pending.setdefault(c, set()).add(di)
    if not pending:
        return

    def _one(code):
        try:
            return code, _flow_raw(code)    # 메모이즈 공유 — 이후 단계 재사용
        except Exception:
            return code, None

    patched = 0
    with ThreadPoolExecutor(max_workers=6) as tp:
        for code, d in tp.map(_one, pending):
            if not d:
                continue
            daily = {str(r0.get("date")): r0 for r0 in (d.get("daily") or [])}
            shorts = {str(r0.get("date")): r0 for r0 in (d.get("shorts") or [])}
            loans = d.get("loans") or []              # 최신순
            for di in pending[code]:
                want = di.replace("-", "")
                e = dict(snaps[di]["final"].get(code) or {})
                n0 = len(e)
                row = daily.get(want)
                if row and row.get("scrt") is not None and e.get("scrt") is None:
                    for k in KEYS:          # 기존 shortAmt/loanAmt 등은 보존
                        e[k] = row.get(k)
                sh = shorts.get(want)
                if sh and e.get("shortAmt") is None:
                    e["shortAmt"] = sh.get("pbmn")
                if e.get("loanAmt") is None:
                    for i, lr in enumerate(loans):
                        if str(lr.get("date")) == want:
                            e["loanAmt"] = lr.get("rmndAmt")
                            e["loanChg"] = lr.get("rmndChg")
                            if i + 1 < len(loans):
                                e["loanPrev"] = loans[i + 1].get("rmndAmt")
                            break
                if len(e) > n0:
                    snaps[di]["final"][code] = e
                    patched += 1
    if patched:
        for di, d in snaps.items():
            with open(os.path.join(RANK_DIR, f"{di}.json"), "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=1)
        print(f"[weekly] netbuy 백필(세분·공매도·T+1 대차): {patched}건 "
              f"(종목 {len(pending)} x 날짜, 파일 {len(snaps)}개)")


def _archive_acc(dates):
    """그 주 netbuy_rank 아카이브 누적(등재일 근사) → (acc, used, final_dates).
    acc = {code: {name, frgn, orgn, fund, prsn, scrt, insu, ivtr, pe, shortSum,
    loans{date: 잔고}, loanPrev, days}}. _netbuy_cum 의 누적부를 분리(2026-09-10) —
    장중(/flow daily 차단)·허브 장애 시 공매도·대차 랭킹/신고가 수급의 폴백 소스로
    공용. 상위 30 리스트에 든 날만 반영되는 근사치(미등재일 0)."""
    acc = {}                        # code -> {name, frgn, orgn, fund, days}
    used, final_dates = [], []
    for dt in dates:
        rel = f"reports/netbuy_rank/{dt.isoformat()}.json"
        # 로컬 우선(방금 저장한 당일 스냅샷·체크아웃 이월분) → 서버 폴백
        try:
            with open(os.path.join(ROOT, "public", rel), encoding="utf-8") as f:
                d = json.load(f)
        except OSError:
            d = _fetch(rel)
        if not d:
            continue
        used.append(dt.isoformat())
        final = d.get("final") or {}    # 종목별 확정(백만원) — 있으면 가집계 대신 사용
        if final:
            final_dates.append(dt.isoformat())
        seen = set()                # 같은 날 여러 리스트 중복 합산 방지 (종목당 1회)
        for rows in (d.get("lists") or {}).values():
            for r in rows or []:
                code = r.get("code")
                if not code or code in seen:
                    continue
                seen.add(code)
                e = acc.setdefault(code, {"code": code, "name": r.get("name"),
                                          "frgn": 0.0, "orgn": 0.0, "fund": 0.0,
                                          "prsn": 0.0, "shortSum": 0.0,
                                          "scrt": 0.0, "insu": 0.0,
                                          "ivtr": 0.0, "pe": 0.0,
                                          "loans": {}, "days": 0})
                src = final.get(code) or r
                # 세분(scrt/insu/ivtr/pe)은 prsn 처럼 확정 병합·백필분만 반영
                for k in ("frgn", "orgn", "fund", "prsn",
                          "scrt", "insu", "ivtr", "pe"):
                    e[k] += float(src.get(k) or 0.0)   # 가집계 행엔 prsn 없음(0)
                if src.get("shortAmt") is not None:
                    e["shortSum"] += float(src["shortAmt"] or 0.0)
                if src.get("loanAmt") is not None:
                    e["loans"][dt.isoformat()] = float(src["loanAmt"])
                    if src.get("loanPrev") is not None and "loanPrev" not in e:
                        e["loanPrev"] = float(src["loanPrev"])
                e["days"] += 1
    return acc, used, final_dates


def _archive_flow_row(e):
    """_archive_acc 항목 → _flow_week 결과와 같은 키(frgn/orgn/prsn 백만원,
    shortSum·loanAmt·loanChg 억) — 폴백 시 호출측 코드를 그대로 재사용."""
    r = {"frgn": round(e["frgn"]), "orgn": round(e["orgn"]), "prsn": round(e["prsn"])}
    if e["shortSum"]:
        r["shortSum"] = round(e["shortSum"], 1)
    if e["loans"]:
        ds = sorted(e["loans"])
        last = e["loans"][ds[-1]]
        base = e.get("loanPrev")
        if base is None:
            base = e["loans"][ds[0]]
        r["loanAmt"] = round(last, 1)
        r["loanChg"] = round(last - base, 1)
    return r


def _netbuy_cum(dates):
    """그 주 일자별 netbuy_rank 아카이브를 합산 — 주체별 누적 순매수 상/하위.
    상위 30 리스트에 든 날만 반영되는 근사치(미등재일은 0 취급)임을 유의."""
    acc, used, final_dates = _archive_acc(dates)
    if not acc:
        return None
    out = {"dates": used, "finalDates": final_dates}
    # 기관 세분 랭킹 키(2026-09-10): 금융투자=scrt, 보험=insu, 투신(사모)=ivtr+pe —
    # 섹터x수급과 동일 명명. prsn 처럼 확정 병합·백필분만 반영
    for e in acc.values():
        e["finInv"], e["insur"], e["trust"] = e["scrt"], e["insu"], e["ivtr"] + e["pe"]
    for k in ("frgn", "orgn", "fund", "prsn", "finInv", "insur", "trust"):
        ranked = sorted(acc.values(), key=lambda e: e[k], reverse=True)
        out[k] = {
            "top": [{"code": e["code"], "name": e["name"], "amt": round(e[k])}
                    for e in ranked[:10] if e[k] > 0],
            "bottom": [{"code": e["code"], "name": e["name"], "amt": round(e[k])}
                       for e in ranked[-10:][::-1] if e[k] < 0],
        }
    # 투자자 합산(외인+기관계 — 연기금은 기관계 하위라 중복 합산 금지) 순위
    # + 개인(확정분만)·주간 공매도 누적(억)·대차잔고 추이(주초→최신, 억)
    def _row(e):
        total = e["frgn"] + e["orgn"]
        r = {"code": e["code"], "name": e["name"], "amt": round(total),
             "frgn": round(e["frgn"]), "orgn": round(e["orgn"]), "prsn": round(e["prsn"]),
             # 기관 세분 누적(섹터x수급과 동일 명명: 금융투자/보험/투신(사모)) —
             # 저장만(2026-09-09), 렌더는 미정. 연기금(fund)은 기존 per-주체 랭킹에 존재
             "finInv": round(e["scrt"]), "insur": round(e["insu"]),
             "trust": round(e["ivtr"] + e["pe"])}
        if e["shortSum"]:
            r["shortSum"] = round(e["shortSum"], 1)
        if e["loans"]:
            ds = sorted(e["loans"])
            r["loanAmt"] = round(e["loans"][ds[-1]], 1)
            base = e.get("loanPrev")
            if base is None:
                base = e["loans"][ds[0]]
            r["loanChg"] = round(e["loans"][ds[-1]] - base, 1)
        return r
    ranked = sorted(acc.values(), key=lambda e: e["frgn"] + e["orgn"], reverse=True)
    out["total"] = {
        "top": [_row(e) for e in ranked[:10] if e["frgn"] + e["orgn"] > 0],
        "bottom": [_row(e) for e in ranked[-10:][::-1] if e["frgn"] + e["orgn"] < 0],
    }
    return out


# ---------------------------------------------------------------------------
# 촉매 타임라인 주간 누적 등락률 (2026-09-08 사용자 요청 — 당일→주간 누적 교체)
# ---------------------------------------------------------------------------
def _norm_listed_name(s):
    """상장사명 정규화 — tools/build_dart_corp_map.py 규칙과 동일해야 한다."""
    s = "".join(ch for ch in str(s or "") if ch not in " \t·ㆍ・")
    for tok in ("(주)", "주식회사"):
        s = s.replace(tok, "")
    return s.upper()


def _yf_closes(tickers):
    """yfinance 일별 종가 배치 조회 -> {ticker: [(iso날짜, 종가), ...]}."""
    import yfinance as yf
    import pandas as pd
    df = yf.download(tickers, period="1mo", progress=False, auto_adjust=True)
    close = df["Close"] if "Close" in df else df
    if isinstance(close, pd.Series):
        close = close.to_frame(tickers[0])
    out = {}
    for t in close.columns:
        s = close[t].dropna()
        out[str(t)] = [(d.strftime("%Y-%m-%d"), float(v)) for d, v in s.items()]
    return out


def _week_cum_map(rows, week_start, dates=None, fetch_closes=None, flow=None):
    """타임라인 종목별 '주간 누적' 등락률(%) — 전주 마지막 종가 대비 최신 종가.

    KIS 우선(2026-09-09): 이름을 krx_listed_names.json(정규화명→코드)으로
    해석해 /flow daily 종가(_flow_week weekChgPct, 동일 산식)로 계산하고,
    결손 종목만 yfinance 폴백(티커는 행의 market 으로 .KS/.KQ). 미해석·
    데이터 결손·주초 이전 종가 없음(그 주 신규상장)은 맵에서 제외 —
    호출측이 기존 '당일' changePct 를 유지(fail-open).
    """
    try:
        with open(os.path.join(ROOT, "public", "assets", "krx_listed_names.json"),
                  encoding="utf-8") as f:
            names = json.load(f) or {}
    except Exception:
        return {}
    code_of, mkt_of = {}, {}                       # stock 이름 -> 코드 / market
    for r in rows:
        nm = r.get("stock")
        if not nm or nm in code_of:
            continue
        code = names.get(_norm_listed_name(nm))
        if code:
            code_of[nm] = code
            mkt_of[nm] = r.get("market")
    if not code_of:
        return {}
    out = {}
    if dates:
        fw = flow if flow is not None else _flow_week(sorted(set(code_of.values())), dates)
        for nm, c in code_of.items():
            wc = (fw.get(c) or {}).get("weekChgPct")
            if wc is not None:
                out[nm] = wc
    n_kis = len(out)
    # yfinance 폴백 — KIS 결손 종목만
    tick_of = {nm: c + (".KQ" if mkt_of.get(nm) == "KOSDAQ" else ".KS")
               for nm, c in code_of.items() if nm not in out}
    if tick_of:
        fetch_closes = fetch_closes or _yf_closes
        try:
            closes = fetch_closes(sorted(set(tick_of.values())))
        except Exception as ex:
            print(f"[weekly] 주간 누적 등락률 yf 폴백 실패(당일 유지): {ex}", file=sys.stderr)
            closes = {}
        for nm, t in tick_of.items():
            rows_t = closes.get(t) or []
            base = None
            for dt, cl in rows_t:                  # 날짜 오름차순
                if dt < week_start:
                    base = cl
            if base and rows_t and rows_t[-1][0] >= week_start:
                out[nm] = round((rows_t[-1][1] / base - 1.0) * 100, 2)
    print(f"[weekly] 타임라인 주간등락 소스: KIS {n_kis} · yf 폴백 {len(out) - n_kis} · "
          f"결손 {len(code_of) - len(out)}")
    return out


def _next_week_preview(econ_n=25, earn_n=40):
    """다음 기간 예정 이벤트 — 경제지표·실적 캘린더에서 결정적으로 추출.
    월간은 개수 확대(econ 40·실적 60)."""
    out = {"econ": [], "earnings": []}
    econ = _fetch("econ_calendar.json") or {}
    for e in (econ.get("upcoming") or [])[:max(60, econ_n * 2)]:
        if isinstance(e, dict) and (e.get("importance") or 0) >= 2:
            out["econ"].append({k: e.get(k) for k in
                                ("releaseAtKST", "nation", "name", "importance", "consensus")
                                if e.get(k) is not None})
    earn = _fetch("earnings_calendar.json") or {}
    for e in (earn.get("upcoming") or [])[:earn_n]:
        if isinstance(e, dict):
            out["earnings"].append({k: e.get(k) for k in
                                    ("date", "when", "name", "market")
                                    if e.get(k) is not None})
    out["econ"] = out["econ"][:econ_n]
    return out


_SCHEMA = {
    "type": "object",
    "properties": {
        "headline":       {"type": "string"},
        "weekNarrative":  {"type": "array", "items": {"type": "string"}},
        "sectorRotation": {"type": "array", "items": {"type": "string"}},
        "catalystTimeline": {"type": "array", "items": {"type": "object", "properties": {
            "date": {"type": "string"},
            "stock": {"type": "string"},
            "market": {"type": "string"},
            "event": {"type": "string"},
            "changePct": {"type": "number"},
            "star": {"type": "integer"}},
            "required": ["date", "event"]}},
        "dailyContext": {"type": "array", "items": {"type": "object", "properties": {
            "date": {"type": "string"}, "note": {"type": "string"}},
            "required": ["date", "note"]}},
        "weeklyComment": {"type": "array", "items": {"type": "string"}},
        "nextWeekPreview": {"type": "array", "items": {"type": "string"}},
        "nextWeek": {"type": "object", "properties": {
            "upside": {"type": "array", "items": {"type": "string"}},
            "downside": {"type": "array", "items": {"type": "string"}},
            "signal": {"type": "string"},
            "events": {"type": "array", "items": {"type": "string"}}},
            "required": ["upside", "downside", "signal", "events"]},
        "watchNotes": {"type": "object", "properties": {
            "long": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "basis": {"type": "string"}},
                "required": ["name", "basis"]}},
            "short": {"type": "array", "items": {"type": "object", "properties": {
                "name": {"type": "string"}, "basis": {"type": "string"}},
                "required": ["name", "basis"]}}}},
    },
    "required": ["headline", "weekNarrative", "sectorRotation", "catalystTimeline"],
}

_SYSTEM = (
    "당신은 한국 주식시장 데일리 브리핑을 주간 단위로 종합하는 애널리스트입니다. "
    "입력은 그 주의 일별 요약(모닝브리핑 미국장 리뷰, 장중 시황 마감 요약, 지수·투자자 "
    "수급·섹터·촉매)입니다. 한국어로, 반드시 입력에 있는 사실만 사용해 작성하세요. "
    "숫자(지수·수급 금액·등락률)는 입력값을 그대로 인용하고 새로 계산하지 마세요. "
    "모의투자·자동매매·매수추천 언급은 금지."
    "\n- headline: 이번 주를 한 문장으로 (예: '반도체가 이끈 사상 최고치 랠리')"
    "\n- weekNarrative: 주간 시장 흐름 서사 4~6개 불릿 — 지수 흐름과 그 원인, 수급 주체 변화"
    "\n- sectorRotation: 주도 섹터/테마의 주중 변화 2~4개 불릿 — 순환인지 지속인지"
    "\n- catalystTimeline: 시장 전체 이벤트(지수 급등락과 원인·수급 총평·환율·매크로)만"
    " 거래일당 1~2행, date 와 event 만 채워라. stock·market·star·changePct 는 채우지"
    " 마라 — 종목 행은 시스템이 스코어 기준(timelineStocks)으로 별도 추가한다"
    "\n- dailyContext: 거래일마다 정확히 1개 — 전일 미국장 주요 이슈·경제지표(usReview,"
    " usCatalystsTop)가 당일 한국장에 어떻게 반영됐는지(지수·섹터·수급 반응, briefing 근거)를"
    " 잇는 1문장. 종목 나열이 아니라 '미국장 원인 → 한국장 반응' 구조로 작성."
    " 그 날 모닝브리핑 스탠스(stance·stanceReason)의 논조를 기준점으로 삼아 방향이"
    " 일치하게 쓰되, 실제 장 반응이 스탠스와 달랐다면 '신중 스탠스에도 불구하고 ~ 급등'"
    " 처럼 그 괴리를 명시적으로 연결하라 (스탠스 근거를 무시한 반대 논조 금지)"
    "\n- weeklyComment: 주간 종합 코멘트 2~4개 불릿 — ① econWeekly(이번 주 발표된"
    " 미국·한국 경제지표의 실제치와 서프라이즈)로 매크로 환경을, ② sectorFlowWeekly"
    "(섹터×수급 매트릭스 — 업종별 주간 등락·투자자 순매수, 억원)로 수급 구도를 짚고"
    " 둘을 잇는 종합 관점으로 마무리. 매크로와 수급 위주로, 숫자는 입력값 그대로 인용"
    "\n- nextWeekPreview: 시나리오형 대응 관점 4~6개 불릿 —"
    " ① 다음 주 전개 시나리오 2개를 '~하면 ~ 전개' 조건부 구조로 (예: '반도체 조정에도"
    " 비테크·브레드스가 버티면 로테이션 지속, 함께 무너지면 단기 리스크오프') ② 두 시나리오를"
    " 가르는 판별 신호 1개 (어떤 지표·수급·이벤트를 보면 되는지 구체적으로) ③ 제공된 예정"
    " 이벤트(nextWeekEvents) 중 핵심 체크 항목 1~3개. 모두 이번 주 입력 데이터에 근거할 것"
    "\n- nextWeek: 위 프리뷰를 구조화한 필수 객체 — upside(상승 시나리오 1~2개,"
    " '~하면 ~' 조건부), downside(하방 시나리오 1~2개), signal(두 시나리오를 가르는"
    " 판별 신호 1문장), events(핵심 이벤트 1~3개). 넷 다 반드시 채워라"
    "\n- watchNotes: 다음 주 '관찰 후보' — long(상방 관찰)·short(하방 관찰) 각각 최대"
    " 5개. 개수를 채우려 하지 말고 아래 근거 데이터가 뚜렷한 종목·업종만 넣을 것"
    "(근거가 약하면 1~2개만 있어도 됨)."
    " 근거는 반드시 입력의 결정적 데이터에서: sectorFlowWeekly(주가 vs 수급 괴리 — 주가"
    " 하락에도 외인·기관 순매수면 상방 관찰, 주가 급등에 수급 이탈이면 하방 관찰),"
    " netbuyTotalTop/Bottom(수급 집중), shortLoan(공매도 누적·대차 증가는 하방 압력,"
    " 대차 감소는 숏커버 여지). name 은 업종명 또는 종목명, basis 는 수치를 인용한 한"
    " 문장. 매수·매도 권유 표현 금지 — '관찰'의 근거만 서술하라"
)


# 월간 리뷰 프롬프트(2026-09-10 P2) — 스키마·키는 주간과 동일(렌더러·엑셀 공용).
# 입력: days=축약 일별(_compact_day), weeklySyntheses=그 달 주간 합성 요약.
_SYSTEM_MONTH = (
    "당신은 한국 주식시장 데일리·주간 브리핑을 월간 단위로 종합하는 애널리스트입니다. "
    "입력은 그 달의 거래일 축약 요약(days: 지수·투자자 수급·섹터·핵심 시황 3줄·경제지표)과"
    " 그 달 각 주의 주간 브리핑 요약(weeklySyntheses)입니다. 한국어로, 반드시 입력에 있는"
    " 사실만 사용하세요. 숫자는 입력값을 그대로 인용하고 새로 계산하지 마세요."
    " 모의투자·자동매매·매수추천 언급은 금지. 키 이름에 'week'가 들어가도 모두 '월간'"
    " 의미로 작성하라(스키마 공용)."
    "\n- headline: 이번 달을 한 문장으로"
    "\n- weekNarrative: 월간 시장 흐름 5~7개 불릿 — 주차별 흐름의 전환점과 원인, 수급 주체 변화"
    "\n- sectorRotation: 월중 주도 섹터/테마 변화 3~5개 불릿 — 순환인지 지속인지"
    "\n- catalystTimeline: 시장 전체 이벤트만 주차당 1~2행(월 최대 8행), date·event 만."
    " stock·market·star·changePct 는 채우지 마라(종목 행은 시스템이 별도 추가)"
    "\n- dailyContext: 거래일마다 정확히 1개, 각 1문장(30자 내외) — 그 날 지수·수급 반응의"
    " 핵심 원인. 입력 days 의 모든 date 를 빠짐없이"
    "\n- weeklyComment: 월간 종합 코멘트 3~4개 불릿 — econWeekly(그 달 발표 경제지표)로"
    " 매크로를, sectorFlowWeekly(업종별 월간 등락·순매수, 억원)로 수급 구도를 짚고 종합"
    "\n- nextWeekPreview: '다음 달' 시나리오형 4~6개 불릿 — 조건부 시나리오 2개, 판별 신호 1개,"
    " 예정 이벤트(nextWeekEvents) 중 핵심 1~3개"
    "\n- nextWeek: 위 프리뷰 구조화 — upside·downside(각 1~2), signal 1문장, events 1~3. 필수"
    "\n- watchNotes: 다음 달 관찰 후보 long·short 각 최대 5개 — sectorFlowWeekly·"
    "netbuyTotalTop/Bottom·shortLoan 근거가 뚜렷한 것만, basis 는 수치 인용 1문장, 권유 금지"
)


def main():
    args = sys.argv[1:]
    base = datetime.datetime.now(KST).date()
    if "--date" in args:
        base = datetime.date.fromisoformat(args[args.index("--date") + 1])
    # 기간(2026-09-10 P2): week(기본) | month — 월간은 같은 파이프라인을 그 달 1일~
    # 기준일로 돌리고 출력만 reports/monthly_review/<YYYY-MM>.json 에 분리 저장.
    period = args[args.index("--period") + 1] if "--period" in args else "week"
    is_month = period == "month"
    dates = _period_dates(base, period)
    week_start = dates[0].isoformat()
    if is_month:
        print(f"[weekly] period=month {dates[0]} ~ {dates[-1]} (평일 {len(dates)}일, "
              f"FLOW_ROWS={FLOW_ROWS})")

    _tmark("일별 수집")
    days = [d for d in (_day_summary(dt.isoformat()) for dt in dates) if d]
    if not days:
        print(f"[weekly] {week_start} 주 데이터 없음 — 생성 생략")
        return 0
    week_end = days[-1]["date"]

    # 당일 순매수 랭킹 스냅샷(거래일이었을 때만) + 주간 누적 합산
    _tmark("스냅샷·백필·누적")
    today_iso = datetime.datetime.now(KST).date().isoformat()
    if not is_month and any(d["date"] == today_iso for d in days):
        _snapshot_netbuy_rank(today_iso)
        _snapshot_breadth(today_iso)
        ft = _day_flow_top(today_iso)      # 방금 저장한 당일 스냅샷으로 재부착
        for d in days:
            if d["date"] == today_iso and ft:
                d["flowTop"] = ft
    _backfill_netbuy_detail(dates)     # 기관 세분 소급(도입 주 한정, 정착 후 no-op)
    netbuy_cum = _netbuy_cum(dates)
    print(f"[weekly] {week_start} ~ {week_end}: 거래일 {len(days)}일 수집")

    # ── Phase 1 결정적 집계 (2026-09-08 주간회의 자료 벤치마킹) ──────────────
    # ① 미국 주간 컨텍스트 — 모닝브리핑 usIndices(전일 미국장) 일별 등락 합산 근사
    us_weekly = {}
    if is_month:                     # 월간: 레벨 기반(결측 시 복리) — 합산 오차 제거
        us_weekly = _us_period_returns(days)
    else:
        for d in days:
            for i in (d.get("usIndices") or []):
                nm, ch = i.get("name"), i.get("changePct")
                if nm and isinstance(ch, (int, float)):
                    us_weekly[nm] = round(us_weekly.get(nm, 0.0) + ch, 2)
    # ①b 미국 섹터 ETF 기간 누적 상위/하위 3 — 주간 단순 합산 / 월간 복리
    us_sec = {}
    if is_month:
        seq = {}
        for d in days:
            for s in (d.get("usSectors") or []):
                seq.setdefault(s["name"], []).append(s["changePct"])
        us_sec = {n: _compound(v) for n, v in seq.items()}
    else:
        for d in days:
            for s in (d.get("usSectors") or []):
                us_sec[s["name"]] = round(us_sec.get(s["name"], 0.0) + s["changePct"], 2)
    us_sector_weekly = None
    if us_sec:
        ranked = sorted(us_sec.items(), key=lambda x: -x[1])
        us_sector_weekly = {
            "up": [{"name": n, "chg": v} for n, v in ranked[:3] if v > 0],
            "down": [{"name": n, "chg": v} for n, v in ranked[-3:][::-1] if v < 0]}

    # ② 공매도·대차 주간 동향 — 유니버스는 netbuy_rank 등재 종목(현행), 값은
    #    /flow shorts·loans 주간 직접 계산(_flow_week 재사용, 2026-09-09) —
    #    구 방식(등재일 값만 누적)의 미등재일 공매도 누락·잔고 스테일 근사 제거
    _tmark("공매도·대차(flow)")
    short_loan = _short_loan_weekly(dates)

    # ⑤ 섹터 x 수급 매트릭스 — 허브 /sector-flow(FHPTJ04040000 업종별 일별 투자자
    #    순매수, 백만원)에서 이번 주 날짜만 합산. V1(2026-09-09): 신호는 여기서
    #    확정 저장 — 웹·엑셀은 저장값만 렌더해 판정 불일치를 원천 차단.
    _tmark("섹터플로(허브)")
    sector_flow = None
    try:
        base = FLOW_RANK_URL.rsplit("/", 1)[0]
        # 창 40(허브 상한, 슬라이스만이라 추가 비용 없음) — 기간 거래일 + 1W 기준 종가
        # (직전 거래일) + 수급 중립 기준선(직전 최대 8주)을 한 응답으로(2026-09-11)
        sf_q = f"?days={max(SECTOR_FLOW_DAYS, len(dates) + 2)}"
        req = urllib.request.Request(f"{base}/sector-flow{sf_q}",
                                     headers={"User-Agent": "weekly-briefing"})
        # 마감 직후 콜드 캐시는 26업종 KIS 콜로 느릴 수 있다(2026-09-08 16:13 타임아웃
        # 실측). 허브는 클라이언트가 끊겨도 수집을 마쳐 10분 캐시에 저장하므로,
        # 타임아웃을 넉넉히 + 실패 시 1회 재시도(캐시 히트)로 복구한다.
        sf = None
        for attempt in (1, 2):
            try:
                with urllib.request.urlopen(req, timeout=150) as r:
                    sf = json.loads(r.read().decode("utf-8"))
                break
            except Exception as ex:
                if attempt == 2:
                    raise
                print(f"[weekly] sector-flow 1차 실패({ex}) — 45초 후 재시도(허브 캐시)",
                      file=sys.stderr)
                import time as _t
                _t.sleep(45)
        want = {dt.strftime("%Y%m%d") for dt in dates}
        rows, n_close, n_band = [], 0, 0
        for s in (sf.get("sectors") or []):
            chg, frgn, orgn, prsn, fund, nd = 0.0, 0.0, 0.0, 0.0, 0.0, 0
            # 기관계 세분(2026-09-09): 금융투자(scrt)·보험(insu)·투신(사모)(ivtr+pe).
            # 허브 구버전 응답엔 키가 없으므로 존재 확인 후에만 세분 필드를 붙인다
            # (0 채움 금지 — 값 조작으로 보임).
            fin, ins, tru, has_det = 0.0, 0.0, 0.0, False
            chg_seq = []                    # 월간 복리용 일별 등락
            for r0 in (s.get("daily") or []):
                if r0.get("date") in want and any(
                        r0.get(k) for k in ("frgn", "orgn", "prsn", "chgPct")):
                    nd += 1
                    chg += float(r0.get("chgPct") or 0.0)
                    chg_seq.append(float(r0.get("chgPct") or 0.0))
                    frgn += float(r0.get("frgn") or 0.0)
                    orgn += float(r0.get("orgn") or 0.0)
                    prsn += float(r0.get("prsn") or 0.0)
                    fund += float(r0.get("fund") or 0.0)
                    if any(k in r0 for k in ("scrt", "insu", "ivtr", "pe")):
                        has_det = True
                        fin += float(r0.get("scrt") or 0.0)
                        ins += float(r0.get("insu") or 0.0)
                        tru += float(r0.get("ivtr") or 0.0) + float(r0.get("pe") or 0.0)
            if nd:
                # 기간 등락 = 시점 대 시점(2026-09-11 사용자 요청): 기간 마지막 종가 /
                # 직전 기간 마지막 종가 − 1 — YTD·3M·1M 과 같은 종가 비교. 종가 결측이면
                # 일별 등락 복리(수학적으로 동일, 반올림 오차만). 종전 주간=일별 단순
                # 합(§2)은 폐기 — 합산은 0 근처에서 부호가 뒤집혀 3/4 판정을 흔들었다.
                chg_p = _period_close_return(s.get("daily"), want)
                if chg_p is None:
                    chg_p = _compound(chg_seq)
                else:
                    n_close += 1
                row = {"name": s.get("name"), "days": nd, "chgPct": chg_p,
                       "frgn": round(frgn / 100), "orgn": round(orgn / 100),
                       "prsn": round(prsn / 100), "fund": round(fund / 100)}  # 억원
                if has_det:                     # 장중시황과 동일 세분 표기 키
                    row.update({"finInv": round(fin / 100), "insur": round(ins / 100),
                                "trust": round(tru / 100)})
                # V1: 다기간 수익률(허브 캔들 기준, 결측은 None 유지) + 상태 신호.
                # 1W 는 기존 주간등락 정의(일별 chgPct 합) 그대로(§2).
                rets = s.get("returns") or {}
                ytd, m6 = rets.get("ytd"), rets.get("m6")
                m3, m1 = rets.get("m3"), rets.get("m1")
                # 가격 축(3/4 동일 방향 규칙 공용): 주간 YTD/3M/1M/1W(=기간) ·
                # 월간 YTD/6M/3M/1M(=기간, 월초~기준일) — 4번째 축은 항상 '이번 기간'
                axes = (ytd, m6, m3, row["chgPct"]) if is_month else (ytd, m3, m1, row["chgPct"])
                # 수급 중립 구간(상대 기준) — 평소 1일 규모 x 이번 기간 거래일 수
                per_day = _flow_baseline(s.get("daily"), min(want))
                band = None
                if per_day is not None and SECTOR_NEUTRAL_RATIO > 0:
                    row["flowBase"] = round(per_day * nd)            # 평소 기간 규모(억)
                    band = round(SECTOR_NEUTRAL_RATIO * per_day * nd, 1)
                    row["neutralBand"] = band
                    n_band += 1
                row.update({"ytd": ytd, "m6": m6, "m3": m3, "m1": m1,
                            "coreFlow": round((frgn + orgn) / 100),
                            "signal": classify_sector_signal(
                                *axes, row["frgn"], row["orgn"], neutral=band)})
                if band and row["signal"] == "혼조" \
                        and classify_sector_signal(*axes, row["frgn"], row["orgn"]) != "혼조":
                    row["flowWeak"] = True                          # 중립 구간으로 혼조 전환
                rows.append(row)
        if rows:
            rows.sort(key=lambda x: -x["chgPct"])
            sector_flow = {"asof": sf.get("asof"), "rows": rows,
                           "chgBasis": "close" if n_close == len(rows) else
                                       ("mixed" if n_close else "compound"),
                           "neutralRatio": SECTOR_NEUTRAL_RATIO if n_band else None}
            print(f"[weekly] 섹터x수급 매트릭스: {len(rows)}업종 · 기간등락 종가비교 "
                  f"{n_close}/{len(rows)} · 중립구간 {n_band}/{len(rows)} "
                  f"(수급미미→혼조 {sum(1 for r in rows if r.get('flowWeak'))})")
    except Exception as ex:
        print(f"[weekly] sector-flow 수집 실패(매트릭스 생략): {ex}", file=sys.stderr)

    # ④ 섹터 주간 지속성 — 일별 상위/하위 섹터 등장 일수 + 평균 등락률
    def _sector_week(key):
        agg = {}
        for d in days:
            for s in (d.get(key) or []):
                nm = s.get("name")
                if not nm:
                    continue
                a = agg.setdefault(nm, {"name": nm, "days": 0, "sum": 0.0})
                a["days"] += 1
                a["sum"] += float(s.get("changePct") or 0.0)
        out = [{"name": a["name"], "days": a["days"], "avgChg": round(a["sum"] / a["days"], 2)}
               for a in agg.values()]
        return sorted(out, key=lambda x: (-x["days"], -abs(x["avgChg"])))[:6]
    sector_weekly = {"up": _sector_week("sectorsUp"), "down": _sector_week("sectorsDown")}


    _tmark("프리뷰·LLM 합성")
    preview = _next_week_preview(40, 60) if is_month else _next_week_preview()
    synthesis, generated_by = None, None
    if "--no-llm" not in args:
        if not llm.configured():
            print("[weekly] LLM 미설정 — synthesis 생략", file=sys.stderr)
        else:
            def _eok(rows):
                """netbuy total 행(백만원)을 억원으로 변환 — LLM 단위 오인용 방지."""
                return [{"name": r.get("name"),
                         "netBuyEok": round((r.get("amt") or 0) / 100),
                         "frgnEok": round((r.get("frgn") or 0) / 100),
                         "orgnEok": round((r.get("orgn") or 0) / 100)}
                        for r in (rows or [])]
            total = (netbuy_cum or {}).get("total") or {}
            # 주간 발표 경제지표(US/KOR) — weeklyComment 매크로 근거
            econ_weekly = [{"date": d["date"], **e}
                           for d in days
                           for e in (d.get("usEcon") or []) + (d.get("koEcon") or [])]
            user = json.dumps({
                "days": [_compact_day(d) for d in days] if is_month else days,
                "nextWeekEvents": preview,
                **({"weeklySyntheses": _period_syntheses(dates)} if is_month else {}),
                # Phase 3 관찰 노트 근거 — 주간 결정적 집계 (전부 억원 단위)
                "sectorFlowWeekly": (sector_flow or {}).get("rows"),
                "netbuyTotalTop": _eok(total.get("top")),
                "netbuyTotalBottom": _eok(total.get("bottom")),
                "shortLoan": short_loan,
                "econWeekly": econ_weekly,
            }, ensure_ascii=False)
            try:
                synthesis, generated_by = llm.generate_json(
                    _SYSTEM_MONTH if is_month else _SYSTEM, user,
                    max_tokens=8192 if is_month else 4096, schema=_SCHEMA,
                    return_model=True)
            except llm.LLMError as ex:
                print(f"[weekly] LLM 합성 실패 — 집계만 저장: {ex}", file=sys.stderr)

    # 타임라인 병합 — LLM 은 시장 이벤트 행만, 종목 행은 스코어 기준으로 결정적 추가
    # (코스피 ★4 이상 / 코스닥 ★5 — LLM 누락·기준 이탈 방지, 2026-09-08)
    stock_rows = [{"date": d["date"], **p}
                  for d in days for p in (d.get("timelineStocks") or [])]
    # 종목 행 등락률을 '주간 누적'으로 교체(전주 종가 대비 최신 종가, 2026-09-08
    # 사용자 요청). 조회 실패 종목은 스코어 시점 당일 등락률 유지(fail-open).
    _tmark("타임라인 주간등락")
    if stock_rows:
        cum = _week_cum_map(stock_rows, week_start, dates)
        n_cum = 0
        for r in stock_rows:
            if r.get("stock") in cum:
                r["changePct"] = cum[r["stock"]]
                n_cum += 1
        print(f"[weekly] 타임라인 주간 누적 등락률: {n_cum}/{len(stock_rows)}행 교체")
    # 재등장 종목 병합(2026-09-09 사용자 요청): 같은 종목이 여러 날 스코어되면
    # 최신 등장일 1행으로 접는다 — 별점 max·촉매 최신, firstDate/appearCount 로
    # 반복(촉매 지속) 신호를 보존하고 달라진 이전 촉매는 history 에 남긴다.
    # 웹/엑셀은 저장값만 렌더하므로 여기서 한 번 접으면 양쪽 자동 반영.
    if stock_rows:
        n_before = len(stock_rows)
        folded = {}
        for r in stock_rows:                     # days 순회 순서 = 날짜 오름차순
            k = (r.get("stock"), r.get("market"))
            prev = folded.get(k)
            if prev is None:
                r["firstDate"] = r["date"]
                r["appearCount"] = 1
                folded[k] = r
                continue
            if r.get("event") and r["event"] != prev.get("event"):
                prev.setdefault("history", []).append(
                    {"date": prev["date"], "event": prev.get("event")})
            prev["date"] = r["date"]
            prev["event"] = r.get("event") or prev.get("event")
            prev["star"] = max(prev.get("star") or 0, r.get("star") or 0)
            if r.get("changePct") is not None:
                prev["changePct"] = r["changePct"]
            prev["appearCount"] += 1
        stock_rows = sorted(folded.values(),
                            key=lambda x: (x["date"], -(x.get("star") or 0),
                                           -(x.get("changePct") or 0)))
        if len(stock_rows) != n_before:
            print(f"[weekly] 타임라인 재등장 병합: {n_before}→{len(stock_rows)}행")
        if is_month:
            # 월간 컷(P2): ★5 만, 주차별 상위 8(별점·기간 등락 순), 총 40행 상한
            byw = {}
            for r in stock_rows:
                if (r.get("star") or 0) < 5:
                    continue
                dt = datetime.date.fromisoformat(r["date"])
                byw.setdefault(dt - datetime.timedelta(days=dt.weekday()), []).append(r)
            kept = []
            for wk in sorted(byw):
                kept += sorted(byw[wk], key=lambda x: (-(x.get("star") or 0),
                                                       -(x.get("changePct") or 0)))[:8]
            kept = sorted(kept, key=lambda x: (x["date"], -(x.get("changePct") or 0)))[:40]
            print(f"[weekly] 월간 타임라인 컷: {len(stock_rows)}→{len(kept)}행 (★5·주차 상위 8)")
            stock_rows = kept
    if stock_rows or synthesis:
        syn = synthesis if isinstance(synthesis, dict) else {}
        market_rows = [t for t in (syn.get("catalystTimeline") or []) if not t.get("stock")]
        merged = []
        for dt in sorted({r["date"] for r in stock_rows}
                         | {t.get("date") for t in market_rows if t.get("date")}):
            merged += [t for t in market_rows if t.get("date") == dt]
            merged += [r for r in stock_rows if r["date"] == dt]
        # 섹터 컬럼(2026-09-09): 종목 행에 업종 부착 — 웹/엑셀은 저장값만 렌더
        for t in merged:
            if t.get("stock"):
                t["sector"] = _sector_of_name(t["stock"], t.get("code"))
        syn["catalystTimeline"] = merged
        synthesis = syn

    # 섹터 시그널 종목 관찰 4-Matrix — 웹/엑셀 공용 스크리닝 결과(§28), 주차별
    # JSON 아카이브로 저장되어 사후 성과검증(H1~H4)에 붙일 수 있음(§35-36)
    sector_screen = build_sector_screen(
        (sector_flow or {}).get("rows"), netbuy_cum, short_loan, stock_rows,
        flow_fill=lambda codes: _flow_week(codes, dates))
    if is_month:                        # 각주 기간 문구(웹·엑셀이 저장값을 그대로 렌더)
        sector_screen["universeNote"] = (sector_screen.get("universeNote") or "") \
            .replace("주간 브리핑", "월간 리뷰").replace("주간 촉매", "월간 촉매")
    n_pick = sum(len(v) for v in sector_screen["matrix"].values())
    cov = sector_screen.get("coverage") or {}
    print(f"[weekly] 섹터 시그널 스크리닝: 유니버스 {len(sector_screen['debug'])}종목"
          f"(섹터 보강 +{cov.get('added')} · 결손 축 채움 {cov.get('filled')}) → "
          f"선정 {n_pick} ({', '.join(k + ' ' + str(len(v)) for k, v in sector_screen['matrix'].items())})")

    _tmark("breadth·신고가·출력 조립")
    out = {
        # 기간 키(2026-09-10 P2). weekStart/weekEnd 는 렌더러·엑셀 빌더 호환 미러 —
        # 월간이면 periodStart/periodEnd 와 같은 값(라벨은 period 로 분기).
        "period": period,
        "periodStart": week_start,
        "periodEnd": week_end,
        "weekStart": week_start,
        "weekEnd": week_end,
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "generatedBy": generated_by,
        # 일별 결정적 집계 — UI 헤더(지수 등락 테이블)용. LLM 실패에도 유지.
        "days": [{"date": d["date"],
                  "indices": d.get("indices") or {},
                  "investors": d.get("investors") or {},
                  "sectorsUp": d.get("sectorsUp") or [],
                  "sectorsDown": d.get("sectorsDown") or [],
                  "usEcon": d.get("usEcon") or [],   # 전일 미국 지표(usReview 언급 기반)
                  "koEcon": d.get("koEcon") or [],   # 당일 한국 지표(★3+)
                  "catalysts": (d.get("catalysts") or [])[:3]} for d in days],
        "netbuyCum": netbuy_cum,        # 주체별(외인/기관/연기금) 주간 누적 순매수 상/하위
        "usWeekly": us_weekly,          # 미국 지수 주간 누적 등락(모닝브리핑 전일 기준 합산)
        "usSectorWeekly": us_sector_weekly,  # 미국 섹터 ETF 주간 누적 상위/하위 3
        "shortLoan": short_loan,        # 공매도 누적·대차잔고 증감 상위 (랭킹 유니버스 한정)
        "sectorWeekly": sector_weekly,  # 섹터 주간 지속성 (등장 일수·평균 등락)
        "sectorFlow": sector_flow,      # 섹터 x 수급 매트릭스 (업종별 주간 등락·투자자 순매수, 억)
        "sectorScreen": sector_screen,  # 섹터 시그널 종목 관찰 4-Matrix (웹/엑셀 공용, §28)
        "breadth": _breadth_weekly(dates),   # 주간 ADR 추이 + 신고가 근접 섹터 그룹
        "synthesis": synthesis,
    }

    out_dir, snap = OUT_DIR, SNAP
    key = week_start
    if is_month:
        out_dir = os.path.join(ROOT, "public", "reports", "monthly_review")
        snap = os.path.join(ROOT, "public", "monthly_review.json")
        key = week_start[:7]                                  # YYYY-MM
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{key}.json")
    for p in (path, snap):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    # index.json — git 폴백용 (DB 서빙은 쿼리로 대체하지만 raw 폴백 경로 대칭 유지)
    idx_path = os.path.join(out_dir, "index.json")
    try:
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
    except OSError:
        idx = []
    if key not in idx:
        idx = sorted(set(idx) | {key}, reverse=True)
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(idx, f, indent=1)
    _tmark(None)                        # 마지막 구간 경과 출력
    print(f"[weekly] 저장: {path} (synthesis={'ok' if synthesis else '생략'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
