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
        day["usIndices"] = [{"name": i.get("name"), "changePct": i.get("changePct")}
                            for i in (us.get("indices") or [])[:5]]
        day["usCatalystsTop"] = [
            {"stock": c.get("stock"), "direction": c.get("direction"),
             "summary": (c.get("summary") or "")[:120]}
            for c in (morning.get("usCatalysts") or [])[:5]]
        # 전일 미국 경제지표 — 시장 영향력 큰 것(★3 이상)만 (실업률·비농업고용 등,
        # 2026-09-08 사용자 요청). actual 없는 행(발표 전/이월 공백)은 제외.
        day["usEcon"] = [
            {k: e.get(k) for k in ("name", "importance", "actual", "forecast",
                                   "previous", "unit", "unitScale", "surprise")
             if e.get(k) is not None}
            for e in ((morning.get("econEvents") or {}).get("usReleased") or [])
            if (e.get("importance") or 0) >= 3 and e.get("actual") is not None]

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
            "counts": counts, "newHighs": d.get("newHighs") or []}
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
    highs = (latest or {}).get("newHighs") or []
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
        out["newHighs"] = {"date": rows[-1]["date"], "count": len(highs),
                           "groups": sorted(({"sector": k, "stocks": v[:6]}
                                             for k, v in groups.items()),
                                            key=lambda g: -len(g["stocks"]))[:8]}
    return out


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
    base = FLOW_RANK_URL.rsplit("/", 1)[0]

    def _one(code):
        try:
            req = urllib.request.Request(f"{base}/flow?code={code}",
                                         headers={"User-Agent": "weekly-briefing"})
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8"))
            out = None
            for row in (d.get("daily") or []):
                if str(row.get("date")) == want:
                    out = {k: row.get(k) for k in ("prsn", "frgn", "orgn", "fund")}
                    break
            if out is not None:
                # 같은 응답의 공매도(pbmn, 억원)·대차잔고(rmndAmt 억원/rmndChg 주) 당일분 병합
                for row in (d.get("shorts") or []):
                    if str(row.get("date")) == want:
                        out["shortAmt"] = row.get("pbmn")
                        break
                for row in (d.get("loans") or []):
                    if str(row.get("date")) == want:
                        out["loanAmt"] = row.get("rmndAmt")
                        out["loanChg"] = row.get("rmndChg")
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


def _netbuy_cum(dates):
    """그 주 일자별 netbuy_rank 아카이브를 합산 — 주체별 누적 순매수 상/하위.
    상위 30 리스트에 든 날만 반영되는 근사치(미등재일은 0 취급)임을 유의."""
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
                                          "loans": {}, "days": 0})
                src = final.get(code) or r
                for k in ("frgn", "orgn", "fund", "prsn"):
                    e[k] += float(src.get(k) or 0.0)   # 가집계 행엔 prsn 없음(0)
                if src.get("shortAmt") is not None:
                    e["shortSum"] += float(src["shortAmt"] or 0.0)
                if src.get("loanAmt") is not None:
                    e["loans"][dt.isoformat()] = float(src["loanAmt"])
                e["days"] += 1
    if not acc:
        return None
    out = {"dates": used, "finalDates": final_dates}
    for k in ("frgn", "orgn", "fund", "prsn"):   # prsn 은 확정 병합일만 반영(가집계 랭킹엔 없음)
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
             "frgn": round(e["frgn"]), "orgn": round(e["orgn"]), "prsn": round(e["prsn"])}
        if e["shortSum"]:
            r["shortSum"] = round(e["shortSum"], 1)
        if e["loans"]:
            ds = sorted(e["loans"])
            r["loanAmt"] = round(e["loans"][ds[-1]], 1)
            r["loanChg"] = round(e["loans"][ds[-1]] - e["loans"][ds[0]], 1)
        return r
    ranked = sorted(acc.values(), key=lambda e: e["frgn"] + e["orgn"], reverse=True)
    out["total"] = {
        "top": [_row(e) for e in ranked[:10] if e["frgn"] + e["orgn"] > 0],
        "bottom": [_row(e) for e in ranked[-10:][::-1] if e["frgn"] + e["orgn"] < 0],
    }
    return out


