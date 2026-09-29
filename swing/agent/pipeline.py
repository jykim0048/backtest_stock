"""trading_agent 무인 파이프라인(P1) — SKILL.md 0~8단계를 LLM 호출만으로 재현.

  0a peer 확정(peers.py, B안) → 0b 수집(kit collect.py, 무수정)
  1~5 애널리스트(각자 자기 파일만) → 6 Bull↔Bear(라운드 SWING_AGENT_ROUNDS) → 7 Research Manager
  → 8 Trader → 9 공격→보수→중립 → 10 PM → agent_iface.decision_from_markdown

LLM 은 루트 llm.generate_json(Gemini 폴백 체인). 역할 출력 형식(output_schemas.md)은 그대로 두고
JSON {"markdown": "..."} 한 겹으로 감싼다. 역할 파일은 수정하지 않고 스윙 전략 맥락은 user 쪽에만 붙인다.
어떤 단계가 실패해도 예외를 올리지 않고 Decision.error + 지금까지의 reports 를 돌려준다.
"""
import json
import os
import tempfile
from pathlib import Path

from .. import agent_iface as A
from .. import config
from . import KIT_DIR, SOURCE_SHA, ensure_kit_path
from . import peers as peers_mod

ROLES = os.path.join(KIT_DIR, "references", "roles")
ROUNDS = int(os.environ.get("SWING_AGENT_ROUNDS", "1") or 1)
SOCIAL = os.environ.get("SWING_AGENT_NO_SOCIAL", "0") != "1"
RUNS_DIR = os.environ.get("SWING_AGENT_RUNS") or os.path.join(tempfile.gettempdir(), "swing_runs")
MAX_INPUT = int(os.environ.get("SWING_AGENT_MAX_INPUT_CHARS", "150000") or 150000)
OHLCV_TAIL = 60                                     # ohlcv.csv 는 1년치 — 최근 N행만 첨부

MD_SCHEMA = {"type": "object", "properties": {"markdown": {"type": "string"}},
             "required": ["markdown"]}
NO_BEAR = "(약세 연구원이 아직 발언하지 않음. 자기 논거로 토론을 시작할 것)"
NO_OPP = "(아직 발언하지 않음. 자기 논거로 시작할 것)"
PAST_NONE = "과거 맥락 없음"

ANALYSTS = (  # (reports 키, 역할 파일, 입력 파일들)
    ("market", "01_market_analyst.md", ("01_price.json", "ohlcv.csv")),
    ("sentiment", "02_sentiment_analyst.md", ("03_news_community.json",)),
    ("news", "03_news_analyst.md", ("03_news_community.json", "05_disclosures.json")),
    ("fundamentals", "04_fundamentals_analyst.md", ("02_fundamentals.json",)),
    ("flow", "05_flow_analyst.md", ("04_flow.json",)),
)
REPORT_TITLES = (("market", "기술적 분석(01)"), ("sentiment", "심리 분석(02)"),
                 ("news", "뉴스·공시 분석(03)"), ("fundamentals", "펀더멘털 분석(04)"),
                 ("flow", "수급 분석(05)"))


class RoleError(Exception):
    pass


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _role(name):
    return _read(os.path.join(ROLES, name))


def _system(role_file):
    rules = _read(os.path.join(KIT_DIR, "references", "kr_market_rules.md"))
    return (rules + "\n\n---\n\n" + _role(role_file)
            + "\n\n---\n## 응답 규약(무인 실행)\n"
              "JSON 객체 하나로만 답한다: {\"markdown\": \"<위 '출력' 형식 그대로의 한국어 본문>\"}. "
              "라벨(예: **Rating**:, **Entry Price**:)과 순서를 그대로 지킨다.")


def _clip(text, label):
    if len(text) <= MAX_INPUT:
        return text
    return text[:MAX_INPUT] + f"\n…(입력 {label} 이 {len(text):,}자라 앞 {MAX_INPUT:,}자만 첨부)"


