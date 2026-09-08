#!/usr/bin/env python3
"""장 마감 후 장중 촉매 스코어링 — 당일 전 회차 촉매를 모아 5점 만점 별점.

평일 16:05(수급 확정 16:00 이후, 주간 브리핑 16:10 이전) 실행:
  1) 당일 장중 시황 아카이브(reports/intraday_briefing/<date>.json)의 전 회차에서
     국내(KOSPI/KOSDAQ) 촉매를 종목 단위로 병합 (등장 회차 수 = 지속성).
  2) 종목별 당일 등락률(네이버) 부착 — '주가 상방 압력' 판단 입력.
  3) LLM 이 촉매 중요도(뉴스/공시의 구체성·확정성) x 상방 압력으로 1~5점 스코어.
  4) 아카이브에 '16:00 촉매 스코어' 회차(scoring=True)를 추가(재실행 시 치환) —
     대시보드 촉매 카드 회차 셀렉터에서 별점(★)으로 표기.
  5) 5점 종목은 회차 fiveStar 에 담겨 주간 브리핑 촉매 타임라인에 반영된다.

라이브 스냅샷(intraday_briefing.json)은 건드리지 않는다 — 마감 시황 본문 유지.

사용: python score_catalysts.py [--date YYYY-MM-DD] [--mock]
  --mock  LLM 없이 결정적 휴리스틱 점수(로컬 UI 검증용 — 커밋 금지)
"""
import os
import sys
import json
import datetime
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import llm

KST = datetime.timezone(datetime.timedelta(hours=9))
ARCHIVE_DIR = os.path.join(ROOT, "public", "reports", "intraday_briefing")

_SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {"type": "object", "properties": {
        "stock": {"type": "string"},
        "score": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string"}},
        "required": ["stock", "score", "reason"]}}},
    "required": ["scores"],
}

_SYSTEM = (
    "너는 한국 주식 장중 촉매를 평가하는 애널리스트다. 각 종목의 촉매를 1~5점으로 "
    "스코어링한다. 기준은 두 축의 종합이다:\n"
    "① 촉매 중요도 — 확정적·구체적 재료(대규모 수주/공급계약, 규제 승인, 확정 실적 "
    "서프라이즈, 단독 공급 등 공시·구체 뉴스)일수록 높고, 루머·단순 테마 편승·재탕 "
    "보도일수록 낮다.\n"
    "② 주가 상방 압력 — 당일 등락률(changePct)·상한가 여부·방향(direction). 하방 "
    "촉매(악재)나 하락 종목은 낮게.\n"
    "5점: 확정적 대형 호재 + 강한 상방(대략 +10% 이상·상한가급). 4점: 명확한 호재 + "
    "뚜렷한 강세. 3점: 재료 보통 또는 주가 반응 제한. 2점: 재료 약함·반응 미미. "
    "1점: 악재이거나 하락. 여러 회차(rounds) 연속 등장은 지속성 가점 요인. "
    "reason 은 한 문장(70자 이내) — 점수 정당화 문구('상방 압력 강함/뚜렷함' 류 평가 "
    "표현)는 금지하고, 주가를 움직인 '원인'을 분석해 쓴다: 어떤 공시·뉴스·수급 재료가 "
    "무슨 이유로 주가를 움직였는지(재료의 구체 내용 중심). 등락률·별점은 별도 컬럼에 "
    "표기되니 수치 재인용 금지. 입력에 없는 사실 금지. "
    "모든 입력 종목에 대해 빠짐없이 score 를 반환한다."
)


def _load_master():
    """이름 → (code, market). krx_companies + 섹터맵 약명 별칭."""
    by_name = {}
    try:
        with open(os.path.join(ROOT, "public", "assets", "krx_companies.json"),
                  encoding="utf-8") as f:
            for c in json.load(f):
                code = str(c.get("code", "")).zfill(6)
                name = (c.get("name") or "").strip()
                if code and name and name not in by_name:
                    by_name[name] = code
    except Exception as ex:
        print(f"[score] master load 실패: {ex}", file=sys.stderr)
    try:
        with open(os.path.join(ROOT, "public", "assets", "krx_sector_map.json"),
                  encoding="utf-8") as f:
            sm = json.load(f) or {}
        for s in (sm.get("sectors") or {}).values():
            for x in (s.get("stocks") or []) + (s.get("kosdaqStocks") or []):
                name = (x.get("name") or "").strip()
                code = str(x.get("code") or "").zfill(6)
                if name and code and name not in by_name:
                    by_name[name] = code
    except Exception:
        pass
    return by_name