def _next_week_preview():
    """다음 주 예정 이벤트 — 경제지표·실적 캘린더에서 결정적으로 추출."""
    out = {"econ": [], "earnings": []}
    econ = _fetch("econ_calendar.json") or {}
    for e in (econ.get("upcoming") or [])[:60]:
        if isinstance(e, dict) and (e.get("importance") or 0) >= 2:
            out["econ"].append({k: e.get(k) for k in
                                ("releaseAtKST", "nation", "name", "importance", "consensus")
                                if e.get(k) is not None})
    earn = _fetch("earnings_calendar.json") or {}
    for e in (earn.get("upcoming") or [])[:40]:
        if isinstance(e, dict):
            out["earnings"].append({k: e.get(k) for k in
                                    ("date", "when", "name", "market")
                                    if e.get(k) is not None})
    out["econ"] = out["econ"][:25]
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
        "nextWeekPreview": {"type": "array", "items": {"type": "string"}},
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
    "\n- nextWeekPreview: 시나리오형 대응 관점 4~6개 불릿 —"
    " ① 다음 주 전개 시나리오 2개를 '~하면 ~ 전개' 조건부 구조로 (예: '반도체 조정에도"
    " 비테크·브레드스가 버티면 로테이션 지속, 함께 무너지면 단기 리스크오프') ② 두 시나리오를"
    " 가르는 판별 신호 1개 (어떤 지표·수급·이벤트를 보면 되는지 구체적으로) ③ 제공된 예정"
    " 이벤트(nextWeekEvents) 중 핵심 체크 항목 1~3개. 모두 이번 주 입력 데이터에 근거할 것"
    "\n- watchNotes: 다음 주 '관찰 후보' — long(상방 관찰) 2~3개, short(하방 관찰) 1~3개."
    " 근거는 반드시 입력의 결정적 데이터에서: sectorFlowWeekly(주가 vs 수급 괴리 — 주가"
    " 하락에도 외인·기관 순매수면 상방 관찰, 주가 급등에 수급 이탈이면 하방 관찰),"
    " netbuyTotalTop/Bottom(수급 집중), shortLoan(공매도 누적·대차 증가는 하방 압력,"
    " 대차 감소는 숏커버 여지). name 은 업종명 또는 종목명, basis 는 수치를 인용한 한"
    " 문장. 매수·매도 권유 표현 금지 — '관찰'의 근거만 서술하라"
)


