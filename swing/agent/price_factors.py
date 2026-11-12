"""당일 등락 요인 분해(추정) — 애널리스트 5명이 낸 요인을 판단일 등락률에 맞춰 %p 로 나눈다 (2026-11-12, PLAN_3D.md).

대시보드 판단 팝업의 3D 카드(종목 → 리포트 항목 구 ⊃ 요인 공)가 쓰는 데이터. main 의 analysis/price_factors.py 와
같은 규칙이다(swing 은 main 과 병합 금지 → 옮겨 적음, 항목만 다름).

흐름
  1) pipeline 의 애널리스트 호출 5개(market·sentiment·news·fundamentals·flow)가 응답 JSON 에 markdown 과 함께
     factors = [{title, direction, strength, evidence}] 를 낸다(SCHEMA · prompt). 항목은 코드가 역할로 붙인다.
     %p 는 LLM 이 쓰지 않는다 — 수치 환각 방지. 요인이 없거나 형식이 틀려도 판단은 그대로 진행.
  2) build(raw_by_cat, basis) — clean → attribute. 기준 = 01_price.json 의 판단일 등락률(returns_pct.1d).
     대시보드는 실시간 시세(전일 종가 대비)가 있으면 같은 식(static/pf3d.js attribute)으로 그 순간 등락률에 다시 맞춘다.

배분 규칙(attribute) — **측정이 아니라 추정**(화면에 '추정' 배지)
  U = 상승 요인 강도 합, D = 하락 요인 강도 합, r = 기준 등락률(%).
  상승 몫 u ≥ 0 · 하락 몫 d ≥ 0 을 u − d = r 이 되게 잡고 각 몫을 강도 비례로 나눈다. 총량 u + d 는
  |r| × (U + D) ÷ |U − D| 이되 |r| 의 GROSS_CAP 배를 넘지 않는다. 필요한 방향의 요인이 없으면 그 몫은
  '기타·시장'(cat "etc") 한 줄로 남긴다. 반올림 뒤 합계 = r. 순수 함수 · 외부 호출 없음.

끄기: SWING_AGENT_PRICE_FACTORS=0 (애널리스트 스키마·프롬프트가 원래대로 → 카드도 안 뜸).
"""
import os

ON = os.environ.get("SWING_AGENT_PRICE_FACTORS", "1") != "0"
CATS = ("market", "sentiment", "news", "fundamentals", "flow")
CAT_LABEL = {"market": "기술적 분석", "sentiment": "심리", "news": "뉴스·공시", "fundamentals": "펀더멘털", "flow": "수급",
             "etc": "기타·시장"}
MAX_PER_CAT = 4
VERSION = 1
GROSS_CAP = 3.0          # 상승 몫 + 하락 몫 ≤ |등락률| × 이 배수
GROSS_MISSING = 2.0      # 필요한 방향의 요인이 없을 때의 배수
GROSS_FLOOR = 1.0        # 등락률이 0 근처여도 상승·하락 요인이 둘 다 있으면 이만큼(%p)은 상쇄한 것으로 본다

_STR = {"type": "string"}
ITEM_SCHEMA = {
    "type": "array",
    "items": {"type": "object", "additionalProperties": False,
              "properties": {"title": _STR,
                             "direction": {"type": "string", "enum": ["up", "down", "neutral"]},
                             "strength": {"type": "string", "enum": ["1", "2", "3", "4", "5"]},
                             "evidence": _STR},
              "required": ["title", "direction", "strength", "evidence"]},
}
# 애널리스트 호출 스키마 — 기존 {"markdown"} 에 factors 를 더한 것(pipeline.MD_SCHEMA 와 같은 markdown 키)
SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"markdown": _STR, "factors": ITEM_SCHEMA},
          "required": ["markdown", "factors"]}

_DIR = {"up": 1, "down": -1, "neutral": 0, "상승": 1, "하락": -1, "중립": 0, "bullish": 1, "bearish": -1}


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def basis(price, date):
    """판단일 등락 — 01_price.json(kit indicators) → {date, ratePct, close, prevClose, kind} | None.
    kind = "day"(최신 행이 판단일) | "last"(최신 행이 판단일보다 앞 — 그 행의 등락)."""
    price = price or {}
    r = _num((price.get("returns_pct") or {}).get("1d"))
    close = _num((price.get("latest_ohlcv") or {}).get("Close"))
    d = price.get("latest_row")
    if r is None or not d:
        return None
    prev = round(close / (1 + r / 100.0), 2) if close and r > -100 else None
    return {"date": d, "ratePct": round(r, 2), "close": close, "prevClose": prev, "kind": "day" if d == date else "last"}


