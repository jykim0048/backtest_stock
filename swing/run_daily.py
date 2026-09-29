"""swing-cron 진입점 — 평일 16:30 KST.

  python -m swing.run_daily                         # 오늘(KST), 실서비스 설정
  python -m swing.run_daily --date 2026-09-28
  python -m swing.run_daily --from 2026-09-07 --to 2026-09-28 --wb-dir swing/data/wb_history
  python -m swing.run_daily --agent mock --prices mock --store file:swing/data/local  # 오프라인

--agent trading_agent = swing/agent(homework trading_agent 헤드리스 이식, HANDOFF_P1).
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from swing import agent_iface, config, daily, prices, store, tradedays  # noqa: E402

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
        run = daily.run_day(d, st, ag, px, wb, force=a.force)
        if run.get("skip"):
            print(f"[swing] {d} 건너뜀: {run['skip']}")
        for n in run.get("notes", []):
            print(f"[swing] {d} {n}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
