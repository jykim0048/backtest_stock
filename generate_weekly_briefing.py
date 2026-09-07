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
    """서버(기본) 또는 로컬 public/ 에서 JSON. 부재/실패 시 None."""
    if USE_LOCAL:
        try:
            with open(os.path.join(ROOT, "public", rel), encoding="utf-8") as f:
                return json.load(f)
        except OSError:
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
        closing = intraday["rounds"][-1]                # 마감 회차 결손 시 마지막 회차
    if closing:
        day["indices"] = closing.get("indices") or {}
        day["investors"] = closing.get("investors") or {}
        day["briefing"] = (closing.get("briefing") or [])[:8]
        day["sectorsUp"] = [{"name": s.get("name"), "changePct": s.get("changePct")}
                            for s in (closing.get("sectorsUp") or [])[:3]]
        day["sectorsDown"] = [{"name": s.get("name"), "changePct": s.get("changePct")}
                              for s in (closing.get("sectorsDown") or [])[:3]]
        day["catalysts"] = [
            {"stock": c.get("stock"), "direction": c.get("direction"),
             "summary": (c.get("summary") or "")[:120]}
            for c in (closing.get("catalysts") or [])[:8]]
    return day


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
            "date": {"type": "string"}, "event": {"type": "string"}},
            "required": ["date", "event"]}},
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
    "\n- catalystTimeline: 날짜별 핵심 이벤트 1줄씩 (거래일당 1~2개, 가장 영향 큰 것)"
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
