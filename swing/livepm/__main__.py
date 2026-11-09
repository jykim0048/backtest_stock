"""swing-livepm 진입점(PLAN_PM.md).

  python -m swing.livepm --live
      # 상시 장중 루프(M3a, 2026-11-09) — 영업일 08:55~15:30 Redis 시세 버스 구독 → 판단 → livepm/<date>/instructions.
      # 변수 DATABASE_URL · REDIS_URL(또는 LIVETRADER_REDIS_URL) · FLOW_API_BASE · ANTHROPIC_API_KEY · GEMINI_API_KEY.

  python -m swing.livepm --replay 2026-10-20 --source file:<dir> --store file:<dir> [--agent mock|llm]
      # source 의 ledger·intraday/<date>(Live Trader 기록)·intraday/<date>/ticks/* 로 하루를 다시 돌려
      # store 의 livepm/<date>/… 에 판단·지시를 쓴다. 기본 mock(네트워크·API 키 불필요).
"""
import argparse

from .. import store as S
from ..intraday.feed import ReplayFeed
from ..vwap.state import VwapState
from . import agent as A
from .runner import DayRunner, intraday_at


def _store(spec):
    if spec and spec.startswith("file:"):
        return S.FileStore(spec[5:])
    return S.from_env()


def entries_from_ticks(ticks):
    """저장된 틱 → 버스 항목 형식 [{ts, ticks, vwap}] (VWAP 은 VwapState 로 다시 계산)."""
    states, out = {}, []
    for ts, batch in ReplayFeed(ticks).batches():
        vw = {}
        for t in batch:
            st = states.setdefault(t.code, VwapState(t.code))
            st.update(t)
            vw[t.code] = {"vwap": st.vwap, "sigma": st.sigma, "src": st.vwap_source}
        out.append({"ts": ts, "ticks": batch, "vwap": vw})
    return out


def replay(source, target, date, agent=None, log=print):
    ticks = []
    for k in source.keys(f"intraday/{date}/ticks/"):
        ticks += source.get(k) or []
    if not ticks:
        log(f"[livepm] {date} 저장된 틱 없음")
        return None
    intraday = source.get(f"intraday/{date}")
    r = DayRunner(target, date, source.get("ledger"), agent, intraday_fn=lambda ts: intraday_at(intraday, ts), log=log,
                  resume=False)
    for e in entries_from_ticks(ticks):
        r.on_entry(e)
    r.save()
    log(f"[livepm] 재생 {date}: {r.summary()}")
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live PM(PLAN_PM.md) — 장중 루프(--live) · 재생(--replay)")
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--replay", metavar="DATE")
    ap.add_argument("--source", default="env")
    ap.add_argument("--store", default="env")
    ap.add_argument("--agent", default="mock", choices=("mock", "llm"))
    a = ap.parse_args(argv)
    if a.live:
        from ..intraday.bus import BusFeed
        from .live import main_loop
        main_loop(_store(None if a.store == "env" else a.store), BusFeed(), log=lambda m: print(m, flush=True))
        return
    if not a.replay:
        print("[livepm] --live(장중 루프) 또는 --replay DATE 를 지정하세요", flush=True)
        return
    agent = A.LLMAgent() if a.agent == "llm" else A.MockLivePM()
    replay(_store(None if a.source == "env" else a.source), _store(None if a.store == "env" else a.store), a.replay, agent)


if __name__ == "__main__":
    main()
