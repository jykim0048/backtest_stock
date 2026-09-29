"""일봉 시세 소스 — 체결·청산 '도달 여부' 판정과 평가용. 형식: {code: {open, high, low, close}}.

- DictPrices   : 고정 데이터(테스트·백필 재현)
- MockPrices   : 코드별 결정적 랜덤워크(로컬 프리뷰)
- YFinancePrices: Railway 용(네트워크). **미검증** — P0 에서 KIS 허브·네이버와 비교해 확정.
"""
import datetime
import hashlib
import json
import os
import random

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DictPrices:
    def __init__(self, data):
        self.data = data                      # {date: {code: bar}}

    def daily_bars(self, codes, date):
        day = self.data.get(date) or {}
        return {c: day[c] for c in codes if c in day}


class MockPrices:
    """기준가 1만~10만, 일 변동 ±3% 랜덤워크. 같은 (code, date) 는 항상 같은 값."""

    def _bar(self, code, date):
        seed = int(hashlib.md5(code.encode()).hexdigest()[:8], 16)
        base = 10_000 + seed % 90_000
        d0 = datetime.date(2026, 1, 1)
        n = (datetime.date.fromisoformat(date) - d0).days
        rng = random.Random(seed)
        px = base
        for _ in range(max(n, 0)):
            px *= 1 + rng.uniform(-0.03, 0.03)
        o = px * (1 + rng.uniform(-0.01, 0.01))
        c = px * (1 + rng.uniform(-0.02, 0.02))
        h = max(o, c) * (1 + rng.uniform(0, 0.02))
        lo = min(o, c) * (1 - rng.uniform(0, 0.02))
        return {"open": round(o), "high": round(h), "low": round(lo), "close": round(c)}

    def daily_bars(self, codes, date):
        return {c: self._bar(c, date) for c in codes}


class YFinancePrices:
    """yfinance .KS/.KQ 일봉. 시장은 public/assets/krx_companies.json(보통주) 기준,
    우선주는 보통주 코드의 시장 사용. 당일 봉은 장 마감 후 반영 지연 가능(미검증)."""

    def __init__(self):
        self._mkt = None

    def _market(self, code):
        if self._mkt is None:
            self._mkt = {}
            try:
                with open(os.path.join(_ROOT, "public", "assets", "krx_companies.json"),
                          encoding="utf-8") as f:
                    for e in json.load(f):
                        self._mkt[str(e.get("code")).zfill(6)] = e.get("market")
            except Exception:
                pass
        m = self._mkt.get(code) or self._mkt.get(code[:5] + "0")
        return "KQ" if str(m or "").upper().startswith("KOSDAQ") else "KS"

    def daily_bars(self, codes, date):
        import yfinance as yf
        d = datetime.date.fromisoformat(date)
        out = {}
        for c in codes:
            try:
                df = yf.download(f"{c}.{self._market(c)}", start=d.isoformat(),
                                 end=(d + datetime.timedelta(days=1)).isoformat(),
                                 progress=False, auto_adjust=False)
                if df is None or df.empty:
                    continue
                r = df.iloc[-1]
                if str(df.index[-1].date()) != date:
                    continue

                def v(k):
                    x = r[k]
                    return float(x.iloc[0] if hasattr(x, "iloc") else x)
                out[c] = {"open": v("Open"), "high": v("High"), "low": v("Low"),
                          "close": v("Close")}
            except Exception as ex:
                print(f"[swing] yfinance {c} {date}: {ex}")
        return out
