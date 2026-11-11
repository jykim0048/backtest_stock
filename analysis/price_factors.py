"""주가 영향 분해(추정) — 딥리서치 LLM 이 낸 요인 목록을 기준일 등락률에 맞춰 %p 로 환산한다 (2026-11-11).

대시보드 종목 상세 팝업의 3D 요인 그림(종목 → 딥리서치 항목 ⊃ 요인)이 쓰는 데이터.

흐름
  1) LLM(generate_analysis) 이 priceFactors = [{category, title, direction, strength(1~5), evidence}] 를 낸다(PROMPT · ITEM_SCHEMA).
     %p 는 LLM 이 쓰지 않는다 — 수치 환각 방지.
  2) 코드(build)가 정리(clean) → 기준일 등락률(basis: 수급 일별 표의 가장 최근 행) → 배분(attribute).

배분 규칙(attribute) — **측정이 아니라 추정**이다(화면에도 '추정'으로 표시)
  U = 상승 요인 강도 합, D = 하락 요인 강도 합, r = 기준일 등락률(%).
  상승 몫 u ≥ 0 · 하락 몫 d ≥ 0 을 u − d = r 이 되게 잡고, 각 몫을 요인 강도 비례로 나눈다.
  총량 u + d 는 |r| × (U + D) ÷ |U − D| (상쇄가 클수록 커짐)이되 |r| 의 GROSS_CAP 배를 넘지 않게 한다
  (U ≈ D 일 때 숫자가 폭주하지 않게). 필요한 방향의 요인이 아예 없으면(예: 주가는 내렸는데 하락 요인이 없음)
  그 몫은 '기타·시장 요인' 한 줄(category "etc")로 남긴다 — 요인으로 설명되지 않은 변동.
  반올림 뒤 합계는 r 과 같게 맞춘다. 순수 함수 · 외부 호출 없음.
"""

CATS = ("research", "flow", "peers", "news", "community", "dart")
CAT_LABEL = {"research": "증권사 리포트", "flow": "수급", "peers": "해외 Peer", "news": "뉴스 분석",
             "community": "토론방", "dart": "DART · CA", "etc": "기타·시장"}
MAX_PER_CAT = 4
MAX_TOTAL = 16
GROSS_CAP = 3.0          # 상승 몫 + 하락 몫 ≤ |등락률| × 이 배수
GROSS_MISSING = 2.0      # 필요한 방향의 요인이 없을 때의 배수(반대 방향 요인에도 몫을 조금 남긴다)
GROSS_FLOOR = 1.0        # 등락률이 0 에 가까워도 상승·하락 요인이 둘 다 있으면 이만큼(%p)은 서로 상쇄한 것으로 본다

_STR = {"type": "string"}
# generate_analysis.ANALYSIS_SCHEMA 에 끼우는 항목 스키마(strength 는 정수 타입 대신 문자열 enum — 제공자별 스키마 변환 차이 회피)
ITEM_SCHEMA = {
    "type": "array",
    "items": {"type": "object", "additionalProperties": False,
              "properties": {"category": {"type": "string", "enum": list(CATS)},
                             "title": _STR,
                             "direction": {"type": "string", "enum": ["up", "down", "neutral"]},
                             "strength": {"type": "string", "enum": ["1", "2", "3", "4", "5"]},
                             "evidence": _STR},
              "required": ["category", "title", "direction", "strength", "evidence"]},
}

PROMPT = """
- priceFactors: 가장 최근 거래일의 주가 움직임에 영향을 준 요인을 딥리서치 항목별로 나눈 목록(전체 6~14개, 항목당 0~4개).
  category = 근거가 나온 항목 — research(증권사 리포트) | flow(수급) | peers(해외 Peer) | news(뉴스) |
  community(토론방) | dart(DART 공시·재무·CA). title = 20자 안팎의 구체적인 한 줄(수치를 넣으면 좋다).
  direction = 주가에 준 방향 up | down | neutral. strength = 그 요인이 최근 주가 움직임에 준 상대적 영향
  "1"(미미) ~ "5"(결정적). evidence = RAW 의 수치·사실을 인용한 근거 1문장.
  같은 사건을 여러 항목에 겹쳐 넣지 말고 가장 직접적인 항목 하나에만 둔다. RAW 에 근거가 없는 요인은 만들지 말고,
  근거가 없는 항목은 비워 둔다. 상승·하락 요인을 모두 찾되 억지로 균형을 맞추지 않는다.
  %p 같은 기여도 수치는 코드가 계산하므로 쓰지 않는다."""

