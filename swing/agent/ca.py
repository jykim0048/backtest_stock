"""예탁원정보 CA(기업행위) 일정 → 딥리서치 뉴스·공시 분석가 입력 (2026-11-04 사용자 결정: 전용 분석가 대신 뉴스·공시에 통합).

kit(trading_agent 원본)은 무수정 — 수집 뒤 이 모듈이 05_disclosures.json 에 `corporate_actions` 를 덧붙이고,
pipeline 이 뉴스 분석가 user 프롬프트에만 해석 가이드(ca_playbook.md)를 붙인다.

원천: KIS 허브 `/ca`(trading_bot_HUB_KIS core/ksd_ca — ksdinfo 12종 정규화). 이 PC 에서 직접 부르지 않는다(Railway 실행).
날짜·비율 계산은 코드가 미리 한다(LLM 날짜 계산 오류 방지):
  d_n = 기준일(판단일) 대비 영업일 차(+ 미래, − 과거), inHoldWindow = 0 ≤ d_n ≤ 최대 보유일,
  haltInWindow = 매매정지 기간이 [판단일, 최대 보유일] 과 겹침, gapPct = 현재가 ÷ (발행가·매수청구가·공모가) − 1.
"""
from __future__ import annotations

import datetime
import json
import os

import requests

from .. import config, tradedays

FLOW_API_BASE = os.environ.get("FLOW_API_BASE", "https://tradingstrategies-production-09d4.up.railway.app")
PLAYBOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ca_playbook.md")
KEEP_PAST_DAYS = 60                         # 마지막 일정이 이보다 오래된 CA 는 버린다(배당 과거 이력 소음)
REF_PRICE = (("issuePrice", "발행가"), ("buyReqPrice", "매수청구가"), ("offerPrice", "공모가"))


