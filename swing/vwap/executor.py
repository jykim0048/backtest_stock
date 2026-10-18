"""Live Trader(장중 집행기)(PLAN_LIVE_TRADER.md) — swing 원장의 그날 진입 주문과 청산(예약 매도·손절)을 같은 방식으로 분할 체결한다.

분할 방식(진입·청산 공통, 2026-10-18 사용자 결정):
  ① 첫 분할 = 수량의 OPEN_PCT(10%) — 진입·예약 청산은 **시가**(진입은 진입가 상한 무시), 손절은 **손절 도달 가격**.
     이 체결가가 기준가(ref).
  ② 나머지 = TRANCHES(60)분할, MIN_GAP_MIN(5분) 간격, VWAP 보다 유리할 때 그 틱 가격으로 체결
     - 진입 Long: 가격 ≤ 진입가(상한) 그리고 ≤ VWAP · 진입 Short: 가격 ≥ 진입가 그리고 ≥ VWAP
     - 청산 Long(매도): 가격 ≥ VWAP · 청산 Short(환매): 가격 ≤ VWAP
  ③ 기다리면 손해인 방향으로 기준가 대비 FORCE_MOVE(5%) 이상 움직이면 VWAP·상한 무시하고 남은 수량 전량을 그 가격에 체결
     — Long 진입·Short 청산은 상승, Short 진입·Long 청산은 하락일 때만. 반대 방향이면 VWAP 분할을 계속한다.
  진입은 언제나 손절가 안쪽에서만 체결(Long 가격 > 손절). 그날 못 채운 진입은 잔여분 추가 주문, 못 끝낸 청산은 다음 영업일 계속.

swing 원장은 읽기만 한다. 결과는 intraday/<date> 에 남기고, 장 마감 후 engine.settle_day(…, intraday=doc) 가 확정한다.
doc = {date, asof, complete, watched: {orders, positions}, orders: {주문 id: 슬라이스 기록}, exits: {포지션 id | "o:<주문 id>": 슬라이스 기록},
       vwap: {code: {...}}}
슬라이스 기록 = {code, name, side, plan[수량], next, ref, fills[{ts, qty, price, mode: open|vwap|force|stop|stop_gap, vwap}], done, why, …}
  진입 기록 + cap·stop·planned·topupOf, 청산 기록 + trigger(scheduled|stop)·kind(청산 사유)·planned·triggerTs.
"""
import math

from .. import engine
from ..intraday.feed import Tick
from . import config as C
from .state import VwapState


def tranche_qtys(planned, n=None):
    n = max(1, n or C.TRANCHES)
    if planned <= 0:
        return []
    if planned < n:
        return [1] * planned                  # 분할 수보다 적으면 1주씩
    base, extra = divmod(planned, n)
    return [base + 1] * extra + [base] * (n - extra)   # 나머지는 앞쪽 분할에 1주씩


def slice_plan(planned):
    """[첫 분할] + VWAP 분할. 첫 분할 = max(1, 반올림(수량 × OPEN_PCT))."""
    if planned <= 0:
        return []
    first = min(planned, max(1, int(math.floor(planned * C.OPEN_PCT + 0.5))))
    return [first] + tranche_qtys(planned - first)


def _minutes(a, b):
    from datetime import datetime
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 60


def _new_doc(date):
    return {"date": date, "asof": None, "complete": False, "watched": {"orders": [], "positions": []},
            "orders": {}, "exits": {}, "vwap": {}}


def filled(rec):
    return sum(f["qty"] for f in rec["fills"])


