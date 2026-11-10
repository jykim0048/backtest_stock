"""노출·현금 한도(PLAN_EXPOSURE.md, 2026-11-10 사용자 결정) — 한도값과 현황 계산(순수 함수, 외부 호출 없음).

  R1 순노출 (Long − Short) ÷ 평가액 : NET_MIN ~ NET_MAX (−10% ~ +15%)
  R2 현금 ÷ 평가액                 : CASH_MAX (10%) 이하 — 현금 = 원장 현금(담보로 묶이지 않은 몫)
  R3 종목당 비중 상한               : 코스피 시총 상위 TOP_N(2) 종목 WEIGHT_MAX_TOP(10%), 그 외 WEIGHT_MAX(5%)
  R1·R2 는 주당 10%p 씩 단계적으로 좁힌다(glide_limits — 아래 "단계 한도").
  섹터 순노출                      : ±SECTOR_NET_MAX(10%)에 닿으면 같은 방향 신규·채우기 중단(브레이크), ±SECTOR_NET_RELEASE(8%) 안으로 오면 해제

이 모듈은 한도와 계산만 담는다. 대시보드는 표시(monitor)에 쓰고, 주문 단계의 적용(브레이크·상한)은 daily·engine 이 부른다.
값을 바꿀 땐 이 파일을 고치고 PLAN_EXPOSURE.md 에 함께 기록한다(환경변수로 덮어쓰지 않는다 — 규칙 변경이 git 이력에 남게).
주문 단계 적용을 통째로 끄는 스위치만 환경변수(SWING_EXPOSURE_GUARD=0)로 둔다.
"""
import datetime
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


# ── 단계 한도(2026-11-10 사용자 결정: 순노출·현금 한도는 주당 10%p 씩 단계적으로) ─────────────────────────────────
# 1주차는 GLIDE_START(2026-11-16 월, 사용자 결정 "1주차는 다음 주부터") 진입분부터 — 그 전 정산은 순노출·현금 한도를 표시만 한다.
# 원장 exposureGlide = {start, asof, net0, cash0} — 진입일이 GLIDE_START 이후인 첫 정산(11/13 금)이 기록: start = 그 주문의 진입일(1주차 첫날),
# net0·cash0 = 그 정산일(asof) 종가 장부의 순노출·현금 비중. 거기서 출발해 7일마다 GLIDE_STEP 씩 최종 한도(R1·R2)로 좁힌다.
# 그 주의 한도 = 출발값 − 10%p × 주차(start 부터 7일 = 1주차, 다음 7일 = 2주차 …), 최종 한도에 닿으면 그 뒤로는 최종 한도. 주차는 주문의 진입일 기준.
# 맞추는 장치(보유 강제 축소 없음 — 신규 주문만 조절):
#   · 방향 게이트(R1): 순노출이 그 주 상한을 넘기게 되는 Long 주문은 축소·중단(하한이면 Short). 보수적으로 본다 —
#     같은 방향 대기 주문은 전량 체결, 반대 방향 대기 주문은 미체결로 가정(Short 체결률이 낮아 계획만 믿으면 상한을 넘는다).
#   · 비중 확대(R2): 현금이 그 주 한도를 넘는 동안 신규 주문 비중을 최대 BOOST_MAX 배(종목 상한 R3 안), 확대분 합계는 현금 초과분까지.
GLIDE_ON = os.environ.get("SWING_EXPOSURE_GLIDE", "1") != "0"   # 0 = 순노출·현금 한도는 표시만(방향 게이트·비중 확대 미적용)
GLIDE_START = "2026-11-16"      # 1주차 첫날(주문 진입일 기준) — 이 날짜 전 진입분은 단계 한도 미적용(표시만)
GLIDE_STEP = 0.10              # 주당 좁히는 폭(10%p)
BOOST_MAX = 2.0                # 비중 확대 배수 상한(1.0 = 확대 없음)


