"""Live PM 하루 처리(PLAN_PM M1) — 버스 항목 → LiveBook → 거래 상태 → 트리거 → 스냅샷 → 판단 → RiskGate → 지시.

라이브(M3 서비스)와 재생(--replay)이 같은 on_entry() 를 쓴다. 시각 판단은 항목 ts 기준이라 재생 결과가 재현된다.
지시는 `livepm/<date>/instructions` 에 **추가만** 한다 — Live Trader 가 읽어 VWAP 분할로 집행한다(PLAN_LIVE_TRADER §5-2, M2).
저장: livepm/<date>/instructions · livepm/<date>/calls/<n>(스냅샷·결정·게이트·비용) · livepm/<date>/run(요약·상태 변화)
"""
from . import agent as A
from . import config as C
from . import gate as G
from . import snapshot as SN
from .book import LiveBook
from .state import TradingState
from .triggers import Triggers


def intraday_at(doc, ts):
    """재생용 — Live Trader 하루 기록을 ts 시점까지로 자른다(나중 청산·체결을 미리 알지 않게)."""
    if not doc:
        return {}
    exits = {k: dict(v, fills=[f for f in v.get("fills") or [] if f["ts"] <= ts])
             for k, v in (doc.get("exits") or {}).items()}
    exits = {k: v for k, v in exits.items() if v["fills"]}
    orders = {}
    for k, v in (doc.get("orders") or {}).items():
        fills = [f for f in v.get("fills") or [] if f["ts"] <= ts]
        orders[k] = dict(v, fills=fills, done=sum(f["qty"] for f in fills) >= (v.get("planned") or 0))
    return dict(doc, exits=exits, orders=orders)


class DayRunner:
    def __init__(self, store, date, ledger, agent=None, *, status_fn=None, intraday_fn=None, log=print):
        """status_fn(ts) → /status dict | None, intraday_fn(ts) → Live Trader intraday/<date> | None."""
        self.store, self.date, self.log = store, date, log
        self.agent = agent or A.MockLivePM()
        self.status_fn = status_fn or (lambda ts: None)
        self.intraday_fn = intraday_fn or (lambda ts: store.get(f"intraday/{date}"))
        self.book = LiveBook(ledger)
        self.state = TradingState(store, log=log)
        self.trig = Triggers()
        self.instructions = store.get(f"livepm/{date}/instructions") or []
        self.calls = len(store.keys(f"livepm/{date}/calls/"))
        self.memory = {}             # pid → 마지막 결정 {ts, action, reason}
        self.decisions = []          # "HH:MM action code" 한 줄 요약(단기 기억)
        self.costs = 0.0

    # ── 처리 ───────────────────────────────────────────────────────────────
    def on_entry(self, entry, *, trader_alive=True, feed_age_s=0):
        ts = entry["ts"]
        self.book.update(entry)
        return self.step(ts, trader_alive=trader_alive, feed_age_s=feed_age_s)

    def step(self, ts, *, trader_alive=True, feed_age_s=0):
        """상태 평가 → 트리거 → (있으면) 판단. 반환 이번에 낸 지시 목록."""
        st = self.state.evaluate(ts, day_pnl=self.book.day_pnl(), feed_age_s=feed_age_s, trader_alive=trader_alive)
        if st == "halted":
            return []
        status = self.status_fn(ts)
        intraday = self.intraday_fn(ts) or {}
        trigs = self.trig.scan(ts, self.book, status, exiting=set((intraday.get("exits") or {}).keys()))
        if not trigs:
            return []
        return self._decide(ts, trigs, status, intraday)

    def _decide(self, ts, trigs, status, intraday):
        today = {"callsUsed": self.trig.calls, "costKrw": round(self.trig.cost), "decisions": self.decisions[-10:]}
        snap, text = SN.build(ts, self.state.state, trigs, self.book, status=status, intraday=intraday,
                              memory=self.memory, today=today)
        dec, meta = self.agent.decide(text)
        self.trig.spend(meta.get("costKrw") or 0.0)
        reviewed = G.review(dec.get("decisions"), ts=ts, book=self.book, state=self.state, intraday=intraday,
                            recent=self.instructions)
        self.calls += 1
        n = self.calls
        new = []
        for k, r in enumerate(reviewed):
            if r["pid"]:
                self.memory[r["pid"]] = {"ts": ts[11:16], "action": r["action"], "reason": (r["decision"].get("reason") or "")[:120]}
            if r["outcome"] in ("pass", "downgrade") and r["action"] != "hold":
                ins = {"id": f"{self.date}-{n:03d}-{k}", "ts": ts, "code": r["code"], "positionId": r["pid"],
                       "action": r["action"], "reason": r["decision"].get("reason"),
                       "confidence": r["decision"].get("confidence"), "callId": n, "gate": r["why"]}
                if "pct" in r and r["action"] in ("exit", "trim"):
                    ins["pct"] = r["pct"]
                if "newStop" in r:
                    ins["newStop"] = r["newStop"]
                new.append(ins)
                self.decisions.append(f"{ts[11:16]} {r['action']} {r['code']}" + (f" {r['pct']:.0%}" if ins.get("pct") else ""))
        self.instructions += new
        self.store.put(f"livepm/{self.date}/calls/{n:03d}", {
            "ts": ts, "triggers": trigs, "snapshot": snap, "decision": dec, "meta": meta, "gate": reviewed,
            "instructions": [i["id"] for i in new]})
        if new:
            self.store.put(f"livepm/{self.date}/instructions", self.instructions)
        self.log(f"[livepm] {ts[11:19]} 판단 #{n} 트리거 {','.join(sorted({t['id'] for t in trigs}))} → 지시 "
                 + (", ".join(f"{i['action']} {i['code']}" for i in new) or "없음")
                 + (f" ({meta['error']})" if meta.get("error") else ""))
        return new

    # ── 저장 ───────────────────────────────────────────────────────────────
    def summary(self):
        by = {}
        for i in self.instructions:
            by[i["action"]] = by.get(i["action"], 0) + 1
        return {"date": self.date, "calls": self.calls, "costKrw": round(self.trig.cost, 1), "state": self.state.state,
                "stateEvents": self.state.events, "instructions": len(self.instructions), "byAction": by,
                "agent": getattr(self.agent, "name", "?"), "asof": self.book.asof}

    def save(self):
        self.store.put(f"livepm/{self.date}/run", self.summary())