def strategy_context(req):
    if getattr(req, "side", "long") == "short":
        return _short_context(req)
    if req.purpose == "review":
        p = req.position or {}
        return ("[전략 맥락] 보유 중 종목 매도 검토. 진입 {e}·손절 {s}·목표 {t}·보유 {h}/{n}일(최대 {mx}일). 트리거: {r}. "
                "PM 이 Sell/Underweight 면 다음 영업일 시가 매도, 그 외 계속 보유. 트리거가 목표가 도달·보유 만기·연장 보유면 "
                "계속 보유할 경우 Trader 는 새 Stop Loss(현재가 아래), PM 은 새 Price Target(현재가 위)을 원 단위로 제시한다"
                "(목표 도달 후 보유는 손절가가 매수가 아래로 내려가지 않는다). Trader Action 은 Sell/Hold 중심.").format(
            e=p.get("entry"), s=p.get("stop"), t=p.get("target") or "없음", h=p.get("holdDay"),
            n=config.HOLD_DAYS, mx=config.MAX_HOLD_DAYS, r=req.reason or "-")
    return ("[전략 맥락] 스윙 모의투자 신규 진입 검토. 주문은 다음 영업일 1일 유효 지정가(Entry Price 도달 시 "
            "그 가격 체결), 보유 최대 {n}영업일 후 종가 청산. Stop Loss 필수(없으면 주문 안 됨). Price Target 은 "
            "{n}영업일 안에 현실적인 수준으로. 섹터 신호: {sig} {sec}, 스크리닝 근거: {r}.").format(
        n=config.HOLD_DAYS, sig=req.signal or "-", sec=req.sector or "-", r=req.reason or "-")


def _short_context(req):
    """공매도(Short) 맥락 — 역할 파일은 롱 관점이라 Sell 의 뜻·가격 방향을 user 쪽에서 명시한다."""
    if req.purpose == "review":
        p = req.position or {}
        return ("[전략 맥락] **공매도(Short) 보유 종목 환매 검토.** 공매도 진입 {e}·손절 {s}(진입가 위)·목표 {t}"
                "(진입가 아래)·보유 {h}/{n}일(최대 {mx}일). 트리거: {r}. 이 포지션은 주가가 내려야 이익이다. "
                "PM Rating 이 Buy/Overweight(상승 전망)면 다음 영업일 시가 환매, Hold·Underweight·Sell 이면 계속 보유. "
                "트리거가 목표가 도달·보유 만기·연장 보유면 계속 보유할 경우 Trader 는 새 Stop Loss(현재가 위), "
                "PM 은 새 Price Target(현재가 아래)을 원 단위로 제시한다(목표 도달 후 보유는 손절가가 공매도 진입가 "
                "위로 올라가지 않는다). Trader Action 은 Buy(환매)/Hold 중심.").format(
            e=p.get("entry"), s=p.get("stop"), t=p.get("target") or "없음", h=p.get("holdDay"),
            n=config.HOLD_DAYS, mx=config.MAX_HOLD_DAYS, r=req.reason or "-")
    return ("[전략 맥락] 스윙 모의투자 **공매도(Short) 신규 진입** 검토 — 이 종목은 약세 신호로 선정됐다. "
            "여기서 Trader Action **Sell = 공매도 신규 진입**(보유 주식 매도가 아님), PM Rating Sell/Underweight "
            "= 공매도 진행, 그 외 등급은 진입하지 않는다. 주문은 다음 영업일 1일 유효 지정가 매도(고가가 Entry Price "
            "에 닿으면 그 가격 체결). 업틱룰 근사로 Entry Price 는 전일 종가({lc}) 이상. Stop Loss 는 Entry Price "
            "**위**(필수, 없으면 주문 안 됨), Price Target 은 Entry Price **아래**로 {n}영업일 안에 현실적인 수준. "
            "보유 {n}영업일 후 재판별, 대차수수료 연 {br:.1f}%. 섹터 신호: {sig} {sec}, 스크리닝 근거: {r}.").format(
        lc=f"{req.last_close:,.0f}원" if req.last_close else "미상", n=config.HOLD_DAYS,
        br=config.BORROW_RATE * 100, sig=req.signal or "-", sec=req.sector or "-", r=req.reason or "-")


