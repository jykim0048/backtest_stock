"""스냅샷(L4 입력) — 압축된 JSON 한 덩어리(PLAN_PM §5). 틱 원본은 넣지 않는다.

Live Trader 의 그날 체결·청산 현황(intraday/<date>)을 포지션마다 붙여, 이미 청산 중인 포지션을 LLM 이 알게 한다.
길이가 SNAPSHOT_MAX_CHARS 를 넘으면 트리거 안 걸린 포지션의 thesis·invalidation 부터 줄인다.
"""
import json

from . import config as C
from .triggers import sector_ranks


def _r(x, n=2):
    return None if x is None else round(x * 100, n)


def build(ts, state, triggers, book, *, status=None, intraday=None, memory=None, today=None):
    memory = memory or {}
    intraday = intraday or {}
    exits = intraday.get("exits") or {}
    up, down = sector_ranks(status)
    trig_pids = {t["pid"] for t in triggers if t.get("pid")}
    eq = book.equity()
    positions = []
    for pid, p in book.positions.items():
        m = book.metrics(pid)
        if m is None:
            continue
        x = exits.get(pid)
        row = {"id": pid, "code": p["code"], "name": p.get("name"), "side": p.get("side", "long"), "sector": p.get("sector"),
               "qty": p["qty"], "entry": round(p["entry"], 1), "stop": p["stop"], "target": p.get("target"),
               "heldDays": p.get("holdDay"), "weightPct": _r(m["weight"], 1),
               "px": m["px"], "vwap": None if m["vwap"] is None else round(m["vwap"], 1),
               "pnlPct": _r(m["pnlPct"]), "stopDistUsedPct": round(m["stopDistUsed"] * 100),
               "ret5m": _r(m["ret5m"]), "ret30m": _r(m["ret30m"]), "retDay": _r(m["retDay"]),
               "thesis": p.get("thesis"), "invalidation": p.get("invalidation"), "horizon": p.get("horizon"),
               "sellPending": (p.get("sellPending") or {}).get("reason"),
               "liveTrader": ({"exiting": x.get("trigger"), "filled": sum(f["qty"] for f in x.get("fills") or []),
                               "planned": x.get("planned")} if x else None),
               "lastLivePm": memory.get(pid), "triggered": pid in trig_pids}
        positions.append({k: v for k, v in row.items() if v is not None})
    ex = book.exposure()
    snap = {"asof": ts, "state": state, "trigger": [{k: t[k] for k in ("id", "code", "detail")} for t in triggers],
            "portfolio": {"equity": round(eq), "cash": round(book.L.get("cash", 0)), "dayPnlPct": _r(book.day_pnl()),
                          "longGrossPct": _r(ex["longGross"], 1), "shortGrossPct": _r(ex["shortGross"], 1),
                          "sectorWeights": ex["sectorWeights"]},
            "positions": positions,
            "market": {"sectorsUp": up, "sectorsDown": down},
            "today": today or {}}
    text = json.dumps(snap, ensure_ascii=False)
    for key in ("invalidation", "thesis", "horizon"):
        if len(text) <= C.SNAPSHOT_MAX_CHARS:
            break
        for row in snap["positions"]:
            if not row.get("triggered"):
                row.pop(key, None)
        text = json.dumps(snap, ensure_ascii=False)
    return snap, text