def fetch(code: str, timeout: int = 40) -> dict:
    r = requests.get(f"{FLOW_API_BASE}/ca", params={"code": code, "raw": "0"}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "success":
        raise RuntimeError(f"hub /ca status={d.get('status')} errors={d.get('errors')}")
    return d


def _bdiff(date: str, target: str, days: list[str]) -> int:
    """date 기준 target 까지 영업일 차. days = 충분히 넓은 영업일 목록(정렬)."""
    if target >= date:
        return sum(1 for x in days if date < x <= target)
    return -sum(1 for x in days if target <= x < date)


def features(payload: dict, date: str, close: float | None, hold_days: int | None = None) -> dict:
    hold = int(hold_days or config.MAX_HOLD_DAYS)
    d0 = datetime.date.fromisoformat(date)
    days = tradedays.trading_days((d0 - datetime.timedelta(days=400)).isoformat(),
                                  (d0 + datetime.timedelta(days=400)).isoformat())
    future = [x for x in days if x >= date]
    hold_end = future[min(hold, len(future) - 1)] if future else date
    cutoff = (d0 - datetime.timedelta(days=KEEP_PAST_DAYS)).isoformat()
    items = []
    for e in payload.get("events") or []:
        ms = e.get("milestones") or []
        last = max([m.get("end") or m["date"] for m in ms] or [e.get("recordDate") or ""])
        if not last or last < cutoff:
            continue
        out_ms, nxt, halt = [], None, None
        for m in ms:
            mm = {**m, "d_n": _bdiff(date, m["date"], days)}
            if m.get("end"):
                mm["d_n_end"] = _bdiff(date, m["end"], days)
            out_ms.append(mm)
            if nxt is None and (m.get("end") or m["date"]) >= date and m.get("kind") != "record":
                nxt = mm
            if m.get("kind") == "halt" and m["date"] <= hold_end and (m.get("end") or m["date"]) >= date:
                halt = {"from": m["date"], "to": m.get("end") or m["date"]}
        f = dict(e.get("fields") or {})
        if close and f.get("cashDps") and f.get("cashYieldPct") is None:     # 허브 divi_rate 는 액면 기준 — 시가배당률은 여기서
            f["cashYieldPct"] = round(f["cashDps"] / close * 100, 2)
        it = {"type": e.get("type"), "label": e.get("label"), "recordDate": e.get("recordDate"),
              "milestones": out_ms, "fields": f,
              "stage": (f"{nxt['label']} D{nxt['d_n']:+d}" if nxt and nxt["d_n"] != 0 else
                        f"{nxt['label']} 당일" if nxt else "일정 종료(과거)"),
              "nextMilestone": nxt,
              "inHoldWindow": any(0 <= m["d_n"] <= hold or (m.get("d_n_end") is not None and m["d_n"] <= hold
                                                            and m["d_n_end"] >= 0) for m in out_ms
                                  if m.get("kind") != "record"),
              "haltInWindow": halt}
        size = f.get("newSharesPct") if f.get("newSharesPct") is not None else f.get("pctOfTotal")
        if size is not None:
            it["sizePct"] = size
        for k, lab in REF_PRICE:
            if close and f.get(k):
                it["gapPct"] = round((close / f[k] - 1) * 100, 2)
                it["gapRef"] = lab
                break
        items.append(it)
    items.sort(key=lambda x: (not x["inHoldWindow"], (x["nextMilestone"] or {}).get("d_n", 9999)))
    return {"status": "ok" if items else "empty", "asof": payload.get("asof"), "date": date,
            "holdDays": hold, "holdWindowEnd": hold_end, "close": close, "items": items,
            "inWindow": sum(1 for x in items if x["inHoldWindow"]),
            "haltInWindow": any(x["haltInWindow"] for x in items),
            "hubErrors": payload.get("errors") or {},
            "note": ("예탁원정보(KIS ksdinfo) 기업행위 일정. d_n = 판단일 대비 영업일(+미래/−과거), "
                     "inHoldWindow = 최대 보유일 안에 드는 일정, haltInWindow = 보유 기간과 겹치는 매매정지, "
                     "sizePct = 신주·예치 물량 ÷ 총발행주식(%), gapPct = 현재가 ÷ gapRef − 1(%). "
                     "배당락(추정)은 기준일 전 평일 — 공휴일 미반영. 의무예치는 예치일 기준(해제일 필드 미확인).")}


def attach(out_dir: str, stock: dict, req, *, fetch_fn=None) -> str:
    """05_disclosures.json 에 corporate_actions 를 덧붙인다. 반환 = 수집 상태(ok|empty|unavailable|skipped)."""
    p = os.path.join(out_dir, "05_disclosures.json")
    if not os.path.exists(p):
        return "skipped"
    close = None
    try:
        with open(os.path.join(out_dir, "01_price.json"), encoding="utf-8") as f:
            close = ((json.load(f) or {}).get("latest_ohlcv") or {}).get("Close")
    except Exception:  # noqa: BLE001
        pass
    try:
        ca = features((fetch_fn or fetch)(stock["code"]), req.date, close)
        status = ca["status"]
    except Exception as ex:  # noqa: BLE001
        ca = {"status": "unavailable", "error": f"{type(ex).__name__}: {str(ex)[:200]}", "items": [],
              "note": "수집 실패 — '일정 없음'이 아니다."}
        status = "unavailable"
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    doc["corporate_actions"] = ca
    with open(p, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    return status


def _fmt_num(v, unit=""):
    if v is None:
        return ""
    return (f"{v:,.0f}" if abs(v) >= 100 else f"{v:g}") + unit


def to_markdown(out_dir: str) -> str:
    """판단 문서 부록용 — 코드가 계산한 CA 원자료 표(LLM 해석과 대조용). 05 의 corporate_actions 기준."""
    try:
        with open(os.path.join(out_dir, "05_disclosures.json"), encoding="utf-8") as f:
            ca = (json.load(f) or {}).get("corporate_actions") or {}
    except Exception:  # noqa: BLE001
        return ""
    if not ca:
        return ""
    if ca.get("status") == "unavailable":
        return f"예탁원 CA 수집 실패 — {ca.get('error') or ''} (일정 유무 모름)"
    items = ca.get("items") or []
    if not items:
        return f"판단일 {ca.get('date')} 기준 CA 일정 없음(과거 {KEEP_PAST_DAYS}일 ~ 앞으로 6개월)."
    lines = [f"판단일 {ca.get('date')} · 최대 보유 {ca.get('holdDays')}영업일(~{ca.get('holdWindowEnd')}) · "
             f"현재가 {_fmt_num(ca.get('close'))}원 · 허브 {ca.get('asof') or ''}", "",
             "| 유형 | 다음 핵심일 | 보유기간 내 | 일정(D-n) | 물량 | 가격 대비 |",
             "|---|---|---|---|---|---|"]
    for x in items:
        ms = " · ".join(f"{m['label']} {m['date'][5:]}" + (f"~{m['end'][5:]}" if m.get("end") else "") +
                        f"({m['d_n']:+d})" for m in x["milestones"] if m.get("kind") != "record")
        win = ("매매정지 겹침" if x.get("haltInWindow") else "예" if x.get("inHoldWindow") else "아니오")
        size = _fmt_num(x.get("sizePct"), "%") if x.get("sizePct") is not None else ""
        gap = f"{x['gapRef']} {x['gapPct']:+.1f}%" if x.get("gapPct") is not None else ""
        lines.append(f"| {x.get('label')} | {x.get('stage')} | {win} | {ms} | {size} | {gap} |")
    return "\n".join(lines)


def guide(status: str) -> str:
    """뉴스 분석가 user 프롬프트 끝에 붙일 CA 지시. 일정이 있을 때만 가이드 전문."""
    if status != "ok":
        word = "수집 실패(일정 유무 모름)" if status == "unavailable" else "해당 기간 CA 일정 없음"
        return (f"\n## 예탁원 CA 일정\n`05_disclosures.json` 의 corporate_actions: {word}. "
                "리포트 끝 'CA 일정표' 섹션에 이 한 줄만 적는다.\n")
    with open(PLAYBOOK, encoding="utf-8") as f:
        return "\n" + f.read()
