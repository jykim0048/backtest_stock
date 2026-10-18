"""LiveBook(L1) — swing 원장 + 버스 시세 → 포지션·포트폴리오 지표(PLAN_PM §5).

원장은 세션 시작 때 읽은 것을 그대로 쓴다(원장 쓰기는 16:30 정산뿐). 시세는 Redis 버스 항목(Tick + VWAP).
방향은 d(Long +1 / Short −1)로 맞춘다 — 손익·수익률은 '유리하면 +'.
"""
import datetime
from collections import deque

from .. import engine
from . import config as C


def _d(p):
    return -1 if engine.is_short(p) else 1


def _dt(ts):
    return datetime.datetime.fromisoformat(ts)


class LiveBook:
    def __init__(self, ledger):
        self.L = ledger or {"cash": 0, "positions": [], "orders": []}
        self.positions = {p["id"]: p for p in self.L.get("positions") or []}
        self.ticks = {}                # code → 최신 Tick
        self.vwap = {}                 # code → {vwap, sigma, src}
        self.ring = {}                 # code → deque[(ts, price)]
        self.asof = None
        self.start_equity = self._equity({})          # 전일 종가 기준(원장 lastClose)

    # ── 갱신 ───────────────────────────────────────────────────────────────
    def update(self, entry):
        """버스 항목 {ts, ticks, vwap} 반영."""
        self.asof = entry["ts"]
        for t in entry["ticks"]:
            self.ticks[t.code] = t
            if t.code in entry.get("vwap", {}):
                self.vwap[t.code] = entry["vwap"][t.code]
            r = self.ring.setdefault(t.code, deque())
            r.append((t.ts, t.price))
            cut = _dt(t.ts) - datetime.timedelta(minutes=C.RING_MIN)
            while r and _dt(r[0][0]) < cut:
                r.popleft()

    # ── 조회 ───────────────────────────────────────────────────────────────
    def px(self, code):
        t = self.ticks.get(code)
        return t.price if t else None

    def ret(self, code, minutes):
        """minutes 분 전 대비 수익률(방향 없음). 그만큼 기록이 없으면 가장 오래된 값 기준, 기록 1개 이하면 None."""
        r = self.ring.get(code)
        if not r or len(r) < 2:
            return None
        cut = _dt(r[-1][0]) - datetime.timedelta(minutes=minutes)
        base = r[0][1]
        for ts, p in r:
            if _dt(ts) <= cut:
                base = p
            else:
                break
        return r[-1][1] / base - 1 if base else None

    def _equity(self, prices):
        return self.L.get("cash", 0) + sum(engine.position_value(p, prices.get(p["code"]) or engine._px(p))
                                           for p in self.positions.values())

    def equity(self):
        return self._equity({c: t.price for c, t in self.ticks.items()})

    def day_pnl(self):
        base = self.start_equity
        return self.equity() / base - 1 if base else 0.0

    def metrics(self, pid):
        """포지션 1개 지표 — 스냅샷·트리거 공용. 시세 없으면 None."""
        p = self.positions[pid]
        px = self.px(p["code"])
        if px is None:
            return None
        d = _d(p)
        span = d * (p["entry"] - p["stop"])
        used = d * (p["entry"] - px) / span if span > 0 else 0.0
        t = self.ticks[p["code"]]
        r5, r30 = self.ret(p["code"], 5), self.ret(p["code"], 30)
        eq = self.equity()
        return {"px": px, "pnlPct": d * (px / p["entry"] - 1),
                "stopDistUsed": max(0.0, used),
                "ret5m": r5, "ret30m": r30,
                "adv5m": None if r5 is None else -d * r5,            # 불리한 방향 변동(+면 불리)
                "retDay": (px / t.prev_close - 1) if t.prev_close else None,
                "weight": abs(engine.position_value(p, px)) / eq if eq else None,
                "vwap": (self.vwap.get(p["code"]) or {}).get("vwap"),
                "targetHit": bool(p.get("target")) and d * (px - p["target"]) >= 0}

    def exposure(self):
        eq = self.equity() or 1
        long_g = sum(p["qty"] * (self.px(p["code"]) or engine._px(p)) for p in self.positions.values() if not engine.is_short(p))
        short_g = sum(p["qty"] * (self.px(p["code"]) or engine._px(p)) for p in self.positions.values() if engine.is_short(p))
        sectors = {}
        for p in self.positions.values():
            v = p["qty"] * (self.px(p["code"]) or engine._px(p))
            sectors[p.get("sector") or "기타"] = sectors.get(p.get("sector") or "기타", 0) + v / eq
        return {"longGross": long_g / eq, "shortGross": short_g / eq,
                "sectorWeights": {k: round(v, 4) for k, v in sorted(sectors.items(), key=lambda x: -x[1])}}
