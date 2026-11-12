"""예전 판단에 '당일 등락 요인 분해'(price_factors, 판단 팝업 3D) 소급 — 2026-11-12.

3D 카드는 요인 생성이 배포된 11-12 15:34 뒤의 판단(신규·재판별)에만 생긴다 → 그 전에 들어간 보유 종목(NAVER 등)은 카드가 없다.
예전 판단 문서에도 애널리스트 5역할 리포트(reports.market…flow)와 차트(chart.dates·c)가 남아 있으므로
  1) 리포트를 읽혀 역할별 요인만 뽑고(종목당 LLM 1회 — 새 분석·수집 없음)
  2) 차트 종가로 판단일 등락률을 잡아(price_factors.basis 와 같은 모양)
  3) 같은 규칙(price_factors.build — 강도 비례 배분, %p 는 코드가 계산)으로 판단 문서에 price_factors 를 붙인다.
소급분은 price_factors.backfill = {at, model, source:"reports"} 로 표시한다(대시보드 표시는 그대로 '추정').

대상 = 원장 보유 포지션·대기 주문마다 그 회차의 가장 최근 판단(재판별이 있으면 최신 재판별, 없으면 진입 판단) 중
price_factors 가 없는 것. 대시보드 API 는 같은 회차에서 요인이 있는 가장 최근 판단을 쓰므로 그 문서에 붙인다.

  python swing/tools/backfill_price_factors.py                 # 대상·기준 등락률만(LLM·쓰기 없음)
  python swing/tools/backfill_price_factors.py --llm --limit 1  # LLM 으로 요인을 뽑아 보여 주기만(쓰기 없음)
  python swing/tools/backfill_price_factors.py --apply          # 뽑아서 판단 문서에 저장
  python swing/tools/backfill_price_factors.py --apply --keys decision/2026-11-05/035420/entry
"""
import argparse
import datetime
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from swing import store as S  # noqa: E402
from swing.agent import price_factors as F  # noqa: E402

REPORT_CLIP = 6000            # 역할별 리포트 상한(자) — 지금 저장된 리포트는 1~2천 자
SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {c: F.ITEM_SCHEMA for c in F.CATS}, "required": list(F.CATS)}
SYSTEM = ("너는 한국 주식 애널리스트 리포트에서 '그날 주가 등락의 요인'만 골라내는 정리자다. 새로 분석하거나 리포트에 없는 사실을 "
          "만들지 않는다. 출력은 JSON 하나.")


def chart_basis(chart, date):
    """판단 문서 chart(dates·c) → price_factors.basis 와 같은 모양 {date, ratePct, close, prevClose, kind} | None."""
    dates, closes = (chart or {}).get("dates") or [], (chart or {}).get("c") or []
    idx = [i for i, d in enumerate(dates[:len(closes)]) if d and d <= date]
    if not idx or idx[-1] < 1:
        return None
    i = idx[-1]
    close, prev = closes[i], closes[i - 1]
    if not close or not prev:
        return None
    return {"date": dates[i], "ratePct": round((close / prev - 1) * 100, 2), "close": float(close), "prevClose": float(prev),
            "kind": "day" if dates[i] == date else "last"}


def user_prompt(doc, b):
    rep = doc.get("reports") or {}
    move = f"{b['date']} 종가 {b['close']:,.0f}원, 전일 대비 {b['ratePct']:+.2f}%"
    parts = [f"종목 {doc.get('code')} · 판단일 {doc.get('date')} · 등락: **{move}**", ""]
    for c in F.CATS:
        parts += [f"## {F.CAT_LABEL[c]} 리포트 ({c})", (rep.get(c) or "(없음)")[:REPORT_CLIP], ""]
    parts += ["## 할 일",
              f"위 다섯 리포트 각각에서, 그 리포트 관점으로 **{move}** 의 주가 움직임에 영향을 준 요인을 1~{F.MAX_PER_CAT}개 골라 "
              "같은 이름의 키(market·sentiment·news·fundamentals·flow) 배열에 넣는다.",
              "- title: 20자 안팎의 구체적인 한 줄(수치를 넣으면 좋다)",
              "- direction: 주가에 준 방향 up | down | neutral(불분명하면 neutral)",
              '- strength: 그 요인이 이 등락에 준 상대적 영향 "1"(미미) ~ "5"(결정적)',
              "- evidence: 그 리포트에 적힌 수치·사실을 인용한 근거 1문장(40자 안팎)",
              "리포트에 근거가 없는 요인은 만들지 않는다(그 역할은 빈 배열). 상승·하락을 억지로 맞추지 않는다. "
              "%p 같은 기여도 수치는 코드가 계산하므로 쓰지 않는다."]
    return "\n".join(parts)


