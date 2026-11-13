"""판단 팝업 3D — 시장·업종·종목 고유 분리 + 규칙 요인(가격 되돌림·실적 발표) (2026-11-13).

main 대시보드 종목 팝업 3D(public/index.html PF3D decompose · earnFactor, 2026-11-12)와 같은 규칙을 swing 에 옮긴 것
(swing 은 main 과 병합 금지 → 옮겨 적음). 바꾸면 양쪽 같이.

  당일 등락률 r = 시장(지수 등락, 베타 1 가정) + 업종(업종 등락 − 지수 등락, KOSPI 업종만) + 종목 고유(r − 업종 등락)
  - 업종 안 = 시총 상위 3종목의 몫(시총 비중 × (그 종목 등락 − 지수 등락)) + '그 외 업종 종목'
  - 종목 고유 몫만 애널리스트 요인(price_factors.items, 강도·방향)에 배분(price_factors.attribute)
  - 규칙 요인(항목 '기타'): 전일 등락 ±REV_MIN% 이상이고 종목 고유가 반대 방향이면 '가격 되돌림'(강도 = 전일 등락 ÷ 5),
    장중 수집 실적발표(intraday_briefing.earningsEvents.releasedToday)의 영업이익 컨센 괴리 ±EARN_MIN% 이상이면 '실적 발표'
    (강도 = 괴리율 ÷ 5, 판정 태그만 있으면 2). 애널리스트 요인 제목에 이미 실적이 있으면 중복이라 넣지 않는다.
데이터(읽기 전용, main 산출물): intraday_briefing.json(지수·업종 등락·대표 종목 뉴스·실적, 30분 회차) ·
  assets/krx_code_sector.json · assets/krx_sector_map.json(GitHub raw) · main 대시보드 /api/prices(업종 상위 종목 시세).
하나라도 없거나 브리핑 날짜가 등락 기준일이 아니면 None → 대시보드는 종전 방식(등락률 전체를 요인에 배분, 남는 몫 = 기타·시장).
끄기: SWING_PRICE_SPLIT=0.
"""
import datetime
import json
import os
import re
import threading
import time
import urllib.request

from swing import config
from swing.agent import price_factors as pf_mod

ON = os.environ.get("SWING_PRICE_SPLIT", "1") != "0"
MAIN_API_BASE = os.environ.get("SWING_MAIN_API_BASE", "https://backteststock-production.up.railway.app")
REV_MIN = 10.0
EARN_MIN = 5.0
CTX_TTL = 300            # 장중 브리핑(30분 회차) 캐시
ASSET_TTL = 6 * 3600     # 업종 맵(매일 06:30 갱신)
PX_TTL = 60
_LOCK = threading.Lock()
_CACHE = {}              # key -> (ts, value)

EARN_TAG = {"컨상": 1, "턴어": 1, "컨하": -1, "적전": -1, "적지": -1}
EARN_TAG_NAME = {"컨상": "컨센서스 상회", "턴어": "턴어라운드", "컨하": "컨센서스 하회", "적전": "적자전환", "적지": "적자지속"}
_EARN_TITLE = re.compile(r"실적|영업이익|어닝|잠정|컨센")


def r1(v):
    """JS Math.round(v*10)/10 과 같은 반올림(0.05 → 0.1)."""
    import math
    return math.floor(v * 10 + 0.5) / 10


def kst_now():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=9)


def pct_txt(v):
    return ("+" if v > 0 else "−" if v < 0 else "") + f"{abs(v):.2f}%"


def pp_txt(v):
    a = 0 if abs(v) < 0.05 else v
    return ("+" if a > 0 else "−" if a < 0 else "±") + f"{abs(a):.1f}%p"


def _eok(v):
    a = abs(v)
    s = (f"{a / 10000:.1f}".rstrip("0").rstrip(".") + "조") if a >= 10000 else f"{round(a):,}억"
    return ("−" if v < 0 else "") + s


