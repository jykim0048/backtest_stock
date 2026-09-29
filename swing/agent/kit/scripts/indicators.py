"""OHLCV + 기술적 지표 13종 + 검증 스냅샷 (TradingAgents `get_verified_market_snapshot` 상당).

- 일봉은 yfinance(`005930.KS`)에서 1년+ 내려받아 CSV 로 저장한다.
- 지표는 pandas 만으로 계산한다(stockstats 미의존). 이름은 TradingAgents 와 동일:
  close_10_ema close_50_sma close_200_sma macd macds macdh rsi boll boll_ub boll_lb atr vwma mfi
- 스냅샷은 LLM 을 거치지 않는 '진실 원천'이다. 마켓 애널리스트는 정확한 가격·지표 수치를
  이 파일에서만 인용해야 한다.

사용:  python indicators.py 005930.KS --date 2026-09-22 --out runs/005930/2026-09-22
출력:  01_price.json (스냅샷 + 최근 60일 지표 시계열), ohlcv.csv
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from _common import today_kst, warn, write_json

INDICATORS = ("close_10_ema", "close_50_sma", "close_200_sma", "macd", "macds", "macdh",
              "rsi", "boll", "boll_ub", "boll_lb", "atr", "vwma", "mfi")


# ----------------------------------------------------------------------------
# 지표 계산 (순수 pandas — 오프라인 테스트 가능)
# ----------------------------------------------------------------------------
def _wilder(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """df: Date 인덱스, 컬럼 Open High Low Close Volume. 지표 컬럼을 덧붙여 반환."""
    out = df.copy()
    c, h, l, v = out["Close"], out["High"], out["Low"], out["Volume"]

    out["close_10_ema"] = c.ewm(span=10, adjust=False).mean()
    out["close_50_sma"] = c.rolling(50).mean()
    out["close_200_sma"] = c.rolling(200).mean()

    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    out["macd"] = ema12 - ema26
    out["macds"] = out["macd"].ewm(span=9, adjust=False).mean()
    out["macdh"] = out["macd"] - out["macds"]

    delta = c.diff()
    gain = _wilder(delta.clip(lower=0), 14)
    loss = _wilder(-delta.clip(upper=0), 14)
    rs = gain / loss.replace(0, np.nan)
    out["rsi"] = 100 - 100 / (1 + rs)

    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    out["boll"] = mid
    out["boll_ub"] = mid + 2 * sd
    out["boll_lb"] = mid - 2 * sd

    prev_c = c.shift(1)
    tr = pd.concat([(h - l), (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["atr"] = _wilder(tr, 14)

    out["vwma"] = (c * v).rolling(14).sum() / v.rolling(14).sum().replace(0, np.nan)

    tp = (h + l + c) / 3
    mf = tp * v
    pos = mf.where(tp > tp.shift(1), 0.0)
    neg = mf.where(tp < tp.shift(1), 0.0)
    ratio = pos.rolling(14).sum() / neg.rolling(14).sum().replace(0, np.nan)
    out["mfi"] = 100 - 100 / (1 + ratio)
    return out


# ----------------------------------------------------------------------------
# 데이터 획득
# ----------------------------------------------------------------------------
def fetch_ohlcv(ticker: str, end_date: str, lookback_days: int = 420) -> pd.DataFrame:
    import yfinance as yf

    end = pd.Timestamp(end_date)
    start = end - pd.Timedelta(days=lookback_days)
    # yfinance end 는 배타 → 하루 더해 end_date 행 포함
    hist = yf.Ticker(ticker).history(start=start.strftime("%Y-%m-%d"),
                                     end=(end + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                                     auto_adjust=True)
    if hist is None or hist.empty:
        raise RuntimeError(f"yfinance 가 {ticker} 일봉을 반환하지 않았습니다.")
    hist = hist[["Open", "High", "Low", "Close", "Volume"]].copy()
    if hist.index.tz is not None:
        hist.index = hist.index.tz_localize(None)
    hist.index = hist.index.normalize()
    hist.index.name = "Date"
    hist = hist[hist.index <= end]                 # 룩어헤드 차단
    hist = hist.dropna(subset=["Close"])
    stale = (end - hist.index.max()).days
    if stale > 10:
        raise RuntimeError(f"{ticker} 최신 봉 {hist.index.max().date()} 이 요청일보다 {stale}일 오래됨(stale).")
    return hist


def _f(x, nd=2):
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), nd)


def build_snapshot(ticker: str, end_date: str, df: pd.DataFrame, recent: int = 30) -> dict:
    ind = compute_indicators(df)
    last = ind.iloc[-1]
    latest_date = ind.index[-1].strftime("%Y-%m-%d")
    close = float(last["Close"])
    ret = {}
    for label, n in (("1d", 1), ("5d", 5), ("20d", 20), ("60d", 60), ("120d", 120), ("250d", 250)):
        if len(ind) > n:
            ret[label] = _f((close / float(ind["Close"].iloc[-1 - n]) - 1) * 100)
    hi52 = float(df["High"].tail(250).max())
    lo52 = float(df["Low"].tail(250).min())
    vol20 = float(df["Volume"].tail(20).mean()) if len(df) >= 20 else None
    tail = ind.tail(recent)
    return {
        "ticker": ticker,
        "requested_date": end_date,
        "latest_row": latest_date,
        "rows": int(len(df)),
        "note": ("요청일 이후 행은 제외됨. 가격·지표 수치의 진실 원천. "
                 "다른 자료와 충돌하면 조정된 숫자를 만들지 말고 불일치를 표시할 것."),
        "latest_ohlcv": {k: _f(last[k], 0 if k == "Volume" else 2)
                         for k in ("Open", "High", "Low", "Close", "Volume")},
        "indicators": {k: _f(last[k]) for k in INDICATORS},
        "returns_pct": ret,
        "range_52w": {"high": _f(hi52), "low": _f(lo52),
                      "pct_from_high": _f((close / hi52 - 1) * 100),
                      "pct_from_low": _f((close / lo52 - 1) * 100)},
        "avg_volume_20d": _f(vol20, 0),
        "volume_ratio_vs_20d": _f(float(last["Volume"]) / vol20, 2) if vol20 else None,
        "recent": [
            {"date": d.strftime("%Y-%m-%d"), "close": _f(r["Close"]), "volume": _f(r["Volume"], 0),
             "rsi": _f(r["rsi"]), "macdh": _f(r["macdh"]), "atr": _f(r["atr"]),
             "sma50": _f(r["close_50_sma"]), "sma200": _f(r["close_200_sma"]),
             "boll_ub": _f(r["boll_ub"]), "boll_lb": _f(r["boll_lb"])}
            for d, r in tail.iterrows()
        ],
    }


def run(ticker: str, end_date: str, out_dir: Path) -> dict:
    df = fetch_ohlcv(ticker, end_date)
    df.to_csv(out_dir / "ohlcv.csv", encoding="utf-8")
    snap = build_snapshot(ticker, end_date, df)
    write_json(out_dir / "01_price.json", snap)
    return snap


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ticker", help="야후 티커, 예: 005930.KS")
    ap.add_argument("--date", default=today_kst())
    ap.add_argument("--out", required=True, help="출력 폴더")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        snap = run(a.ticker, a.date, out)
    except Exception as e:  # noqa: BLE001
        warn(f"indicators 실패: {e}")
        write_json(out / "01_price.json", {"ticker": a.ticker, "status": "unavailable", "error": str(e)})
        return 1
    print(json.dumps({"latest_row": snap["latest_row"], "close": snap["latest_ohlcv"]["Close"],
                      "rsi": snap["indicators"]["rsi"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