_DIR = {"up": 1, "down": -1, "neutral": 0, "상승": 1, "하락": -1, "중립": 0, "bullish": 1, "bearish": -1}


def clean(raw):
    """LLM 출력 → [{cat, title, dir(1|-1|0), strength(1~5), evidence}] (형식이 틀린 줄은 버림, 중복 제거, 개수 상한)."""
    out, seen = [], set()
    for x in raw if isinstance(raw, list) else []:
        if not isinstance(x, dict):
            continue
        cat = str(x.get("category") or x.get("cat") or "").strip().lower()
        title = " ".join(str(x.get("title") or "").split())[:60]
        d = _DIR.get(str(x.get("direction") or "").strip().lower())
        if cat not in CATS or not title or d is None:
            continue
        try:
            st = int(float(str(x.get("strength")).strip()))
        except (TypeError, ValueError):
            st = 2
        st = max(1, min(5, st))
        key = (cat, title)
        if key in seen:
            continue
        seen.add(key)
        out.append({"cat": cat, "title": title, "dir": d, "strength": st,
                    "evidence": " ".join(str(x.get("evidence") or "").split())[:220]})
    # 항목당·전체 상한 — 강한 것부터 남긴다(원래 순서 유지)
    keep, per = set(), {}
    for i in sorted(range(len(out)), key=lambda i: (-out[i]["strength"], i)):
        c = out[i]["cat"]
        if per.get(c, 0) < MAX_PER_CAT and len(keep) < MAX_TOTAL:
            keep.add(i)
            per[c] = per.get(c, 0) + 1
    return [out[i] for i in range(len(out)) if i in keep]


def basis(flow):
    """기준일 등락률 — 수급 일별 표(flow.trend, 최신순)에서 등락률이 있는 첫 행. 없으면 None."""
    for row in (flow or {}).get("trend") or []:
        if not isinstance(row, dict):
            continue
        r, d = row.get("rate"), str(row.get("date") or "")
        if isinstance(r, bool) or not isinstance(r, (int, float)):
            continue
        digits = "".join(ch for ch in d if ch.isdigit())
        if len(digits) != 8:
            continue
        return {"date": f"{digits[:4]}-{digits[4:6]}-{digits[6:]}", "ratePct": round(float(r), 2)}
    return None


def attribute(items, rate):
    """요인별 %p 배분(모듈 설명 참조). items = clean() 결과, rate = 기준일 등락률(%). 반환 = pp 가 붙은 새 목록(+ 필요 시 etc 한 줄)."""
    r = float(rate)
    U = sum(x["strength"] for x in items if x["dir"] > 0)
    D = sum(x["strength"] for x in items if x["dir"] < 0)
    missing = (r > 0 and U == 0) or (r < 0 and D == 0)
    if missing:
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
                    "evidence": "제시된 요인의 방향만으로는 기준일 등락률을 설명할 수 없어 남은 부분(시장·업종 흐름 등).",
                    "pp": round(resid, 1)})
    # 반올림 오차는 가장 큰 요인에 얹어 합계를 등락률과 맞춘다
    diff = round(round(r, 1) - sum(x["pp"] for x in out), 1)
    if out and abs(diff) >= 0.05:
        big = max(out, key=lambda x: abs(x["pp"]))
        big["pp"] = round(big["pp"] + diff, 1)
    return out


def build(raw, flow):
    """analysis["priceFactors"] 문서 — 요인이 없거나 기준 등락률을 못 구하면 None(대시보드는 그림을 숨긴다)."""
    items = clean(raw)
    b = basis(flow)
    if not items or not b:
        return None
    rows = attribute(items, b["ratePct"])
    by = {}
    for x in rows:
        by[x["cat"]] = round(by.get(x["cat"], 0.0) + x["pp"], 1)
    return {"basis": b, "items": rows, "byCat": by, "estimated": True,
            "method": "LLM 이 낸 요인 강도를 기준일 등락률에 맞춰 배분한 추정치"}