def _default_llm(system, user, max_tokens, schema):
    import llm                                            # 루트 llm.py (Gemini 폴백 체인)
    return llm.generate_json(system, user, max_tokens=max_tokens, schema=schema, return_model=True)


def targets(st):
    """[(decision key, 원장 항목 이름)] — 회차의 가장 최근 판단, price_factors 없는 것만, 중복 없이."""
    L = st.get("ledger") or {}
    allkeys = [k for k in st.keys("decision/")]
    out, seen = [], set()
    for x in (L.get("positions") or []) + (L.get("orders") or []):
        k = x.get("decisionKey")
        if not k or k in seen:
            continue
        p = k.split("/")
        if len(p) == 4 and p[3].startswith("entry"):
            want = "review-short" if p[3].endswith("-short") else "review"
            revs = sorted((r for r in allkeys if r.endswith(f"/{p[2]}/{want}") and r.split("/")[1] > p[1]),
                          key=lambda r: r.split("/")[1])
            k = revs[-1] if revs else k
        if k in seen:
            continue
        seen.add(k)
        out.append((k, x.get("name") or ""))
    return out


def run(st, *, keys=None, llm_fn=None, use_llm=False, apply=False, limit=0, log=print):
    """반환 {"filled": n, "skipped": n, "failed": [key…]}. apply 면 판단 문서에 저장(use_llm 포함)."""
    use_llm = use_llm or apply
    llm_fn = llm_fn or _default_llm
    todo = [(k, "") for k in keys] if keys else targets(st)
    out = {"filled": 0, "skipped": 0, "failed": []}
    n = 0
    for key, name in todo:
        doc = st.get(key)
        if not doc:
            log(f"[pf] {key}: 판단 문서 없음")
            out["failed"].append(key)
            continue
        if doc.get("price_factors"):
            out["skipped"] += 1
            continue
        b = chart_basis(doc.get("chart"), doc.get("date") or key.split("/")[1])
        if not b:
            log(f"[pf] {key} {name}: 차트 종가가 없어 등락률을 못 잡음 — 건너뜀")
            out["failed"].append(key)
            continue
        if not any((doc.get("reports") or {}).get(c) for c in F.CATS):
            log(f"[pf] {key} {name}: 애널리스트 리포트 없음 — 건너뜀")
            out["failed"].append(key)
            continue
        if limit and n >= limit:
            break
        n += 1
        head = f"[pf] {key} {name} · 기준 {b['date']} {b['ratePct']:+.2f}%" + (" (판단일 이전 행)" if b["kind"] != "day" else "")
        if not use_llm:
            log(head)
            continue
        try:
            raw, model = llm_fn(SYSTEM, user_prompt(doc, b), 4096, SCHEMA)
            pf = F.build(raw if isinstance(raw, dict) else {}, b)
        except Exception as ex:                           # noqa: BLE001  한 종목 실패가 나머지를 막지 않게
            log(f"{head} · 실패: {type(ex).__name__}: {ex}")
            out["failed"].append(key)
            continue
        if not pf:
            log(f"{head} · 요인 없음(리포트에서 못 뽑음)")
            out["failed"].append(key)
            continue
        pf["backfill"] = {"at": datetime.datetime.now().isoformat(timespec="seconds"), "model": model, "source": "reports"}
        pf["method"] = "애널리스트 리포트에서 요인을 뽑아(소급) 판단일 등락률에 맞춰 배분한 추정치"
        log(head + " · " + " / ".join(f"{F.CAT_LABEL[x['cat']]} {x['title']} {x['pp']:+.1f}%p" for x in pf["items"]))
        if apply:
            st.put(key, dict(doc, price_factors=pf))
        out["filled"] += 1
    log(f"[pf] 대상 {len(todo)} · {'저장' if apply else '뽑음' if use_llm else '확인'} {out['filled'] if use_llm else n}"
        f" · 이미 있음 {out['skipped']} · 실패 {len(out['failed'])}" + ("" if apply else " (미리보기 — --apply 로 저장)"))
    return out


def _store(spec):
    if spec and spec.startswith("file:"):
        return S.FileStore(spec[5:])
    return S.from_env()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="LLM 으로 요인을 뽑아 판단 문서에 저장")
    ap.add_argument("--llm", action="store_true", help="LLM 으로 뽑아 보여 주기만(쓰기 없음)")
    ap.add_argument("--keys", default="", help="이 판단 key 만(쉼표)")
    ap.add_argument("--limit", type=int, default=0, help="LLM 부를 종목 수 상한(0 = 전부)")
    ap.add_argument("--store", default="", help="file:<dir> 이면 파일 저장소(시험용)")
    a = ap.parse_args(argv)
    keys = [k.strip() for k in a.keys.split(",") if k.strip()]
    r = run(_store(a.store), keys=keys or None, use_llm=a.llm, apply=a.apply, limit=a.limit)
    return 0 if not r["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
