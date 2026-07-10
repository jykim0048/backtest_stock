#!/usr/bin/env python3
"""네이버 테마별 시세 → 전체 테마 구성종목 맵 생성 (public/assets/theme_map.json).

장중 시황의 섹터→테마 매칭이 네이버 리스트 페이지의 '주도주'(테마당 2종목) 스냅샷이나
회차 시점의 |등락률| 상위 테마 상세 fetch 에 의존하면, 주도주 교체·순위 변동으로
연결이 끊긴다(2026-07-10 케이씨텍↔HBM 실측). 전체 테마의 구성종목을 정적 맵으로
저장해 두면 매칭이 결정적이고 회차 내 상세 fetch 도 표시용으로 줄어든다.

krx_sector_map.json(KRX 업종분류, 수동·분기)과 달리 이 맵은 네이버에서 자동 생성
가능하므로 별도 파일로 두고 런타임에 종목코드로 조인한다(갱신 주기 분리).

실행: python build_theme_map.py   (theme_map.yml 이 주 1회 월요일 06:30 KST 실행,
      Railway 스케줄러 트리거 — 수동 workflow_dispatch 겸용)
"""
import os
import sys
import json
import time
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from analysis import sources

OUT_PATH = os.path.join(ROOT, "public", "assets", "theme_map.json")
KST = datetime.timezone(datetime.timedelta(hours=9))
MIN_THEMES = 50      # 이보다 적게 수집되면 부분 실패로 보고 기존 맵을 유지(중단)
FETCH_GAP = 0.3      # 테마 상세 페이지 요청 간격(초)


def main():
    print("=== Build Naver Theme Map ===")
    ranking = sources.naver_theme_ranking()
    print(f"  테마 랭킹: {len(ranking)}개")
    if len(ranking) < MIN_THEMES:
        print(f"  랭킹 수집 부족(<{MIN_THEMES}) — 기존 맵 유지, 중단", file=sys.stderr)
        sys.exit(1)

    themes, total, failed = {}, 0, 0
    for t in ranking:
        stocks = sources.naver_theme_stocks(t["no"], limit=500)   # 구성종목 전부
        if not stocks:
            failed += 1
            continue
        themes[t["no"]] = {
            "name": t["name"],
            "stocks": [{"code": s["code"], "name": s["name"]} for s in stocks],
        }
        total += len(stocks)
        time.sleep(FETCH_GAP)

    if len(themes) < MIN_THEMES:
        print(f"  구성종목 수집 부족({len(themes)}<{MIN_THEMES}) — 기존 맵 유지, 중단",
              file=sys.stderr)
        sys.exit(1)

    out = {
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "note": "네이버 테마별 시세 구성종목 전체 맵 — 장중 시황 섹터→테마 매칭용. "
                "theme_map.yml 이 주 1회(월 06:30 KST) 재생성.",
        "themes": themes,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(f"  Updated {OUT_PATH} — {len(themes)} themes, {total} memberships "
          f"(실패 스킵 {failed})")
    print("=== Done ===")


if __name__ == "__main__":
    main()
