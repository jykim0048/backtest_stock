"""Live PM 설정 — PLAN_PM.md 규칙값(2026-10-19 M1).

Live Trader 와 같은 원칙: 환경변수로 덮어쓰지 않는다. 값을 바꿀 땐 이 파일과 PLAN_PM.md 를 함께 고쳐 git 이력에 남긴다.
Railway swing-livepm 변수는 연결 정보(DATABASE_URL·REDIS_URL·FLOW_API_BASE·LLM API 키)뿐.
"""

# 시간(KST HH:MM)
SESSION_START = "08:55"
SESSION_END = "15:30"
DECIDE_START = "09:00"          # G7 판단·지시 허용 시간
DECIDE_END = "15:20"

# 입력(§2) — 시세는 Redis 버스(Live Trader 발행), VI·업종은 /status 직접
STATUS_POLL_S = 60
FEED_STALE_S = 300              # X4: 버스 새 항목이 이만큼 없으면 halted
RING_MIN = 40                   # 종목별 가격 링버퍼 보관(분) — ret5m·ret30m

# 트리거(§3)
STOP_NEAR = 0.70                # T1 진입→손절 거리 소진 비율
JUMP_5M = 0.03                  # T3 불리한 방향 5분 변동률
SECTOR_TOP_N = 5                # T5 업종 상·하위 N
DD_DAY = 0.015                  # T6 포트폴리오 일중 손실
SCHEDULE = ("09:10", "11:00", "13:30", "14:50")   # T7 정기 점검
DEBOUNCE_MIN = 15               # 같은 종목·같은 트리거 재호출 금지(분)
BATCH_WAIT_S = 3                # 겹친 트리거를 모으는 시간(초)
MAX_CALLS = 40                  # 하루 LLM 호출 상한
MAX_COST_KRW = 3000             # 하루 LLM 비용 상한(원)

# 거래 상태·반사 규칙(§4)
STATES = ("active", "reducing", "halted")
REDUCE_DD = 0.03                # X2 일중 손실 → reducing
REDUCE_ACTIONS = ("tighten_stop", "trim", "exit", "flag_review")   # reducing 에서 허용(1차 액션은 전부 위험 축소)

# 판단(§6)
ACTIONS = ("hold", "tighten_stop", "trim", "exit", "flag_review")
# 2026-11-09 비용 검토 결정(PLAN_PM M3-비용): Haiku 5.5 · effort low · 하루 40회·3,000원 유지
LLM_CHAIN = "anthropic:claude-haiku-5-5,gemini:gemini-3.5-flash"
LLM_EFFORT = "low"              # Anthropic output_config.effort — 사고 토큰도 출력 과금·max_tokens 포함
LLM_MAX_TOKENS = 8000           # 2048 은 사고 + 31종목 결정에 잘려(max_tokens → 폴백) 늘림
# 실제 사용량(usage) × 단가(USD / 100만 토큰) → 원. 캐시 쓰기 1.25배·읽기 0.1배(5분 TTL 기준).
PRICE_USD_PER_MTOK = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.0, 10.0),
                      "gemini-3.5-flash": (0.0, 0.0)}           # Gemini 무료 티어
USD_KRW = 1450.0                # 환율 가정(비용 상한 계산용)
COST_PER_1K_TOKENS_KRW = 15.0   # usage 를 못 받을 때만 쓰는 문자 수 기반 추정(보수적)
SNAPSHOT_MAX_CHARS = 16000      # 스냅샷 JSON 상한(약 6~8k 토큰)
THESIS_STOP_USED = 0.50         # 트리거 안 걸린 포지션도 손절 거리 이만큼 소진했으면 thesis 전문 유지

# RiskGate(§7)
MIN_CONF_EXIT = 0.6             # G3
FLIP_MIN = 30                   # G4 같은 종목 지시 재발행 금지(분)
MAX_REDUCE_PER_CALL = 0.30      # G6 한 호출 exit+trim 합계 / 평가액
