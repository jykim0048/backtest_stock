"""Live PM 장중 루프(PLAN_PM M3a, 2026-11-09) — swing-livepm 서비스 본체.

  08:55 기동 → 원장 1회 읽기(Live Trader 와 같은 보유) → 오늘 버스 기록 따라잡기(시세만, 판단 없음) →
  루프: Redis 시세 버스 새 항목(BusFeed.poll, 최대 20초 대기) → DayRunner.on_entry(트리거 → 판단 → 게이트 → 지시).
  새 항목이 없을 때도 판단 시간(09:00~)엔 step() 으로 정기 점검(T7)·시세 끊김(X4)을 본다. 15:30 종료·요약 저장.
  /status(VI·KOSPI 업종)는 STATUS_POLL_S 초마다 직접 조회(실패하면 직전 값). 생존 신호 livepm/<date>/heartbeat(60초).
재시작 복구는 DayRunner(resume) — 같은 날 다시 떠도 디바운스·호출 수·비용·지시가 이어진다.
"""
import datetime
import json
import time
import urllib.request

from .. import config as swing_config
from ..intraday.feed import KST, now_kst
from . import agent as A
from . import config as C
from .runner import DayRunner


def at(date, hm):
    h, m = map(int, hm.split(":"))
    return datetime.datetime.fromisoformat(date).replace(hour=h, minute=m, tzinfo=KST)


class StatusFeed:
    """허브 /status(VI·업종) — every_s 초 캐시. get(url) → dict 를 주입하면 네트워크 없이 테스트."""

    def __init__(self, base=None, *, get=None, every_s=None, mono=time.monotonic, log=print):
        self.base = (base or swing_config.FLOW_API_BASE).rstrip("/")
        self._get = get or self._http
        self.every_s = C.STATUS_POLL_S if every_s is None else every_s
        self.mono, self.log = mono, log
        self._last, self._at, self._warned = None, None, 0.0

    @staticmethod
    def _http(url):
        with urllib.request.urlopen(url, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def __call__(self, ts=None):
        now = self.mono()
        if self._at is not None and now - self._at < self.every_s:
            return self._last
        self._at = now
        try:
            self._last = self._get(self.base + "/status")
        except Exception as ex:
            if now - self._warned >= 300:
                self.log(f"[livepm] /status 실패({type(ex).__name__}: {str(ex)[:80]}) — 직전 값 사용(T4·T5 영향)")
                self._warned = now
        return self._last


def run_session(store, date, *, bus, agent=None, status_fn=None, clock=now_kst, sleep=time.sleep,
                mono=time.monotonic, log=print, poll_ms=20000, heartbeat_s=60):
    """하루 세션. bus = BusFeed(history·poll·alive). 반환 DayRunner."""
    start, end, decide = at(date, C.SESSION_START), at(date, C.SESSION_END), at(date, C.DECIDE_START)
    while clock() < start:
        sleep(min(60, max(1, (start - clock()).total_seconds())))
    agent = agent or A.LLMAgent()
    r = DayRunner(store, date, store.get("ledger"), agent, status_fn=status_fn or StatusFeed(log=log), log=log)
    n = r.catch_up(bus.history(date))
    log(f"[livepm] {date} 세션 시작 — 보유 {len(r.book.positions)} · 버스 따라잡기 {n}건 · 모델 {C.LLM_CHAIN} · effort {C.LLM_EFFORT}")
    last_hb = None
    while clock() < end:
        entries = [e for e in (bus.poll(block_ms=poll_ms) or []) if e.get("date") in (None, date)]
        alive = bool(bus.alive())
        for e in entries:
            r.on_entry(e, trader_alive=alive, feed_age_s=0)
        now = clock().replace(microsecond=0)
        if not entries and now >= decide:
            asof = datetime.datetime.fromisoformat(r.book.asof) if r.book.asof else None
            age = (now - asof).total_seconds() if asof else None
            r.step(now.isoformat(), trader_alive=alive, feed_age_s=age)
        m = mono()
        if last_hb is None or m - last_hb >= heartbeat_s:
            store.put(f"livepm/{date}/heartbeat", {"ts": now.isoformat(), "state": r.state.state, "calls": r.trig.calls,
                                                   "costKrw": round(r.trig.cost, 1), "instructions": len(r.instructions),
                                                   "traderAlive": alive, "feedAsof": r.book.asof})
            last_hb = m
    r.save()
    log(f"[livepm] {date} 세션 종료 — {r.summary()}")
    return r


def main_loop(store, bus, *, log=print):
    """상시 — 영업일 세션, 그 외 다음 개장 5분 전까지 대기."""
    from .. import tradedays
    import llm
    if not llm.configured(C.LLM_CHAIN):
        log(f"[livepm] 경고: LLM 키 없음({C.LLM_CHAIN}) — 판단 실패 시 전부 hold")
    while True:
        today = now_kst().date().isoformat()
        if tradedays.is_trading_day(today) and now_kst() < at(today, C.SESSION_END):
            try:
                run_session(store, today, bus=bus, log=log)
            except Exception as ex:                      # 세션 하나가 죽어도 서비스는 살아 있게(재시도는 1분 뒤)
                log(f"[livepm] 세션 오류 {type(ex).__name__}: {str(ex)[:200]} — 1분 뒤 재시도")
                time.sleep(60)
                continue
        nxt = tradedays.next_trading_day(today)
        wake = at(nxt, C.SESSION_START) - datetime.timedelta(minutes=5)
        log(f"[livepm] 다음 세션 {nxt} — {wake.isoformat()} 까지 대기")
        while now_kst() < wake:
            time.sleep(min(600, max(5, (wake - now_kst()).total_seconds())))
