#!/usr/bin/env python3
"""
Morning briefing generator — 전일 미국장 리뷰 + 당일 한국시장 프리뷰.

장전 스크리너(screener.py, pre 모드)가 이미 만들어 둔 선정 산출물
  public/reports/selection/YYYY-MM-DD.json
을 입력으로 받는다. 이 파일은 usMarket(미국 지수·섹터·급등 특징주)과
picks(종목별 촉매·근거), marketView(3~4문장 요약)를 이미 담고 있으므로,
브리핑 생성기는 **추가 네트워크 수집 없이** 그 자료를 재가공·심화한다.
(로컬 SSL 제약과 무관 — yfinance/DART 를 다시 호출하지 않는다.)

핵심 부가가치는 **미국 movers/섹터 → 국내 테마 밸류체인 매핑**과 스탠스 판정으로,
이를 위해 전용 LLM 콜 1회를 쓴다. LLM 이 없거나 실패하면 기계적 폴백으로
브리핑을 만들어 **절대 파이프라인을 막지 않는다**(screener 와 같은 철학).

출력(public/briefing/):
  YYYY-MM-DD.json  — 날짜별 아카이브(대시보드 과거 조회)
  latest.json      — 최신본(대시보드 기본 로드)
  index.json       — 사용 가능한 날짜 목록(최신순)

Env:
  BRIEFING_SELECTION  입력 selection JSON 경로(기본: 오늘자, 없으면 최신 selection)
  BRIEFING_OUT_DIR    출력 디렉토리(기본 public/briefing)
  BRIEFING_DATE       대상 날짜 YYYY-MM-DD(기본 KST 오늘)
  LLM_CHAIN + 키      llm.py 참고. 미설정 시 기계적 폴백.
"""
import os
import re
import sys
import json
import glob
import datetime

import llm

ROOT          = os.path.dirname(os.path.abspath(__file__))
SELECTION_DIR = os.path.join(ROOT, "public", "reports", "selection")
OUT_DIR       = os.environ.get("BRIEFING_OUT_DIR") or os.path.join(ROOT, "public", "briefing")
if not os.path.isabs(OUT_DIR):
    OUT_DIR = os.path.join(ROOT, OUT_DIR)

KST        = datetime.timezone(datetime.timedelta(hours=9))
MAX_TOKENS = 3000


