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
    side: str = "long"              # "long" | "short"(공매도 진입 / 보유 숏 환매 검토)


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
    side: str = "long"               # 요청 방향(대시보드 제목 — 공매도 진입·환매 검토)

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
                    summary=p["summary"], reports=reports or {}, agent=agent, side=req.side)


# ── 판정 ────────────────────────────────────────────────────────────────────
def entry_verdict(d, last_close=None):
    """진입 주문 가능 여부 → (ok, 사유). 방향은 d.side.
    Long : PM Buy/Overweight + Trader Buy + 진입가·손절가, 손절 < 진입.
    Short: PM Sell/Underweight + Trader Sell(= 공매도 진입) + 진입가·손절가, 손절 > 진입,
           업틱룰 근사로 진입가 ≥ 전일 종가(last_close 있을 때).
    목표가는 방향이 맞지 않으면 무시하고 진행(effective_target)."""
    short = d.side == "short"
    if d.error:
        return False, f"판단 실패: {d.error}"
    if d.rating not in (config.SHORT_RATINGS if short else config.BUY_RATINGS):
        return False, f"PM {d.rating}"
    want = "Sell" if short else "Buy"
    if d.action != want:
        return False, f"Trader {d.action or '없음'}"
    if not d.entry:
        return False, "진입가 없음"
    if not d.stop:
        return False, "손절가 없음"
    if short and d.stop <= d.entry:
        return False, "손절가 ≤ 진입가(공매도)"
    if not short and d.stop >= d.entry:
        return False, "손절가 ≥ 진입가"
    if short and last_close and d.entry < last_close:
        return False, f"진입가 < 전일 종가 {last_close:,.0f}(업틱룰 근사)"
    return True, ""


def sell_verdict(d):
    """보유 종목 청산 여부 — Long 은 PM Sell/Underweight 면 매도, Short 는 PM Buy/Overweight 면 환매
    (Hold 는 양쪽 모두 계속 보유). 판단 실패는 보유 유지."""
    ratings = config.COVER_RATINGS if d.side == "short" else config.SELL_RATINGS
    return (not d.error) and d.rating in ratings


def effective_weight(d):
    w = d.weight if d.weight else config.DEFAULT_WEIGHT
    return min(w, config.MAX_WEIGHT)


def effective_target(d):
    """방향이 맞는 목표가만 — Long 목표 > 진입, Short 목표 < 진입."""
    if not (d.target and d.entry):
        return None
    ok = d.target < d.entry if d.side == "short" else d.target > d.entry
    return d.target if ok else None


# ── 가짜 구현(오프라인 테스트·로컬 프리뷰용) ─────────────────────────────────
# 실제 파이프라인(swing/agent/pipeline.py)이 채우는 reports 키 — 대시보드 ROLE_GROUPS(I~V·부록) 순서
REPORT_KEYS = ("peers", "collect", "market", "sentiment", "news", "fundamentals", "flow",
               "debate", "research_manager", "trader", "risk_debate", "pm")


def _won(v):
    return f"{v:,.0f}원" if v else "—"


_MOCK_ANALYSTS = (("market", "Market Analyst", "기술적 분석"), ("sentiment", "Sentiment Analyst", "시장 심리"),
                  ("news", "News Analyst", "뉴스·공시·거시"), ("fundamentals", "Fundamentals Analyst", "재무·밸류에이션"),
                  ("flow", "Flow Analyst", "수급"))


