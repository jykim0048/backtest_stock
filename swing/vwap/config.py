"""장중 VWAP 실행기 설정 — PLAN_VWAP.md 규칙값(2026-10-18).

환경변수로 덮어쓰지 않는다(사용자 결정 2026-10-18): 값을 바꿀 땐 이 파일을 고치고 PLAN_VWAP.md 에 함께 기록해
규칙 변경이 git 이력에 남게 한다. Railway swing-vwap 서비스 변수는 DATABASE_URL·FLOW_API_BASE 둘뿐.
"""

POLL_S = 15                 # /prices 폴링 주기(초)
SAVE_S = 60                 # intraday/<date> 저장 주기(초, 체결·손절 발생 시엔 즉시)
TICK_SAVE_S = 300           # 재생용 틱 저장 주기(초)

SESSION_START = "09:00"
ENTRY_START = "09:05"       # VWAP 분할 시작 — 동시호가 직후 노이즈 회피
ENTRY_END = "15:19"         # 종가 단일가(15:20~) 전까지
SESSION_END = "15:35"       # 종가 체결(15:30) 반영 후 종료
COMPLETE_AT = "15:30"       # 마지막 틱이 이 시각 이후면 '세션 완주'

OPEN_PCT = 0.10             # S1 첫 분할 비율 — 진입·예약 청산은 시가, 손절은 도달 가격
FORCE_MOVE = 0.05           # S4 기준가 대비 기다리면 손해인 방향으로 5% 이상 → 남은 수량 전량
TRANCHES = 60               # S3 첫 분할 뒤 남은 수량의 VWAP 분할 수(2026-10-18 3 → 60)
MIN_GAP_MIN = 5             # S3 VWAP 분할 사이 최소 간격(분, 2026-10-18 20 → 5)
MIN_OBS = 2                 # VWAP 판정 전 최소 관측 구간
SIGMA_FLOOR = 0.002         # σ 하한(가격 대비, 표시용)
