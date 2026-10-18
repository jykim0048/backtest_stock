"""장중 VWAP 실행기(PLAN_VWAP.md) — swing 원장의 그날 진입 주문을 분할 체결하고, 보유·신규 체결분의 손절가를 실시간 판정한다.

swing 원장(ledger)은 읽기만 한다. 결과는 intraday/<date> 문서에 남기고, 장 마감 후 engine.settle_day(…, intraday=doc) 가 확정한다.

doc = {date, asof, complete,
       watched: {orders: [주문 id], positions: [포지션 id]},     # 실행기가 책임진 대상(정산 때 일봉 규칙 대신 이 기록을 씀)
       orders: {주문 id: {code, side, cap, stop, planned, tranches[수량], fills[{ts, qty, price, vwap}], stop_hit{ts, price, kind}|None, done, why, topupOf}},
       stops: {포지션 id: {ts, price, kind: stop|stop_gap, code}},
       vwap: {code: {price, vwap, sigma, source, ts}}}

분할 진입(Long, Short 는 대칭): 가격 ≤ 진입가(상한) 이고 가격 ≤ VWAP(VWAP 보다 유리) 이고 가격 > 손절가일 때 한 분할을
그 틱 가격으로 체결. 분할 사이 MIN_GAP_MIN 분 이상, ENTRY_START~ENTRY_END. 남은 분할은 체결하지 않고 정산 때 잔여분 추가 주문으로 넘어간다.
손절: 현재가가 손절 넘음 또는 당일 저가(Short 고가)가 직전 틱 이후 손절 너머로 갱신 → 손절가. 처음 본 순간 이미 당일 저가가
손절 너머면(재시작·기동 지연) 시가가 손절 너머였으면 시가(stop_gap), 아니면 손절가.
"""
from .. import engine
from ..intraday.feed import Tick
from . import config as C
from .state import VwapState


def tranche_qtys(planned, n=None):
    n = max(1, n or C.TRANCHES)
    if planned <= 0:
        return []
    if planned < n:
        return [planned]
    base = planned // n
    q = [base] * n
    q[-1] += planned - base * n
    return q


def _minutes(a, b):
    from datetime import datetime
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def _new_doc(date):
    return {"date": date, "asof": None, "complete": False, "watched": {"orders": [], "positions": []},
            "orders": {}, "stops": {}, "vwap": {}}


