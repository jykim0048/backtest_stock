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
LLM_CHAIN = "anthropic:claude-sonnet-5-5,gemini:gemini-3.5-flash"
LLM_MAX_TOKENS = 2048
COST_PER_1K_TOKENS_KRW = 15.0   # 호출 비용 추정(입출력 합 토큰 × 단가) — M3 에서 실제 사용량으로 교체
SNAPSHOT_MAX_CHARS = 16000      # 스냅샷 JSON 상한(약 5~6k 토큰)

# RiskGate(§7)
MIN_CONF_EXIT = 0.6             # G3
FLIP_MIN = 30                   # G4 같은 종목 지시 재발행 금지(분)
MAX_REDUCE_PER_CALL = 0.30      # G6 한 호출 exit+trim 합계 / 평가액