def _default_llm(system, user, max_tokens, schema):
    import llm                                            # 루트 llm.py (Gemini 폴백 체인)
    return llm.generate_json(system, user, max_tokens=max_tokens, schema=schema, return_model=True)


def _default_collect(stock, date, out, social_on):
    ensure_kit_path()
    import collect                                        # kit/scripts/collect.py (무수정)
    # 스킬 collect(out: Path) 는 하위 모듈(indicators.run 등)에서 `out / "ohlcv.csv"` 로 경로를 잇고, 폴더는
    # CLI 쪽(run_dir)이 미리 만든다 → 직접 호출할 땐 Path 로 넘기고 폴더를 먼저 만든다. 둘 다 빠지면 가격 레그가
    # TypeError / "non-existent directory" 로 실패(2026-09-29 첫 실운용에서 발견)
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    return collect.collect(stock, date, out, social_on=social_on, full_dart=False)


def _default_resolve(code):
    ensure_kit_path()
    import resolve
    return resolve.resolve(code)


class TradingAgent:
    def __init__(self, store=None, *, llm_fn=None, collect_fn=None, resolve_fn=None, peer_fn=None,
                 rounds=None, social=None, runs_dir=None):
        self.store = store
        self.llm_fn = llm_fn or _default_llm
        self.collect_fn = collect_fn or _default_collect
        self.resolve_fn = resolve_fn or _default_resolve
        self.peer_fn = peer_fn or (lambda stock: peers_mod.resolve_peers(stock, self.store, self.llm_fn))
        self.rounds = rounds or ROUNDS
        self.social = SOCIAL if social is None else social
        self.runs_dir = runs_dir or RUNS_DIR
        self.name = f"trading_agent@{SOURCE_SHA}"
        self.calls = []                                   # (역할 파일, 모델) — 테스트·로그용

    # ── LLM 역할 호출 ──
    def _call(self, role_file, user, max_tokens=4096):
        try:
            data, model = self.llm_fn(_system(role_file), user, max_tokens, MD_SCHEMA)
        except Exception as ex:
            raise RoleError(f"{role_file}: {type(ex).__name__}: {ex}") from ex
        self.calls.append((role_file, model))
        md = data.get("markdown") if isinstance(data, dict) else data
        if not isinstance(md, str) or not md.strip():
            raise RoleError(f"{role_file}: 빈 응답")
        return md.strip()

    def _stock_line(self, stock, req):
        return f"## 종목\n{stock['name']} ({stock['code']}), 야후 티커 {stock.get('ticker')}, 기준일 {req.date}\n"

    def _analyst_user(self, stock, req, out, files):
        parts = [self._stock_line(stock, req)]
        for fn in files:
            p = os.path.join(out, fn)
            if not os.path.exists(p):
                parts.append(f"## 입력 파일: {fn}\n(파일 없음 — 수집 실패로 취급)\n")
                continue
            txt = _read(p)
            if fn == "ohlcv.csv":
                ls = txt.splitlines()
                txt = "\n".join(ls[:1] + ls[-OHLCV_TAIL:])
                fn = f"ohlcv.csv (최근 {OHLCV_TAIL}거래일)"
            parts.append(f"## 입력 파일: {fn}\n```\n{_clip(txt, fn)}\n```\n")
        return "\n".join(parts)

    def _reports_block(self, R):
        return "\n\n".join(f"## 애널리스트 리포트 — {t}\n{R[k]}" for k, t in REPORT_TITLES)

    # ── 판단 ──
    def decide(self, req):
        R = {}
        try:
            stock = self.resolve_fn(req.code)
            if not stock:
                raise RoleError(f"종목 해석 실패: {req.code}")
            # 0a peer
            try:
                pinfo = self.peer_fn(stock)
            except Exception as ex:
                pinfo = {"source": "error", "peers": [], "note": f"{type(ex).__name__}: {ex}"}
                peers_mod.write_memory(stock["code"], None)
            R["peers"] = json.dumps(pinfo, ensure_ascii=False, indent=1)
            # 0b 수집
            out = os.path.join(self.runs_dir, req.code, req.date, req.purpose)
            os.makedirs(out, exist_ok=True)
            manifest = self.collect_fn(stock, req.date, out, self.social) or {}
            legs = {k: (v or {}).get("status") for k, v in (manifest.get("legs") or {}).items()}
            R["collect"] = "\n".join(f"- {k}: {v}" for k, v in sorted(legs.items())) or "(manifest 없음)"
            price_p = os.path.join(out, "01_price.json")
            price = json.loads(_read(price_p)) if os.path.exists(price_p) else {}
            if not price or price.get("status") == "unavailable" or legs.get("price") == "unavailable":
                raise RoleError("가격 수집 실패(01_price unavailable) — 가격 없이 분석하지 않음")

            # 1~5 애널리스트
            for key, role, files in ANALYSTS:
                R[key] = self._call(role, self._analyst_user(stock, req, out, files))
            reports = self._reports_block(R)

            # 6 Bull ↔ Bear
            history, last_bear = [], ""
            for _ in range(self.rounds):
                bull = self._call("06_bull_researcher.md",
                                  f"{self._stock_line(stock, req)}\n{reports}\n\n## 토론 이력\n"
                                  f"{chr(10).join(history) or '(없음)'}\n\n## 약세 연구원 직전 발언\n"
                                  f"{last_bear or NO_BEAR}", 2048)
                history.append(bull)
                bear = self._call("07_bear_researcher.md",
                                  f"{self._stock_line(stock, req)}\n{reports}\n\n## 토론 이력\n"
                                  f"{chr(10).join(history)}\n\n## 강세 연구원 직전 발언\n{bull}", 2048)
                history.append(bear)
                last_bear = bear
            R["debate"] = "\n\n".join(history)

            ctx = strategy_context(req)
            # 7 Research Manager
            R["research_manager"] = self._call(
                "08_research_manager.md",
                f"{ctx}\n\n{self._stock_line(stock, req)}\n## 토론 이력 전문\n{R['debate']}", 2048)
            # 8 Trader
            R["trader"] = self._call(
                "09_trader.md",
                f"{ctx}\n\n{self._stock_line(stock, req)}\n## 투자 계획(ResearchPlan)\n"
                f"{R['research_manager']}\n\n## Market Analyst 리포트(01)\n{R['market']}", 2048)
            # 9 리스크 3자 (공격 → 보수 → 중립)
            risk, last = [], {}
            order = (("aggressive", "10_aggressive_risk.md"), ("conservative", "11_conservative_risk.md"),
                     ("neutral", "12_neutral_risk.md"))
            names = {"aggressive": "공격", "conservative": "보수", "neutral": "중립"}
            for _ in range(self.rounds):
                for who, role in order:
                    others = "\n\n".join(f"### {names[o]} 분석가 직전 발언\n{last.get(o) or NO_OPP}"
                                         for o, _r in order if o != who)
                    msg = self._call(role, f"{self._stock_line(stock, req)}\n## 트레이더 제안\n{R['trader']}\n\n"
                                           f"{reports}\n\n## 리스크 토론 이력\n{chr(10).join(risk) or '(없음)'}\n\n"
                                           f"{others}", 2048)
                    risk.append(msg)
                    last[who] = msg
            R["risk_debate"] = "\n\n".join(risk)
            # 10 PM
            R["pm"] = self._call(
                "13_portfolio_manager.md",
                f"{ctx}\n\n{self._stock_line(stock, req)}\n## 투자 계획(ResearchPlan)\n{R['research_manager']}\n\n"
                f"## 트레이더 제안\n{R['trader']}\n\n## 리스크 토론 이력 전문\n{R['risk_debate']}\n\n"
                f"## 과거 맥락\n{PAST_NONE}", 3072)
        except Exception as ex:                           # 수집 예외 등도 부분 리포트와 함께 error 로
            msg = str(ex) if isinstance(ex, RoleError) else f"{type(ex).__name__}: {ex}"
            return A.Decision(code=req.code, date=req.date, purpose=req.purpose, agent=self.name,
                              error=msg, reports=R, side=req.side)
        d = A.decision_from_markdown(req, R["trader"], R["pm"], reports=R, agent=self.name)
        return d
