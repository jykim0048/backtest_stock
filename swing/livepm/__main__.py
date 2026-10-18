"""swing-livepm 진입점 — M1: 저장된 하루 재생만. 장중 루프(버스 구독·LLM)는 M3(PLAN_PM.md).

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
    r = DayRunner(target, date, source.get("ledger"), agent, intraday_fn=lambda ts: intraday_at(intraday, ts), log=log)
    for e in entries_from_ticks(ticks):
        r.on_entry(e)
    r.save()
    log(f"[livepm] 재생 {date}: {r.summary()}")
    return r


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live PM(PLAN_PM.md) — M1 재생")
    ap.add_argument("--replay", metavar="DATE")
    ap.add_argument("--source", default="env")
    ap.add_argument("--store", default="env")
    ap.add_argument("--agent", default="mock", choices=("mock", "llm"))
    a = ap.parse_args(argv)
    if not a.replay:
        print("[livepm] 장중 루프는 M3 에서 구현 — 지금은 --replay 만 지원", flush=True)
        return
    agent = A.LLMAgent() if a.agent == "llm" else A.MockLivePM()
    replay(_store(None if a.source == "env" else a.source), _store(None if a.store == "env" else a.store), a.replay, agent)


if __name__ == "__main__":
    main()