def _mock_reports(req, d):
    """대시보드 판단 원문 구조 확인용 자리표시 — 12개 키 전부, 스킬 리포트 머리 형식을 흉내.
    trader·pm 은 파서 라벨 유지."""
    who = f"{req.name} ({req.code}) {req.date}"
    note = "[mock] 데모 자리표시 — 실제 판단(--agent trading_agent)에서는 역할 원문이 들어갑니다."
    r = {k: f"# {role} — {who} {title}\n{note}" for k, role, title in _MOCK_ANALYSTS}
    r["peers"] = f'{{"source": "mock", "peers": []}}\n{note}'
    r["collect"] = "\n".join(f"- {k}: mock" for k in ("price", "naver_news", "hub_flow", "dart_financials"))
    r["debate"] = (f"# 강세·약세 토론 — {who}\n라운드 1.\n\n## Round 1 — Bull\n\nBull Analyst: {note}\n\n"
                   f"## Round 1 — Bear\n\nBear Analyst: {note}")
    r["research_manager"] = f"**Recommendation**: {d.rating}\n\n**Rationale**: {note}"
    r["risk_debate"] = (f"# 리스크 3자 토론 — {who}\n라운드 1. 순서 공격 → 보수 → 중립.\n\n" + "\n\n".join(
        f"## Round 1 — {s}\n\n{s} Analyst: {note}" for s in ("Aggressive", "Conservative", "Neutral")))
    r["trader"] = (f"**Action**: {d.action or 'Hold'}\n\n**Reasoning**: {note}\n\n**Entry Price**: {_won(d.entry)}\n"
                   f"**Stop Loss**: {_won(d.stop)}\n"
                   f"**Position Sizing**: 포트폴리오의 {(d.weight or 0) * 100:.0f}%\n\n"
                   f"FINAL TRANSACTION PROPOSAL: **{(d.action or 'Hold').upper()}**")
    r["pm"] = (f"**Rating**: {d.rating}\n\n**Executive Summary**: {d.summary}\n\n**Investment Thesis**: {note}\n\n"
               f"**Price Target**: {_won(d.target)}")
    return {k: r[k] for k in REPORT_KEYS}


class MockAgent:
    """결정적 가짜 판단. 신규 Long: 전일 종가 기준 진입 -1%, 손절 -5%, 목표 +6%, 비중 5%.
    신규 Short: 진입 +1%, 손절 +5%, 목표 -6%(Underweight·Sell). 코드 끝자리가 7·8·9 면 Hold(미진입 경로).
    청산 검토: 끝자리 짝수면 Long Sell / Short Buy(환매), 아니면 Hold(새 목표·손절 제시).
    reports 는 실제 파이프라인과 같은 12개 키를 자리표시로 채운다(판단 실패 제외)."""
    name = "mock"

    def decide(self, req):
        d = self._decide(req)
        if not d.error:
            d.reports = _mock_reports(req, d)
        return d

    def _decide(self, req):
        d = Decision(code=req.code, date=req.date, purpose=req.purpose, agent=self.name, side=req.side)
        last = req.last_close
        short = req.side == "short"
        if req.purpose == "review":
            exit_ = int(req.code[-1]) % 2 == 0
            if short:
                d.rating, d.action = ("Buy", "Buy") if exit_ else ("Hold", "Hold")
                if not exit_ and last:                      # 계속 보유 시 새 목표(아래)·손절(위)
                    d.target, d.stop = round(last * 0.95), round(last * 1.04)
                d.summary = f"[mock] 환매 검토 — {req.reason or ''}"
            else:
                d.rating, d.action = ("Sell", "Sell") if exit_ else ("Hold", "Hold")
                if not exit_ and last:                      # 계속 보유 시 새 목표·손절(목표/만기 재판별용)
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
        d.weight = 0.05
        if short:
            d.rating, d.action = "Underweight", "Sell"
            d.entry, d.stop, d.target = round(last * 1.01), round(last * 1.05), round(last * 0.94)
            d.summary = f"[mock] {req.signal} {req.sector} — 공매도 {d.entry:,.0f} 손절 {d.stop:,.0f}"
            return d
        d.rating, d.action = "Buy", "Buy"
        d.entry = round(last * 0.99)
        d.stop = round(last * 0.95)
        d.target = round(last * 1.06)
        d.summary = f"[mock] {req.signal} {req.sector} — 진입 {d.entry:,.0f} 손절 {d.stop:,.0f}"
        return d
