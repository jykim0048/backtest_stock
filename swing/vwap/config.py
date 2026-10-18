"""장중 VWAP 실행기 노브 — PLAN_VWAP.md 기본값(2026-10-18). env 로만 덮어쓴다."""
import os

from ..config import _f, _i

POLL_S = _f("VWAP_POLL_S", 15)                         # /prices 폴링 주기
SAVE_S = _f("VWAP_SAVE_S", 60)                         # intraday/<date> 저장 주기(체결·손절 발생 시엔 즉시)
TICK_SAVE_S = _f("VWAP_TICK_SAVE_S", 300)              # 재생용 틱 저장 주기

SESSION_START = os.environ.get("VWAP_SESSION_START", "09:00")
ENTRY_START = os.environ.get("VWAP_ENTRY_START", "09:05")   # 동시호가 직후 노이즈 회피
ENTRY_END = os.environ.get("VWAP_ENTRY_END", "15:19")       # 종가 단일가(15:20~) 전까지
SESSION_END = os.environ.get("VWAP_SESSION_END", "15:35")   # 종가 체결(15:30) 반영 후 종료
COMPLETE_AT = os.environ.get("VWAP_COMPLETE_AT", "15:30")   # 마지막 틱이 이 시각 이후면 '세션 완주'

OPEN_PCT = _f("VWAP_OPEN_PCT", 0.10)                  # 진입·청산 첫 분할 비율 — 진입·예약 청산은 시가, 손절은 도달 가격(2026-10-18)
FORCE_MOVE = _f("VWAP_FORCE_MOVE", 0.05)              # 기준가(첫 분할 체결가) 대비 ±5% 이상 움직이면 VWAP 무시하고 남은 수량 전량
TRANCHES = _i("VWAP_TRANCHES", 60)                    # 첫 분할 뒤 남은 수량의 VWAP 분할 횟수(2026-10-18 3 → 60)
MIN_GAP_MIN = _f("VWAP_MIN_GAP_MIN", 5)               # VWAP 분할 사이 최소 간격(분) — 2026-10-18 20 → 5(60분할이 하루 안에 가능)
MIN_OBS = _i("VWAP_MIN_OBS", 2)                       # VWAP 판정 전 최소 관측 구간
SIGMA_FLOOR = _f("VWAP_SIGMA_FLOOR", 0.002)           # σ 하한(표시용)
