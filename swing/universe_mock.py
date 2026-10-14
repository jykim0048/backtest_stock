"""유니버스 오프라인 mock(데모·테스트) — DB증권·KIS 허브 응답을 결정적 난수로 흉내 낸다(네트워크 없음).
업종·종목·시총은 swing/tools/demo_sectors.json(2026-10-12 실제 업종·섹터 맵 상위), 수치는 가상.
응답 필드명·단위는 universe.DbQuote 가 기대하는 DB증권 원본 형식(Avls 100만원, AcmlTrPbmn 원, 투자자 금액 천원)."""
import datetime
import json
import os
import random

from . import tradedays

_HERE = os.path.dirname(os.path.abspath(__file__))


def load_sectors():
    with open(os.path.join(_HERE, "tools", "demo_sectors.json"), encoding="utf-8") as f:
        return json.load(f)["sectors"]


def mock_sector_map(sectors=None):
    """load_sector_map 형식 — demo_sectors 그대로."""
    from . import signals
    return {signals.norm_sec(s["name"]): {"name": s["name"], "codes": {x["code"] for x in s["stocks"]}} for s in (sectors or load_sectors())}


class MockDbQuote:
    """industry_codes → demo 업종(코드 1005…), industry_components → 시총·당일 거래대금, daily_investor → 25영업일."""

    def __init__(self, sectors=None, seed=7, futures=None):
        self.sectors = sectors or load_sectors()
        self.seed, self.calls = seed, 0
        self._fut = futures

    def _rng(self, *keys):
        return random.Random(f"{self.seed}:{':'.join(map(str, keys))}")

    def industry_codes(self, mkt="K"):
        self.calls += 1
        return [{"code": str(1005 + i), "name": s["name"]} for i, s in enumerate(self.sectors)]

    def industry_components(self, code, sort="42"):
        self.calls += 1
        s = self.sectors[int(code) - 1005]
        rows = []
        for x in s["stocks"]:
            g = self._rng(x["code"], "comp")
            tv = max(5e8, x["cap"] * 1e8 * (0.004 + g.random() * 0.03))        # 원
            rows.append({"Iscd": x["code"], "KorIsnm": x["name"], "Avls": str(int(x["cap"] * 100)),
                         "AvlsRlim": "0", "Prpr": str(int(10000 + g.random() * 90000)),
                         "PrdyCtrt": f"{(g.random() - 0.5) * 8:.2f}", "AcmlVol": str(int(tv / 30000)),
                         "AcmlTrPbmn": str(int(tv))})
        rows.sort(key=lambda r: -int(r["AcmlTrPbmn"]))
        return rows

    def daily_investor(self, code, d1, d2):
        self.calls += 1
        g = self._rng(code, "inv")
        cap = next((x["cap"] for s in self.sectors for x in s["stocks"] if x["code"] == code), 1000)
        base_tv = cap * 1e8 * (0.004 + g.random() * 0.02)
        accel = 1 + (g.random() - 0.4) * 1.2                                  # 최근 주 가속/감속
        px = 10000 + g.random() * 90000
        drift = (g.random() - 0.5) * 0.004
        rows = []
        days = tradedays.trading_days(d1, d2)
        for i, d in enumerate(days):
            recent = i >= len(days) - 5
            tv = base_tv * (accel if recent else 1) * (0.7 + g.random() * 0.6)
            px *= 1 + drift + (g.random() - 0.5) * 0.03
            bias = (g.random() - 0.5) * 0.3
            fb, fs = tv * (0.25 + bias) / 1000, tv * (0.25 - bias) / 1000       # 천원
            ob, os_ = tv * (0.2 + bias * 0.6) / 1000, tv * (0.2 - bias * 0.6) / 1000
            rows.append({"Date": d.replace("-", ""), "Prpr": f"{px:.0f}", "PrdyCtrt": "0", "AcmlVol": f"{tv / px:.0f}",
                         "AcmlTrPbmn": "", "FrgnRegShnuTrPbmn": f"{fb:.0f}", "FrgnRegSelnTrPbmn": f"{fs:.0f}",
                         "OrgnShnuTrPbmn": f"{ob:.0f}", "OrgnSelnTrPbmn": f"{os_:.0f}",
                         "PrsnShnuTrPbmn": f"{tv * 0.3 / 1000:.0f}", "PrsnSelnTrPbmn": f"{tv * 0.3 / 1000:.0f}"})
        return rows[::-1]                                                     # 최신순(순서 무관 처리 확인용)

    def stock_future_underlyings(self):
        self.calls += 1
        if self._fut is not None:
            return set(self._fut)
        return {x["code"] for s in self.sectors for x in s["stocks"][:4]}      # 업종별 시총 상위 4 = 선물 상장 가정

    def chart_day(self, code, d1, d2):
        """CHARTDAY 흉내 — 결정적 랜덤워크 OHLC(일별 투자자 Prpr 과는 독립)."""
        self.calls += 1
        g = self._rng(code, "chart")
        px, rows = 10000 + g.random() * 90000, []
        for d in tradedays.trading_days(d1, d2):
            o = px * (1 + (g.random() - 0.5) * 0.02)
            c = o * (1 + (g.random() - 0.5) * 0.05)
            h, l = max(o, c) * (1 + g.random() * 0.015), min(o, c) * (1 - g.random() * 0.015)
            rows.append({"Date": d.replace("-", ""), "Oprc": f"{o:.0f}", "Hprc": f"{h:.0f}", "Lprc": f"{l:.0f}", "Prpr": f"{c:.0f}",
                         "CntgVol": "1000"})
            px = c
        return rows


def mock_chart_prices(dbq):
    """sim_universe.prices_fetch 형식: {code: {date: {o,h,l,c}}}."""
    def fetch(codes, d1, d2):
        out = {}
        for c in codes:
            px = {}
            for r in dbq.chart_day(c, d1, d2):
                d = r["Date"]
                px[f"{d[:4]}-{d[4:6]}-{d[6:]}"] = {"o": float(r["Oprc"]), "h": float(r["Hprc"]), "l": float(r["Lprc"]), "c": float(r["Prpr"])}
            out[c] = px
        return out
    return fetch


class MockHubFlow:
    def __init__(self, seed=11, d0="2026-10-12", n=30):
        self.seed, self.calls, self.d0, self.n = seed, 0, d0, n

    def one(self, code):
        self.calls += 1
        g = random.Random(f"{self.seed}:{code}")
        base, step = 1 + g.random() * 6, (g.random() - 0.5) * 0.4
        d0 = datetime.date.fromisoformat(self.d0)
        shorts, loans, amt = [], [], 100 + g.random() * 2000
        for i in range(self.n):
            d = (d0 - datetime.timedelta(days=i)).strftime("%Y%m%d")      # 허브 실측 형식
            shorts.append({"date": d, "qty": int(g.random() * 1e6), "volRlim": round(max(0.1, base + step * (10 - i) / 10 + (g.random() - 0.5)), 2),
                           "pbmn": round(g.random() * 100, 1), "pbmnRlim": 0})
            loans.append({"date": d, "rmndAmt": round(amt, 1), "rmndChg": 0})
            amt *= 1 + (g.random() - 0.5) * 0.04
        return {"shorts": shorts, "loans": loans}

    def many(self, codes):
        return {c: self.one(c) for c in codes}, []
