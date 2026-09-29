"""trading_agent 연결 형식(1단계) — 판단 요청/결과와 파서, 그리고 가짜 구현.

실제 구현(P1: homework 레포 trading_agent 역할 프롬프트 헤드리스 이식)은 같은 형식의
`decide(req) -> Decision` 만 맞추면 run_daily 에 그대로 꽂힌다.

가격 출처(사용자 합의): 진입가·손절가 = Trader, 목표가 = PM Price Target(생략 가능).
등급은 PM `**Rating**:` 줄. 5단계 단어가 없으면 REVIEW(Hold 로 뭉개지 않음 — 스킬 규칙).
"""
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from . import config

RATINGS = ("Buy", "Overweight", "Hold", "Underweight", "Sell")
ACTIONS = ("Buy", "Hold", "Sell")


@dataclass
class DecisionRequest:
    code: str
    name: str
    date: str                       # 판단 기준일(YYYY-MM-DD, 장 마감 후)
    purpose: str                    # "entry"(신규 후보) | "review"(보유 종목 매도 검토)
    sector: Optional[str] = None
    signal: Optional[str] = None    # 매트릭스 칸(동반강세 등) 또는 트리거 신호
    reason: Optional[str] = None    # 스크리닝 근거 문구 / 매도 검토 트리거 사유
    last_close: Optional[float] = None
    position: Optional[dict] = None  # review 일 때 보유 정보(진입가·손절·목표·보유일)


@dataclass
class Decision:
    code: str
    date: str
    purpose: str
    rating: str = "REVIEW"           # PM 등급
    action: Optional[str] = None     # Trader Action
    entry: Optional[float] = None    # Trader Entry Price(원)
    stop: Optional[float] = None     # Trader Stop Loss(원)
    target: Optional[float] = None   # PM Price Target(원)
    weight: Optional[float] = None   # Trader Position Sizing → 비율(0~1)
    summary: str = ""                # PM Executive Summary
    reports: dict = field(default_factory=dict)   # 역할별 원문(대시보드 판단 리포트)
    agent: str = ""                  # 구현 식별자(mock / trading_agent@<sha>)
    error: Optional[str] = None      # 판단 실패 사유(수집 실패·LLM 실패)

    def to_dict(self):
        return asdict(self)


# ── 파서: trading_agent 마크다운 출력(output_schemas.md) → 값 ───────────────────
_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def parse_price(s):
    """'12,300원' · '약 12300 원' → 12300.0. 범위('26만~27만')·%·만원 표기는 None(절대가 원칙)."""
    if s is None:
        return None
    s = str(s).strip()
    if not s or "%" in s or "~" in s or "만" in s:
        return None
    m = _NUM.search(s)
    if not m:
        return None
    v = float(m.group().replace(",", ""))
    return v if v > 0 else None


def parse_weight(s):
    """'포트폴리오의 5%' → 0.05. 숫자 없으면 None."""
    if s is None:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", str(s))
    if not m:
        return None
    v = float(m.group(1)) / 100
    return v if v > 0 else None


def _field(text, label):
    m = re.search(r"\*\*" + re.escape(label) + r"\*\*\s*:\s*(.+)", text or "")
    return m.group(1).strip() if m else None


def _word(val, allowed):
    if not val:
        return None
    v = val.replace("*", "").strip()
    for w in allowed:
        if re.match(w + r"\b", v, re.I):
            return w
    return None


def parse_trader(text):
    return {"action": _word(_field(text, "Action"), ACTIONS),
            "entry": parse_price(_field(text, "Entry Price")),
            "stop": parse_price(_field(text, "Stop Loss")),
            "weight": parse_weight(_field(text, "Position Sizing"))}


def parse_pm(text):
    return {"rating": _word(_field(text, "Rating"), RATINGS) or "REVIEW",
            "target": parse_price(_field(text, "Price Target")),
            "summary": _field(text, "Executive Summary") or ""}


def decision_from_markdown(req, trader_md, pm_md, reports=None, agent=""):
    t, p = parse_trader(trader_md), parse_pm(pm_md)
    return Decision(code=req.code, date=req.date, purpose=req.purpose,
                    rating=p["rating"], action=t["action"], entry=t["entry"],
                    stop=t["stop"], target=p["target"], weight=t["weight"],
                    summary=p["summary"], reports=reports or {}, agent=agent)


# ── 판정 ────────────────────────────────────────────────────────────────────
def entry_verdict(d):
    """진입 주문 가능 여부 → (ok, 사유). 조건: PM Buy/Overweight + Trader Buy + 진입가·손절가,
    손절 < 진입, 목표가 있으면 목표 > 진입(아니면 목표 무시하고 진행)."""
    if d.error:
        return False, f"판단 실패: {d.error}"
    if d.rating not in config.BUY_RATINGS:
        return False, f"PM {d.rating}"
    if d.action != "Buy":
        return False, f"Trader {d.action or '없음'}"
    if not d.entry:
        return False, "진입가 없음"
    if not d.stop:
        return False, "손절가 없음"
    if d.stop >= d.entry:
        return False, "손절가 ≥ 진입가"
    return True, ""


def sell_verdict(d):
    """보유 종목 매도 여부 — PM Sell/Underweight 면 매도. 판단 실패는 보유 유지."""
    return (not d.error) and d.rating in config.SELL_RATINGS


def effective_weight(d):
    w = d.weight if d.weight else config.DEFAULT_WEIGHT
    return min(w, config.MAX_WEIGHT)


def effective_target(d):
    return d.target if (d.target and d.entry and d.target > d.entry) else None


# ── 가짜 구현(오프라인 테스트·로컬 프리뷰용) ─────────────────────────────────
class MockAgent:
    """결정적 가짜 판단. 신규: 전일 종가 기준 진입 -1%, 손절 -5%, 목표 +6%, 비중 5%.
    코드 끝자리가 7·8·9 면 Hold(미진입 경로 확인용). 매도 검토: 끝자리 짝수면 Sell."""
    name = "mock"

    def decide(self, req):
        d = Decision(code=req.code, date=req.date, purpose=req.purpose, agent=self.name)
        last = req.last_close
        if req.purpose == "review":
            d.rating = "Sell" if int(req.code[-1]) % 2 == 0 else "Hold"
            d.action = "Sell" if d.rating == "Sell" else "Hold"
            if d.rating == "Hold" and last:                 # 계속 보유 시 새 목표·손절(목표/만기 재판별용)
                d.target, d.stop = round(last * 1.05), round(last * 0.96)
            d.summary = f"[mock] 매도 검토 — {req.reason or ''}"
            return d
        if not last:
            d.error = "전일 종가 없음"
            return d
        if req.code[-1] in "789":
            d.rating, d.action = "Hold", "Hold"
            d.summary = "[mock] 근거 균형 — Hold"
            return d
        d.rating, d.action = "Buy", "Buy"
        d.entry = round(last * 0.99)
        d.stop = round(last * 0.95)
        d.target = round(last * 1.06)
        d.weight = 0.05
        d.summary = f"[mock] {req.signal} {req.sector} — 진입 {d.entry:,.0f} 손절 {d.stop:,.0f}"
        d.reports = {"trader": f"**Action**: Buy\n\n**Entry Price**: {d.entry:,.0f}원",
                     "pm": f"**Rating**: Buy\n\n**Price Target**: {d.target:,.0f}원"}
        return d