class Executor:
    def __init__(self, store, date, ledger, *, log=print, save_ticks=True):
        self.store, self.date, self.log, self.save_ticks = store, date, log, save_ticks
        self.doc = store.get(f"intraday/{date}") or _new_doc(date)
        self.states, self.ticks, self.seen = {}, {}, set()
        self.dirty = False
        self._plan(ledger or {})
        if save_ticks:
            self._warm()

    # ── 대상 ───────────────────────────────────────────────────────────────
    def _plan(self, L):
        doc, held = self.doc, {p["code"] for p in L.get("positions") or []}
        self.positions = {}
        for p in L.get("positions") or []:
            if p.get("sellPending") or engine._stop_suspended(p, self.date):
                continue                      # 시가 청산 예약(엔진 ①) · 체결일 손절 재판별 대기(그날 손절 유예)는 제외
            self.positions[p["id"]] = {"code": p["code"], "side": p.get("side", "long"), "stop": p["stop"], "name": p.get("name")}
        self.base_of = {}
        for o in L.get("orders") or []:
            if o.get("validDate") != self.date or o.get("status", "open") != "open":
                continue
            if not o.get("topupOf") and o["code"] in held:
                continue                      # 엔진이 '이미 보유'로 건너뜀
            rec = doc["orders"].setdefault(o["id"], {
                "code": o["code"], "name": o.get("name"), "side": o.get("side", "long"), "cap": o["entry"], "stop": o["stop"],
                "planned": o["qty"], "tranches": tranche_qtys(o["qty"]), "fills": [], "stop_hit": None, "done": False, "why": None})
            if o.get("topupOf"):
                self.base_of[o["id"]] = o["topupOf"]
            if o["id"] not in doc["watched"]["orders"]:
                doc["watched"]["orders"].append(o["id"])
            rec.setdefault("topupOf", o.get("topupOf"))
        for pid in self.positions:
            if pid not in doc["watched"]["positions"]:
                doc["watched"]["positions"].append(pid)
        self.dirty = True

    def codes(self):
        return sorted({x["code"] for x in self.positions.values()} | {r["code"] for r in self.doc["orders"].values()})

    def _warm(self):
        """재시작 — 오늘 저장된 틱으로 VWAP 상태만 복원(체결·손절 판정 없음)."""
        saved = []
        for k in self.store.keys(f"intraday/{self.date}/ticks/"):
            saved += self.store.get(k) or []
        for d in sorted(saved, key=lambda x: x["ts"]):
            t = Tick.from_dict(d)
            self.ticks.setdefault(t.code, []).append(d)
            self.states.setdefault(t.code, VwapState(t.code)).update(t)

    # ── 처리 ───────────────────────────────────────────────────────────────
    def on_batch(self, ts, ticks):
        hm = ts[11:16]
        for t in ticks:
            if self.save_ticks:
                self.ticks.setdefault(t.code, []).append(t.to_dict())
            st = self.states.setdefault(t.code, VwapState(t.code))
            st.update(t)
            self.doc["vwap"][t.code] = {"price": t.price, "vwap": st.vwap, "sigma": st.sigma, "source": st.vwap_source, "ts": ts}
            self._positions(st, ts)
            self._orders(st, ts, hm)
        self.doc["asof"] = ts
        self.doc["complete"] = hm >= C.COMPLETE_AT

    def _stop_hit(self, st, side, stop, first):
        """(가격, kind) 또는 None."""
        t = st.last
        long_ = side == "long"
        if first:
            ext = t.low if long_ else t.high
            if ext is not None and (ext <= stop if long_ else ext >= stop):
                if t.open is not None and (t.open <= stop if long_ else t.open >= stop):
                    return t.open, "stop_gap"
                return stop, "stop"
            return None
        through = t.price <= stop if long_ else t.price >= stop
        if through or (st.low_broke(stop) if long_ else st.high_broke(stop)):
            return stop, "stop"
        return None

    def _positions(self, st, ts):
        for pid, p in self.positions.items():
            if p["code"] != st.code or pid in self.doc["stops"]:
                continue
            hit = self._stop_hit(st, p["side"], p["stop"], first=pid not in self.seen)
            self.seen.add(pid)
            if hit:
                self.doc["stops"][pid] = {"ts": ts, "price": hit[0], "kind": hit[1], "code": p["code"]}
                self.dirty = True
                self.log(f"[vwap] {ts[11:19]} 손절 {p['code']} {p.get('name') or ''} {p['side']} @ {hit[0]:,.0f} ({hit[1]})")
                for oid, base in self.base_of.items():      # 본 포지션 손절 → 그 잔여분 추가 주문도 중단
                    r = self.doc["orders"].get(oid)
                    if base == pid and r and not r["done"]:
                        r.update(done=True, why="본 포지션 손절 — 남은 분할 취소")

    def _orders(self, st, ts, hm):
        t = st.last
        for oid, r in self.doc["orders"].items():
            if r["code"] != st.code or r["done"]:
                continue
            long_ = r["side"] == "long"
            if r["fills"]:
                hit = self._stop_hit(st, r["side"], r["stop"], first=False)
                if hit:
                    r.update(stop_hit={"ts": ts, "price": hit[0], "kind": hit[1]}, done=True, why="체결 후 손절 — 남은 분할 취소")
                    self.dirty = True
                    self.log(f"[vwap] {ts[11:19]} 손절(체결분) {r['code']} {r['side']} @ {hit[0]:,.0f}")
                    continue
            if len(r["fills"]) >= len(r["tranches"]):
                r.update(done=True, why=r["why"] or "분할 완료")
                continue
            if not (C.ENTRY_START <= hm < C.ENTRY_END) or st.n_obs < C.MIN_OBS or st.vwap is None:
                continue
            if r["fills"] and _minutes(r["fills"][-1]["ts"], ts) < C.MIN_GAP_MIN:
                continue
            px = t.price
            ok = (px <= r["cap"] and px <= st.vwap and px > r["stop"]) if long_ else \
                 (px >= r["cap"] and px >= st.vwap and px < r["stop"])
            if not ok:
                continue
            q = r["tranches"][len(r["fills"])]
            r["fills"].append({"ts": ts, "qty": q, "price": px, "vwap": st.vwap})
            self.dirty = True
            self.log(f"[vwap] {ts[11:19]} 분할 {len(r['fills'])}/{len(r['tranches'])} {r['code']} {r['side']} {q:,}주 @ {px:,.0f} "
                     f"(상한 {r['cap']:,.0f} · VWAP {st.vwap:,.0f})")

    # ── 마감·저장 ──────────────────────────────────────────────────────────
    def finish(self):
        for r in self.doc["orders"].values():
            if not r["done"]:
                n, m = len(r["fills"]), len(r["tranches"])
                r.update(done=True, why=r["why"] or (f"분할 {n}/{m} 체결 — 진입가·VWAP 조건 미충족" if n < m else "분할 완료"))
        self.dirty = True

    def summary(self):
        o = self.doc["orders"].values()
        return {"orders": len(self.doc["orders"]), "filledOrders": sum(1 for r in o if r["fills"]),
                "fills": sum(len(r["fills"]) for r in o), "stops": len(self.doc["stops"]),
                "positions": len(self.positions), "complete": self.doc["complete"], "asof": self.doc["asof"]}

    def save(self, ticks=False):
        self.store.put(f"intraday/{self.date}", self.doc)
        self.dirty = False
        if ticks and self.save_ticks:
            for code, ts in self.ticks.items():
                self.store.put(f"intraday/{self.date}/ticks/{code}", ts)