def _warn(msg):
    print(f"[briefing] {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------
# 입력 로드 — 오늘자 selection, 없으면 가장 최근 selection
# ----------------------------------------------------------------------------
def _load_selection(date_str):
    explicit = os.environ.get("BRIEFING_SELECTION")
    if explicit:
        path = explicit if os.path.isabs(explicit) else os.path.join(ROOT, explicit)
        with open(path, encoding="utf-8") as f:
            return json.load(f), path

    today_path = os.path.join(SELECTION_DIR, f"{date_str}.json")
    if os.path.exists(today_path):
        with open(today_path, encoding="utf-8") as f:
            return json.load(f), today_path

    # 오늘자가 아직 없으면(장전 스크리너 미실행) 가장 최근 selection 을 쓴다.
    dated = sorted(glob.glob(os.path.join(SELECTION_DIR, "20*.json")))
    if not dated:
        raise RuntimeError(f"selection JSON 없음: {SELECTION_DIR}")
    with open(dated[-1], encoding="utf-8") as f:
        _warn(f"오늘자 selection 없음 → 최신본 사용: {os.path.basename(dated[-1])}")
        return json.load(f), dated[-1]


# ----------------------------------------------------------------------------
# LLM 브리핑 (전일 미국장 리뷰 + 당일 한국 프리뷰 + 밸류체인 매핑)
# ----------------------------------------------------------------------------
SYSTEM = """\
너는 한국 주식 데이 트레이딩 데스크의 애널리스트다. 장 시작 전(08시경), 주어진 데이터로
'전일 미국장 리뷰'와 '당일 한국시장 프리뷰'로 구성된 모닝 브리핑을 작성한다.

입력:
- usMarket.indices : 전일 미국 주요 지수/VIX 등락(changePct). 시장 위험선호도 판단용.
- usMarket.sectors : 미국 섹터 ETF 등락(강→약 정렬). 어떤 섹터가 주도했는지.
- usMarket.movers  : 미국 당일 급등 특징주(symbol/name/changePct). 상승 테마·밸류체인 추론용.
- usMarket.losers  : 미국 당일 급락 특징주(symbol/name/changePct). 하락 테마·국내 약세 주의 추론용.
- picks            : 장전 스크리너가 오늘 뽑은 국내 워치리스트(code/name/market/catalyst/reason).
- marketView       : 스크리너가 남긴 간단 시황 코멘트(참고).

작성 원칙:
1) stance: 전일 미국장 위험선호도를 '위험선호|중립|위험회피' 중 하나로 판정한다.
   나스닥·필라델피아 반도체 강세 + VIX 하락 = 위험선호. 반대면 위험회피.
2) usReview.narrative: 전일 미국장(지수·섹터·급등 특징주)을 2~3문장으로 요약한다.
3) flow: **미국에서 강했던 급등 테마 → 국내 밸류체인/동조(강세 기대) 종목** 연결을 2~4개 만든다(핵심).
   각 항목:
     - usTheme  : 미국 쪽 상승 테마명(예: 반도체·장비, 자율주행·라이다, 방산·우주, 에너지·수소).
     - usSymbols: 그 테마의 대표 급등주 티커. 반드시 입력 movers 안의 symbol 에서만 고른다(2~4개).
     - krTheme  : 연결되는 국내 테마명(예: 반도체 소부장, 자율주행·전장, 전력·AI 인프라).
     - krNames  : 관련 국내 종목명. **가능하면 picks 안의 종목명을 우선**, 대표 종목명 추가 가능(1~4개).
     - rationale: 미국→한국이 왜 연결되는지 한 줄.
4) downFlow: **미국에서 급락한 테마 → 국내 약세 주의 섹터/종목** 연결을 0~3개 만든다(flow 와 대칭, 하락).
   losers 가 비었거나 뚜렷한 급락 테마가 없으면 빈 배열([])로 둔다.
     - usTheme  : 미국 쪽 급락 테마명.
     - usSymbols: 그 테마의 대표 급락주 티커. 반드시 입력 losers 안의 symbol 에서만 고른다(2~4개).
     - krTheme  : 약세가 우려되는 국내 테마/섹터명.
     - krNames  : 주의할 국내 대표 종목명(0~4개, 없으면 빈 배열). picks 강세주와 억지로 엮지 마라.
     - rationale: 왜 국내에 부담(약세)이 될 수 있는지 한 줄.
5) krPreview.narrative: 당일 국내 관점 1~2문장. catalystSummary: 전일 장마감 후 국내 촉매
   (picks 의 catalyst)를 1~2문장으로 압축.
6) 과장·투자권유 표현을 피하고, 담백한 데스크 코멘트 톤으로 한국어로 쓴다.

반드시 아래 스키마와 정확히 동일한 JSON만 출력한다. 마크다운 펜스/설명 금지.
{
  "stance": "위험선호|중립|위험회피",
  "stanceReason": "지수·VIX 근거 한 줄",
  "usReview": {"narrative": "전일 미국장 2~3문장"},
  "flow": [
    {"usTheme": "미국 상승 테마", "usSymbols": ["TICKER", ...],
     "krTheme": "국내 테마", "krNames": ["종목명", ...], "rationale": "미국→한국 연결 근거"}
  ],
  "downFlow": [
    {"usTheme": "미국 급락 테마", "usSymbols": ["TICKER", ...],
     "krTheme": "국내 약세 주의 섹터", "krNames": ["종목명", ...], "rationale": "국내 부담 근거"}
  ],
  "krPreview": {
    "narrative": "당일 국내 관점 1~2문장",
    "catalystSummary": "전일 장마감 후 국내 촉매 요약"
  }
}"""

_STR   = {"type": "string"}
_STRLIST = {"type": "array", "items": _STR}
BRIEF_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "stance": _STR,
        "stanceReason": _STR,
        "usReview": {
            "type": "object", "additionalProperties": False,
            "properties": {"narrative": _STR},
            "required": ["narrative"],
        },
        "flow": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"usTheme": _STR, "usSymbols": _STRLIST,
                           "krTheme": _STR, "krNames": _STRLIST, "rationale": _STR},
            "required": ["usTheme", "usSymbols", "krTheme", "krNames", "rationale"]}},
        "downFlow": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"usTheme": _STR, "usSymbols": _STRLIST,
                           "krTheme": _STR, "krNames": _STRLIST, "rationale": _STR},
            "required": ["usTheme", "usSymbols", "krTheme", "krNames", "rationale"]}},
        "krPreview": {
            "type": "object", "additionalProperties": False,
            "properties": {"narrative": _STR, "catalystSummary": _STR},
            "required": ["narrative", "catalystSummary"],
        },
    },
    "required": ["stance", "stanceReason", "usReview", "flow", "downFlow", "krPreview"],
}


