"""영업일 헬퍼 — 레포 루트 krx_calendar(data/krx_holidays.json) 재사용.
(모듈명을 calendar 로 두면 표준 라이브러리를 가리므로 tradedays.)"""
import datetime
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import krx_calendar  # noqa: E402

_HOL = None


def holidays():
    global _HOL
    if _HOL is None:
        _HOL = krx_calendar.load_holidays()
    return _HOL


def _d(s):
    return s if isinstance(s, datetime.date) else datetime.date.fromisoformat(str(s)[:10])


def is_trading_day(d):
    return krx_calendar.is_trading_day(_d(d), holidays())


def next_trading_day(d):
    x = _d(d) + datetime.timedelta(days=1)
    while not is_trading_day(x):
        x += datetime.timedelta(days=1)
    return x.isoformat()


def trading_days(start, end):
    """start~end(포함) 영업일 목록."""
    a, b, out = _d(start), _d(end), []
    while a <= b:
        if is_trading_day(a):
            out.append(a.isoformat())
        a += datetime.timedelta(days=1)
    return out
