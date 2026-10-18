"""swing 장중 VWAP 실행기 — 진입 주문 분할 체결(진입가 상한 + VWAP 보다 유리한 가격)과 손절가 실시간 처리.
규칙은 swing/PLAN_VWAP.md. 실행기는 swing 원장을 고치지 않고 intraday/<date> 에 기록만 하며,
장 마감 후 engine.settle_day 가 그 기록으로 체결·손절을 확정한다.
"""
