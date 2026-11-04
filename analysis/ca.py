"""예탁원정보 CA(기업행위) 일정 → 온디맨드 딥리서치(generate_analysis.analyze_stock) 입력·표시.

원천: KIS 허브 `GET {FLOW_API_BASE}/ca?code=<6자리>&raw=0` (trading_bot_HUB_KIS core/ksd_ca —
ksdinfo 12종 정규화, 종목 조회 기본 창 = 과거 365일 ~ 앞으로 180일). 이 PC 에서 직접 부르지 않는다.

swing/agent/ca.py features() 이식(2026-11-04). 차이: 스윙 '최대 보유일' 대신 딥리서치 관점의
'단기 영향 구간' WINDOW_BD(20영업일) 상수, 영업일은 krx_calendar(주말 + KRX 휴장일).
날짜·비율 계산은 전부 코드가 한다(LLM 날짜 계산 오류 방지):
  d_n          = 기준일 대비 영업일 차(+ 미래, − 과거). 기간 일정은 d_n_end 도.
  stage        = 다음 핵심일 라벨 + D±n (당일이면 '당일', 남은 일정 없으면 '일정 종료(과거)')
  inWindow     = 기준일 ~ WINDOW_BD 영업일 안에 드는(또는 걸치는) 기준일 외 일정이 있음
  haltInWindow = 매매정지 기간이 [기준일, 창 끝] 과 겹침 → {"from","to"} | None
  sizePct      = newSharesPct(신주 비율) 또는 pctOfTotal(예치·상장 물량 비율)
  gapPct       = 현재가 ÷ (발행가 | 매수청구가 | 공모가) − 1 (%), gapRef = 기준 가격 라벨
마지막 일정이 KEEP_PAST_DAYS(60일)보다 오래된 CA 는 버린다(과거 배당 이력 소음).
fields 키 이름은 KIS 문서 기준이고 실서비스 미검증 — 전부 방어적으로 읽는다.
"""
from __future__ import annotations

import datetime
import os

FLOW_API_BASE = os.environ.get(
    "FLOW_API_BASE", "https://tradingstrategies-production-09d4.up.railway.app")
WINDOW_BD = 20                 # 단기 영향 구간(영업일) — 딥리서치는 스윙이 아니므로 고정 상수
KEEP_PAST_DAYS = 60
REF_PRICE = (("issuePrice", "발행가"), ("buyReqPrice", "매수청구가"), ("offerPrice", "공모가"))
_FIELD_MAX = 14
_STR_MAX = 80
KST = datetime.timezone(datetime.timedelta(hours=9))

NOTE = ("예탁원정보(KIS ksdinfo) 기업행위 일정. d_n = 기준일 대비 영업일(+미래/−과거), "
        f"inWindow = 앞으로 {WINDOW_BD}영업일(단기 영향 구간) 안의 일정, haltInWindow = 그 구간과 겹치는 "
        "매매정지, sizePct = 신주·예치 물량 ÷ 총발행주식(%), gapPct = 현재가 ÷ gapRef − 1(%). "
        "배당락(추정)은 기준일 전 평일 — 공휴일 미반영. 의무예치는 예치일 기준(해제일은 사유·공시로 확인).")