def _llm_input(sel):
    us = sel.get("usMarket", {}) or {}
    return json.dumps({
        "usMarket": {
            "indices": us.get("indices", []),
            "sectors": us.get("sectors", []),
            "movers":  us.get("movers", [])[:15],
            "losers":  us.get("losers", [])[:15],
        },
        "picks": [{"name": p.get("name"), "market": p.get("market"),
                   "catalyst": p.get("catalyst", ""), "reason": p.get("reason", "")}
                  for p in sel.get("picks", [])],
        "marketView": sel.get("marketView", ""),
    }, ensure_ascii=False)


def llm_briefing(sel):
    """LLM 으로 구조화 브리핑 생성. 실패 시 None(호출부에서 기계적 폴백)."""
    if not llm.configured():
        _warn("LLM 미설정 — 기계적 폴백 사용")
        return None, None
    try:
        data, model = llm.generate_json(
            SYSTEM, _llm_input(sel),
            max_tokens=MAX_TOKENS, schema=BRIEF_SCHEMA, return_model=True)
        return data, model
    except Exception as e:
        _warn(f"LLM 브리핑 실패: {e} — 기계적 폴백 사용")
        return None, None


# ----------------------------------------------------------------------------
# 기계적 폴백 — LLM 없이 selection 자료만으로 브리핑을 구성한다.
# ----------------------------------------------------------------------------
# 급등 특징주 name → 테마 매핑(부분일치, 소문자). 폴백 전용 근사.
_MOVER_THEMES = [
    ("자율주행·라이다", ["mobileye", "ouster", "hesai", "aurora", "luminar", "innoviz"]),
    ("반도체·장비",     ["ambarella", "maxlinear", "formfactor", "sandisk", "credo",
                         "chipmos", "penguin", "micron", "wolfspeed", "sk hynix"]),
    ("방산·우주",       ["aerovironment", "mercury", "viasat", "kratos", "rocket"]),
    ("에너지·수소",     ["fuelcell", "landbridge", "bloom", "plug", "ballard"]),
    ("양자컴퓨팅",      ["quantinuum", "rigetti", "ionq", "quantum", "d-wave"]),
    ("바이오·제약",     ["abivax", "bio", "pharma", "therapeutics", "genomics"]),
    ("보안·소프트웨어", ["tenable", "crowdstrike", "cyber", "palantir"]),
]