def prompt(cat, b):
    """애널리스트 user 프롬프트 끝에 붙이는 요구(역할 파일은 그대로 — 2026-11-12)."""
    if b:
        move = (f"{b['date']} 종가 {b['close']:,.0f}원, 전일 대비 {b['ratePct']:+.2f}%" if b.get("close")
                else f"{b['date']} 전일 대비 {b['ratePct']:+.2f}%")
    else:
        move = "가격 자료의 가장 최근 거래일 등락"
    return (f"\n\n## 추가 출력: 당일 등락 요인(factors)\n"
            f"응답 JSON 의 factors 배열에, 당신이 맡은 '{CAT_LABEL[cat]}' 관점에서 **{move}** 의 주가 움직임에 영향을 준 요인을 "
            f"1~{MAX_PER_CAT}개 넣는다(markdown 리포트는 지금까지와 똑같이 쓴다).\n"
            "- title: 20자 안팎의 구체적인 한 줄(수치를 넣으면 좋다)\n"
            "- direction: 주가에 준 방향 up | down | neutral(불분명하면 neutral)\n"
            "- strength: 그 요인이 이 등락에 준 상대적 영향 \"1\"(미미) ~ \"5\"(결정적)\n"
            "- evidence: 입력 자료의 수치·사실을 인용한 근거 1문장(40자 안팎)\n"
            "입력 자료에 근거가 없는 요인은 만들지 않는다. 상승·하락을 억지로 맞추지 않는다. %p 같은 기여도 수치는 코드가 계산하므로 쓰지 않는다. "
            "입력이 비었거나 수집 실패면 factors 는 빈 배열.")


def clean(raw, cat):
    """한 애널리스트의 LLM 출력 → [{cat, title, dir(1|-1|0), strength(1~5), evidence}] (틀린 줄 버림·중복 제거·상한)."""
    out, seen = [], set()
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict):
            continue
        title = " ".join(str(x.get("title") or "").split())[:60]
        d = _DIR.get(str(x.get("direction") or "").strip().lower())
        if not title or d is None or title in seen:
            continue
        try:
            st = int(float(str(x.get("strength")).strip()))
        except (TypeError, ValueError):
            st = 2
        seen.add(title)
        out.append({"cat": cat, "title": title, "dir": d, "strength": max(1, min(5, st)),
                    "evidence": " ".join(str(x.get("evidence") or "").split())[:220]})
    keep = sorted(range(len(out)), key=lambda i: (-out[i]["strength"], i))[:MAX_PER_CAT]
    return [out[i] for i in sorted(keep)]


def attribute(items, rate):
    """요인별 %p 배분(모듈 설명). items = clean() 결과, rate = 기준 등락률(%). 반환 = pp 가 붙은 새 목록(+ 필요 시 etc 한 줄)."""
    r = float(rate)
    U = sum(x["strength"] for x in items if x["dir"] > 0)
    D = sum(x["strength"] for x in items if x["dir"] < 0)
    if (r > 0 and U == 0) or (r < 0 and D == 0):
        gross = abs(r) * GROSS_MISSING
    else:
        gross = abs(r) * min((U + D) / max(abs(U - D), 1e-9), GROSS_CAP) if (U or D) else abs(r)
        if U and D:
            gross = max(gross, GROSS_FLOOR)
    gross = max(gross, abs(r))
    u, d = (gross + r) / 2.0, (gross - r) / 2.0
    resid = 0.0
    if U == 0:
        resid += u
        u = 0.0
    if D == 0:
        resid -= d
        d = 0.0
    out = []
    for x in items:
        pp = u * x["strength"] / U if x["dir"] > 0 else (-d * x["strength"] / D if x["dir"] < 0 else 0.0)
        out.append(dict(x, pp=round(pp, 1)))
    if abs(resid) >= 0.05:
        out.append({"cat": "etc", "title": "요인으로 설명되지 않은 변동", "dir": 1 if resid > 0 else -1, "strength": 0,
                    "evidence": "제시된 요인의 방향만으로는 등락률을 설명할 수 없어 남은 부분(시장·업종 흐름 등).",
                    "pp": round(resid, 1)})
    diff = round(round(r, 1) - sum(x["pp"] for x in out), 1)
    if out and abs(diff) >= 0.05:
        big = max(out, key=lambda x: abs(x["pp"]))
        big["pp"] = round(big["pp"] + diff, 1)
    return out


def build(raw_by_cat, b):
    """판단 문서의 price_factors — 요인이 하나도 없거나 기준 등락률이 없으면 None(대시보드는 카드를 숨김)."""
    items = []
    for cat in CATS:
        items.extend(clean((raw_by_cat or {}).get(cat), cat))
    if not items or not b:
        return None
    rows = attribute(items, b["ratePct"])
    by = {}
    for x in rows:
        by[x["cat"]] = round(by.get(x["cat"], 0.0) + x["pp"], 1)
    return {"version": VERSION, "basis": b, "items": rows, "byCat": by, "estimated": True,
            "method": "애널리스트가 낸 요인 강도를 판단일 등락률에 맞춰 배분한 추정치"}