# ── 원격(main 산출물) ─────────────────────────────────────────────────────────
def _get_json(url, timeout=5):
    req = urllib.request.Request(url, headers={"User-Agent": "swing-web", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _cached(key, ttl, fn):
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    try:
        val = fn()
    except Exception as ex:
        print(f"[price_split] {key} 실패: {type(ex).__name__}: {str(ex)[:120]}", flush=True)
        val = hit[1] if hit else None                       # 실패면 직전 값(있으면)
    with _LOCK:
        _CACHE[key] = (now, val)
    return val


def _raw(rel):
    return _get_json(f"{config.MAIN_RAW_BASE.rstrip('/')}/{rel}?t={int(time.time())}")


def build_context(ib, code_sector, sector_map):
    """원자료 → {ib, secOf, secStocks(업종명 → 시총순 종목), reason(code → 대표 뉴스), earn(code → 실적)}."""
    sec_stocks = {}
    for v in ((sector_map or {}).get("sectors") or {}).values():
        sec_stocks[v.get("name")] = sorted(v.get("stocks") or [], key=lambda x: -(x.get("cap") or 0))
    reason = {}
    for sx in ((ib or {}).get("sectorsUp") or []) + ((ib or {}).get("sectorsDown") or []):
        for x in sx.get("stocks") or []:
            if x and (x.get("news") or [None])[0]:
                reason[x.get("code")] = x["news"][0]
    earn = {}
    for e in (((ib or {}).get("earningsEvents") or {}).get("releasedToday") or []):
        if e and e.get("code") and e["code"] not in earn:
            earn[e["code"]] = e
    return {"ib": ib, "secOf": (code_sector or {}).get("map") or {}, "secStocks": sec_stocks, "reason": reason, "earn": earn}


def context():
    ib = _cached("ib", CTX_TTL, lambda: _raw("public/intraday_briefing.json"))
    if not ib:
        return None
    cs = _cached("code_sector", ASSET_TTL, lambda: _raw("public/assets/krx_code_sector.json"))
    sm = _cached("sector_map", ASSET_TTL, lambda: _raw("public/assets/krx_sector_map.json"))
    return build_context(ib, cs, sm)


def main_prices(tickers):
    """main 대시보드 /api/prices?codes= → {code6: {price, prevClose, rate}}. 실패면 {}."""
    if not tickers:
        return {}
    key = "px:" + ",".join(sorted(tickers))

    def fetch():
        d = _get_json(f"{MAIN_API_BASE.rstrip('/')}/api/prices?codes={','.join(tickers)}", timeout=4)
        out = {}
        for k, q in ((d or {}).get("stocks") or {}).items():
            # 가격 = 전일 종가 그대로면 버림 — 장 초반(~09:20) 야후 지연으로 전일 종가가 현재가로 오는 일이 잦다(2026-11-13 실측).
            # 업종 종목은 '그 외' 로 들어가고, 미보유 종목 자신은 분리를 건너뛴다(정말 보합인 종목도 같은 처리 — 손해 작음).
            if q and q.get("price") and q.get("prevClose") and float(q["price"]) != float(q["prevClose"]):
                out[str(k).zfill(6)] = {"price": float(q["price"]), "prevClose": float(q["prevClose"]),
                                        "rate": (float(q["price"]) / float(q["prevClose"]) - 1) * 100}
        return out
    return _cached(key, PX_TTL, fetch) or {}


# ── 규칙 요인 ─────────────────────────────────────────────────────────────────
def prev_move(basis, today):
    """전일 등락 — 요인 기준일(basis)이 오늘의 전 거래일이면(기준 종가 = 오늘의 전일 종가) 그 등락률. 아니면 None."""
    b, t = basis or {}, today or {}
    if not (b.get("date") and t.get("date") and b["date"] < t["date"]):
        return None
    if not (b.get("close") and t.get("prevClose") and isinstance(b.get("ratePct"), (int, float))):
        return None
    if abs(b["close"] / t["prevClose"] - 1) > 0.005:          # 사이에 다른 거래일이 끼었거나 값이 다름
        return None
    return {"date": b["date"], "close": t["prevClose"], "rate": float(b["ratePct"])}


def reversal_factor(pm, idio):
    if not pm or abs(pm["rate"]) < REV_MIN or not idio or pm["rate"] * idio >= 0:
        return None
    up = pm["rate"] > 0
    st = max(1, min(5, int(abs(pm["rate"]) / 5 + 0.5)))
    md = pm["date"][5:].replace("-", "/")
    return {"cat": "etc", "dir": -1 if up else 1, "strength": st, "rule": True, "ruleName": "가격 되돌림", "title": "",
            "fullTitle": f"전일 {'+' if up else '−'}{abs(pm['rate']):.1f}% {'급등' if up else '급락'} 되돌림",
            "evidence": f"{md} 종가 {round(pm['close']):,}원({pct_txt(pm['rate'])}) 뒤 오늘 반대 방향 — "
                        f"{'차익실현·과열 해소로' if up else '기술적 반등으로'} 추정."}


def earn_factor(e, today_date, pf):
    """장중 수집 실적발표 → 규칙 요인 | None (main earnFactor 와 같음)."""
    if not e:
        return None
    if e.get("date") == today_date and (e.get("time") or "") >= "15:30":
        return None                                           # 오늘 마감 뒤 발표 → 다음 날 몫
    gap = (e.get("surprise") or {}).get("opGap")
    gap = gap if isinstance(gap, (int, float)) and not isinstance(gap, bool) else None
    d = st = 0
    if gap is not None and abs(gap) >= EARN_MIN:
        d, st = (1 if gap > 0 else -1), max(1, min(5, int(abs(gap) / 5 + 0.5)))
    elif gap is None and EARN_TAG.get(e.get("tag")):
        d, st = EARN_TAG[e["tag"]], 2
    if not d:
        return None
    bd = str(((pf or {}).get("basis") or {}).get("date") or "")[:10]
    if bd and bd >= str(e.get("date") or "") and any(_EARN_TITLE.search(x.get("title") or "") for x in (pf or {}).get("items") or []):
        return None                                           # 애널리스트가 발표 뒤 이미 실적 요인을 냄
    per = e.get("period") or ""
    per = (f"{per[2:4]}년 {int(per[4:]) // 3}Q" if re.fullmatch(r"\d{6}", per) and int(per[4:]) % 3 == 0
           else (e.get("quarter") or ""))
    cons, ng = (e.get("consensus") or {}).get("op"), (e.get("surprise") or {}).get("npGap")
    head = (f"실적 {'컨센서스 상회 +' if gap > 0 else '컨센서스 하회 −'}{abs(gap):.1f}%" if gap is not None
            else "실적 " + EARN_TAG_NAME[e["tag"]])
    ed = str(e.get("date") or "")
    when = ("오늘" if ed == today_date else ed[5:].replace("-", "/")) + (f" {e['time']}" if e.get("time") else "") + " 공시" + \
           ("(장 마감 뒤 → 오늘 반영)" if e.get("late") or ed != today_date else "")
    op, yoy = e.get("op"), e.get("opYoY")
    ev = ((per + " ") if per else "") + ((e["fs"] + " ") if e.get("fs") else "") + "잠정 영업이익 " + \
         (_eok(op) if isinstance(op, (int, float)) else "—") + \
         (f"(YoY {'+' if yoy >= 0 else '−'}{abs(yoy):g}%)" if isinstance(yoy, (int, float)) else f"({yoy})" if isinstance(yoy, str) and yoy else "") + \
         (f" · 컨센서스 {_eok(cons)}" if isinstance(cons, (int, float)) else "") + \
         (f" · 순이익 컨센比 {'+' if ng >= 0 else '−'}{abs(ng):.1f}%" if isinstance(ng, (int, float)) else "") + f" — {when}."
    return {"cat": "etc", "dir": d, "strength": st, "rule": True, "ruleName": "실적 발표", "title": "", "fullTitle": head, "evidence": ev}


# ── 분해 ─────────────────────────────────────────────────────────────────────
def decompose(code, kosdaq, r, ctx, px, pf, today):
    """r = 당일 등락률(%), px = {code6: {rate}}(업종 상위 종목), pf = 판단의 price_factors, today = {date, prevClose, …}.
    → {ratePct, date, items, sector, layers} | None(지수 등락 없음)."""
    ib = ctx["ib"] or {}
    idx = (ib.get("indices") or {}).get("kosdaq" if kosdaq else "kospi") or {}
    if not isinstance(idx.get("rate"), (int, float)):
        return None
    mkt = float(idx["rate"])
    sec_name = ctx["secOf"].get(code)
    sec = None
    if not kosdaq and sec_name:
        sec = next((x for x in ib.get("sectorHeat") or [] if x.get("name") == sec_name), None)
    sec_rate = float(sec["changePct"]) if sec else mkt
    excess = sec_rate - mkt if sec else 0.0
    idio = r - sec_rate
    m_name = "KOSDAQ" if kosdaq else "KOSPI"
    rows = [{"cat": "index", "title": f"{m_name} {pct_txt(mkt)}", "dir": 1 if mkt >= 0 else -1, "strength": 0, "pp": r1(mkt),
             "evidence": f"{m_name} 지수 {pct_txt(mkt)} — 시장 전체가 움직인 몫(지수와 같은 폭으로 움직였다고 가정)."}]
    if sec:
        lst = ctx["secStocks"].get(sec_name) or []
        cap_sum = sum(x.get("cap") or 0 for x in lst) or 1
        used = 0.0
        for x in [x for x in lst if x.get("code") != code][:3]:
            q = px.get(x.get("code"))
            if not q or not isinstance(q.get("rate"), (int, float)):
                continue
            w = (x.get("cap") or 0) / cap_sum
            v = w * (q["rate"] - mkt)
            used += v
            rs = ctx["reason"].get(x.get("code"))
            rows.append({"cat": "sector", "title": f"{x.get('name')} {pct_txt(q['rate'])}", "dir": 1 if v >= 0 else -1, "strength": 0,
                         "pp": r1(v), "w": round(w, 4),
                         "evidence": f"뉴스: {rs}" if rs else f"{x.get('name')} {pct_txt(q['rate'])}이 {sec_name} 업종 움직임에서 차지한 몫."})
        rows.append({"cat": "sector", "title": f"그 외 {sec_name} 종목", "dir": 1 if excess - used >= 0 else -1, "strength": 0,
                     "pp": r1(excess - used),
                     "evidence": f"{sec_name} 업종 {pct_txt(sec_rate)}(지수 대비 {pp_txt(excess)}) 중 시총 상위 3종목 몫을 뺀 나머지."})
    src = [dict(x) for x in (pf or {}).get("items") or []
           if x and x.get("cat") != "etc" and isinstance(x.get("strength"), (int, float)) and x.get("dir") in (1, -1, 0)]
    extra = [f for f in (reversal_factor(prev_move((pf or {}).get("basis"), today), idio),
                         earn_factor(ctx["earn"].get(code), (today or {}).get("date"), pf)) if f]
    for f in pf_mod.attribute(src + extra, idio):
        if f.get("cat") == "etc" and not f.get("rule"):
            f["title"] = "설명되지 않은 종목 고유 변동"
            f["evidence"] = "시장·업종을 뺀 종목 고유 움직임 중 애널리스트 요인으로 설명되지 않는 몫."
        f["pp"] = r1(f["pp"])
        rows.append(f)
    diff = r1(r1(r) - r1(sum(x["pp"] for x in rows)))       # 반올림 오차 → 가장 큰 칸
    if abs(diff) >= 0.05:
        big = max(rows, key=lambda x: abs(x["pp"]))
        big["pp"] = r1(big["pp"] + diff)
    return {"ratePct": round(r, 2), "date": (today or {}).get("date"), "items": rows, "sector": sec_name if sec else None,
            "layers": {"mkt": round(mkt, 2), "excess": round(excess, 2), "idio": round(idio, 2), "secRate": round(sec_rate, 2)},
            "rules": [f["ruleName"] for f in extra]}


def for_decision(code, kosdaq, pf, today, ctx=None, prices=None, now=None):
    """api_decision 용 — (priceSplit | None, today | None). today 가 없으면(보유 안 한 종목) 평일 09:00~20:00 에
    main /api/prices 로 오늘 등락을 받아 채운다(main 검색 창과 같은 창)."""
    if not ON or not pf or not (pf.get("items")):
        return None, today
    ctx = ctx if ctx is not None else context()
    if not ctx or not ctx.get("ib"):
        return None, today
    now = now or kst_now()
    ib_date = (ctx["ib"] or {}).get("date")
    sec_name = ctx["secOf"].get(code)
    peers = [] if kosdaq or not sec_name else [x.get("code") for x in (ctx["secStocks"].get(sec_name) or []) if x.get("code") != code][:3]
    want_self = not today
    if want_self and (now.weekday() >= 5 or not (900 <= now.hour * 100 + now.minute <= 2000) or ib_date != now.date().isoformat()):
        return None, today
    tickers = [c + ".KS" for c in peers] + ([code + (".KQ" if kosdaq else ".KS")] if want_self else [])
    px = (prices if prices is not None else main_prices(tickers)) if tickers else {}
    if want_self:
        q = px.get(code)
        if not q:
            return None, today
        today = {"date": now.date().isoformat(), "ts": now.strftime("%H:%M"), "price": q["price"], "prevClose": q["prevClose"],
                 "ratePct": round(q["rate"], 2), "kind": "live", "source": "main"}
    if not today or not isinstance(today.get("ratePct"), (int, float)) or ib_date != today.get("date"):
        return None, today                                     # 브리핑이 등락 기준일 것이 아니면 업종 비교 불가
    return decompose(code, kosdaq, float(today["ratePct"]), ctx, px, pf, today), today
