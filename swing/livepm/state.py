"""거래 상태(L2) — active / reducing / halted 와 반사 규칙 X2~X4(PLAN_PM §4). X1 손절은 Live Trader 가 집행한다.

  X2 일중 손실 ≤ −REDUCE_DD      → reducing(그날 끝까지 유지)
  X3 킬스위치 livepm/state=halted → halted(사람이 지울 때까지)
  X4 버스 heartbeat 만료 또는 새 시세 FEED_STALE_S 이상 없음 → halted, 복구되면 X2·X3 사유가 없을 때 active 로
"""
from . import config as C

KILL_KEY = "livepm/state"


class TradingState:
    def __init__(self, store=None, log=print):
        self.store, self.log = store, log
        self.state = "active"
        self.reduce_reason = None
        self.events = []

    def _kill(self):
        doc = (self.store.get(KILL_KEY) if self.store else None) or {}
        return doc.get("state") == "halted", doc.get("reason")

    def _set(self, ts, state, why):
        if state != self.state:
            self.events.append({"ts": ts, "from": self.state, "to": state, "why": why})
            self.log(f"[livepm] {ts[11:19] if ts else ''} 상태 {self.state} → {state} ({why})")
            self.state = state

    def evaluate(self, ts, *, day_pnl, feed_age_s, trader_alive):
        """매 갱신마다 호출. 반환 현재 상태."""
        killed, why = self._kill()
        if killed:
            self._set(ts, "halted", f"X3 킬스위치{(' — ' + why) if why else ''}")
            return self.state
        if not trader_alive or (feed_age_s is not None and feed_age_s >= C.FEED_STALE_S):
            self._set(ts, "halted", "X4 Live Trader 시세 끊김" if trader_alive else "X4 Live Trader heartbeat 없음")
            return self.state
        if self.reduce_reason is None and day_pnl is not None and day_pnl <= -C.REDUCE_DD:
            self.reduce_reason = f"X2 일중 손실 {day_pnl * 100:.2f}%"
        self._set(ts, "reducing" if self.reduce_reason else "active", self.reduce_reason or "정상")
        return self.state

    def allows(self, action):
        if self.state == "halted":
            return False
        if self.state == "reducing":
            return action in C.REDUCE_ACTIONS or action == "hold"
        return True
