"""매매 복기(2026-11-02 1단계) — 청산이 끝난 거래를 진입 근거와 비교해 교훈을 남긴다.

배치(swing-cron, 하루 처리 뒤) → store `reflection/<청산일>` → /api/swing/reflections → 트레이딩 리포트 '매매 복기'.
**PM 판단에는 아직 넣지 않는다** — pipeline 의 "과거 맥락 없음" 은 그대로(내용을 사용자가 확인한 뒤 2단계에서 연결).

복기 단위 = 포지션 1개(분할 청산 거래는 합침). 마지막 청산까지 끝난 뒤에만 쓴다 — 보유 5영업일이라 중간 손익은 노이즈.
평가 = 포지션 수익률 − 방향 × 시장(코스피/코스닥) 수익률. 진입일 종가 → 청산일 종가, 시장 값이 없으면 수익률만.
역할 프롬프트 = kit roles/14_reflector.md(무수정). 원본 outcome.py·decision_log.py 는 로컬 파일 기반이라 가져오지 않고 store 로 다시 썼다.
복기가 실패해도 원장·판단에는 영향이 없고, 다음 런이 이어서 처리한다.
"""
import datetime
import os

from . import config, prices, tradedays
from .agent import KIT_DIR

MD_SCHEMA = {"type": "object", "properties": {"markdown": {"type": "string"}}, "required": ["markdown"]}
REASON = {"stop": "손절", "stop_gap": "갭 손절(시가)", "target": "목표 도달", "expiry": "보유 만기", "review_target": "목표 도달 후 재판별 청산",
          "review_expiry": "만기 재판별 청산", "pm_sell": "PM 청산", "switch": "신규 매수 재원 매도", "fut_none": "주식선물 미상장 환매",
          "margin": "담보 부족 반대매매", "livepm_exit": "Live PM 청산", "max_hold": "최대 보유 청산", "cash": "현금 확보 매도"}
PM_CLIP = 2500                                            # 판단 원문 길이 상한(자)


def _system():
    with open(os.path.join(KIT_DIR, "references", "roles", "14_reflector.md"), encoding="utf-8") as f:
        role = f.read()
    return (role + "\n\n---\n## 응답 규약(무인 실행)\n"
                   "JSON 객체 하나로만 답한다: {\"markdown\": \"<위 '출력' 형식의 한국어 산문>\"}.")


def _default_llm(system, user, max_tokens, schema):
    import llm                                            # 루트 llm.py (Gemini 폴백 체인)
    return llm.generate_json(system, user, max_tokens=max_tokens, schema=schema, return_model=True)


class YFinanceBench:
    """시장 지수(^KS11/^KQ11) 기간 수익률(%) — 실패하면 None(복기는 수익률만으로 진행)."""

    def __init__(self):
        self._mk = prices.YFinancePrices()
        self._close = {}

    def _series(self, sym):
        if sym not in self._close:
            import yfinance as yf
            end = datetime.date.today() + datetime.timedelta(days=1)
            df = yf.download(sym, start=(end - datetime.timedelta(days=120)).isoformat(), end=end.isoformat(),
                             progress=False, auto_adjust=False)
            c = df["Close"]
            c = c.iloc[:, 0] if hasattr(c, "columns") else c
            self._close[sym] = {str(i.date()): float(v) for i, v in c.dropna().items()}
        return self._close[sym]

    def __call__(self, code, d1, d2):
        kq = self._mk._market(code) == "KQ"
        s = self._series("^KQ11" if kq else "^KS11")
        if d1 not in s or d2 not in s:
            return None, None
        return (s[d2] / s[d1] - 1) * 100, "코스닥" if kq else "코스피"


def closed_rounds(trades):
    """청산 거래 → 끝난 포지션 목록(같은 진입의 분할 청산을 합침). 잔여 수량이 남은 포지션은 뺀다."""
    groups = {}
    for t in trades:
        k = (t["code"], t.get("side") or "long", t.get("entryDate"), t.get("decisionKey"))
        groups.setdefault(k, []).append(t)
    out = []
    for (code, side, entry_date, dkey), ts in groups.items():
        last = ts[-1]
        if last.get("partial"):
            continue
        qty = sum(t["qty"] for t in ts)
        cost = sum(t["qty"] * t["entryPrice"] for t in ts)
        pnl = sum(t.get("pnl") or 0 for t in ts)
        out.append({"key": f"{code}_{side}_{entry_date}", "code": code, "name": last.get("name"), "side": side,
                    "sector": last.get("sector"), "signal": last.get("signal"), "source": last.get("source"),
                    "entryDate": entry_date, "exitDate": last["exitDate"], "holdDays": last.get("holdDays"), "qty": qty,
                    "entryPrice": round(cost / qty, 2) if qty else None,
                    "exitPrice": round(sum(t["qty"] * t["exitPrice"] for t in ts) / qty, 2) if qty else None,
                    "reason": last.get("reason"), "note": last.get("note"), "pnl": round(pnl), "retPct": round(pnl / cost * 100, 2) if cost else 0.0,
                    "decisionKey": dkey, "exitDecisionKey": last.get("exitDecisionKey"), "exits": len(ts)})
    return out


def _clip(s, n=PM_CLIP):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …(생략)"


def _won(v):
    return f"{v:,.0f}원" if v else "없음"