# 미국 movers 테마 → (국내 테마명, picks 매칭용 키워드) 매핑(폴백 전용 근사).
# krNames 는 picks 의 reason/catalyst 에 아래 키워드가 있으면 그 종목명을 담아 실제 선정과 연결한다.
_US_TO_KR = {
    "자율주행·라이다": ("자율주행·전장",  ["자율주행", "전장", "카메라", "라이다", "ADAS", "모빌"]),
    "반도체·장비":     ("반도체 소부장",  ["반도체", "장비", "소부장", "CMP", "전공정", "소켓", "검사", "웨이퍼"]),
    "방산·우주":       ("방산·우주",      ["방산", "우주", "위성", "항공"]),
    "에너지·수소":     ("전력·AI 인프라", ["전력", "송배전", "변압기", "수소", "연료전지", "인프라", "데이터센터", "MLCC"]),
    "양자컴퓨팅":      ("양자·차세대IT",  ["양자", "퀀텀"]),
    "바이오·제약":     ("제약·바이오",    ["바이오", "제약", "임상", "신약", "허가"]),
    "보안·소프트웨어": ("보안·소프트웨어", ["보안", "소프트", "SW", "클라우드"]),
    "기타 급등 특징주": ("장전 촉매주",    []),
}


def _pct(items, name):
    for it in items:
        if it.get("name") == name:
            return it.get("changePct")
    return None


def _mechanical(sel):
    us = sel.get("usMarket", {}) or {}
    indices = us.get("indices", [])
    sectors = us.get("sectors", [])
    movers  = us.get("movers", [])
    losers  = us.get("losers", [])
    picks   = sel.get("picks", [])
    mv      = sel.get("marketView", "")

    vix = _pct(indices, "VIX 변동성")
    nas = _pct(indices, "나스닥")
    sox = _pct(indices, "필라델피아 반도체")

    # 스탠스: 나스닥/필라델피아 반도체 상승 + VIX 하락 = 위험선호
    up_signals   = sum(1 for v in (nas, sox) if v is not None and v > 0)
    down_signals = sum(1 for v in (nas, sox) if v is not None and v < 0)
    vix_calm     = (vix is not None and vix < 0)
    if up_signals >= 1 and down_signals == 0 and (vix_calm or vix is None):
        stance = "위험선호"
    elif down_signals >= 1 and not vix_calm:
        stance = "위험회피"
    else:
        stance = "중립"

    def fmt(v):
        return "—" if v is None else f"{v:+.2f}%"
    stance_reason = f"나스닥 {fmt(nas)} · 필라델피아 반도체 {fmt(sox)} · VIX {fmt(vix)}"

    def _kr_names(match_kws):
        """picks 중 reason/catalyst 에 매칭 키워드가 있는 종목명."""
        if not match_kws:
            return []
        out = []
        for p in picks:
            text = f"{p.get('reason', '')} {p.get('catalyst', '')}"
            if any(k in text for k in match_kws) and p.get("name"):
                out.append(p["name"])
        return out[:4]

    # 미국 movers → 테마 그룹핑 → 국내 밸류체인 flow
    flow, used = [], set()
    for tname, kws in _MOVER_THEMES:
        syms = [m["symbol"] for m in movers
                if any(k in (m.get("name", "").lower()) for k in kws)]
        syms = [s for s in syms if s not in used]
        if not syms:
            continue
        kr_theme, match_kws = _US_TO_KR.get(tname, (tname, []))
        flow.append({
            "usTheme": tname,
            "usSymbols": syms[:4],
            "krTheme": kr_theme,
            "krNames": _kr_names(match_kws),
            "rationale": f"미국 {tname} 급등 → 국내 {kr_theme} 동조 기대",
        })
        used.update(syms)
    # 테마로 안 잡힌 상위 급등주도 한 줄로(연결 근거 약하면 장전 촉매주로)
    leftover = [m["symbol"] for m in movers[:6] if m["symbol"] not in used]
    if leftover and len(flow) < 2:
        flow.append({
            "usTheme": "기타 급등 특징주", "usSymbols": leftover[:4],
            "krTheme": "장전 촉매주", "krNames": [p["name"] for p in picks[:4]],
            "rationale": "테마 미분류 상위 급등주 — 국내는 장전 촉매주 중심",
        })

    # 미국 losers → 급락 테마 → 국내 약세 주의 downFlow.
    # 하락 주의는 picks(강세 후보)와 엮지 않는다 → krNames 는 비운다(테마 레벨 주의).
    down_flow, dused = [], set()
    for tname, kws in _MOVER_THEMES:
        syms = [m["symbol"] for m in losers
                if any(k in (m.get("name", "").lower()) for k in kws)]
        syms = [s for s in syms if s not in dused]
        if not syms:
            continue
        kr_theme, _ = _US_TO_KR.get(tname, (tname, []))
        down_flow.append({
            "usTheme": tname,
            "usSymbols": syms[:4],
            "krTheme": kr_theme,
            "krNames": [],
            "rationale": f"미국 {tname} 급락 → 국내 {kr_theme} 약세 주의",
        })
        dused.update(syms)

    catalyst = " / ".join(f"{p['name']}: {p.get('catalyst', '')}".strip(": ")
                          for p in picks[:5]) or "특이 촉매 없음"

    return {
        "stance": stance,
        "stanceReason": stance_reason,
        "usReview": {
            "narrative": mv or "전일 미국장 데이터 기반 자동 요약입니다.",
        },
        "flow": flow,
        "downFlow": down_flow,
        "krPreview": {
            "narrative": "미국 강세 테마·급등주와 국내 밸류체인 동조 흐름을 관찰합니다. "
                         "(LLM 미가동 — 기계적 요약)",
            "catalystSummary": catalyst,
        },
    }


