"""swing-cron 진입점 — 평일 16:30 KST.

  python -m swing.run_daily                         # 오늘(KST), 실서비스 설정
  python -m swing.run_daily --date 2026-09-28
  python -m swing.run_daily --from 2026-09-07 --to 2026-09-28 --wb-dir swing/data/wb_history
  python -m swing.run_daily --agent mock --prices mock --store file:swing/data/local  # 오프라인
  python -m swing.run_daily --universe-only                                        # 유니버스만 다시 산출
  python -m swing.run_daily --date 2026-10-16 --redo                               # 판단 실패만 재분석(swing/redo.py)

하루 처리 안에서 업종별 종목 수급 모멘텀 유니버스(swing/universe.py)를 산출(universe/<date>)하고, 매트릭스 후보에
같은 모멘텀 점수를 매기며 유니버스 후보도 독립 소스로 trading_agent 에 넘긴다(daily.run_day 5).
SWING_UNIVERSE=0 또는 --universe off 면 전부 생략, --universe mock 은 오프라인 mock. 산출 실패는 노트만 남는다.

--agent trading_agent = swing/agent(homework trading_agent 헤드리스 이식, HANDOFF_P1).
"""
import argparse
import datetime
import json
import os
import sys
import time
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from swing import agent_iface, config, daily, prices, redo, store, tradedays, universe  # noqa: E402

KST = datetime.timezone(datetime.timedelta(hours=9))


