"""종목별 장중 상태 — VWAP·σ·1분봉·당일 고저 변화(PLAN_VWAP §2).

VWAP = 누적거래대금 ÷ 누적거래량(KIS 당일 누적, 정확). 거래대금이 없으면 Σ현재가×Δ거래량 근사(vwap_source="approx").
구간 가격 = Δ거래대금 ÷ Δ거래량(폴링 사이 체결 평균가, 그날 고저 범위로 자름) → σ = 구간 가격의 거래량 가중 표준편차.
1분봉은 폴링 샘플로 만들고, 다음 분의 첫 틱이 오면 직전 봉을 마감해 돌려준다.
"""
import math

from . import config as C


class VwapState:
    def __init__(self, code):
        self.code = code
        self.prev = None          # 직전 틱
        self.last = None          # 현재 틱
        self.vwap = None
        self.sigma = None
        self.vwap_source = None
        self.n_obs = 0            # Δ거래량 > 0 인 관측 구간 수
        self._w = self._wp = self._wp2 = 0.0
        self.bars = []            # 마감된 1분봉 [{t,o,h,l,c,v,vwap,sigma}]
        self.cur = None           # 진행 중인 봉

    # ── 갱신 ───────────────────────────────────────────────────────────────
    def update(self, t):
        """틱 반영. 분이 바뀌어 직전 봉이 마감되면 그 봉(dict)을 돌려준다."""
        last = self.last
        dv = 0.0
        if last is not None and t.volume >= last.volume:
            dv = t.volume - last.volume
        if dv > 0:
            ip = t.price
            if t.value is not None and last.value is not None and t.value > last.value:
                ip = (t.value - last.value) / dv
                lo, hi = t.low or ip, t.high or ip
                ip = min(max(ip, lo), hi)
            self._w += dv
            self._wp += dv * ip
            self._wp2 += dv * ip * ip
            self.n_obs += 1
        if t.value is not None and t.volume > 0:
            self.vwap, self.vwap_source = t.value / t.volume, "exact"
        elif self._w > 0:
            self.vwap, self.vwap_source = self._wp / self._w, "approx"
        elif self.vwap is None:
            self.vwap, self.vwap_source = t.price, "approx"
        floor = C.SIGMA_FLOOR * t.price
        if self._w > 0:
            m1, m2 = self._wp / self._w, self._wp2 / self._w
            var = m2 - 2 * self.vwap * m1 + self.vwap * self.vwap
            self.sigma = max(math.sqrt(max(var, 0.0)), floor)
        else:
            self.sigma = floor
        self.prev, self.last = last, t
        return self._bar(t, dv)

    def _bar(self, t, dv):
        minute = t.ts[11:16]
        closed = None
        if self.cur is not None and self.cur["t"] != minute:
            closed = self.cur
            self.bars.append(closed)
            self.cur = None
        if self.cur is None:
            self.cur = {"t": minute, "o": t.price, "h": t.price, "l": t.price, "c": t.price, "v": dv}
        else:
            c = self.cur
            c["h"], c["l"], c["c"] = max(c["h"], t.price), min(c["l"], t.price), t.price
            c["v"] += dv
        self.cur.update(vwap=self.vwap, sigma=self.sigma)
        return closed

    # ── 조회 ───────────────────────────────────────────────────────────────
    def ready(self):
        return self.n_obs >= C.MIN_OBS and self.vwap is not None and self.sigma

    def z(self, price, side):
        d = 1 if side == "long" else -1
        return d * (price - self.vwap) / self.sigma

    def low_broke(self, level):
        """직전 틱 이후 당일 저가가 새로 내려가며 level 이하가 됐는가 — 폴링 사이 꼬리 감지(V6·V7)."""
        t, p = self.last, self.prev
        return bool(t and p and t.low is not None and p.low is not None and t.low < p.low and t.low <= level)

    def high_broke(self, level):
        t, p = self.last, self.prev
        return bool(t and p and t.high is not None and p.high is not None and t.high > p.high and t.high >= level)
