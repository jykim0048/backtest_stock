"""Live PM 노브 — PLAN_PM.md 기본값(2026-10-18). swing/config.py 와 같은 방식으로 env 로만 덮어쓴다."""
import os

from ..config import _f, _i

MODE = os.environ.get("LIVEPM_MODE", "shadow")                 # shadow(기록만) | paper(원장 반영, M3) — §8
MODES = ("shadow", "paper")

# 시세 공급(§2) — vi_limit 서비스 /prices(현재가·시가·전일종가, 고저 없음)·/status(vi·sectors)
POLL_PRICES_S = _f("LIVEPM_POLL_PRICES_S", 15)
POLL_STATUS_S = _f("LIVEPM_POLL_STATUS_S", 60)
FEED_STALE_S = _f("LIVEPM_FEED_STALE_S", 300)                  # X4: 이만큼 틱 없으면 halted
TICK_SAMPLE_S = _f("LIVEPM_TICK_SAMPLE_S", 60)                 # livepm/<date>/ticks 저장 간격(재생용)

# 장 시간(KST, HH:MM) — 루프·G7
SESSION_START = os.environ.get("LIVEPM_SESSION_START", "08:55")
SESSION_END = os.environ.get("LIVEPM_SESSION_END", "15:30")
DECIDE_START = os.environ.get("LIVEPM_DECIDE_START", "09:00")
DECIDE_END = os.environ.get("LIVEPM_DECIDE_END", "15:20")

# 트리거(§3)
STOP_NEAR = _f("LIVEPM_STOP_NEAR", 0.70)                       # T1: 진입→손절 거리 소진 비율
JUMP_5M = _f("LIVEPM_JUMP_5M", 0.03)                           # T3: 5분 불리한 변동률
SECTOR_TOP_N = _i("LIVEPM_SECTOR_TOP_N", 5)                    # T5: 업종 상·하위 N
DD_DAY = _f("LIVEPM_DD_DAY", 0.015)                            # T6: 포트폴리오 일중 손실
SCHEDULE = tuple(t.strip() for t in os.environ.get("LIVEPM_SCHEDULE", "09:10,11:00,13:30,14:50").split(",") if t.strip())  # T7
DEBOUNCE_MIN = _f("LIVEPM_DEBOUNCE_MIN", 15)
BATCH_WAIT_S = _f("LIVEPM_BATCH_WAIT_S", 3)
MAX_CALLS = _i("LIVEPM_MAX_CALLS", 40)
MAX_COST_KRW = _f("LIVEPM_MAX_COST_KRW", 3000)

# 거래 상태·반사 규칙(§4)
REDUCE_DD = _f("LIVEPM_REDUCE_DD", 0.03)                       # X2: 일중 손실 → reducing
STATES = ("active", "reducing", "halted")

# RiskGate(§7)
MIN_CONF_EXIT = _f("LIVEPM_MIN_CONF_EXIT", 0.6)                # G3
FLIP_MIN = _f("LIVEPM_FLIP_MIN", 30)                           # G4
MAX_REDUCE_PER_CALL = _f("LIVEPM_MAX_REDUCE_PER_CALL", 0.30)   # G6: 평가액 대비

# 실행(§8)
SLIPPAGE = _f("LIVEPM_SLIPPAGE", 0.001)                        # paper 장중 청산 불리 슬리피지

# 판단 모델(§9) — llm.generate_json(chain=…). 기본은 Anthropic 우선, Gemini 폴백
LLM_CHAIN = os.environ.get("LIVEPM_LLM_CHAIN", "anthropic:claude-sonnet-5-5,gemini:gemini-3.5-flash")
LLM_MAX_TOKENS = _i("LIVEPM_LLM_MAX_TOKENS", 2048)