def main():
    args = sys.argv[1:]
    base = datetime.datetime.now(KST).date()
    if "--date" in args:
        base = datetime.date.fromisoformat(args[args.index("--date") + 1])
    dates = _week_dates(base)
    week_start = dates[0].isoformat()

    days = [d for d in (_day_summary(dt.isoformat()) for dt in dates) if d]
    if not days:
        print(f"[weekly] {week_start} 주 데이터 없음 — 생성 생략")
        return 0
    week_end = days[-1]["date"]

    # 당일 순매수 랭킹 스냅샷(거래일이었을 때만) + 주간 누적 합산
    today_iso = datetime.datetime.now(KST).date().isoformat()
    if any(d["date"] == today_iso for d in days):
        _snapshot_netbuy_rank(today_iso)
        _snapshot_breadth(today_iso)
        ft = _day_flow_top(today_iso)      # 방금 저장한 당일 스냅샷으로 재부착
        for d in days:
            if d["date"] == today_iso and ft:
                d["flowTop"] = ft
    netbuy_cum = _netbuy_cum(dates)
    print(f"[weekly] {week_start} ~ {week_end}: 거래일 {len(days)}일 수집")

    # ── Phase 1 결정적 집계 (2026-09-08 주간회의 자료 벤치마킹) ──────────────
    # ① 미국 주간 컨텍스트 — 모닝브리핑 usIndices(전일 미국장) 일별 등락 합산 근사
    us_weekly = {}
    for d in days:
        for i in (d.get("usIndices") or []):
            nm, ch = i.get("name"), i.get("changePct")
            if nm and isinstance(ch, (int, float)):
                us_weekly[nm] = round(us_weekly.get(nm, 0.0) + ch, 2)

    # ② 공매도·대차 주간 동향 — netbuy_rank 유니버스 한정 (억원)
    short_loan = None
    acc2 = {}
    for dt in dates:
        rel = f"reports/netbuy_rank/{dt.isoformat()}.json"
        d2 = _fetch(rel)
        if not d2:
            continue
        fin = d2.get("final") or {}
        seen = set()
        for rows in (d2.get("lists") or {}).values():
            for r in rows or []:
                code = r.get("code")
                if not code or code in seen:
                    continue
                seen.add(code)
                f = fin.get(code) or {}
                e = acc2.setdefault(code, {"name": r.get("name"), "shortSum": 0.0, "loans": {}})
                if f.get("shortAmt") is not None:
                    e["shortSum"] += float(f["shortAmt"] or 0.0)
                if f.get("loanAmt") is not None:
                    e["loans"][dt.isoformat()] = float(f["loanAmt"])
    if acc2:
        ent = list(acc2.values())
        for e in ent:
            ds = sorted(e["loans"])
            e["loanChg"] = round(e["loans"][ds[-1]] - e["loans"][ds[0]], 1) if len(ds) >= 2 else 0.0
            e["loanAmt"] = round(e["loans"][ds[-1]], 1) if ds else None
        short_top = sorted([e for e in ent if e["shortSum"] > 0],
                           key=lambda x: -x["shortSum"])[:5]
        loan_up = sorted([e for e in ent if e["loanChg"] > 0], key=lambda x: -x["loanChg"])[:5]
        loan_dn = sorted([e for e in ent if e["loanChg"] < 0], key=lambda x: x["loanChg"])[:5]
        short_loan = {
            "shortTop": [{"name": e["name"], "amt": round(e["shortSum"])} for e in short_top],
            "loanUp": [{"name": e["name"], "chg": e["loanChg"], "amt": e["loanAmt"]} for e in loan_up],
            "loanDown": [{"name": e["name"], "chg": e["loanChg"], "amt": e["loanAmt"]} for e in loan_dn],
        }

    # ⑤ 섹터 x 수급 매트릭스 — 허브 /sector-flow(FHPTJ04040000 업종별 일별 투자자
    #    순매수, 백만원)에서 이번 주 날짜만 합산. 주가 vs 수급 괴리 판정은 UI 에서.
    sector_flow = None
    try:
        base = FLOW_RANK_URL.rsplit("/", 1)[0]
        req = urllib.request.Request(f"{base}/sector-flow",
                                     headers={"User-Agent": "weekly-briefing"})
        with urllib.request.urlopen(req, timeout=90) as r:
            sf = json.loads(r.read().decode("utf-8"))
        want = {dt.strftime("%Y%m%d") for dt in dates}
        rows = []
        for s in (sf.get("sectors") or []):
            chg, frgn, orgn, prsn, fund, nd = 0.0, 0.0, 0.0, 0.0, 0.0, 0
            for r0 in (s.get("daily") or []):
                if r0.get("date") in want and any(
                        r0.get(k) for k in ("frgn", "orgn", "prsn", "chgPct")):
                    nd += 1
                    chg += float(r0.get("chgPct") or 0.0)
                    frgn += float(r0.get("frgn") or 0.0)
                    orgn += float(r0.get("orgn") or 0.0)
                    prsn += float(r0.get("prsn") or 0.0)
                    fund += float(r0.get("fund") or 0.0)
            if nd:
                rows.append({"name": s.get("name"), "days": nd, "chgPct": round(chg, 2),
                             "frgn": round(frgn / 100), "orgn": round(orgn / 100),
                             "prsn": round(prsn / 100), "fund": round(fund / 100)})  # 억원
        if rows:
            rows.sort(key=lambda x: -x["chgPct"])
            sector_flow = {"asof": sf.get("asof"), "rows": rows}
            print(f"[weekly] 섹터x수급 매트릭스: {len(rows)}업종")
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


    preview = _next_week_preview()
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
            user = json.dumps({
                "days": days, "nextWeekEvents": preview,
                # Phase 3 관찰 노트 근거 — 주간 결정적 집계 (전부 억원 단위)
                "sectorFlowWeekly": (sector_flow or {}).get("rows"),
                "netbuyTotalTop": _eok(total.get("top")),
                "netbuyTotalBottom": _eok(total.get("bottom")),
                "shortLoan": short_loan,
            }, ensure_ascii=False)
            try:
                synthesis, generated_by = llm.generate_json(
                    _SYSTEM, user, max_tokens=4096, schema=_SCHEMA, return_model=True)
            except llm.LLMError as ex:
                print(f"[weekly] LLM 합성 실패 — 집계만 저장: {ex}", file=sys.stderr)

    # 타임라인 병합 — LLM 은 시장 이벤트 행만, 종목 행은 스코어 기준으로 결정적 추가
    # (코스피 ★4 이상 / 코스닥 ★5 — LLM 누락·기준 이탈 방지, 2026-09-08)
    stock_rows = [{"date": d["date"], **p}
                  for d in days for p in (d.get("timelineStocks") or [])]
    if stock_rows or synthesis:
        syn = synthesis if isinstance(synthesis, dict) else {}
        market_rows = [t for t in (syn.get("catalystTimeline") or []) if not t.get("stock")]
        merged = []
        for dt in sorted({r["date"] for r in stock_rows}
                         | {t.get("date") for t in market_rows if t.get("date")}):
            merged += [t for t in market_rows if t.get("date") == dt]
            merged += [r for r in stock_rows if r["date"] == dt]
        syn["catalystTimeline"] = merged
        synthesis = syn

    out = {
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
                  "usEcon": d.get("usEcon") or [],   # 전일 미국 주요 지표(★3+)
                  "catalysts": (d.get("catalysts") or [])[:3]} for d in days],
        "netbuyCum": netbuy_cum,        # 주체별(외인/기관/연기금) 주간 누적 순매수 상/하위
        "usWeekly": us_weekly,          # 미국 지수 주간 누적 등락(모닝브리핑 전일 기준 합산)
        "shortLoan": short_loan,        # 공매도 누적·대차잔고 증감 상위 (랭킹 유니버스 한정)
        "sectorWeekly": sector_weekly,  # 섹터 주간 지속성 (등장 일수·평균 등락)
        "sectorFlow": sector_flow,      # 섹터 x 수급 매트릭스 (업종별 주간 등락·투자자 순매수, 억)
        "breadth": _breadth_weekly(dates),   # 주간 ADR 추이 + 신고가 근접 섹터 그룹
        "synthesis": synthesis,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{week_start}.json")
    for p in (path, SNAP):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    # index.json — git 폴백용 (DB 서빙은 쿼리로 대체하지만 raw 폴백 경로 대칭 유지)
    idx_path = os.path.join(OUT_DIR, "index.json")
    try:
        with open(idx_path, encoding="utf-8") as f:
            idx = json.load(f)
    except OSError:
        idx = []
    if week_start not in idx:
        idx = sorted(set(idx) | {week_start}, reverse=True)
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(idx, f, indent=1)
    print(f"[weekly] 저장: {path} (synthesis={'ok' if synthesis else '생략'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
