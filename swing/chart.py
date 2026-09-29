"""기술적 분석 차트 데이터 — 판단 원문 팝업의 I-1 Market Analyst 에 그리는 시계열(2026-09-29).

스킬 kit/scripts/indicators.py `compute_indicators` 와 **같은 수식**(EMA10·SMA50/200·볼린저 20·2σ(모표준편차)·
RSI14 Wilder·MACD 12/26/9)을 순수 파이썬으로 옮겼다 — 애널리스트가 인용한 수치와 차트가 일치해야 하고,
데모(mock)는 표준 라이브러리만으로 돌아야 하므로. 일치는 tests/test_chart.py 가 pandas 결과와 대조한다.

출력(열 지향, 최근 n 거래일): {"dates", "o","h","l","c","v", "ema10","sma50","sma200", "bu","bm","bl",
"rsi", "macd","macds","macdh"} — 값이 없으면 None. 약 90행 × 16열 ≈ 10KB.
"""
import csv
import math

SERIES = ("ema10", "sma50", "sma200", "bu", "bm", "bl", "rsi", "macd", "macds", "macdh")


def _ema(xs, span=None, alpha=None, min_periods=0):
    """pandas ewm(adjust=False) — 첫 유효값에서 시작, None 은 건너뛰고 직전 값 유지(출력 None)."""
    a = alpha if alpha is not None else 2.0 / (span + 1)
    out, y, n = [], None, 0
    for x in xs:
        if x is None:
            out.append(None if y is None or n < min_periods else y)
            continue
        y = x if y is None else (1 - a) * y + a * x
        n += 1
        out.append(y if n >= max(min_periods, 1) else None)
    return out


def _sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def _std(xs, n, means):
    out = []
    for i in range(len(xs)):
        if i < n - 1:
            out.append(None)
            continue
        m = means[i]
        out.append(math.sqrt(sum((x - m) ** 2 for x in xs[i - n + 1:i + 1]) / n))
    return out


def indicators(close):
    """close 리스트 → {ema10, sma50, sma200, bm, bu, bl, rsi, macd, macds, macdh} (같은 길이, 없으면 None)."""
    ema10 = _ema(close, span=10)
    sma50, sma200, bm = _sma(close, 50), _sma(close, 200), _sma(close, 20)
    sd = _std(close, 20, bm)
    bu = [m + 2 * s if m is not None else None for m, s in zip(bm, sd)]
    bl = [m - 2 * s if m is not None else None for m, s in zip(bm, sd)]
    e12, e26 = _ema(close, span=12), _ema(close, span=26)
    macd = [a - b for a, b in zip(e12, e26)]
    macds = _ema(macd, span=9)
    macdh = [a - b for a, b in zip(macd, macds)]
    delta = [None] + [close[i] - close[i - 1] for i in range(1, len(close))]
    gain = _ema([None if d is None else max(d, 0.0) for d in delta], alpha=1 / 14, min_periods=14)
    loss = _ema([None if d is None else max(-d, 0.0) for d in delta], alpha=1 / 14, min_periods=14)
    rsi = [None if g is None or l is None or l == 0 else 100 - 100 / (1 + g / l) for g, l in zip(gain, loss)]
    return {"ema10": ema10, "sma50": sma50, "sma200": sma200, "bm": bm, "bu": bu, "bl": bl,
            "rsi": rsi, "macd": macd, "macds": macds, "macdh": macdh}


def _r(x, nd):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(x, nd)


def build(rows, n=90):
    """rows: [{date, open, high, low, close, volume}] 날짜 오름차순(1년치 권장 — SMA200 워밍업).
    최근 n 행을 열 지향 dict 로. 행이 없으면 None."""
    rows = [r for r in rows if r.get("close") is not None]
    if not rows:
        return None
    close = [float(r["close"]) for r in rows]
    ind = indicators(close)
    k = max(0, len(rows) - n)
    tail = rows[k:]
    out = {"dates": [str(r["date"])[:10] for r in tail],
           "o": [_r(float(r["open"]), 2) for r in tail], "h": [_r(float(r["high"]), 2) for r in tail],
           "l": [_r(float(r["low"]), 2) for r in tail], "c": [_r(float(r["close"]), 2) for r in tail],
           "v": [int(float(r.get("volume") or 0)) for r in tail]}
    for key in SERIES:
        out[key] = [_r(x, 2) for x in ind[key][k:]]
    return out


def from_csv(path, n=90):
    """스킬 ohlcv.csv(Date,Open,High,Low,Close,Volume) → build()."""
    with open(path, encoding="utf-8") as f:
        rows = [{"date": r.get("Date") or r.get("date"), "open": r["Open"], "high": r["High"], "low": r["Low"],
                 "close": r["Close"], "volume": r.get("Volume") or 0}
                for r in csv.DictReader(f) if r.get("Close") not in (None, "")]
    rows.sort(key=lambda r: r["date"])
    return build(rows, n)
