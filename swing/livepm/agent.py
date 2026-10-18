"""판단(L4) — 스냅샷 → 구조화된 결정(PLAN_PM §6 스키마·§10 프롬프트). 실패·스키마 위반은 전부 hold.

LLMAgent  = llm.generate_json(chain=LLM_CHAIN) — 실제 판단(M3 서비스에서 사용)
MockLivePM = 규칙 기반 가짜 판단 — 오프라인 테스트·재생용(네트워크·API 키 없음)
"""
from . import config as C

SCHEMA = {
    "type": "object",
    "required": ["assessment", "decisions"],
    "properties": {
        "assessment": {"type": "string"},
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["code", "action", "reason", "confidence", "thesisValid"],
                "properties": {
                    "code": {"type": "string"},
                    "action": {"type": "string", "enum": list(C.ACTIONS)},
                    "trimPct": {"type": "number"},
                    "newStop": {"type": "number"},
                    "reason": {"type": "string"},
                    "thesisValid": {"type": "boolean"},
                    "confidence": {"type": "number"},
                    "invalidation": {"type": "string"},
                },
            },
        },
    },
}

SYSTEM = """당신은 한국 주식 스윙 모의 포트폴리오의 장중 포트폴리오 매니저(Live PM)다.
보유 종목은 이미 별도 리서치(trading_agent)를 거쳐 진입했다. 당신의 역할은 장중 정보를 보고
'진입 논거(thesis)가 아직 유효한가'와 '포트폴리오 위험이 허용 범위인가'를 판단하는 것이다.
당신의 결정은 지시로만 전달되고, 실제 체결은 Live Trader 가 VWAP 분할 규칙으로 한다. 손절가 도달 청산은 Live Trader 가 이미 자동으로 한다.

원칙:
1. 기본값은 hold 다. 근거가 스냅샷 수치로 설명될 때만 행동한다. 단기 노이즈(5분 변동)만으로 exit 하지 않는다.
2. 허용 액션은 hold, tighten_stop, trim, exit, flag_review 뿐이다. 매수·비중 확대는 할 수 없다.
3. thesis 의 invalidation 조건이 충족됐으면 thesisValid=false 로 표시하고 exit 또는 trim 을 검토한다.
4. 손절가는 유리한 방향으로만 옮길 수 있다(Long 은 올리기만, Short 는 내리기만, 현재가를 넘지 않게).
5. 섹터 전체가 밀리는 날에는 개별 종목보다 섹터 집중도를 먼저 본다.
6. 직전 결정(lastLivePm, today.decisions)과 반대로 가려면 무엇이 바뀌었는지 reason 에 적는다.
7. 확신이 낮으면 exit 대신 flag_review 를 쓴다.
8. liveTrader.exiting 이 있는 포지션은 이미 청산 중이므로 결정하지 않는다.
9. 트리거된 종목(triggered=true)은 반드시 decisions 에 포함하고, 나머지는 바꿀 필요가 있을 때만 포함한다.
출력은 주어진 JSON 스키마만 따른다. reason 은 한국어 1~2문장, 스냅샷 수치를 인용한다."""


def _valid(d):
    if not isinstance(d, dict) or not isinstance(d.get("decisions"), list):
        return False
    for x in d["decisions"]:
        if not isinstance(x, dict) or x.get("action") not in C.ACTIONS or not x.get("code"):
            return False
    return True


def estimate_cost(text_in, text_out=""):
    tokens = (len(text_in) + len(text_out)) / 2.5          # 한국어·JSON 혼합 대략치
    return tokens / 1000 * C.COST_PER_1K_TOKENS_KRW


class LLMAgent:
    name = "llm"

    def __init__(self, generate=None):
        if generate is None:
            import llm                                       # 루트 llm.py
            generate = llm.generate_json
        self._gen = generate

    def decide(self, snap_text):
        """반환 (결정 dict, 메타{model, error, costKrw})."""
        try:
            data, model = self._gen(SYSTEM, snap_text, schema=SCHEMA, max_tokens=C.LLM_MAX_TOKENS,
                                    return_model=True, chain=C.LLM_CHAIN)
        except Exception as ex:
            return {"assessment": "", "decisions": []}, {"model": None, "error": f"{type(ex).__name__}: {str(ex)[:160]}",
                                                         "costKrw": estimate_cost(snap_text)}
        if not _valid(data):
            return {"assessment": "", "decisions": []}, {"model": model, "error": "스키마 위반 → 전부 hold",
                                                         "costKrw": estimate_cost(snap_text)}
        return data, {"model": model, "error": None, "costKrw": estimate_cost(snap_text, str(data))}


class MockLivePM:
    """트리거 → 고정 반응: T1 → trim 50%(conf 0.7), T2 → tighten_stop 본전(진입가), T3 → exit(conf 0.8), T5·T6 → flag_review, 나머지 hold."""
    name = "mock"

    def decide(self, snap_text):
        import json
        snap = json.loads(snap_text)
        by_code = {p["code"]: p for p in snap["positions"]}
        out = {}
        for t in snap["trigger"]:
            p = by_code.get(t["code"])
            if not p:
                continue
            if t["id"] == "T3":
                d = {"action": "exit", "confidence": 0.8}
            elif t["id"] == "T1":
                d = {"action": "trim", "trimPct": 0.5, "confidence": 0.7}
            elif t["id"] == "T2":
                d = {"action": "tighten_stop", "newStop": p["entry"], "confidence": 0.7}
            elif t["id"] in ("T4", "T5"):
                d = {"action": "flag_review", "confidence": 0.6}
            else:
                d = {"action": "hold", "confidence": 0.5}
            prev = out.get(t["code"])
            rank = ["hold", "flag_review", "tighten_stop", "trim", "exit"]
            if prev is None or rank.index(d["action"]) > rank.index(prev["action"]):
                out[t["code"]] = dict(d, code=t["code"], reason=f"mock: {t['id']} {t['detail']}", thesisValid=d["action"] != "exit")
        return {"assessment": "mock", "decisions": list(out.values())}, {"model": "mock", "error": None, "costKrw": 0.0}
