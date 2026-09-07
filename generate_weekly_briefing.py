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

    closing = None
    for r in reversed((intraday or {}).get("rounds") or []):
        if r.get("final"):
            closing = r
            break
    if closing is None and (intraday or {}).get("rounds"):
        rs = [r for r in intraday["rounds"] if not r.get("scoring")]
        closing = rs[-1] if rs else None                # 마감 회차 결손 시 마지막 시황 회차
    # 16:00 촉매 스코어 회차 — 5점(만점) 종목은 주간 촉매 타임라인 필수 반영 대상
    for r in reversed((intraday or {}).get("rounds") or []):
        if r.get("scoring"):
            fs = [{"stock": c.get("stock"), "market": c.get("market"),
                   "changePct": c.get("changePct"), "reason": (c.get("reason") or "")[:80]}
                  for c in (r.get("catalysts") or []) if c.get("score") == 5]
            if fs:
                day["fiveStar"] = fs
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
    "\n- catalystTimeline: 날짜별 핵심 이벤트 행 (거래일당 2~4행). 각 행은 가능한 한"
    " 종목 1개 단위로 분리해 stock(종목명)·market(KOSPI|KOSDAQ — 입력 catalysts 의 market)·"
    " event(촉매 한 문장, 종목명 반복 금지)·changePct(입력의 당일 등락률 숫자 그대로)를 채워라. 시장 전체 이벤트(지수·환율 등)는"
    " stock 없이 event 만. fiveStar(촉매 스코어 5점) 종목이 있는 날은 그 종목 행을 반드시"
    " 포함하고 star 필드에 5 를 넣어라 (그 외 종목은 star 생략)"
    "\n- dailyContext: 거래일마다 정확히 1개 — 전일 미국장 주요 이슈·경제지표(usReview,"
    " usCatalystsTop)가 당일 한국장에 어떻게 반영됐는지(지수·섹터·수급 반응, briefing 근거)를"
    " 잇는 1문장. 종목 나열이 아니라 '미국장 원인 → 한국장 반응' 구조로 작성."
    " 그 날 모닝브리핑 스탠스(stance·stanceReason)의 논조를 기준점으로 삼아 방향이"
    " 일치하게 쓰되, 실제 장 반응이 스탠스와 달랐다면 '신중 스탠스에도 불구하고 ~ 급등'"
    " 처럼 그 괴리를 명시적으로 연결하라 (스탠스 근거를 무시한 반대 논조 금지)"
    "\n- nextWeekPreview: 다음 주 주목 포인트 2~4개 불릿 (제공된 예정 이벤트 기반, 없으면 빈 배열)"
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
        ft = _day_flow_top(today_iso)      # 방금 저장한 당일 스냅샷으로 재부착
        for d in days:
            if d["date"] == today_iso and ft:
                d["flowTop"] = ft
    netbuy_cum = _netbuy_cum(dates)
    print(f"[weekly] {week_start} ~ {week_end}: 거래일 {len(days)}일 수집")

    preview = _next_week_preview()
    synthesis, generated_by = None, None
    if "--no-llm" not in args:
        if not llm.configured():
            print("[weekly] LLM 미설정 — synthesis 생략", file=sys.stderr)
        else:
            user = json.dumps({"days": days, "nextWeekEvents": preview},
                              ensure_ascii=False)
            try:
                synthesis, generated_by = llm.generate_json(
                    _SYSTEM, user, max_tokens=4096, schema=_SCHEMA, return_model=True)
            except llm.LLMError as ex:
                print(f"[weekly] LLM 합성 실패 — 집계만 저장: {ex}", file=sys.stderr)

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
                  "catalysts": (d.get("catalysts") or [])[:3]} for d in days],
        "netbuyCum": netbuy_cum,        # 주체별(외인/기관/연기금) 주간 누적 순매수 상/하위
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