def _hold_reviews(store, r):
    """보유 중 재판별(진입 판단 제외) 한 줄씩 — 판단 문서는 decision/<날짜>/<종목>/<목적>."""
    lines = []
    for d in tradedays.trading_days(r["entryDate"], r["exitDate"]):
        for k in store.keys(f"decision/{d}/{r['code']}/"):
            if k == r["decisionKey"]:
                continue
            x = store.get(k) or {}
            lines.append(f"- {d} {x.get('rating') or '-'}: {_clip(x.get('summary'), 240) or '요약 없음'}")
    return lines


def build_user(store, r, entry):
    """복기 입력 — 원본 outcome.py 출력(결정 전문·raw·alpha·보유 기간)에 스윙 체결 정보를 더한 것."""
    long_ = r["side"] == "long"
    alpha = f"{r['alphaPct']:+.2f}%({r['benchName']} {r['benchPct']:+.2f}% 대비)" if r.get("alphaPct") is not None else "산출 불가(시장 수익률 없음)"
    head = [f"## 복기 대상\n{r['name']} ({r['code']}) · {'Long(매수)' if long_ else 'Short(공매도 — 주가 하락이 수익)'} · 섹터 {r.get('sector') or '-'}"
            f" · 신호 {r.get('signal') or '-'} · 선정 경로 {r.get('source') or '-'}",
            f"## 결과\n실현 수익률 raw {r['retPct']:+.2f}%(세금·수수료 포함), 시장 대비 alpha {alpha}, 보유 {r.get('holdDays')}영업일, 확정일 {r['exitDate']}",
            f"진입 {r['entryDate']} 평단 {_won(r['entryPrice'])} → 청산 평단 {_won(r['exitPrice'])}, {r['qty']:,}주, "
            f"청산 사유 {REASON.get(r.get('reason'), r.get('reason'))}" + (f" ({r['note']})" if r.get("note") else ""),
            f"## 당시 최종 결정\n등급 {entry.get('rating')}, 계획 진입 {_won(entry.get('entry'))} · 손절 {_won(entry.get('stop'))} · 목표 {_won(entry.get('target'))}\n"
            f"요약: {_clip(entry.get('summary'), 600)}\n\n{_clip((entry.get('reports') or {}).get('pm'))}"]
    rev = _hold_reviews(store, r)
    if rev:
        head.append("## 보유 중 재판별\n" + "\n".join(rev))
    return "\n\n".join(head)


def _done(store):
    done = set()
    for k in store.keys("reflection/"):
        done.update(x["key"] for x in (store.get(k) or {}).get("items") or [])
    return done


def _save(store, item):
    k = f"reflection/{item['exitDate']}"
    doc = store.get(k) or {"date": item["exitDate"], "items": []}
    doc["items"].append(item)
    store.put(k, doc)


def run(store, date, *, llm_fn=None, bench_fn=None, log=print, limit=None):
    """date 까지 끝난 포지션 중 복기가 없는 것을 최근 청산부터 limit 건 처리. LLM 실패(한도 소진 등)면 멈추고 다음 런이 잇는다.
    반환: {written, skipped, left, error}."""
    llm_fn = llm_fn or _default_llm
    bench_fn = bench_fn or YFinanceBench()
    limit = config.REFLECT_MAX if limit is None else limit
    L = store.get("ledger") or {}
    done = _done(store)
    todo = [r for r in closed_rounds(L.get("trades") or [])
            if config.REFLECT_FROM <= r["exitDate"] <= date and r["key"] not in done]
    todo.sort(key=lambda r: r["exitDate"], reverse=True)
    res = {"written": 0, "skipped": 0, "left": max(0, len(todo) - limit), "error": None}
    system = _system()
    for r in todo[:limit]:
        entry = store.get(r["decisionKey"]) if r.get("decisionKey") else None
        if not entry:                                     # 판단 문서가 없으면 쓸 수 없다 — 표시만 남겨 다시 시도하지 않게
            _save(store, dict(r, text=None, why="진입 판단 문서 없음"))
            res["skipped"] += 1
            continue
        sign = 1 if r["side"] == "long" else -1
        try:
            bench, name = bench_fn(r["code"], r["entryDate"], r["exitDate"])
        except Exception as ex:
            log(f"[swing] 복기 시장 수익률 실패 {r['code']}: {type(ex).__name__}: {ex}")
            bench, name = None, None
        if bench is not None:
            r.update(benchPct=round(bench, 2), benchName=name, alphaPct=round(r["retPct"] - sign * bench, 2))
        try:
            data, model = llm_fn(system, build_user(store, r, entry), 1024, MD_SCHEMA)
            text = (data.get("markdown") if isinstance(data, dict) else data or "").strip()
            if not text:
                raise ValueError("빈 응답")
        except Exception as ex:
            res["error"] = f"{type(ex).__name__}: {str(ex)[:160]}"
            res["left"] += len(todo[:limit]) - res["written"] - res["skipped"]
            log(f"[swing] 복기 중단({r['code']}) — 남은 건은 다음 런: {res['error']}")
            break
        _save(store, dict(r, text=text, model=model, right=(r["alphaPct"] if "alphaPct" in r else r["retPct"]) > 0,
                          at=datetime.datetime.now().isoformat(timespec="seconds")))
        res["written"] += 1
    return res
