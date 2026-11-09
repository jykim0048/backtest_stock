"""Live Trader(장중 집행기)(PLAN_LIVE_TRADER.md) — swing 원장의 그날 진입 주문과 청산(예약 매도·손절)을 같은 방식으로 분할 체결한다.

분할 방식(진입·청산 공통, 2026-10-18 사용자 결정):
  ① 첫 분할 = 수량의 OPEN_PCT(10%) — 진입·예약 청산은 **시가**(진입은 진입가 상한 무시), 손절은 **손절 도달 가격**.
     이 체결가가 기준가(ref).
  ② 나머지 = TRANCHES(60)분할, MIN_GAP_MIN(5분) 간격, VWAP 보다 유리할 때 그 틱 가격으로 체결
     - 진입 Long: 가격 ≤ 진입가(상한) 그리고 ≤ VWAP · 진입 Short: 가격 ≥ 진입가 그리고 ≥ VWAP
     - 청산 Long(매도): 가격 ≥ VWAP · 청산 Short(환매): 가격 ≤ VWAP
  ③ 기다리면 손해인 방향으로 기준가 대비 FORCE_MOVE(5%) 이상 움직이면 VWAP 무시하고 남은 수량 전량을 그 가격에 체결
     — Long 진입·Short 청산은 상승, Short 진입·Long 청산은 하락일 때만. 반대 방향이면 VWAP 분할을 계속한다.
     진입은 시가 첫 분할까지 포함해 진입가 상한 안쪽에서만(2026-11-03 사용자 지시 — 삼아알미늄 10-29 상한 +5.9% 추격 매수). 청산은 가격 제한 없음.
  진입은 언제나 손절가 안쪽에서만 체결(Long 가격 > 손절). 그날 못 채운 진입은 잔여분 추가 주문, 못 끝낸 청산은 다음 영업일 계속.

swing 원장은 읽기만 한다. 결과는 intraday/<date> 에 남기고, 장 마감 후 engine.settle_day(…, intraday=doc) 가 확정한다.
doc = {date, asof, complete, watched: {orders, positions}, orders: {주문 id: 슬라이스 기록},
       exits: {포지션 id | "o:<주문 id>" | "i:<지시 id>"(Live PM trim): 슬라이스 기록},
       instructions: {지시 id: {ts, status: done|rejected, why}}, stopMoves: {포지션 id: {ts, old, newStop}},
       reviews: {포지션 id: {ts, reason}}, vwap: {code: {...}}}

Live PM 지시(2026-10-19, PLAN_LIVE_TRADER §5-2) — 매 폴링마다 livepm/<date>/instructions 의 새 지시를 읽어 집행한다:
  exit  → 포지션 청산 슬라이스(trigger livepm, kind livepm_exit), 첫 10% 는 다음 틱 현재가, 나머지 VWAP 분할·5% 전량
  trim  → "i:<지시 id>" 슬라이스(kind livepm_trim, 수량 = 보유 × pct). trim 중에도 손절 감시는 계속, 손절되면 남은 수량만 손절
  tighten_stop → 그 시각부터 새 손절가로 감시(유리한 쪽으로만), 정산 때 원장 손절가 갱신
  flag_review  → 정산 때 포지션에 표시 → 그날 배치가 trading_agent 재판별

스위칭(2026-10-19 사용자 결정, PLAN_LIVE_TRADER §2-1) — 시가 일괄 현금 확보 청산 대신:
  Long 신규 매수는 **그 시점까지 쓸 수 있는 현금** 안에서만 체결(현금 = 세션 시작 가용 현금 + 오늘 Long 매도 대금 − 오늘 Long 매수 대금).
  **적시 스위칭**(2026-10-20 수정) — 매수 분할의 조건이 맞았는데 현금이 모자랄 때만, 그 **부족 금액만큼** 보유 Long 을 판다
  ("s:<포지션 id>", kind switch). 손실은 빨리, 수익은 길게(2026-11-03 사용자 지시) — 4등급을 위에서부터 판다(_tier, 수익 = 진입가 대비):
    A 손절 우선: 손실 중 + 가격 < VWAP, 또는 시가 대비 FORCE_MOVE 하락(수익 SWITCH_WINNER_PCT 미만) — 손절선에 가까운 순
    B 정체: SWITCH_STALE_DAYS 영업일 이상 보유 + 수익 SWITCH_STALE_PCT 미만 — 수익 낮은 순
    C 소폭 손익: 가격 ≥ VWAP + 수익 SWITCH_WINNER_PCT 미만 — 수익 낮은 순
    D 승자 보호: 수익 SWITCH_WINNER_PCT 이상 또는 목표 도달 후 보유 — A~C 로 모자라고 막힌 매수에 PM Buy 가 있을 때만, 현재 비중이 PM 비중을 넘은 초과분만
    어디에도 안 드는 종목(소폭 수익 + 가격 < VWAP)은 팔지 않는다.
  시가에는 팔지 않는다(ENTRY_START 부터). 판 직후 같은 폴링에서 막혔던 매수를 다시 시도한다.
  (10-20 첫 운영의 '남은 매수 금액 전체 − 현금' 방식은 살 수 없는 주문 몫까지 미리 팔아 현금 9,240만 원이 남았다 → 폐기.)
  정산은 진입 전에 재원 매도를 현금으로 반영하고, 못 산 수량은 잔여분 추가 주문으로 다음 날 이어진다.
진입 우선순위(2026-11-03 사용자 지시) — 매 폴링마다 주문을 engine.entry_priority(PM 등급 → 높은 비중) 순으로 검토해 현금을 먼저 준다.
  순위가 높아도 그 폴링에 체결 조건(진입가 상한·VWAP)이 안 맞으면 다음 순위를 바로 검토한다 — 현금을 묶어 두지 않는다.
슬라이스 기록 = {code, name, side, plan[수량], next, ref, fills[{ts, qty, price, mode: open|vwap|force|stop|stop_gap, vwap}], done, why, …}
  진입 기록 + cap·stop·planned·topupOf, 청산 기록 + trigger(scheduled|stop)·kind(청산 사유)·planned·triggerTs.
"""
import math

