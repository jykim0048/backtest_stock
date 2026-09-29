"""종목 해석 — 이름/코드 → {code, name, market, ticker, corp_code}.

네트워크 없이 스킬 동봉 정적 마스터 두 개만 쓴다:
  assets/krx_companies.json   name/code/market/ticker(.KS/.KQ)
  assets/dart_corp_map.json   stock_code → DART corp_code

사용:  python resolve.py 삼성전자      /  python resolve.py 005930
출력:  JSON 한 줄(stdout). 못 찾으면 exit 2 와 후보 목록(stderr).
"""
from __future__ import annotations

import difflib
import json
import re
import sys

from _common import ASSETS_DIR, read_json

_CODE_RE = re.compile(r"^\d{6}$")
_ROWS = None


def _load():
    global _ROWS
    if _ROWS is not None:
        return _ROWS
    comps = read_json(ASSETS_DIR / "krx_companies.json", []) or []
    corp = read_json(ASSETS_DIR / "dart_corp_map.json", {}) or {}
    rows = []
    for c in comps:
        code = str(c.get("code") or "").zfill(6)
        tk = c.get("ticker") or ""
        if not code or not tk:
            continue
        market = "KOSPI" if tk.upper().endswith(".KS") else "KOSDAQ"
        rows.append({"code": code, "name": (c.get("name") or "").strip(),
                     "market": market, "ticker": tk,
                     "corp_code": corp.get(code) or corp.get(code.lstrip("0")) or None})
    _ROWS = rows
    return rows


def _norm(s: str) -> str:
    return re.sub(r"[\s\(\)\[\]·,.\-_]", "", (s or "")).lower()


def resolve(query: str) -> dict | None:
    rows = _load()
    q = (query or "").strip()
    if not q:
        return None
    if _CODE_RE.match(q):
        for r in rows:
            if r["code"] == q:
                return r
        return None
    nq = _norm(q)
    exact = [r for r in rows if _norm(r["name"]) == nq]
    if exact:
        return exact[0]
    # 접두/부분 일치 → 짧은 이름(보통주) 우선
    partial = sorted((r for r in rows if nq and nq in _norm(r["name"])),
                     key=lambda r: (len(r["name"]), r["code"]))
    if partial:
        return partial[0]
    names = {_norm(r["name"]): r for r in rows}
    close = difflib.get_close_matches(nq, list(names), n=1, cutoff=0.75)
    return names[close[0]] if close else None


def candidates(query: str, n: int = 8) -> list[dict]:
    rows = _load()
    nq = _norm(query)
    names = {_norm(r["name"]): r for r in rows}
    hits = difflib.get_close_matches(nq, list(names), n=n, cutoff=0.5)
    return [names[h] for h in hits]


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    if not argv:
        print(__doc__)
        return 1
    q = " ".join(argv)
    r = resolve(q)
    if not r:
        print(f"종목을 찾지 못했습니다: {q}", file=sys.stderr)
        for c in candidates(q):
            print(f"  후보: {c['name']} ({c['code']}, {c['market']})", file=sys.stderr)
        return 2
    print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
