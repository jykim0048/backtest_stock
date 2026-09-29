"""신호 추출(3단계) — main 주간 브리핑(weekly_briefing.json)에서 매수·공매도 후보와 청산 검토 트리거.

- 매수 후보: sectorScreen.matrix 의 동반강세·수급유입 칸(칸당 ≤5, 생성기 정렬 유지 —
  동반강세 먼저). 매일 누적 진행이므로 그날 칸에 있는 종목 전부가 후보이고, 보유 중·주문 대기
  종목만 제외한다.
- 공매도 후보(2026-09-29): 동반약세 → 수급이탈 칸, 같은 방식(보유·주문 대기는 방향 무관 제외).
- 청산 검토: Long 보유는 섹터가 sectorFlow 에서 수급이탈/동반약세 **또는** 종목이 그 칸에 등재,
  Short 보유는 반대로 동반강세/수급유입 전환 또는 그 칸에 등재(둘 중 하나라도).
섹터명 매칭은 생성기 _norm_sec 와 같은 규칙(공백·가운뎃점·괄호 제거).
"""
import re

from . import config


def norm_sec(s):
    return re.sub(r"[\s·・()]", "", str(s or ""))


def asof_date(wb):
    """'2026-09-28 16:13 KST' → '2026-09-28'. 없으면 None."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", str((wb or {}).get("asof") or ""))
    return m.group(1) if m else None


def sector_signals(wb):
    """{정규화 섹터명: 신호}"""
    return {norm_sec(r.get("name")): r.get("signal")
            for r in ((wb or {}).get("sectorFlow") or {}).get("rows") or []}


def _matrix(wb):
    return ((wb or {}).get("sectorScreen") or {}).get("matrix") or {}


def buy_candidates(wb, exclude_codes=()):
    """[{code,name,sector,signal,score,reason}] — 동반강세 → 수급유입 순, 코드 중복 제거."""
    return _candidates(wb, config.BUY_SIGNALS, exclude_codes)


def short_candidates(wb, exclude_codes=()):
    """공매도 후보 — 동반약세 → 수급이탈 순, 코드 중복 제거."""
    return _candidates(wb, config.SHORT_SIGNALS, exclude_codes)


def _candidates(wb, sigs, exclude_codes):
    out, seen = [], set(exclude_codes)
    m = _matrix(wb)
    for sig in sigs:
        for x in m.get(sig) or []:
            code = str(x.get("code") or "").zfill(6) if x.get("code") else None
            if not code or code in seen:
                continue
            seen.add(code)
            out.append({"code": code, "name": x.get("name"), "sector": x.get("sector"),
                        "signal": sig, "score": x.get("score"), "reason": x.get("reason")})
    return out


def _cell_codes(wb, sigs):
    m = _matrix(wb)
    return {str(x["code"]).zfill(6): sig for sig in sigs for x in (m.get(sig) or []) if x.get("code")}


def down_codes(wb):
    """{code: 칸} — 수급이탈·동반약세 칸 등재 종목."""
    return _cell_codes(wb, config.SELL_SIGNALS)


def up_codes(wb):
    """{code: 칸} — 동반강세·수급유입 칸 등재 종목(보유 Short 환매 검토 트리거)."""
    return _cell_codes(wb, config.BUY_SIGNALS)


def review_triggers(positions, wb):
    """보유 종목 중 청산 검토 대상 → [{code, name, reasons:[...], signal, side}]. 청산 예약된 종목 제외.
    Long 은 하방 신호(수급이탈·동반약세), Short 는 상방 신호(동반강세·수급유입)가 트리거."""
    secs, downs, ups, out = sector_signals(wb), down_codes(wb), up_codes(wb), []
    for p in positions or []:
        if p.get("sellPending"):
            continue
        short = p.get("side") == "short"
        trig, cells = (config.BUY_SIGNALS, ups) if short else (config.SELL_SIGNALS, downs)
        reasons, sig = [], None
        s = secs.get(norm_sec(p.get("sector")))
        if s in trig:
            reasons.append(f"섹터 {p.get('sector')} {s} 전환")
            sig = s
        if p["code"] in cells:
            reasons.append(f"종목 {cells[p['code']]} 칸 등재")
            sig = sig or cells[p["code"]]
        if reasons:
            out.append({"code": p["code"], "name": p.get("name"), "sector": p.get("sector"),
                        "signal": sig, "reasons": reasons, "side": "short" if short else "long"})
    return out


def compact(wb):
    """백필·저장용 축약본 — 신호 판정 키 + 대시보드 섹터x수급 매트릭스 행 전체."""
    m = _matrix(wb)
    return {"asof": (wb or {}).get("asof"), "weekStart": (wb or {}).get("weekStart"),
            # 섹터x수급 매트릭스는 주간 브리핑 양식 그대로 렌더하므로 행 전체(YTD/3M/1M/1W·투자자 세분·
            # 신호)와 범례용 chgBasis·neutralRatio 를 보존
            "sectorFlow": {"rows": list(((wb or {}).get("sectorFlow") or {}).get("rows") or []),
                           **{k: ((wb or {}).get("sectorFlow") or {}).get(k)
                              for k in ("asof", "chgBasis", "neutralRatio")}},
            "sectorScreen": {"matrix": {k: [{f: x.get(f) for f in
                                             ("code", "name", "sector", "sectorSignal",
                                              "score", "reason")} for x in v]
                                        for k, v in m.items()}}}