from .. import config as swing_config
from .. import engine
from ..config import MAX_WEIGHT, SELL_TAX
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


def _tier(p, st, ret):
    """스위칭 재원 등급(모듈 설명) — A·B·C·D 또는 None(팔지 않음)."""
    t, has_vwap = st.last, st.n_obs >= C.MIN_OBS and st.vwap is not None
    fall = t.open and t.price <= t.open * (1 - C.FORCE_MOVE)
    winner = ret >= C.SWITCH_WINNER_PCT
    if (ret < 0 and has_vwap and t.price < st.vwap) or (fall and not winner):
        return "A"
    if winner or p.get("targetHit"):
        return "D"
    if (p.get("holdDay") or 0) >= C.SWITCH_STALE_DAYS and ret < C.SWITCH_STALE_PCT:
        return "B"
    return "C" if has_vwap and t.price >= st.vwap else None


def _stop_progress(p, px):
    """손절선까지 진행률 — 0 = 진입가, 1 = 손절가. 손절가가 진입가 이상이면(수익 잠금) 1."""
    room = p["entry"] - p["stop"]
    return (p["entry"] - px) / room if room > 0 else 1.0


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
        for k in ("exits", "instructions", "stopMoves", "reviews", "switch"):
            self.doc.setdefault(k, {})
        self.states, self.ticks, self.seen = {}, {}, set()
        self.dirty = False
        self._plan(ledger or {})
        if save_ticks:
            self._warm()

    # ── 대상 ───────────────────────────────────────────────────────────────
    def _plan(self, L):
        doc, held = self.doc, {p["code"] for p in L.get("positions") or []}
        self.positions = {}                   # 손절 감시
        self.price_only = set()               # 시세만 받는 종목(체결일 손절 재판별 대기 — 손절 유예, 2026-11-09)
        self.held = {p["id"]: p for p in L.get("positions") or []}   # Live PM 지시 대상(원장 보유 전부)
        for p in L.get("positions") or []:
            sp = p.get("sellPending")
            if sp:                            # 예약 청산 → 시가 10% + VWAP 분할
                doc["exits"].setdefault(p["id"], {
                    "code": p["code"], "name": p.get("name"), "side": p.get("side", "long"), "trigger": "scheduled",
                    "kind": sp.get("kind") or "pm_sell", "planned": p["qty"], "plan": slice_plan(p["qty"]), "next": 0,
                    "ref": None, "fills": [], "done": False, "why": None, "triggerTs": None})
            elif not engine._stop_suspended(p, self.date):
                mv = doc["stopMoves"].get(p["id"])            # 재시작 — 오늘 Live PM 이 올린 손절가 유지
                self.positions[p["id"]] = {"code": p["code"], "side": p.get("side", "long"),
                                           "stop": mv["newStop"] if mv else p["stop"], "name": p.get("name"), "qty": p["qty"]}
            else:
                # 체결일 손절 재판별 대기 — 그날 손절 유예(엔진과 같음). 손절 감시는 안 하지만 시세는 받는다:
                # 폴링 대상에서 빠지면 대시보드 현재가가 전일 종가에 멈추고(11-09 BGF리테일·팬오션) Live PM·히스토리 틱도 비었다.
                self.price_only.add(p["code"])
                continue
            if p["id"] not in doc["watched"]["positions"]:
                doc["watched"]["positions"].append(p["id"])
        self.pool = {p["id"]: p for p in L.get("positions") or []           # 스위칭 재원 후보 — 예약 청산 없는 Long
                     if not engine.is_short(p) and not p.get("sellPending")}
        self.equity = engine.equity_now(L) if "cash" in L else 0.0             # D 등급 비중 기준 — 전일 종가 평가액
        if "cashStart" not in doc["switch"]:
            doc["switch"]["cashStart"] = float(engine._free_cash(L)) if L.get("positions") is not None and "cash" in L else 0.0
        self.base_of = {}
        for o in L.get("orders") or []:
            if o.get("validDate") != self.date or o.get("status", "open") != "open":
                continue
            if not o.get("topupOf") and o["code"] in held:
                continue                      # 엔진이 '이미 보유'로 건너뜀
            doc["orders"].setdefault(o["id"], {
                "code": o["code"], "name": o.get("name"), "side": o.get("side", "long"), "cap": o["entry"], "stop": o["stop"],
                "planned": o["qty"], "plan": slice_plan(o["qty"]), "next": 0, "ref": None, "fills": [], "done": False,
                "why": None, "topupOf": o.get("topupOf"), "rating": o.get("rating"), "weight": o.get("weight")})
            if o.get("topupOf"):
                self.base_of[o["id"]] = o["topupOf"]
            if o["id"] not in doc["watched"]["orders"]:
                doc["watched"]["orders"].append(o["id"])
        self.dirty = True

    def codes(self):
        return sorted({x["code"] for x in self.positions.values()} | {r["code"] for r in self.doc["orders"].values()}
                      | {r["code"] for r in self.doc["exits"].values()} | self.price_only)

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
        self._requests = []                  # (슬라이스 기록, 상태, 부족 수량, 가격) — 현금 때문에 막힌 매수
        for t in ticks:
            if self.save_ticks:
                self.ticks.setdefault(t.code, []).append(t.to_dict())
            st = self.states.setdefault(t.code, VwapState(t.code))
            st.update(t)
            self.doc["vwap"][t.code] = {"price": t.price, "vwap": st.vwap, "sigma": st.sigma, "source": st.vwap_source, "ts": ts}
            self._stops(st, ts)
            for r in list(self.doc["exits"].values()):
                if r["code"] == t.code and not r["done"] and r.get("trigger") != "switch":
                    self._advance(r, st, ts, hm, "exit")
        live = {t.code for t in ticks}                       # 청산을 먼저 돌려 같은 폴링의 매도 대금도 우선순위대로 쓴다
        for key, r in sorted(self.doc["orders"].items(), key=lambda kv: engine.entry_priority(kv[1])):
            if r["code"] in live and not r["done"]:
                self._entry(key, r, self.states[r["code"]], ts, hm)
        if self._requests and self._switch(ts, hm, self._requests):
            for r, st, _q, _px in self._requests:                # 판 직후 같은 폴링에서 막혔던 매수 다시 시도
                if not r["done"]:
                    self._advance(r, st, ts, hm, "entry", retry=True)
        self._requests = []
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
                trimmed = self._close_trims(pid, "손절로 대체")
                # 오늘 이미 체결된 추가 매수분도 함께 청산(2026-11-03 광전자: 시가 85주가 빠져 다음 날 '손절'로 따로 팔림) — 정산은 진입(③)이 먼저라 합쳐진다
                added = sum(f["qty"] for oid, base in self.base_of.items() if base == pid
                            for f in (self.doc["orders"].get(oid) or {}).get("fills") or [])
                self._open_exit(pid, code=p["code"], name=p.get("name"), side=p["side"], qty=p["qty"] - trimmed + added,
                                price=hit[0], kind=hit[1], ts=ts)
                self.dirty = True
                for oid, base in self.base_of.items():      # 본 포지션 손절 → 그 잔여분 추가 주문도 중단
                    r = self.doc["orders"].get(oid)
                    if base == pid and r and not r["done"]:
                        r.update(done=True, why="본 포지션 손절 — 남은 분할 취소")

    # ── 현금·스위칭 ─────────────────────────────────────────────────────────
    def cash(self):
        """지금 쓸 수 있는 현금 = 세션 시작 가용 현금 + 오늘 Long 매도 대금(세금 뺀) − 오늘 Long 매수 대금."""
        raised = sum(f["qty"] * f["price"] * (1 - SELL_TAX) for r in self.doc["exits"].values()
                     if r.get("side", "long") == "long" for f in r.get("fills") or [])
        spent = sum(f["qty"] * f["price"] for r in self.doc["orders"].values()
                    if r.get("side", "long") == "long" for f in r.get("fills") or [])
        return (self.doc["switch"].get("cashStart") or 0.0) + raised - spent

    def _afford(self, px):
        return max(0, int(math.floor(self.cash() / px))) if px > 0 else 0

    def _switch(self, ts, hm, requests):
        """적시 스위칭 — 막힌 매수의 부족 금액만큼 보유 Long 을 판다. 반환 True = 하나라도 팔았음."""
        if not (C.ENTRY_START <= hm < C.ENTRY_END):
            return False                                         # 시가(동시호가 직후)에는 재원 매도 안 함
        need = sum(q * px for _r, _st, q, px in requests) - max(0.0, self.cash())
        if need <= 0:
            return False
        net = 1 - SELL_TAX
        sw = self.doc["switch"]
        sw["requests"] = (sw.get("requests") or 0) + 1
        buying = {r["code"] for r in self.doc["orders"].values() if not r["done"]}
        groups = {"A": [], "B": [], "C": [], "D": []}
        for pid, p in self.pool.items():
            if pid in self.doc["exits"] or p["code"] in buying:
                continue                                         # 이미 전량 청산 중이거나, 오늘 그 종목을 사는 중(잔여분 추가)
            st = self.states.get(p["code"])
            if not st or not st.last:
                continue
            rem = p["qty"] - sum(filled(r) for r in self._trims(pid))
            if rem > 0:
                ret = st.last.price / p["entry"] - 1
                tier = _tier(p, st, ret)
                if tier:
                    groups[tier].append((ret, pid, p, st, rem))
        groups["A"].sort(key=lambda c: -_stop_progress(c[2], c[3].last.price))
        for tier in ("B", "C", "D"):
            groups[tier].sort(key=lambda c: c[0])
        order = [("A", c) for c in groups["A"]] + [("B", c) for c in groups["B"]] + [("C", c) for c in groups["C"]]
        if any(r.get("rating") == "Buy" for r, _st, _q, _px in requests):
            order += [("D", c) for c in groups["D"]]
        sold = False
        for tier, (ret, pid, p, st, rem) in order:
            if need <= 0:
                break
            px = st.last.price
            q = min(rem, int(math.ceil(need / (px * net))))
            if tier == "D":                                      # 승자는 PM 비중을 넘은 만큼만
                q = min(q, int(math.floor(max(0.0, rem * px - self._pm_weight(p) * self.equity) / px)))
            if q <= 0:
                continue
            self._switch_fill(pid, p, q, px, ts, "sw" + tier, st, ret)
            need -= q * px * net
            sold = True
        if need > 0:
            sw["unmet"] = (sw.get("unmet") or 0) + 1             # 팔 수 있는 재원이 없어 매수가 계속 막힘
        return sold

    def _pm_weight(self, p):
        """PM 목표 비중 — 포지션에 없으면(2026-11-03 이전 진입) 진입 판단 문서에서. 모르면 1.0(초과분 없음 = 팔지 않음)."""
        w = p.get("weight")
        if w is None:
            d = self.store.get(p["decisionKey"]) if p.get("decisionKey") else None
            w = (d or {}).get("pm_weight") or (d or {}).get("weight")
            p["weight"] = w = min(w, MAX_WEIGHT) if w else 1.0
        return w

    def _switch_fill(self, pid, p, q, px, ts, mode, st, ret):
        r = self.doc["exits"].setdefault("s:" + pid, {
            "code": p["code"], "name": p.get("name"), "side": "long", "trigger": "switch", "kind": "switch",
            "positionId": pid, "planned": 0, "plan": [], "next": 0, "ref": None, "fills": [], "done": False, "why": None,
            "triggerTs": ts})
        r["planned"] += q
        self._fill(r, q, px, ts, mode, st.vwap)
        self.log(f"[vwap] {ts[11:19]} 스위칭 재원 매도 {p['code']} {p.get('name') or ''} {q:,}주 @ {px:,.0f} "
                 f"({mode}, 진입가 대비 {ret * 100:+.1f}%)")

    # ── Live PM 지시 ───────────────────────────────────────────────────────
    def _trims(self, pid):
        """그 포지션의 부분 청산 기록 — Live PM trim("i:")·스위칭 재원("s:")."""
        return [r for k, r in self.doc["exits"].items() if k[:2] in ("i:", "s:") and r.get("positionId") == pid]

    def _close_trims(self, pid, why):
        """그 포지션의 부분 청산(trim·스위칭)을 끝내고 지금까지 체결된 수량 합을 돌려준다."""
        n = 0
        for r in self._trims(pid):
            n += filled(r)
            if not r["done"]:
                r.update(done=True, why=why)
        return n

    def apply_instructions(self, instructions, ts):
        """Live PM 지시 목록(livepm/<date>/instructions) 중 아직 처리 안 한 것을 집행. 반환 처리한 개수."""
        n = 0
        for ins in instructions or []:
            iid = ins.get("id")
            if not iid or iid in self.doc["instructions"]:
                continue
            ok, why = self._apply(ins, ts)
            self.doc["instructions"][iid] = {"ts": ts, "status": "done" if ok else "rejected", "why": why,
                                             "action": ins.get("action"), "code": ins.get("code")}
            self.dirty = True
            n += 1
            self.log(f"[vwap] {ts[11:19]} Live PM 지시 {iid} {ins.get('action')} {ins.get('code')} → {'집행' if ok else '거부'}"
                     + (f" ({why})" if why else ""))
        return n

    def _apply(self, ins, ts):
        pid, act = ins.get("positionId"), ins.get("action")
        p = self.held.get(pid)
        if p is None:
            return False, "원장에 없는 포지션"
        side = p.get("side", "long")
        full = self.doc["exits"].get(pid)
        if act in ("exit", "trim", "tighten_stop") and full is not None:
            return False, f"이미 청산 중({full.get('trigger')})"
        if act == "exit":
            trimmed = self._close_trims(pid, "exit 으로 대체")
            qty = p["qty"] - trimmed
            if qty <= 0:
                return False, "남은 수량 없음"
            self.doc["exits"][pid] = self._livepm_slices(p, qty, "livepm_exit", ts, ins)
            return True, None
        if act == "trim":
            trimmed = sum(filled(r) for r in self._trims(pid))
            open_trim = sum(r["planned"] - filled(r) for r in self._trims(pid) if not r["done"] and r.get("trigger") != "switch")
            room = p["qty"] - trimmed - open_trim
            qty = min(room, max(1, int(round(p["qty"] * float(ins.get("pct") or 0.5)))))
            if qty <= 0:
                return False, "남은 수량 없음"
            self.doc["exits"]["i:" + ins["id"]] = dict(self._livepm_slices(p, qty, "livepm_trim", ts, ins), positionId=pid)
            return True, None
        if act == "tighten_stop":
            w = self.positions.get(pid)
            ns = ins.get("newStop")
            if w is None or not isinstance(ns, (int, float)):
                return False, "손절 감시 대상 아님"
            if (ns >= w["stop"]) if side == "short" else (ns <= w["stop"]):
                return False, "유리한 방향 아님"
            self.doc["stopMoves"][pid] = {"ts": ts, "old": w["stop"], "newStop": float(ns)}
            w["stop"] = float(ns)
            return True, None
        if act == "flag_review":
            self.doc["reviews"][pid] = {"ts": ts, "reason": ins.get("reason")}
            return True, None
        return False, f"모르는 지시 {act}"

    def _livepm_slices(self, p, qty, kind, ts, ins):
        return {"code": p["code"], "name": p.get("name"), "side": p.get("side", "long"), "trigger": "livepm", "kind": kind,
                "planned": qty, "plan": slice_plan(qty), "next": 0, "ref": None, "fills": [], "done": False, "why": None,
                "triggerTs": ts, "instruction": ins.get("id")}

    def _entry(self, oid, r, st, ts, hm):
        if r["fills"]:
            hit = self._stop_hit(st, r["side"], r["stop"], first=False)
            if hit and swing_config.FILL_DAY_STOP == "review" and not r.get("topupOf"):
                # 체결일 손절 유예(2026-10-16 사용자 지시, 11-04 두산 — 실행기가 이 규칙을 앞질러 당일 손절하던 문제):
                # 남은 분할만 멈추고 체결분은 그대로 둔다 → 정산이 fillStop 표시 → D+1 손절 유예·종가 재판별 → 매도면 D+2 시가
                r.update(done=True, why="체결 후 손절가 도달 — 남은 분할 취소, 체결분은 체결일 손절 유예(내일 종가 재판별)",
                         fillStop={"ts": ts, "price": hit[0]})
                self.dirty = True
                return
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

    def _ask_cash(self, r, st, q, px):
        """현금 때문에 막힌 매수 등록 — 적시 스위칭이 이 금액만큼 판다."""
        if q > 0 and getattr(self, "_requests", None) is not None:
            self._requests.append((r, st, q, px))

    def _advance(self, r, st, ts, hm, kind, retry=False):
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

        def in_cap(px):                                   # 진입은 진입가 상한 안쪽에서만(Long 이하·Short 이상)
            return not entry or (px <= r["cap"] if long_ else px >= r["cap"])

        if r["next"] == 0 and r.get("trigger") == "livepm":   # Live PM 지시 → 첫 10% 는 지금 가격
            r.update(ref=t.price, next=1)
            self._fill(r, r["plan"][0], t.price, ts, "livepm", st.vwap)
            self.log(f"[vwap] {ts[11:19]} 지시 청산 {r['code']} {r['side']} {r['plan'][0]:,}주 @ {t.price:,.0f} ({r['kind']})")
            if filled(r) >= r["planned"]:
                r.update(done=True, why="청산 완료")
            return
        if r["next"] == 0:                               # ① 시가 첫 분할
            if t.open is None or t.volume <= 0:
                return
            r["ref"] = t.open
            r["next"] = 1
            q0 = r["plan"][0]
            ok = inside(t.open) and in_cap(t.open)            # 시가가 손절·진입가 상한 밖이면 첫 분할도 체결하지 않는다(2026-11-03)
            if ok and entry and long_:
                q0 = min(q0, self._afford(t.open))                    # Long 매수는 현금 한도 안에서만(시가엔 재원 매도 없음)
                if q0 < r["plan"][0] and len(r["plan"]) > 1:
                    r["plan"][1] += r["plan"][0] - q0
                    r["plan"][0] = q0
            if ok and q0 > 0:
                self._fill(r, q0, t.open, ts, "open", st.vwap)
                self.log(f"[vwap] {ts[11:19]} 시가 {'진입' if entry else '청산'} {r['code']} {r['side']} {q0:,}주 @ {t.open:,.0f}")
            elif ok:
                r["why"] = "현금 부족 — 시가 체결 생략"
            else:
                if len(r["plan"]) > 1:                        # 첫 분할을 다음 분할에 합침
                    r["plan"][1] += r["plan"][0]
                else:
                    r["plan"].append(r["plan"][0])
                r["plan"][0] = 0
                r["why"] = "시가가 " + ("손절가" if not inside(t.open) else "진입가 상한") + " 너머 — 시가 체결 생략"
            return
        px = t.price
        # ③ 기다리면 손해인 방향으로 기준가 대비 5% 이상 → 전량(2026-10-18): Long 진입·Short 청산 = 상승, Short 진입·Long 청산 = 하락
        away = 1 if entry == long_ else -1
        if r["ref"] and away * (px / r["ref"] - 1) >= C.FORCE_MOVE and inside(px) and in_cap(px):
            q = min(remain, self._afford(px)) if entry and long_ else remain
            if entry and long_ and q < remain:
                self._ask_cash(r, st, remain - q, px)            # 모자란 만큼 적시 스위칭
            if q <= 0:
                return
            self._fill(r, q, px, ts, "force", st.vwap)
            if q >= remain:
                r.update(done=True, why=f"기준가 {r['ref']:,.0f} 대비 {px / r['ref'] - 1:+.1%} — 남은 수량 전량")
            self.log(f"[vwap] {ts[11:19]} 전량 {'진입' if entry else '청산'} {r['code']} {r['side']} {q:,}주 @ {px:,.0f}"
                     f" (기준가 대비 {px / r['ref'] - 1:+.1%})")
            return
        if not (C.ENTRY_START <= hm < C.ENTRY_END) or st.n_obs < C.MIN_OBS or st.vwap is None:
            return
        vw = [f for f in r["fills"] if f["mode"] == "vwap"]
        if vw and _minutes(vw[-1]["ts"], ts) < C.MIN_GAP_MIN and not (retry and vw[-1]["ts"] == ts):
            return                                               # 간격은 VWAP 분할끼리만(같은 폴링 재시도는 이어서)
        if entry:
            ok = (px <= r["cap"] and px <= st.vwap) if long_ else (px >= r["cap"] and px >= st.vwap)
            ok = ok and inside(px)
        else:
            ok = px >= st.vwap if long_ else px <= st.vwap
        if not ok or r["next"] >= len(r["plan"]):
            return
        q = min(r["plan"][r["next"]], remain)
        if entry and long_:
            qa = min(q, self._afford(px))                         # 현금 한도 — 모자라면 이 분할을 줄여 체결하고 나머지는 남긴다
            if qa < q and not retry:
                self._ask_cash(r, st, q - qa, px)                 # 모자란 만큼 적시 스위칭
            if qa <= 0:
                return
            if qa < q:
                r["plan"][r["next"]] -= qa
                q = qa
            else:
                r["next"] += 1
        else:
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
            if not r["done"] and r.get("trigger") == "switch":
                r.update(done=True, why=f"스위칭 재원 {filled(r):,}주")
            elif not r["done"]:
                r.update(done=True, why=r["why"] or f"{filled(r):,}/{r['planned']:,}주 청산 — 나머지 다음 영업일 계속")
        self.doc["switch"]["cashEnd"] = self.cash()
        self.dirty = True

    def summary(self):
        o, x = self.doc["orders"].values(), self.doc["exits"].values()
        ins = self.doc["instructions"].values()
        return {"orders": len(self.doc["orders"]), "filledOrders": sum(1 for r in o if r["fills"]),
                "entryFills": sum(len(r["fills"]) for r in o), "exits": len(self.doc["exits"]),
                "stopExits": sum(1 for r in x if r["trigger"] == "stop"), "exitFills": sum(len(r["fills"]) for r in x),
                "watchedPositions": len(self.positions), "complete": self.doc["complete"], "asof": self.doc["asof"],
                "instructions": sum(1 for i in ins if i["status"] == "done"),
                "rejected": sum(1 for i in ins if i["status"] != "done")}

    def save(self, ticks=False):
        self.store.put(f"intraday/{self.date}", self.doc)
        self.dirty = False
        if ticks and self.save_ticks:
            for code, ts in self.ticks.items():
                self.store.put(f"intraday/{self.date}/ticks/{code}", ts)