def glide_week(start, date):
    """단계 주차 — 시작일부터 7일 = 1, 그다음 7일 = 2 … (시작일 전 날짜도 1)."""
    d0, d1 = datetime.date.fromisoformat(str(start)[:10]), datetime.date.fromisoformat(str(date)[:10])
    return max(0, (d1 - d0).days // 7) + 1


def glide_limits(g, date):
    """그 날짜(주문 진입일)에 적용할 한도. g = 원장 exposureGlide{start, net0, cash0}(없으면 최종 한도 그대로).
    반환 {netMin, netMax, cashMax, week, final(최종 한도 도달), start, net0, cash0}."""
    out = {"netMin": NET_MIN, "netMax": NET_MAX, "cashMax": CASH_MAX, "week": None, "final": True}
    if not g or not g.get("start"):
        return out
    k = glide_week(g["start"], date)
    net0, cash0 = float(g.get("net0") or 0.0), float(g.get("cash0") or 0.0)
    if net0 > NET_MAX:
        out["netMax"] = max(NET_MAX, round(net0 - GLIDE_STEP * k, 4))
    if net0 < NET_MIN:
        out["netMin"] = min(NET_MIN, round(net0 + GLIDE_STEP * k, 4))
    if cash0 > CASH_MAX:
        out["cashMax"] = max(CASH_MAX, round(cash0 - GLIDE_STEP * k, 4))
    out.update(week=k, start=g["start"], net0=net0, cash0=cash0,
               final=(out["netMin"], out["netMax"], out["cashMax"]) == (NET_MIN, NET_MAX, CASH_MAX))
    return out


def net_room(ctx, positions, orders, side):
    """순노출 한도(ctx["limits"])까지 그 방향으로 더 낼 수 있는 금액(음수 = 이미 초과). 한도가 없으면 None.
    보유(종가 평가, 청산 예약분 제외) + 같은 방향 대기 주문 전량 기준 — 반대 방향 대기 주문은 미체결로 본다."""
    lim, eq = (ctx or {}).get("limits"), float((ctx or {}).get("eq") or 0)
    if not lim or eq <= 0:
        return None

    def tot(b, k):
        return sum(v[k] for v in b["sector"].values())
    bp, bo = book(positions, [], ctx.get("px")), book([], orders)
    net = tot(bp, "long") - tot(bp, "short")
    if side == "short":
        return (net - tot(bo, "short")) - lim["netMin"] * eq
    return lim["netMax"] * eq - (net + tot(bo, "long"))


def net_label(ctx, side):
    lim = (ctx or {}).get("limits") or {}
    v = lim.get("netMin" if side == "short" else "netMax") or 0.0
    last = NET_MIN if side == "short" else NET_MAX                 # 순노출 쪽이 최종 한도에 닿았으면 '한도', 아직 좁히는 중이면 '단계 한도'
    return f"순노출 {'한도' if abs(v - last) < 1e-9 else '단계 한도'} {v * 100:+.1f}%"


def boost_weight(ctx, weight, code):
    """현금이 그 주 한도를 넘는 동안 신규 주문 비중 확대(R2) → 확대한 비중(확대 없으면 그대로).
    배수 상한 BOOST_MAX · 종목 상한(R3) 안 · 확대분은 남은 현금 초과분 ctx["boostLeft"](금액) 안. 차감은 주문을 낸 쪽(daily)이 실제 주문 금액으로 한다."""
    left, eq = float((ctx or {}).get("boostLeft") or 0), float((ctx or {}).get("eq") or 0)
    if not ctx or not weight or eq <= 0 or BOOST_MAX <= 1 or left < MIN_ORDER_PCT * eq:
        return weight
    w = min(weight * BOOST_MAX, weight_cap(code, ctx.get("top")), weight + left / eq)
    return round(w, 4) if w > weight + 1e-9 else weight


def guard_order(ctx, positions, orders, o):
    """진입 주문 한 건에 종목 상한(R3)·섹터 브레이크·순노출 (단계) 한도(R1)를 적용 → (허용 수량, 사유 또는 None).
    ctx = {eq, top, brakes, px[, limits, boostLeft]}. 보유 + 이미 낸 대기 주문 + 이 주문이 종목 상한·섹터 ±10%·순노출 한도를 넘지 않게
    수량을 줄이고, 여유가 MIN_ORDER_PCT 미만이거나 섹터가 같은 방향으로 브레이크 중이면 0. limits 가 없으면 순노출은 보지 않는다."""
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
    rooms = [(room_code, f"종목 상한 {cap * 100:.0f}%"), (room_sec, f"섹터 {sector} 순노출 한도 ±{SECTOR_NET_MAX * 100:.0f}%")]
    room_net = net_room(ctx, positions, orders, side)
    if room_net is not None:
        rooms.append((room_net, net_label(ctx, side)))
    room, why = min(rooms, key=lambda x: x[0])               # 같으면 앞의 것(종목 → 섹터 → 순노출)
    max_qty = int(room // entry) if room > 0 else 0
    if max_qty >= qty:
        return qty, None
    if max_qty * entry < MIN_ORDER_PCT * eq:
        return 0, f"{why} 도달 — 주문 안 함"
    return max_qty, f"{why} — {max_qty:,}/{qty:,}주로 축소"
