"""swing-vwap 진입점 — Live Trader(장중 집행기)(PLAN_LIVE_TRADER.md).

  python -m swing.vwap                 # 상시: 영업일 09:00~15:35 폴링, 그 외 다음 세션까지 대기
  python -m swing.vwap --once          # 오늘 세션 하나만
  python -m swing.vwap --replay 2026-10-20 --source file:<dir> --store file:<dir>
      # source 의 ledger·intraday/<date>/ticks/* 로 실행기를 다시 돌려 store 의 intraday/<date> 에 쓴다(검증용)

저장소 = store.from_env()(swing 과 같은 DB). swing 원장은 읽기만 하고, 결과는 intraday/<date> 에 쓴다.
시세는 REDIS_URL 이 있으면 Redis 시세 버스(swing/intraday/bus.py, livetrader:*)로도 발행해 Live PM 이 구독한다.
"""
import argparse
import datetime
import time

from .. import store as S
from .. import tradedays
from ..intraday.bus import PriceBus
from ..intraday.feed import KST, PollingFeed, ReplayFeed, now_kst
from . import config as C
from .executor import Executor


def _store(spec):
    if spec and spec.startswith("file:"):
        return S.FileStore(spec[5:])
    return S.from_env()


def _at(date, hm):
    h, m = map(int, hm.split(":"))
    return datetime.datetime.fromisoformat(date).replace(hour=h, minute=m, tzinfo=KST)


def replay(source, target, date, log=print):
    ticks = []
    for k in source.keys(f"intraday/{date}/ticks/"):
        ticks += source.get(k) or []
    if not ticks:
        log(f"[vwap] {date} 저장된 틱 없음")
        return None
    ex = Executor(target, date, source.get("ledger"), log=log, save_ticks=False)
    for ts, batch in ReplayFeed(ticks).batches():
        ex.on_batch(ts, batch)
    ex.finish()
    ex.save()
    log(f"[vwap] 재생 {date}: {ex.summary()}")
    return ex


def run_session(st, date, *, feed=None, bus=None, clock=now_kst, sleep=time.sleep, log=print):
    """bus = 시세 버스(Redis) 발행기 — Live PM 이 구독한다. 실패해도 집행은 계속(PriceBus 가 예외를 삼킴)."""
    feed = feed or PollingFeed(log=log)
    bus = bus if bus is not None else PriceBus.from_env(log=log)
    start, end = _at(date, C.SESSION_START), _at(date, C.SESSION_END)
    while clock() < start:
        sleep(min(60, max(1, (start - clock()).total_seconds())))
    ex = Executor(st, date, st.get("ledger"), log=log)
    ex.save()
    log(f"[vwap] {date} 세션 시작 — 진입 주문 {len(ex.doc['orders'])} · 손절 감시 {len(ex.positions)}")
    last_save = last_ticks = time.monotonic()
    last_warn = 0.0
    while clock() < end:
        mono = time.monotonic()
        ex.apply_instructions(st.get(f"livepm/{date}/instructions"), clock().replace(microsecond=0).isoformat())
        codes = ex.codes()
        if codes:
            ticks = feed.poll(codes)
            if ticks:
                ex.on_batch(ticks[0].ts, ticks)
                bus.publish(date, ticks[0].ts, ticks, ex.states)
        bus.heartbeat(date, clock().replace(microsecond=0).isoformat(), codes)
        now_ts = clock()
        if codes and now_ts >= start + datetime.timedelta(minutes=5) and mono - last_warn >= 300:
            ok = feed.last_ok if hasattr(feed, "last_ok") else None
            age = (now_ts - datetime.datetime.fromisoformat(ok)).total_seconds() if ok else None
            if age is None or age >= 300:
                log(f"[vwap] 경고: 시세 {'한 번도 못 받음' if age is None else f'{age / 60:.0f}분째 없음'} — 집행·손절 멈춤, 정산은 일봉 규칙으로 폴백")
                last_warn = mono
        tick_due = mono - last_ticks >= C.TICK_SAVE_S
        if ex.dirty or mono - last_save >= C.SAVE_S or tick_due:
            ex.save(ticks=tick_due)
            last_save = mono
            if tick_due:
                last_ticks = mono
        sleep(max(1.0, C.POLL_S - (time.monotonic() - mono)))
    ex.finish()
    ex.save(ticks=True)
    log(f"[vwap] {date} 세션 종료 — {ex.summary()}")
    return ex


def main(argv=None):
    ap = argparse.ArgumentParser(description="swing Live Trader(장중 집행기)(PLAN_LIVE_TRADER.md)")
    ap.add_argument("--replay", metavar="DATE")
    ap.add_argument("--source", default="env")
    ap.add_argument("--store", default="env")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    st = _store(None if a.store == "env" else a.store)
    if a.replay:
        replay(_store(None if a.source == "env" else a.source), st, a.replay)
        return
    while True:
        today = now_kst().date().isoformat()
        if tradedays.is_trading_day(today) and now_kst() < _at(today, C.SESSION_END):
            run_session(st, today)
            if a.once:
                return
        nxt = tradedays.next_trading_day(today)
        wake = _at(nxt, C.SESSION_START) - datetime.timedelta(minutes=5)
        print(f"[vwap] 다음 세션 {nxt} — {wake.isoformat()} 까지 대기", flush=True)
        while now_kst() < wake:
            time.sleep(min(600, max(1, (wake - now_kst()).total_seconds())))


if __name__ == "__main__":
    main()
