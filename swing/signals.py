"""신호 추출(3단계) — main 주간 브리핑(weekly_briefing.json)에서 매수 후보·매도 검토 트리거.

- 매수 후보: sectorScreen.matrix 의 동반강세·수급유입 칸(칸당 ≤5, 생성기 정렬 유지 —
  동반강세 먼저). 매일 누적 진행이므로 그날 칸에 있는 종목 전부가 후보이고, 보유 중·주문 대기
  종목만 제외한다.
- 매도 검토: 보유 종목의 섹터가 sectorFlow 에서 수급이탈/동반약세 **또는** 종목이 matrix 의
  수급이탈/동반약세 칸에 등재(둘 중 하나라도).
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
    out, seen = [], set(exclude_codes)
    m = _matrix(wb)
    for sig in config.BUY_SIGNALS:
        for x in m.get(sig) or []:
            code = str(x.get("code") or "").zfill(6) if x.get("code") else None
            if not code or code in seen:
                continue
            seen.add(code)
            out.append({"code": code, "name": x.get("name"), "sector": x.get("sector"),
                        "signal": sig, "score": x.get("score"), "reason": x.get("reason")})
    return out


def down_codes(wb):
    """{code: 칸} — 수급이탈·동반약세 칸 등재 종목."""
    m = _matrix(wb)
    return {str(x["code"]).zfill(6): sig
            for sig in config.SELL_SIGNALS for x in (m.get(sig) or []) if x.get("code")}


def review_triggers(positions, wb):
    """보유 종목 중 매도 검토 대상 → [{code, name, reasons:[...], signal}]. 매도 예약된 종목 제외."""
    secs, downs, out = sector_signals(wb), down_codes(wb), []
    for p in positions or []:
        if p.get("sellPending"):
            continue
        reasons, sig = [], None
        s = secs.get(norm_sec(p.get("sector")))
        if s in config.SELL_SIGNALS:
            reasons.append(f"섹터 {p.get('sector')} {s} 전환")
            sig = s
        if p["code"] in downs:
            reasons.append(f"종목 {downs[p['code']]} 칸 등재")
            sig = sig or downs[p["code"]]
        if reasons:
            out.append({"code": p["code"], "name": p.get("name"), "sector": p.get("sector"),
                        "signal": sig, "reasons": reasons})
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
