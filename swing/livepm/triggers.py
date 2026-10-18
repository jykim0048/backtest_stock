"""트리거(L3) — 언제 Live PM 판단(LLM)을 부를지(PLAN_PM §3).

  T1 손절선 근접(진입→손절 거리 STOP_NEAR 이상 소진, 아직 손절 전)   T2 목표가 도달
  T3 불리한 방향 5분 변동 JUMP_5M 이상                               T4 VI 발동(/status vi)
  T5 보유 업종이 KOSPI 업종 하락 상위 N(Long)·상승 상위 N(Short)      T6 포트폴리오 일중 손실 DD_DAY 이상
  T7 정기 점검(SCHEDULE 시각마다 1번)
같은 종목·같은 트리거는 DEBOUNCE_MIN 분 안에 다시 내지 않는다. 한 버스 항목(폴링 1회)에서 나온 트리거는 한 호출로 묶는다
(폴링 15초 > BATCH_WAIT_S 3초라 폴링 단위 묶음과 같다). 하루 호출 MAX_CALLS·비용 MAX_COST_KRW 를 넘으면 T7 만, 둘 다 넘으면 없음.
"""
import datetime
import re

from .. import engine
from . import config as C


def norm_sector(s):
    return re.sub(r"[\s·.,/()]|업$", "", s or "")


def sector_match(a, b):
    """KRX 업종명 ↔ KIS 업종명 — 정규화 후 같거나 한쪽이 다른 쪽으로 시작(예: '의료·정밀기' ↔ '의료·정밀기기')."""
    x, y = norm_sector(a), norm_sector(b)
    return bool(x and y) and len(min(x, y, key=len)) >= 2 and (x.startswith(y) or y.startswith(x))


def sector_ranks(status, n=None):
    n = n or C.SECTOR_TOP_N
    rows = [s for s in (status or {}).get("sectors") or [] if s.get("changePct") is not None]
    down = [s["name"] for s in sorted(rows, key=lambda s: s["changePct"])[:n]]
    up = [s["name"] for s in sorted(rows, key=lambda s: -s["changePct"])[:n]]
    return up, down


def _min(a, b):
    return (datetime.datetime.fromisoformat(b) - datetime.datetime.fromisoformat(a)).total_seconds() / 60


class Triggers:
    def __init__(self):
        self.last = {}            # (code, tid) → ts
        self.sched_done = set()
        self.calls = 0
        self.cost = 0.0

    def _fresh(self, key, ts):
        prev = self.last.get(key)
        return prev is None or _min(prev, ts) >= C.DEBOUNCE_MIN

    def scan(self, ts, book, status=None, exiting=()):
        """반환 [{id, code, pid, detail}] — 디바운스 통과분만, 기록까지 한다. exiting = Live Trader 가 청산 중인 포지션 id."""
        hm = ts[11:16]
        out = []
        vi_on = {v.get("code") for v in (status or {}).get("vi") or [] if v.get("vi_on")}
        up, down = sector_ranks(status)
        for pid, p in book.positions.items():
            if pid in exiting:
                continue
            m = book.metrics(pid)
            if m is None:
                continue
            code, short = p["code"], engine.is_short(p)
            cand = []
            if C.STOP_NEAR <= m["stopDistUsed"] < 1.0:
                cand.append(("T1", f"손절 거리 {m['stopDistUsed'] * 100:.0f}% 소진"))
            if m["targetHit"]:
                cand.append(("T2", f"목표가 {p['target']:,.0f} 도달"))
            if m["adv5m"] is not None and m["adv5m"] >= C.JUMP_5M:
                cand.append(("T3", f"5분 {m['ret5m'] * 100:+.1f}%"))
            if code in vi_on:
                cand.append(("T4", "VI 발동"))
            ranks = up if short else down
            hit = next((i for i, s in enumerate(ranks) if sector_match(p.get("sector"), s)), None)
            if hit is not None:
                cand.append(("T5", f"보유 업종 {'상승' if short else '하락'} 상위 {hit + 1}위({ranks[hit]})"))
            for tid, detail in cand:
                if self._fresh((code, tid), ts):
                    self.last[(code, tid)] = ts
                    out.append({"id": tid, "code": code, "pid": pid, "detail": detail})
        dp = book.day_pnl()
        if dp <= -C.DD_DAY and self._fresh(("*", "T6"), ts):
            self.last[("*", "T6")] = ts
            out.append({"id": "T6", "code": "*", "pid": None, "detail": f"포트폴리오 일중 {dp * 100:.2f}%"})
        for s in C.SCHEDULE:
            if hm >= s and s not in self.sched_done:
                self.sched_done.add(s)
                if hm < C.DECIDE_END:
                    out.append({"id": "T7", "code": "*", "pid": None, "detail": f"정기 점검 {s}"})
        return self.budget(out)

    def budget(self, trigs):
        over_calls, over_cost = self.calls >= C.MAX_CALLS, self.cost >= C.MAX_COST_KRW
        if over_calls and over_cost:
            return []
        if over_calls or over_cost:
            return [t for t in trigs if t["id"] == "T7"]
        return trigs

    def spend(self, cost_krw):
        self.calls += 1
        self.cost += cost_krw
