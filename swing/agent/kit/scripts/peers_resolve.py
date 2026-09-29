"""해외 peer 확정 — 큐레이션 → 동적 캐시(Claude 제안·yfinance 검증) → 업종 기본표.

backtest_stock `generate_analysis.resolve_peers` 의 이식판. 원본은 LLM API(Gemini)가 peer 를
제안하고 yfinance 로 티커를 검증했다. 이 스킬은 LLM API 를 쓰지 않으므로 **제안은 Claude(메인
컨텍스트)가, 검증·저장은 이 스크립트가** 맡는다.

  python scripts/peers_resolve.py status  --code 005930
      → {"code","name","source": curated|dynamic|industry-default|none, "peers": [...],
         "industry": {...}, "needs_proposal": bool}
        needs_proposal 이 true 면 Claude 가 아래 규칙으로 4~5개를 제안해 propose 를 호출한다.

  python scripts/peers_resolve.py propose --code 005930 --file proposal.json   (또는 --json '<JSON>' / stdin)
      입력: {"peers": [{"name": "Micron Technology", "ticker": "MU", "note": "왜 peer 인가(한국어 한 줄)"}, ...]}
      → 형식 검증(한국 상장 제외·중복 제거) → yfinance 5일 시세로 실재 확인 → memory/peers_dynamic.json 저장
      → {"code","valid":[...],"rejected":[{"ticker","reason"}],"saved": bool}

제안 규칙(원본 PEER_SYSTEM 과 동일 + 벨웨더):
  - 한국 종목과 **사업이 가장 유사한 해외 상장** 비교기업 4~5개. 한국 상장사 제외.
  - ticker 는 Yahoo Finance 심볼(미국 AAPL, 일본 6479.T, 대만 3008.TW, 홍콩 2382.HK, 독일 ENR.DE, 영국 BAB.L).
  - **앞 2개는 미국 상장(접미사 없는 심볼) 벨웨더** — StockTwits·Reddit 수집 대상이 된다.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import ASSETS_DIR, MEMORY_DIR, read_json, warn, write_json  # noqa: E402

DYNAMIC_PATH = MEMORY_DIR / "peers_dynamic.json"
_KR_SUFFIX = re.compile(r"\.(KS|KQ|KN)$", re.I)
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-=^]{0,14}$")


# ----------------------------------------------------------------------------
# 순수 함수 (오프라인 테스트 대상)
# ----------------------------------------------------------------------------
def validate_proposal(items) -> tuple[list, list]:
    """형식 검증 → (accepted, rejected). 순서 유지, 티커 대문자화, 중복·한국 상장·빈 티커 제거."""
    accepted, rejected, seen = [], [], set()
    for p in items or []:
        if not isinstance(p, dict):
            rejected.append({"ticker": str(p)[:30], "reason": "객체 아님"})
            continue
        tk = (p.get("ticker") or "").strip().upper()
        name = (p.get("name") or "").strip()
        if not tk:
            rejected.append({"ticker": "", "reason": "ticker 없음"})
            continue
        if _KR_SUFFIX.search(tk):
            rejected.append({"ticker": tk, "reason": "한국 상장사 제외"})
            continue
        if not _TICKER_RE.match(tk):
            rejected.append({"ticker": tk, "reason": "야후 심볼 형식 아님"})
            continue
        if tk in seen:
            rejected.append({"ticker": tk, "reason": "중복"})
            continue
        seen.add(tk)
        accepted.append({"name": name or tk, "ticker": tk, "note": (p.get("note") or "").strip()})
    return accepted, rejected


def is_us_symbol(ticker: str) -> bool:
    t = (ticker or "").strip().upper()
    return bool(t) and "." not in t and "=" not in t and "^" not in t


def order_bellwethers_first(peers: list) -> list:
    """미국 상장 심볼을 앞으로(상대 순서 유지). StockTwits·Reddit 은 앞 2개를 본다."""
    us = [p for p in peers if is_us_symbol(p.get("ticker"))]
    rest = [p for p in peers if not is_us_symbol(p.get("ticker"))]
    return us + rest


def load_dynamic() -> dict:
    d = read_json(DYNAMIC_PATH, {}) or {}
    return d if isinstance(d, dict) else {}


# ----------------------------------------------------------------------------
# 네트워크
# ----------------------------------------------------------------------------
def check_yfinance(peers: list, period: str = "5d") -> tuple[list, list]:
    """yfinance 로 시세가 조회되는 티커만 남긴다(환각 티커 제거). 다운로드 자체가 실패하면 과하게 거르지 않는다."""
    if not peers:
        return [], []
    import yfinance as yf  # noqa: WPS433
    import sources_kr as sources  # noqa: WPS433
    tickers = [p["ticker"] for p in peers]
    try:
        df = yf.download(tickers, period=period, group_by="ticker", progress=False,
                         threads=True, auto_adjust=True, timeout=15)
    except Exception as e:  # noqa: BLE001
        warn(f"yfinance 검증 실패(전부 유지): {e}")
        return list(peers), []
    keep, rejected = [], []
    for p in peers:
        ok = False
        try:
            tdf = sources._ticker_frame(df, p["ticker"])
            closes = tdf["Close"].dropna() if (tdf is not None and "Close" in tdf.columns) else None
            ok = closes is not None and len(closes) >= 1
        except Exception:  # noqa: BLE001
            ok = False
        (keep if ok else rejected).append(p if ok else {"ticker": p["ticker"], "reason": "yfinance 시세 없음"})
    return keep, rejected


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def cmd_status(code: str) -> dict:
    import sources_kr as sources  # noqa: WPS433
    from resolve import resolve  # noqa: WPS433
    stock = resolve(code) or {"code": code, "name": "", "ticker": ""}
    peer_cfg = read_json(ASSETS_DIR / "peers.json", {}) or {}
    industry_cfg = read_json(ASSETS_DIR / "industry_peers.json", {}) or {}
    profile = sources.yahoo_profile(stock.get("ticker") or "") if stock.get("ticker") else {}
    peers, source = sources.select_peers(code, profile.get("industryKey"), peer_cfg, industry_cfg,
                                         dynamic_cfg=load_dynamic())
    dyn = load_dynamic().get(code) or {}
    return {
        "code": code, "name": stock.get("name", ""), "ticker": stock.get("ticker", ""),
        "source": source, "peers": peers,
        "industry": {k: profile.get(k) for k in ("sector", "industry", "industryKey")},
        "dynamic_resolved_at": dyn.get("resolved_at"),
        "needs_proposal": source in ("industry-default", "none"),
        "hint": ("Claude 가 사업 유사 해외 상장 peer 4~5개(앞 2개는 미국 상장 벨웨더)를 제안해 "
                 "`peers_resolve.py propose --code <code> --file <json>` 로 검증·저장할 것") if source in ("industry-default", "none") else "",
    }


def cmd_propose(code: str, payload: dict) -> dict:
    from resolve import resolve  # noqa: WPS433
    items = payload.get("peers") if isinstance(payload, dict) else payload
    accepted, rejected = validate_proposal(items)
    valid, rej_net = check_yfinance(accepted)
    rejected += rej_net
    valid = order_bellwethers_first(valid)
    saved = False
    if valid:
        d = load_dynamic()
        stock = resolve(code) or {}
        d[code] = {"name": stock.get("name", ""), "resolved_at": _dt.datetime.now().strftime("%Y-%m-%d"),
                   "source": "claude-proposed", "peers": valid}
        write_json(DYNAMIC_PATH, d)
        saved = True
    return {"code": code, "valid": valid, "rejected": rejected, "saved": saved,
            "bellwethers": [p["ticker"] for p in valid[:2] if is_us_symbol(p["ticker"])]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status"); s.add_argument("--code", required=True)
    p = sub.add_parser("propose"); p.add_argument("--code", required=True)
    p.add_argument("--file"); p.add_argument("--json")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        out = cmd_status(a.code)
    else:
        if a.file:
            payload = json.loads(Path(a.file).read_text(encoding="utf-8"))
        elif a.json:
            payload = json.loads(a.json)
        else:
            payload = json.loads(sys.stdin.read())
        out = cmd_propose(a.code, payload)
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
