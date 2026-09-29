"""섹터 시그널 스윙 모의투자 (swing_paper 브랜치 전용 — main 에 병합하지 않는다).

흐름(평일 16:30 KST, swing-cron): 일일 원장 갱신 → 보유 종목 매도 검토(trading_agent PM)
→ 신규 후보(주간 브리핑 동반강세·수급유입) trading_agent 분석 → 다음 영업일 주문.
"""
