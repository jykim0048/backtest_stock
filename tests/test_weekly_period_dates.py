"""generate_weekly_briefing._period_dates 회귀 — 주간·월간 기간에서 KRX 휴장일 제외 (오프라인).

배경(2026-09-24): 추석 연휴 휴장일에 파이프라인이 돌아 산출물이 생기자 '휴장일은 데이터
부재로 자연 스킵' 전제가 깨져 주간(9/21~9/24)·월간에 9/24 가 포함됐다(지수가 9/23 과 동일).
실행: python tests/test_weekly_period_dates.py
"""
import os
import sys
import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import generate_weekly_briefing as gw

D = datetime.date
HOL = {"2026-09-24", "2026-09-25", "2026-10-05"}


def main():
    # 주간: 9/24(목) 기준 → 월~수만 (9/24 휴장 제외)
    assert gw._period_dates(D(2026, 9, 24), "week", holidays=HOL) == [D(2026, 9, 21), D(2026, 9, 22), D(2026, 9, 23)]
    # 주간: 9/26(토) 기준 → 그 주 금요일까지인데 목·금 휴장 → 월~수
    assert gw._period_dates(D(2026, 9, 26), "week", holidays=HOL) == [D(2026, 9, 21), D(2026, 9, 22), D(2026, 9, 23)]
    # 주간: 휴장 없는 주는 종전과 동일
    assert gw._period_dates(D(2026, 9, 17), "week", holidays=HOL) == [D(2026, 9, 14), D(2026, 9, 15), D(2026, 9, 16), D(2026, 9, 17)]
    # 월간: 9/1~9/24 평일에서 9/24 제외 → 마지막이 9/23, 총 17일
    m = gw._period_dates(D(2026, 9, 24), "month", holidays=HOL)
    assert m[0] == D(2026, 9, 1) and m[-1] == D(2026, 9, 23) and D(2026, 9, 24) not in m and len(m) == 17, (m[-1], len(m))
    # 월간: 10/5 대체휴일 제외
    m2 = gw._period_dates(D(2026, 10, 7), "month", holidays=HOL)
    assert D(2026, 10, 5) not in m2 and D(2026, 10, 6) in m2
    # holidays 미지정 → 레포 파일(data/krx_holidays.json) 사용: 9/24 제외돼야 한다
    assert D(2026, 9, 24) not in gw._period_dates(D(2026, 9, 24), "week")
    # 빈 집합(파일 없음, fail-open) → 평일 전부
    assert gw._period_dates(D(2026, 9, 24), "week", holidays=set())[-1] == D(2026, 9, 24)
    print("ALL PASS (weekly period dates exclude KRX holidays)")


if __name__ == "__main__":
    main()