def load_wb(date, wb_dir=None):
    """wb_dir 가 있으면 <dir>/<date>.json(백필), 없으면 main raw 최신본(오늘 런)."""
    if wb_dir:
        p = os.path.join(wb_dir, f"{date}.json")
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    url = f"{config.MAIN_RAW_BASE}/public/weekly_briefing.json?t={int(datetime.datetime.now().timestamp())}"
    req = urllib.request.Request(url, headers={"User-Agent": "swing-cron", "Cache-Control": "no-cache"})
    tok = os.environ.get("GH_RAW_TOKEN")
    if tok:
        req.add_header("Authorization", f"token {tok}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def make_agent(kind, store=None):
    if kind == "mock":
        return agent_iface.MockAgent()
    if kind == "trading_agent":
        from swing.agent.pipeline import TradingAgent
        return TradingAgent(store=store)                  # store = 해외 peer 캐시(peers/dynamic)
    raise SystemExit(f"알 수 없는 agent: {kind}")


def make_prices(kind):
    return {"mock": prices.MockPrices, "yfinance": prices.YFinancePrices,
            "dbhub": prices.DbHubPrices}[kind]()


def make_store(spec):
    if spec == "env":
        return store.from_env()
    if spec.startswith("file:"):
        return store.FileStore(spec[5:])
    raise SystemExit(f"알 수 없는 store: {spec}")


def main(argv=None):
    for _s in (sys.stdout, sys.stderr):                  # Windows 콘솔(cp949)에서 한글 로그 깨짐 방지
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--from", dest="start")
    ap.add_argument("--to", dest="end")
    ap.add_argument("--wb-dir")
    ap.add_argument("--agent", default=os.environ.get("SWING_AGENT", "trading_agent"))
    ap.add_argument("--prices", default=os.environ.get("SWING_PRICES", "yfinance"))
    ap.add_argument("--store", default=os.environ.get("SWING_STORE", "env"))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--universe", default=os.environ.get("SWING_UNIVERSE_SOURCE", "live"),
                    help="live(DB증권+KIS 허브) | mock(오프라인) | off")
    ap.add_argument("--universe-only", action="store_true", help="하루 처리 없이 유니버스만 산출")
    ap.add_argument("--redo", action="store_true", help="하루 처리 없이 그날 판단 실패 후보·매도 검토만 재분석")
    a = ap.parse_args(argv)

    if a.start:
        dates = tradedays.trading_days(a.start, a.end or a.start)
    else:
        dates = [a.date or datetime.datetime.now(KST).date().isoformat()]
    st = make_store(a.store)
    ag, px = make_agent(a.agent, st), make_prices(a.prices)
    rc = 0
    for d in dates:
        try:
            wb = load_wb(d, a.wb_dir)
        except Exception as ex:
            print(f"[swing] {d} 주간 브리핑 로드 실패: {ex}", file=sys.stderr)
            wb = None
        uni = _runner(st, a)
        if a.redo:
            r = redo.redo_failed(d, st, ag, px, uni=uni, cat=_cat_runner(st, a))
            print(f"[swing] {d} 재분석: {r}")
            continue
        if a.universe_only:
            if uni is None:
                print(f"[swing] {d} 유니버스 꺼짐(--universe off / SWING_UNIVERSE=0)")
                continue
            try:
                r = uni.build_or_load(d, wb, st.get("ledger"))
                print(f"[swing] {d} 유니버스 {r['summary']} {r['diag']['secs']}s")
            except Exception as ex:
                print(f"[swing] {d} 유니버스 실패: {type(ex).__name__}: {ex}", file=sys.stderr)
                rc = 1
            continue
        try:
            run = daily.run_day(d, st, ag, px, wb, force=a.force, uni=uni, cat=_cat_runner(st, a))
        except Exception as ex:
            # 진행률이 "실행 중"으로 남지 않게 실패로 닫고 다시 올린다(원장은 저장되지 않음)
            p = st.get("progress") or {}
            if p.get("date") == d and p.get("status") == "running":
                p.update(status="failed", error=f"{type(ex).__name__}: {ex}"[:300],
                         updatedAt=datetime.datetime.now().isoformat(timespec="seconds"))
                st.put("progress", p)
            raise
        if run.get("skip"):
            print(f"[swing] {d} 건너뜀: {run['skip']}")
        for n in run.get("notes", []):
            print(f"[swing] {d} {n}")
        # 판단 실패 자동 재분석 1회(2026-10-18) — LLM 1순위 장애·폴백 한도 소진은 보통 수~수십 분 뒤 풀린다.
        # 여러 날 백필(--from)에서는 생략(다음 날 처리 전에 기다리지 않음).
        nfail = 0 if run.get("skip") else redo.count_failed(run)
        if nfail and config.REDO_WAIT_S > 0 and not a.start:
            print(f"[swing] {d} 판단 실패 {nfail}건 — {config.REDO_WAIT_S}초 뒤 재분석")
            time.sleep(config.REDO_WAIT_S)
            try:
                print(f"[swing] {d} 재분석: {redo.redo_failed(d, st, ag, px, uni=uni, cat=_cat_runner(st, a))}")
            except Exception as ex:
                print(f"[swing] {d} 재분석 실패: {type(ex).__name__}: {ex}", file=sys.stderr)
    return rc


def _cat_runner(st, a):
    """하방 촉매 Short 소스(2026-10-15) — SWING_CAT_SHORT=0 이거나 mock 유니버스(오프라인)면 None."""
    if not config.CAT_SHORT_ENABLED or getattr(a, "universe", "") == "mock":
        return None
    from swing import catalysts
    return catalysts.CatalystRunner(st)


def _runner(st, a):
    """유니버스 어댑터 — off 면 None(run_day 가 유니버스 단계 생략). live 는 DB증권·KIS 허브, mock 은 오프라인."""
    if a.universe == "off" or not config.UNIVERSE_ENABLED:
        return None
    force = a.force or a.universe_only
    if a.universe == "mock":
        from swing import universe_mock
        secs = universe_mock.load_sectors()
        caps = {x["code"]: x["cap"] for s_ in secs for x in s_["stocks"]}
        return universe.UniverseRunner(st, universe_mock.MockDbQuote(secs), universe_mock.MockHubFlow(),
                                       caps_loader=lambda d: caps, force=force)
    return universe.UniverseRunner(st, force=force)


if __name__ == "__main__":
    sys.exit(main())