def fetch(code, timeout=40):
    """허브 /ca 종목 조회. status!=success 면 예외(호출부가 'unavailable' 로 구분)."""
    import requests
    r = requests.get(f"{FLOW_API_BASE.rstrip('/')}/ca",
                     params={"code": code, "raw": "0"}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    if not isinstance(d, dict) or d.get("status") != "success":
        raise RuntimeError(f"hub /ca status={(d or {}).get('status') if isinstance(d, dict) else d!r} "
                           f"errors={(d or {}).get('errors') if isinstance(d, dict) else ''}")
    return d


# -- 헬퍼 -------------------------------------------------------------------------

def _norm_date(s):
    if s is None:
        return None
    t = str(s).strip().replace(".", "-").replace("/", "-")
    if len(t) == 8 and t.isdigit():
        t = f"{t[:4]}-{t[4:6]}-{t[6:]}"
    t = t[:10]
    try:
        datetime.date.fromisoformat(t)
    except ValueError:
        return None
    return t


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _slim_fields(fields):
    out = {}
    if not isinstance(fields, dict):
        return out
    for k, v in fields.items():
        if len(out) >= _FIELD_MAX:
            break
        if v is None or isinstance(v, (dict, list, tuple)):
            continue
        if isinstance(v, str):
            v = v.strip()[:_STR_MAX]
            if not v:
                continue
        out[str(k)] = v
    return out


def _holidays():
    try:
        import krx_calendar
        return krx_calendar.load_holidays()
    except Exception:
        return set()


def trading_days(start, end, holidays=None):
    """[start, end] 영업일('YYYY-MM-DD') 정렬 목록 — 주말·KRX 휴장일 제외."""
    hol = _holidays() if holidays is None else holidays
    d, e = datetime.date.fromisoformat(start), datetime.date.fromisoformat(end)
    out = []
    while d <= e:
        if d.weekday() < 5 and d.isoformat() not in hol:
            out.append(d.isoformat())
        d += datetime.timedelta(days=1)
    return out


def base_date(now=None, holidays=None):
    """기준일 = 오늘(KST)이 영업일이면 오늘, 아니면 직전 영업일(주말·휴장 조회)."""
    hol = _holidays() if holidays is None else holidays
    if now is None:
        d = datetime.datetime.now(KST).date()
    elif isinstance(now, datetime.datetime):
        d = now.astimezone(KST).date() if now.tzinfo else now.date()
    else:
        d = now
    for _ in range(15):
        if d.weekday() < 5 and d.isoformat() not in hol:
            return d.isoformat()
        d -= datetime.timedelta(days=1)
    return d.isoformat()


def _bdiff(date, target, days):
    """date 기준 target 까지 영업일 차. days = 충분히 넓은 영업일 목록(정렬)."""
    if target >= date:
        return sum(1 for x in days if date < x <= target)
    return -sum(1 for x in days if target <= x < date)


# -- 피처 ---------------------------------------------------------------------------

def features(payload, date, close, window_bd=WINDOW_BD, holidays=None):
    """허브 /ca 응답 → 결정적 피처(대시보드 표·LLM 입력 공용). date = 기준일 'YYYY-MM-DD'."""
    d0 = datetime.date.fromisoformat(date)
    days = trading_days((d0 - datetime.timedelta(days=400)).isoformat(),
                        (d0 + datetime.timedelta(days=400)).isoformat(), holidays)
    future = [x for x in days if x >= date]
    win_end = future[min(window_bd, len(future) - 1)] if future else date
    cutoff = (d0 - datetime.timedelta(days=KEEP_PAST_DAYS)).isoformat()
    close_f = _num(close)
    items = []
    for e in (payload or {}).get("events") or []:
        if not isinstance(e, dict):
            continue
        ms = []
        for m in e.get("milestones") or []:
            if not isinstance(m, dict):
                continue
            dt = _norm_date(m.get("date"))
            if not dt:
                continue
            mm = {"kind": str(m.get("kind") or ""), "label": m.get("label") or m.get("kind") or "일정",
                  "date": dt}
            end = _norm_date(m.get("end"))
            if end and end > dt:
                mm["end"] = end
            if m.get("note"):
                mm["note"] = str(m["note"])[:120]
            ms.append(mm)
        rec = _norm_date(e.get("recordDate"))
        if not ms and rec:
            ms = [{"kind": "record", "label": "기준일", "date": rec}]
        if not ms:
            continue
        ms.sort(key=lambda m: (m["date"], m.get("end") or ""))
        last = max(m.get("end") or m["date"] for m in ms)
        if last < cutoff:
            continue
        nxt = nxt_rec = halt = None
        for m in ms:
            m["d_n"] = _bdiff(date, m["date"], days)
            if m.get("end"):
                m["d_n_end"] = _bdiff(date, m["end"], days)
            upcoming = (m.get("end") or m["date"]) >= date
            if upcoming and m["kind"] != "record" and nxt is None:
                nxt = m
            if upcoming and m["kind"] == "record" and nxt_rec is None:
                nxt_rec = m
            if (halt is None and (m["kind"] == "halt" or "매매정지" in str(m["label"]))
                    and m["date"] <= win_end and (m.get("end") or m["date"]) >= date):
                halt = {"from": m["date"], "to": m.get("end") or m["date"]}
        nxt = nxt or nxt_rec                     # 기준일만 남은 일정(주총·배당 기준일)
        if nxt is None:
            stage = "일정 종료(과거)"
        elif nxt["d_n"] == 0 or (nxt.get("end") and nxt["date"] < date):
            stage = f"{nxt['label']} {'진행 중' if nxt.get('end') and nxt['date'] < date else '당일'}"
        else:
            stage = f"{nxt['label']} D{nxt['d_n']:+d}"
        in_win = any(
            m["kind"] != "record" and (
                0 <= m["d_n"] <= window_bd
                or (m.get("d_n_end") is not None and m["d_n"] <= window_bd and m["d_n_end"] >= 0))
            for m in ms)
        f = _slim_fields(e.get("fields"))
        dps = _num(f.get("cashDps"))
        if close_f and dps and f.get("cashYieldPct") is None:   # 허브 divi_rate 는 액면 기준(2026-11-04 실측) — 시가배당률은 현재가로
            f["cashYieldPct"] = round(dps / close_f * 100, 2)
        it = {"type": e.get("type"), "label": e.get("label") or e.get("type"),
              "recordDate": rec, "milestones": ms, "fields": f, "stage": stage,
              "nextMilestone": nxt, "inWindow": in_win, "haltInWindow": halt}
        size = _num(f.get("newSharesPct"))
        if size is None:
            size = _num(f.get("pctOfTotal"))
        if size is not None:
            it["sizePct"] = size
        for k, lab in REF_PRICE:
            ref = _num(f.get(k))
            if close_f and ref:
                it["gapPct"] = round((close_f / ref - 1) * 100, 2)
                it["gapRef"] = lab
                break
        items.append(it)
    items.sort(key=lambda x: (not x["inWindow"], (x["nextMilestone"] or {}).get("d_n", 9999)))
    return {"status": "ok" if items else "empty", "asof": (payload or {}).get("asof"),
            "date": date, "windowDays": window_bd, "windowEnd": win_end, "close": close_f,
            "items": items, "inWindow": sum(1 for x in items if x["inWindow"]),
            "haltInWindow": any(x["haltInWindow"] for x in items),
            "hubErrors": (payload or {}).get("errors") or {}, "note": NOTE}


def unavailable(err):
    """수집 실패 피처 — '일정 없음'과 구분(대시보드·LLM 모두 '수집 실패'로 표기)."""
    return {"status": "unavailable", "error": str(err)[:200], "items": [],
            "windowDays": WINDOW_BD, "note": "수집 실패 — '일정 없음'이 아니다."}


def llm_view(ca):
    """LLM 입력용 슬림 — 코드가 계산한 d_n·비율·가격을 그대로 넘긴다(재계산 유인 제거)."""
    ca = ca or {}
    out = {"status": ca.get("status", "unavailable"), "baseDate": ca.get("date"),
           "windowDays": ca.get("windowDays", WINDOW_BD), "windowEnd": ca.get("windowEnd"),
           "close": ca.get("close"), "note": ca.get("note")}
    if ca.get("status") == "unavailable":
        out["error"] = ca.get("error")
    items = []
    for x in ca.get("items") or []:
        y = {"type": x.get("type"), "label": x.get("label"), "stage": x.get("stage"),
             "inWindow": x.get("inWindow"), "haltInWindow": x.get("haltInWindow"),
             "milestones": [{k: m.get(k) for k in ("kind", "label", "date", "end", "d_n", "d_n_end", "note")
                             if m.get(k) is not None} for m in x.get("milestones") or []],
             "fields": x.get("fields") or {}}
        for k in ("sizePct", "gapPct", "gapRef", "recordDate"):
            if x.get(k) is not None:
                y[k] = x[k]
        items.append(y)
    out["items"] = items
    return out


EMPTY_SUMMARY = "해당 기간 CA 일정 없음"
FAIL_SUMMARY = "수집 실패 — CA 일정 유무 모름"


def merge_interp(llm_ca, ca):
    """LLM corporateActions 를 결정적 피처와 정렬 — type + 같은 type 내 순서로 매칭.

    반환 {"summary", "items"(LLM 원본), "byItem"(ca.items 와 같은 길이, 못 맞추면 None)}.
    CA 없음/수집 실패면 items·byItem 비우고 summary 를 규칙 문구로 고정(LLM 이 지어낸 행 차단)."""
    llm_ca = llm_ca if isinstance(llm_ca, dict) else {}
    status = (ca or {}).get("status", "unavailable")
    if status != "ok":
        return {"summary": FAIL_SUMMARY if status == "unavailable" else EMPTY_SUMMARY,
                "items": [], "byItem": []}
    pools = {}
    for it in llm_ca.get("items") or []:
        if isinstance(it, dict):
            pools.setdefault(str(it.get("type") or ""), []).append(it)
    used = {}
    by_item = []
    for x in ca.get("items") or []:
        t = str(x.get("type") or "")
        i = used.get(t, 0)
        cand = pools.get(t) or []
        by_item.append(cand[i] if i < len(cand) else None)
        used[t] = i + 1
    return {"summary": llm_ca.get("summary") or "", "items": llm_ca.get("items") or [],
            "byItem": by_item}
