"""노출·현금 한도(PLAN_EXPOSURE.md, 2026-11-10 사용자 결정) — 한도값과 현황 계산(순수 함수, 외부 호출 없음).

  R1 순노출 (Long − Short) ÷ 평가액 : NET_MIN ~ NET_MAX (−10% ~ +15%)
  R2 현금 ÷ 평가액                 : CASH_MAX (10%) 이하 — 현금 = 원장 현금(담보로 묶이지 않은 몫)
  R3 종목당 비중 상한               : 코스피 시총 상위 TOP_N(2) 종목 WEIGHT_MAX_TOP(10%), 그 외 WEIGHT_MAX(5%)
  섹터 순노출                      : ±SECTOR_NET_MAX(10%)에 닿으면 같은 방향 신규·채우기 중단(브레이크), ±SECTOR_NET_RELEASE(8%) 안으로 오면 해제

이 모듈은 한도와 계산만 담는다. 대시보드는 표시(monitor)에 쓰고, 주문 단계의 적용(브레이크·상한)은 daily·engine 이 부른다.
값을 바꿀 땐 이 파일을 고치고 PLAN_EXPOSURE.md 에 함께 기록한다(환경변수로 덮어쓰지 않는다 — 규칙 변경이 git 이력에 남게).
"""

NET_MIN = -0.10
NET_MAX = 0.15
CASH_MAX = 0.10
SECTOR_NET_MAX = 0.10
SECTOR_NET_RELEASE = 0.08
WEIGHT_MAX = 0.05
WEIGHT_MAX_TOP = 0.10
TOP_N = 2
DEFAULT_TOP = ("005930", "000660")          # 유니버스 시총을 못 읽을 때(삼성전자·SK하이닉스, 2026-11 기준)


def top_cap_codes(universe_doc, is_kospi=None, n=TOP_N):
    """유니버스 문서(universe/<date>)에서 코스피 시가총액 상위 n 종목 코드. 문서 어디에 있든 {code, cap} 행을 모아 판정한다.
    is_kospi(code) → bool 을 주면 코스피 종목만 본다. 못 구하면 DEFAULT_TOP."""
    caps = {}

    def walk(x):
        if isinstance(x, dict):
            c, cap = x.get("code"), x.get("cap")
            if c and isinstance(cap, (int, float)) and cap > 0:
                c = str(c).zfill(6)
                caps[c] = max(caps.get(c, 0), float(cap))
            for v in x.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(universe_doc or {})
    codes = [c for c in caps if is_kospi is None or is_kospi(c)]
    top = sorted(codes, key=lambda c: (-caps[c], c))[:n]
    return top if len(top) == n else list(DEFAULT_TOP[:n])


def weight_cap(code, top=DEFAULT_TOP):
    """종목당 비중 상한(R3)."""
    return WEIGHT_MAX_TOP if str(code).zfill(6) in set(top or ()) else WEIGHT_MAX


def limits(top=DEFAULT_TOP):
    """대시보드·API 용 한도 묶음."""
    return {"netMin": NET_MIN, "netMax": NET_MAX, "cashMax": CASH_MAX,
            "sectorNetMax": SECTOR_NET_MAX, "sectorNetRelease": SECTOR_NET_RELEASE,
            "weightMax": WEIGHT_MAX, "weightMaxTop": WEIGHT_MAX_TOP, "topCodes": list(top or ())}


def _side(p):
    return "short" if (p.get("side") or "long") == "short" else "long"


def summarize(positions, cash, equity, top=DEFAULT_TOP):
    """현재 노출 현황. positions = [{code, name, side, sector, value}] (value = 평가금액, Short 도 양수), cash = 원장 현금, equity = 평가액.
    반환 {long, short, net, gross, cash (금액) · longPct… (평가액 대비 비율) · netOver/cashOver(한도 초과 폭, 없으면 0) ·
          sectors[{sector, long, short, net, netPct, over, brake: 'long'|'short'|None}] · overWeight[{code, name, side, pct, cap}]}"""
    eq = float(equity or 0) or 1.0
    lv = sum(float(p.get("value") or 0) for p in positions if _side(p) == "long")
    sv = sum(float(p.get("value") or 0) for p in positions if _side(p) == "short")
    net, cashp = (lv - sv) / eq, float(cash or 0) / eq
    secs = {}
    for p in positions:
        s = secs.setdefault(p.get("sector") or "기타", {"long": 0.0, "short": 0.0})
        s[_side(p)] += float(p.get("value") or 0)
    rows = []
    for name, s in secs.items():
        n = (s["long"] - s["short"]) / eq
        rows.append({"sector": name, "long": s["long"], "short": s["short"], "net": s["long"] - s["short"], "netPct": n,
                     "over": abs(n) >= SECTOR_NET_MAX,
                     "brake": ("long" if n >= SECTOR_NET_MAX else "short" if n <= -SECTOR_NET_MAX else None)})
    rows.sort(key=lambda r: -abs(r["netPct"]))
    over_w = []
    for p in positions:
        pct, cap = float(p.get("value") or 0) / eq, weight_cap(p.get("code"), top)
        if pct > cap + 1e-9:
            over_w.append({"code": p.get("code"), "name": p.get("name"), "side": _side(p), "pct": pct, "cap": cap})
    over_w.sort(key=lambda r: -(r["pct"] - r["cap"]))
    return {"long": lv, "short": sv, "net": lv - sv, "gross": lv + sv, "cash": float(cash or 0),
            "longPct": lv / eq, "shortPct": sv / eq, "netPct": net, "grossPct": (lv + sv) / eq, "cashPct": cashp,
            "netOver": max(0.0, net - NET_MAX) if net > NET_MAX else (min(0.0, net - NET_MIN) if net < NET_MIN else 0.0),
            "cashOver": max(0.0, cashp - CASH_MAX),
            "sectors": rows, "overWeight": over_w}
