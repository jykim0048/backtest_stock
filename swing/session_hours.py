"""장 시간이 평소(09:00~15:30)와 다른 날 — 개장·마감 지연(분).

2026-11-19(수능) 장이 10:00~16:30 이었는데 Live Trader 는 15:19 진입 마감·15:35 감시 종료, Live PM 은 15:30 종료로 고정이라
마지막 1시간(신규 진입·손절 감시·남은 청산)이 비었다(2026-11-19 사용자 결정으로 추가).

· SPECIAL 에 날짜를 넣으면 Live Trader(swing/vwap)·Live PM(swing/livepm)의 시각 상수가 그날만 밀린다.
  12:00 전 시각(개장·진입 시작·판단 시작·오전 정기 점검)은 개장 지연만큼, 12:00 이후 시각(진입 마감·감시 종료·완주 판정·
  판단 마감·오후 정기 점검)은 마감 지연만큼.
· 매년 수능일(보통 11월 셋째 목요일 — 교육부 발표 확인)·새해 첫 거래일(개장 10:00, 마감 그대로)을 KRX 공지대로 추가한다.
  급할 땐 환경변수 SWING_SESSION_SHIFTS="YYYY-MM-DD:개장지연:마감지연,…"(분)로 코드 수정 없이 더할 수 있다(파일 값보다 우선).
"""
import datetime
import functools
import os

SPECIAL = {
    "2026-11-19": (60, 60),    # 2027학년도 수능 — 10:00~16:30
    "2027-01-04": (60, 0),     # 2027 새해 첫 거래일 — 10:00 개장, 15:30 마감
}


def _env():
    out = {}
    for part in (os.environ.get("SWING_SESSION_SHIFTS") or "").split(","):
        bits = part.strip().split(":")
        if len(bits) != 3:
            continue
        try:
            datetime.date.fromisoformat(bits[0])
            out[bits[0]] = (int(bits[1]), int(bits[2]))
        except ValueError:
            continue
    return out


def delays(date):
    """(개장 지연 분, 마감 지연 분) — 평소엔 (0, 0)."""
    d = str(date)[:10]
    env = _env()
    return env.get(d) or SPECIAL.get(d) or (0, 0)


def shift(hm, date):
    """평소 시각 'HH:MM' → 그날 시각. 12:00 전은 개장 지연, 이후는 마감 지연만큼 늦춘다."""
    o, c = delays(date)
    m = o if hm < "12:00" else c
    if not m:
        return hm
    h, mi = map(int, hm.split(":"))
    t = h * 60 + mi + m
    return f"{t // 60:02d}:{t % 60:02d}"


@functools.lru_cache(maxsize=64)
def _times(items, date, env):
    out = {}
    for k, v in items:
        out[k] = tuple(shift(x, date) for x in v) if isinstance(v, tuple) else shift(v, date)
    return out


def times(base, date):
    """base = {이름: 'HH:MM' 또는 ('HH:MM', …)} → 그날 값 dict."""
    return dict(_times(tuple(sorted(base.items())), str(date)[:10], os.environ.get("SWING_SESSION_SHIFTS") or ""))