class Executor:
    def __init__(self, store, date, ledger, *, log=print, save_ticks=True):
        self.store, self.date, self.log, self.save_ticks = store, date, log, save_ticks
        self.doc = store.get(f"intraday/{date}") or _new_doc(date)
        self.doc.setdefault("exits", {})
        self.states, self.ticks, self.seen = {}, {}, set()
        self.dirty = False
        self._plan(ledger or {})
        if save_ticks:
            self._warm()

    # ── 대상 ───────────────────────────────────────────────────────────────
    def _plan(self, L):
        doc, held = self.doc, {p["code"] for p in L.get("positions") or []}
        self.positions = {}                   # 손절 감시
        for p in L.get("positions") or []:
            sp = p.get("sellPending")
            if sp:                            # 예약 청산 → 시가 10% + VWAP 분할
                doc["exits"].setdefault(p["id"], {
                    "code": p["code"], "name": p.get("name"), "side": p.get("side", "long"), "trigger": "scheduled",
                    "kind": sp.get("kind") or "pm_sell", "planned": p["qty"], "plan": slice_plan(p["qty"]), "next": 0,
                    "ref": None, "fills": [], "done": False, "why": None, "triggerTs": None})
            elif not engine._stop_suspended(p, self.date):
                self.positions[p["id"]] = {"code": p["code"], "side": p.get("side", "long"), "stop": p["stop"],
                                           "name": p.get("name"), "qty": p["qty"]}
            else:
                continue                      # 체결일 손절 재판별 대기 — 그날 손절 유예(엔진과 같음)
            if p["id"] not in doc["watched"]["positions"]:
                doc["watched"]["positions"].append(p["id"])
        self.base_of = {}
        for o in L.get("orders") or []:
            if o.get("validDate") != self.date or o.get("status", "open") != "open":
                continue
            if not o.get("topupOf") and o["code"] in held:
                continue                      # 엔진이 '이미 보유'로 건너뜀
            doc["orders"].setdefault(o["id"], {
                "code": o["code"], "name": o.get("name"), "side": o.get("side", "long"), "cap": o["entry"], "stop": o["stop"],
                "planned": o["qty"], "plan": slice_plan(o["qty"]), "next": 0, "ref": None, "fills": [], "done": False,
                "why": None, "topupOf": o.get("topupOf")})
            if o.get("topupOf"):
                self.base_of[o["id"]] = o["topupOf"]
            if o["id"] not in doc["watched"]["orders"]:
                doc["watched"]["orders"].append(o["id"])
        self.dirty = True

    def codes(self):
        return sorted({x["code"] for x in self.positions.values()} | {r["code"] for r in self.doc["orders"].values()}
                      | {r["code"] for r in self.doc["exits"].values()})

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
            self._stops(st, ts)
            for key, r in list(self.doc["orders"].items()):
                if r["code"] == t.code and not r["done"]:
                    self._entry(key, r, st, ts, hm)
            for r in list(self.doc["exits"].values()):
                if r["code"] == t.code and not r["done"]:
                    self._advance(r, st, ts, hm, "exit")
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

    def _open_exit(self, key, *, code, name, side, qty, price, kind, ts):
        """손절 → 청산 슬라이스 기록 생성 + 첫 분할(도달 가격)."""
        r = {"code": code, "name": name, "side": side, "trigger": "stop", "kind": kind, "planned": qty,
             "plan": slice_plan(qty), "next": 0, "ref": price, "fills": [], "done": False, "why": None, "triggerTs": ts}
        self.doc["exits"][key] = r
        self._fill(r, r["plan"][0], price, ts, kind, None)
        r["next"] = 1
        if filled(r) >= r["planned"]:
            r.update(done=True, why="청산 완료")
        self.log(f"[vwap] {ts[11:19]} 손절 {code} {name or ''} {side} @ {price:,.0f} ({kind}) — 첫 분할 {r['plan'][0]:,}/{qty:,}주")

    def _stops(self, st, ts):
        for pid, p in self.positions.items():
            if p["code"] != st.code or pid in self.doc["exits"]:
                continue
            hit = self._stop_hit(st, p["side"], p["stop"], first=pid not in self.seen)
            self.seen.add(pid)
            if hit:
                self._open_exit(pid, code=p["code"], name=p.get("name"), side=p["side"], qty=p["qty"],
                                price=hit[0], kind=hit[1], ts=ts)
                self.dirty = True
                for oid, base in self.base_of.items():      # 본 포지션 손절 → 그 잔여분 추가 주문도 중단
                    r = self.doc["orders"].get(oid)
                    if base == pid and r and not r["done"]:
                        r.update(done=True, why="본 포지션 손절 — 남은 분할 취소")

    def _entry(self, oid, r, st, ts, hm):
        if r["fills"]:
            hit = self._stop_hit(st, r["side"], r["stop"], first=False)
            if hit:
                r.update(done=True, why="체결 후 손절 — 남은 분할 취소")
                self._open_exit("o:" + oid, code=r["code"], name=r.get("name"), side=r["side"], qty=filled(r),
                                price=hit[0], kind=hit[1], ts=ts)
                self.dirty = True
                return
        self._advance(r, st, ts, hm, "entry")

    def _fill(self, r, q, px, ts, mode, vwap):
        r["fills"].append({"ts": ts, "qty": q, "price": px, "mode": mode, "vwap": vwap})
        self.dirty = True

    def _advance(self, r, st, ts, hm, kind):
        """슬라이스 기록 한 틱 진행 — ① 시가 첫 분할 → ③ ±FORCE_MOVE 전량 → ② VWAP 분할."""
        t = st.last
        long_ = r["side"] == "long"
        entry = kind == "entry"
        remain = r["planned"] - filled(r)
        if remain <= 0:
            r.update(done=True, why=r["why"] or ("분할 완료" if entry else "청산 완료"))
            return

        def inside(px):                                   # 진입은 손절 안쪽에서만
            return not entry or (px > r["stop"] if long_ else px < r["stop"])

        if r["next"] == 0:                               # ① 시가 첫 분할
            if t.open is None or t.volume <= 0:
                return
            r["ref"] = t.open
            r["next"] = 1
            if inside(t.open):
                self._fill(r, r["plan"][0], t.open, ts, "open", st.vwap)
                self.log(f"[vwap] {ts[11:19]} 시가 {'진입' if entry else '청산'} {r['code']} {r['side']} {r['plan'][0]:,}주 @ {t.open:,.0f}")
            elif len(r["plan"]) > 1:
                r["plan"][1] += r["plan"][0]                  # 시가가 손절 너머 → 첫 분할을 다음 분할에 합침
                r["plan"][0] = 0
                r["why"] = "시가가 손절가 너머 — 시가 체결 생략"
            return
        px = t.price
        # ③ 기다리면 손해인 방향으로 기준가 대비 5% 이상 → 전량(2026-10-18): Long 진입·Short 청산 = 상승, Short 진입·Long 청산 = 하락
        away = 1 if entry == long_ else -1
        if r["ref"] and away * (px / r["ref"] - 1) >= C.FORCE_MOVE and inside(px):
            self._fill(r, remain, px, ts, "force", st.vwap)
            r.update(done=True, why=f"기준가 {r['ref']:,.0f} 대비 {px / r['ref'] - 1:+.1%} — 남은 수량 전량")
            self.log(f"[vwap] {ts[11:19]} 전량 {'진입' if entry else '청산'} {r['code']} {r['side']} {remain:,}주 @ {px:,.0f} ({r['why']})")
            return
        if not (C.ENTRY_START <= hm < C.ENTRY_END) or st.n_obs < C.MIN_OBS or st.vwap is None:
            return
        vw = [f for f in r["fills"] if f["mode"] == "vwap"]
        if vw and _minutes(vw[-1]["ts"], ts) < C.MIN_GAP_MIN:         # 간격은 VWAP 분할끼리만(첫 분할 직후 바로 가능)
            return
        if entry:
            ok = (px <= r["cap"] and px <= st.vwap) if long_ else (px >= r["cap"] and px >= st.vwap)
            ok = ok and inside(px)
        else:
            ok = px >= st.vwap if long_ else px <= st.vwap
        if not ok or r["next"] >= len(r["plan"]):
            return
        q = min(r["plan"][r["next"]], remain)
        r["next"] += 1
        self._fill(r, q, px, ts, "vwap", st.vwap)
        self.log(f"[vwap] {ts[11:19]} VWAP 분할 {'진입' if entry else '청산'} {r['code']} {r['side']} {q:,}주 @ {px:,.0f} (VWAP {st.vwap:,.0f})")
        if filled(r) >= r["planned"]:
            r.update(done=True, why="분할 완료" if entry else "청산 완료")

    # ── 마감·저장 ──────────────────────────────────────────────────────────
    def finish(self):
        for r in self.doc["orders"].values():
            if not r["done"]:
                r.update(done=True, why=r["why"] or f"{filled(r):,}/{r['planned']:,}주 체결 — 나머지 잔여분 추가 주문")
        for r in self.doc["exits"].values():
            if not r["done"]:
                r.update(done=True, why=r["why"] or f"{filled(r):,}/{r['planned']:,}주 청산 — 나머지 다음 영업일 계속")
        self.dirty = True

    def summary(self):
        o, x = self.doc["orders"].values(), self.doc["exits"].values()
        return {"orders": len(self.doc["orders"]), "filledOrders": sum(1 for r in o if r["fills"]),
                "entryFills": sum(len(r["fills"]) for r in o), "exits": len(self.doc["exits"]),
                "stopExits": sum(1 for r in x if r["trigger"] == "stop"), "exitFills": sum(len(r["fills"]) for r in x),
                "watchedPositions": len(self.positions), "complete": self.doc["complete"], "asof": self.doc["asof"]}

    def save(self, ticks=False):
        self.store.put(f"intraday/{self.date}", self.doc)
        self.dirty = False
        if ticks and self.save_ticks:
            for code, ts in self.ticks.items():
                self.store.put(f"intraday/{self.date}/ticks/{code}", ts)
