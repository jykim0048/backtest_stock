#!/usr/bin/env python3
"""애프터마켓 실측 프로브 — 2026-09-14 KRX 애프터마켓(16:00~20:00 접속매매) 대비.

미확정 분기점(PROGRESS '애프터마켓 대응 체크리스트')을 실측으로 판정한다:
  C: KIS 종목별 투자자 확정 수급(FHPTJ04160001, 허브 /flow daily)의 '당일' 행이
     애프터마켓 거래로 20:00 이후 바뀌는가(= 16:10 파이프라인 기준 재정의 필요 여부)
  A(부분): 업종 현재지수 등락률·등락 종목수(허브 /breadth)가 애프터마켓 중 갱신되는가
     + 당일 종가(daily close)가 정규장 종가로 유지되는가(B 재확인)

같은 날 16:12(애프터 초반)·20:35(애프터 종료 후) 두 번 실행해 같은 종목 값을 비교한다.
기준선: 9/11(금)은 애프터마켓 도입 전(구 시간외 단일가 16~18시) — 9/14 결과와 대조.
산출: public/reports/aftermarket_probe/<YYYY-MM-DD>_<HHMM>.json (두 번째 실행은 첫
실행과의 차이 요약 diff 포함). 표준 라이브러리만 사용(워크플로 설치 단계 없음).
실행: Actions aftermarket_probe.yml (railway 스케줄러가 기간 한정 dispatch). PC 실행 금지.
"""
import os
import sys
import json
import glob
import datetime
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "public", "reports", "aftermarket_probe")
KST = datetime.timezone(datetime.timedelta(hours=9))
HUB = os.environ.get("FLOW_RANK_URL",
                     "https://tradingstrategies-production-09d4.up.railway.app/flow-rank"
                     ).rsplit("/", 1)[0]
# 시총·거래대금 상위 대형주(코스피 7 + 코스닥 3) — 애프터마켓 거래가 확실히 있는 종목
CODES = {"005930": "삼성전자", "000660": "SK하이닉스", "373220": "LG에너지솔루션",
         "207940": "삼성바이오로직스", "005380": "현대차", "035420": "NAVER",
         "068270": "셀트리온", "247540": "에코프로비엠", "086520": "에코프로",
         "196170": "알테오젠"}
FLOW_KEYS = ("close", "rate", "frgn", "orgn", "prsn", "fund", "scrt", "insu", "ivtr", "pe")


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "aftermarket-probe"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def collect(now):
    today = now.strftime("%Y%m%d")
    flow = {}
    for code, name in CODES.items():
        try:
            d = _get(f"{HUB}/flow?code={code}&rows=3")
            row = next((r for r in (d.get("daily") or []) if str(r.get("date")) == today), None)
            flow[code] = {"name": name, "asof": d.get("asof"),
                          "today": {k: row.get(k) for k in FLOW_KEYS} if row else None}
        except Exception as ex:
            flow[code] = {"name": name, "error": str(ex)[:120]}
    try:
        b = _get(f"{HUB}/breadth", timeout=120)
        breadth = {"asof": b.get("asof"), "counts": b.get("counts"),
                   "newHighs": len(b.get("newHighs") or [])}
    except Exception as ex:
        breadth = {"error": str(ex)[:120]}
    return {"date": now.date().isoformat(), "hhmm": now.strftime("%H%M"),
            "asof": now.strftime("%Y-%m-%d %H:%M:%S KST"), "flow": flow, "breadth": breadth}


def diff(a, b):
    """같은 날 앞선 스냅샷 a 대비 b 의 변화 — 수급·종가·등락수 변동 여부."""
    out = {"base": a.get("hhmm"), "cmp": b.get("hhmm"), "flow": {}, "breadth": {}}
    changed = 0
    for code, fb in (b.get("flow") or {}).items():
        ta = ((a.get("flow") or {}).get(code) or {}).get("today") or {}
        tb = (fb or {}).get("today") or {}
        dd = {k: [ta.get(k), tb.get(k)] for k in FLOW_KEYS
              if ta.get(k) is not None and tb.get(k) is not None and ta.get(k) != tb.get(k)}
        if dd:
            changed += 1
            out["flow"][code] = {"name": fb.get("name"), **dd}
    ca = ((a.get("breadth") or {}).get("counts") or {})
    cb = ((b.get("breadth") or {}).get("counts") or {})
    for mk in ("kospi", "kosdaq"):
        xa, xb = ca.get(mk) or {}, cb.get(mk) or {}
        dd = {k: [xa.get(k), xb.get(k)] for k in set(xa) | set(xb) if xa.get(k) != xb.get(k)}
        if dd:
            out["breadth"][mk] = dd
    out["verdict"] = {
        "C_flow_changed_after_close": f"{changed}/{len(CODES)} 종목 당일 확정 수급·종가 변동",
        "A_breadth_changed": bool(out["breadth"]),
    }
    return out


def main():
    now = datetime.datetime.now(KST)
    snap = collect(now)
    os.makedirs(OUT_DIR, exist_ok=True)
    prev = sorted(p for p in glob.glob(os.path.join(OUT_DIR, f"{snap['date']}_*.json"))
                  if not p.endswith(f"_{snap['hhmm']}.json"))
    if prev:
        with open(prev[0], encoding="utf-8") as f:
            snap["diff"] = diff(json.load(f), snap)
    path = os.path.join(OUT_DIR, f"{snap['date']}_{snap['hhmm']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    ok = sum(1 for v in snap["flow"].values() if v.get("today"))
    print(f"[probe] {path} · 당일 수급 행 {ok}/{len(CODES)} · breadth "
          f"{'ok' if 'counts' in snap['breadth'] else snap['breadth'].get('error')}")
    if snap.get("diff"):
        print("[probe] diff:", json.dumps(snap["diff"]["verdict"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
