#!/usr/bin/env python3
"""장중 회차마다 수급(flow)만 갱신 — LLM 재분석 없이 리포트의 analysis["flow"] 교체.

장전 워치리스트·장중 관심종목·하락 관찰 리포트의 기존 딥리서치(뉴스·DART·
flowComment 등)는 그대로 이월하고, 결정적 수급 데이터만 최신화한다:
  - KIS 허브 /flow: 당일 가집계(잠정) 랭킹 + 공매도/대차 일별
  - 네이버 trend: 일자별 외국인/기관/개인 순매매량(주)·외국인 보유율

비용: LLM 0 (flow 는 LLM 미경유 결정적 데이터), KIS 종목당 4~5콜(허브 종목
10분 캐시·랭킹 전역 5분 캐시 공유), 네이버 종목당 1콜. 조회 실패 종목은
기존 flow 를 유지한다(덮어쓰지 않음 — 장중 회차 실패가 수급 표를 비우지 않게).
intraday_screener.yml 이 회차마다(:07/:37) 딥리서치 스텝 뒤에 실행한다.
"""
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor

from analysis import sources
from generate_analysis import fetch_investor_flow

ROOT = os.path.dirname(os.path.abspath(__file__))
REPORTS = [
    os.path.join(ROOT, "public", "daily_market_report.json"),    # 장전 워치리스트
    os.path.join(ROOT, "public", "intraday_report.json"),        # 장중 관심종목
    os.path.join(ROOT, "public", "intraday_down_report.json"),   # 하락 관찰
]


def _fresh_flow(code):
    """(허브 flow dict|None, 네이버 trend list|None) — 실패한 파트만 None.

    부분 실패(예: 허브 타임아웃, 네이버만 성공)에서 성공 파트만 갱신하고
    실패 파트는 기존 값을 유지하기 위해 파트별로 반환한다. 첫 회차 캐시
    콜드 상태에서 허브가 12초를 넘기는 사례가 있어 타임아웃 25초.
    """
    kis = trend = None
    try:
        f = fetch_investor_flow(code, timeout=25)   # 실패는 내부에서 {} 반환
        if f:
            kis = f
    except Exception as e:
        print(f"[flow-refresh] {code} 허브 조회 실패(기존 유지): {e}", file=sys.stderr)
    try:
        trend = sources.naver_investor_trend(code) or None
    except Exception as e:
        print(f"[flow-refresh] {code} 네이버 조회 실패(기존 유지): {e}", file=sys.stderr)
    return kis, trend


def main():
    # 3개 리포트의 분석 보유 종목을 모아 중복 코드는 1회만 조회
    reports, codes = [], []
    for path in REPORTS:
        try:
            with open(path, encoding="utf-8") as fp:
                entries = json.load(fp)
        except Exception:
            continue
        if not isinstance(entries, list):
            continue
        reports.append((path, entries))
        for e in entries:
            if (isinstance(e, dict) and e.get("code")
                    and isinstance(e.get("analysis"), dict)
                    and e["code"] not in codes):
                codes.append(e["code"])
    if not codes:
        print("[flow-refresh] 대상 종목 없음 — 종료")
        return

    print(f"[flow-refresh] {len(codes)}종목 수급 갱신 시작")
    with ThreadPoolExecutor(max_workers=4) as pool:
        flows = dict(zip(codes, pool.map(_fresh_flow, codes)))
    n_kis = sum(1 for k, _ in flows.values() if k)
    n_nv = sum(1 for _, t in flows.values() if t)
    print(f"[flow-refresh] 조회 성공: 허브 {n_kis}/{len(codes)}, 네이버 {n_nv}/{len(codes)}")

    for path, entries in reports:
        changed = 0
        for e in entries:
            if not (isinstance(e, dict) and isinstance(e.get("analysis"), dict)):
                continue
            kis, trend = flows.get(e.get("code"), (None, None))
            if not kis and not trend:
                continue                       # 전체 실패 — 기존 flow 그대로
            # 파트별 병합: 성공 파트만 덮어쓰고(허브 성공 시 랭킹 []도 유효값 —
            # 랭킹 이탈 반영), 실패 파트는 기존 값 유지.
            merged = dict(e["analysis"].get("flow") or {})
            if kis:
                merged.update(kis)             # rank/shorts/loans/rankAsof
            if trend:
                merged["trend"] = trend
            e["analysis"]["flow"] = merged
            changed += 1
        if changed:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(entries, fp, ensure_ascii=False, indent=2)
            print(f"[flow-refresh] {os.path.relpath(path, ROOT)}: {changed}종목 갱신")


if __name__ == "__main__":
    main()
