"""노출·현금 한도(PLAN_EXPOSURE.md, 2026-11-10 사용자 결정) — 한도값과 현황 계산(순수 함수, 외부 호출 없음).

  R1 순노출 (Long − Short) ÷ 평가액 : NET_MIN ~ NET_MAX (−10% ~ +15%)
  R2 현금 ÷ 평가액                 : CASH_MAX (10%) 이하 — 현금 = 원장 현금(담보로 묶이지 않은 몫)
  R3 종목당 비중 상한               : 코스피 시총 상위 TOP_N(2) 종목 WEIGHT_MAX_TOP(10%), 그 외 WEIGHT_MAX(5%)
  섹터 순노출                      : ±SECTOR_NET_MAX(10%)에 닿으면 같은 방향 신규·채우기 중단(브레이크), ±SECTOR_NET_RELEASE(8%) 안으로 오면 해제

이 모듈은 한도와 계산만 담는다. 대시보드는 표시(monitor)에 쓰고, 주문 단계의 적용(브레이크·상한)은 daily·engine 이 부른다.
값을 바꿀 땐 이 파일을 고치고 PLAN_EXPOSURE.md 에 함께 기록한다(환경변수로 덮어쓰지 않는다 — 규칙 변경이 git 이력에 남게).
주문 단계 적용을 통째로 끄는 스위치만 환경변수(SWING_EXPOSURE_GUARD=0)로 둔다.
"""
import os

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


# ── 주문 단계 적용(2026-11-10) — engine.place_entry 가 EXPOSURE_CTX 가 있을 때 guard_order 를 부른다 ─────────────────

GUARD_ON = os.environ.get("SWING_EXPOSURE_GUARD", "1") != "0"   # 급할 때 Railway 변수로 끄는 스위치(0 = 종목 상한·섹터 브레이크 미적용)
MIN_ORDER_PCT = 0.003          # 한도 여유가 평가액의 0.3% 미만이면 주문하지 않는다(자투리 주문 방지)


def _val(p, px):
    """포지션 평가금액(Short 도 양수) — 청산 예약(sellPending) 포지션은 다음 날 나가므로 0."""
    if p.get("sellPending"):
        return 0.0
    price = (px or {}).get(p.get("code")) or p.get("lastClose") or p.get("entry") or 0
    return float(p.get("qty") or 0) * float(price)


def book(positions, orders, px=None):
    """보유(종가 평가) + 대기 진입 주문(수량 × 진입가) → {"sector": {섹터: {long, short}}, "code": {코드: 금액}}."""
    sec, code = {}, {}

    def add(x, v):
        if v <= 0:
            return
        s = sec.setdefault(x.get("sector") or "기타", {"long": 0.0, "short": 0.0})
        s[_side(x)] += v
        c = str(x.get("code") or "").zfill(6)
        code[c] = code.get(c, 0.0) + v
    for p in positions or []:
        add(p, _val(p, px))
    for o in orders or []:
        if o.get("type", "entry") == "entry" and o.get("status", "open") == "open":
            add(o, float(o.get("qty") or 0) * float(o.get("entry") or 0))
    return {"sector": sec, "code": code}


def sector_net_pct(b, eq):
    eq = float(eq or 0) or 1.0
    return {s: (v["long"] - v["short"]) / eq for s, v in (b.get("sector") or {}).items()}


def update_brakes(prev, net_pct):
    """섹터 브레이크 갱신 — ±SECTOR_NET_MAX 도달이면 건다, 걸려 있던 건 ±SECTOR_NET_RELEASE 안으로 와야 푼다.
    반환 {섹터: 'long'|'short'} — 그 방향의 신규·비중 채우기를 멈춘다."""
    out = {}
    for s, n in (net_pct or {}).items():
        was = (prev or {}).get(s)
        if n >= SECTOR_NET_MAX or (was == "long" and n > SECTOR_NET_RELEASE):
            out[s] = "long"
        elif n <= -SECTOR_NET_MAX or (was == "short" and n < -SECTOR_NET_RELEASE):
            out[s] = "short"
    return out


def guard_order(ctx, positions, orders, o):
    """진입 주문 한 건에 종목 상한(R3)·섹터 브레이크를 적용 → (허용 수량, 사유 또는 None).
    ctx = {eq, top, brakes, px}. 보유 + 이미 낸 대기 주문 + 이 주문이 종목 상한·섹터 ±10% 를 넘지 않게 수량을 줄이고,
    여유가 MIN_ORDER_PCT 미만이거나 섹터가 같은 방향으로 브레이크 중이면 0."""
    qty, entry, eq = int(o.get("qty") or 0), float(o.get("entry") or 0), float((ctx or {}).get("eq") or 0)
    if not ctx or qty <= 0 or entry <= 0 or eq <= 0:
        return qty, None
    side, sector, code = _side(o), o.get("sector") or "기타", str(o.get("code") or "").zfill(6)
    word = "Short" if side == "short" else "Long"
    if (ctx.get("brakes") or {}).get(sector) == side:
        return 0, f"섹터 {sector} 순노출 한도 ±{SECTOR_NET_MAX * 100:.0f}% — {word} 신규·비중 채우기 중단"
    b = book(positions, orders, ctx.get("px"))
    cap = weight_cap(code, ctx.get("top"))
    room_code = cap * eq - b["code"].get(code, 0.0)
    s = b["sector"].get(sector) or {"long": 0.0, "short": 0.0}
    net = s["long"] - s["short"]
    room_sec = SECTOR_NET_MAX * eq - net if side == "long" else SECTOR_NET_MAX * eq + net
    room = min(room_code, room_sec)
    max_qty = int(room // entry) if room > 0 else 0
    if max_qty >= qty:
        return qty, None
    why = f"종목 상한 {cap * 100:.0f}%" if room_code <= room_sec else f"섹터 {sector} 순노출 한도 ±{SECTOR_NET_MAX * 100:.0f}%"
    if max_qty * entry < MIN_ORDER_PCT * eq:
        return 0, f"{why} 도달 — 주문 안 함"
    return max_qty, f"{why} — {max_qty:,}/{qty:,}주로 축소"