# ----------------------------------------------------------------------------
# 출력
# ----------------------------------------------------------------------------
def write_outputs(payload, date_str):
    os.makedirs(OUT_DIR, exist_ok=True)

    dated = os.path.join(OUT_DIR, f"{date_str}.json")
    with open(dated, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  Saved briefing : {dated}")

    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("  Updated        : latest.json")

    index_path = os.path.join(OUT_DIR, "index.json")
    index = []
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        except Exception:
            index = []
    if date_str not in index:
        index.append(date_str)
    index = sorted({d for d in index if re.match(r"^\d{4}-\d{2}-\d{2}$", d)}, reverse=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    print(f"  Updated index  : {index_path} ({len(index)} entries)")


def main():
    date_str = os.environ.get("BRIEFING_DATE") or datetime.datetime.now(KST).strftime("%Y-%m-%d")
    print(f"=== Morning briefing ({date_str}) ===")

    sel, sel_path = _load_selection(date_str)
    print(f"  Selection      : {os.path.basename(sel_path)} "
          f"(picks {len(sel.get('picks', []))}, movers {len(sel.get('usMarket', {}).get('movers', []))})")

    brief, model = llm_briefing(sel)
    if brief is None:
        brief = _mechanical(sel)
        generated_by = "mechanical"
    else:
        generated_by = model
    print(f"  Generated by   : {generated_by} · stance={brief.get('stance')}")

    payload = {
        "date": sel.get("date", date_str),
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "sourceAsof": sel.get("asof", ""),
        "generatedBy": generated_by,
        "stance": brief["stance"],
        "stanceReason": brief["stanceReason"],
        "usReview": brief["usReview"],
        "flow": brief.get("flow", []),
        "downFlow": brief.get("downFlow", []),
        "krPreview": brief["krPreview"],
        # 카드·테이블 렌더용 원본 데이터(프런트가 그대로 사용)
        "usMarket": sel.get("usMarket", {}),
        "picks": sel.get("picks", []),
        "marketView": sel.get("marketView", ""),
    }
    write_outputs(payload, date_str)
    print("=== Done ===")


if __name__ == "__main__":
    main()