def _naver_rate(code):
    """당일 등락률(%) — 네이버 종목 기본 API. 실패 시 None."""
    try:
        req = urllib.request.Request(f"https://m.stock.naver.com/api/stock/{code}/basic",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            d = json.loads(r.read().decode("utf-8"))
        v = str(d.get("fluctuationsRatio") or "").replace(",", "")
        return float(v) if v not in ("", "None") else None
    except Exception:
        return None


def collect(rounds):
    """국내 촉매를 종목 단위로 병합 — {stock: entry}. 미국(US) 촉매는 제외."""
    merged = {}
    for rd in rounds:
        if rd.get("scoring"):
            continue
        for c in (rd.get("catalysts") or []):
            if c.get("market") not in ("KOSPI", "KOSDAQ"):
                continue
            name = (c.get("stock") or "").strip()
            if not name:
                continue
            e = merged.setdefault(name, {
                "stock": name, "market": c.get("market"),
                "direction": c.get("direction"), "kind": c.get("kind"),
                "summary": c.get("summary") or "", "url": c.get("url"),
                "rounds": 0, "summaries": []})
            e["rounds"] += 1
            s = (c.get("summary") or "").strip()
            if s and s not in e["summaries"]:
                e["summaries"].append(s)
            # 마지막 회차 값으로 대표 필드 갱신(방향 전환 반영)
            e["direction"] = c.get("direction") or e["direction"]
            e["kind"] = c.get("kind") or e["kind"]
            e["summary"] = s or e["summary"]
    return merged


def _mock_score(e):
    r = e.get("changePct")
    sc = 3
    if r is not None:
        sc = 5 if r >= 10 else 4 if r >= 5 else 3 if r >= 0 else 1
    if e.get("direction") == "bearish":
        sc = min(sc, 2)
    return sc, f"[mock] 등락률 {r}% 휴리스틱"


def main():
    args = sys.argv[1:]
    date = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    if "--date" in args:
        date = args[args.index("--date") + 1]
    mock = "--mock" in args

    path = os.path.join(ARCHIVE_DIR, f"{date}.json")
    try:
        with open(path, encoding="utf-8") as f:
            arch = json.load(f)
    except OSError:
        print(f"[score] {date} 아카이브 없음 — 종료")
        return 0
    rounds = arch.get("rounds") or []
    merged = collect(rounds)
    if not merged:
        print(f"[score] {date} 국내 촉매 없음 — 종료")
        return 0

    by_name = _load_master()
    for name, e in merged.items():
        e["code"] = by_name.get(name)
    with ThreadPoolExecutor(max_workers=6) as tp:
        rates = dict(tp.map(lambda kv: (kv[0], _naver_rate(kv[1]["code"]) if kv[1]["code"] else None),
                            merged.items()))
    for name, e in merged.items():
        e["changePct"] = rates.get(name)
    print(f"[score] 대상 {len(merged)}종목 (코드 매칭 {sum(1 for e in merged.values() if e['code'])}, "
          f"등락률 {sum(1 for e in merged.values() if e['changePct'] is not None)})")

    entries = list(merged.values())
    if mock or not llm.configured():
        if not mock:
            print("[score] LLM 미설정 — mock 휴리스틱으로 대체", file=sys.stderr)
        for e in entries:
            e["score"], e["reason"] = _mock_score(e)
        generated_by = "mock"
    else:
        user = json.dumps([{k: e.get(k) for k in
                            ("stock", "market", "direction", "kind", "changePct",
                             "rounds", "summaries")} for e in entries], ensure_ascii=False)
        data, generated_by = llm.generate_json(_SYSTEM, user, max_tokens=8192,
                                               schema=_SCHEMA, return_model=True)
        smap = {s.get("stock"): s for s in (data.get("scores") or [])}
        for e in entries:
            hit = smap.get(e["stock"]) or {}
            e["score"] = max(1, min(5, int(hit.get("score") or 3)))
            e["reason"] = (hit.get("reason") or "")[:120]

    for e in entries:
        e.pop("summaries", None)
    entries.sort(key=lambda e: (-(e.get("score") or 0), -(e.get("changePct") or -999)))
    five = [e["stock"] for e in entries if e.get("score") == 5]

    scoring_round = {
        "date": date,
        "asof": f"{date} 16:00 KST",
        "scoring": True,
        "generatedBy": generated_by,
        "fiveStar": five,
        "catalysts": entries,
    }
    rounds = [rd for rd in rounds if not rd.get("scoring")] + [scoring_round]
    arch["rounds"] = rounds
    with open(path, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False, indent=1)
    print(f"[score] 저장: {path} — {len(entries)}종목, 5점 {len(five)}개 {five}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
