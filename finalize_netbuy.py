#!/usr/bin/env python3
"""장 마감 후 수급 확정 패스 — 마감 회차의 섹터 가집계(잠정)를 일별 확정으로 치환.

KIS FHPTJ04160001(종목별 투자자매매동향 일별)은 15:40 이후에야 확정 집계가 반영되는
TR 이라(문서 명시), 15:40 마감 회차의 netbuy attach 는 타이밍상 확정을 못 받을 수 있다
(허브 조회 시점 ~15:43 — 집계 지연·재배포 타이밍 변수). 16:00 에 이 스크립트가
확정 집계만 한 번 더 조회해 마감 산출물을 패치한다(사용자 결정 2026-07-31 —
전체 회차 재실행은 마감 시황 LLM 중복·아카이브 회차 중복이라 배제).

동작:
  1) public/intraday_briefing.json 이 오늘자·마감 회차인지 확인(아니면 no-op).
  2) sectorsUp/Down 상위 종목별 허브 /flow 의 daily[0](당일 확정, 백만원)로 netBuy 치환
     — generate_intraday_briefing.attach_sector_stock_netbuy 의 확정 우선 로직과 동일
     기준(당일 date 매칭). 확정이 없으면 기존 값 유지(무해).
  3) 아카이브 reports/intraday_briefing/<date>.json 의 같은 asof 회차도 동일 패치.
  4) 변경이 있을 때만 저장(커밋은 워크플로가 [skip railway]로).

의존성: requests 만(경량 — generate_intraday_briefing import 는 pandas/yfinance 체인이라 회피).
"""
import datetime
import json
import os
from concurrent.futures import ThreadPoolExecutor

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
LIVE_PATH = os.path.join(ROOT, "public", "intraday_briefing.json")
ARCHIVE_DIR = os.path.join(ROOT, "public", "reports", "intraday_briefing")
KST = datetime.timezone(datetime.timedelta(hours=9))
KIS_HUB_URL = os.environ.get(
    "KIS_HUB_URL", "https://tradingstrategies-production-09d4.up.railway.app")
TOP = int(os.environ.get("BRIEFING_SECTOR_NETBUY_TOP", "5"))


def _log(msg):
    print(f"[finalize-netbuy] {msg}")


def fetch_final(code, today_c):
    """허브 /flow daily[0] 이 당일 확정이면 {frgn, orgn, fund}(백만원), 아니면 None."""
    try:
        r = requests.get(f"{KIS_HUB_URL}/flow", params={"code": code}, timeout=12)
        r.raise_for_status()
        d = r.json()
        if d.get("status") != "success":
            return None
        d0 = (d.get("daily") or [None])[0]
        if d0 and str(d0.get("date")) == today_c:
            return {"frgn": d0.get("frgn"), "orgn": d0.get("orgn"), "fund": d0.get("fund")}
    except Exception as e:
        _log(f"/flow {code} 실패(유지): {e}")
    return None


def patch_sectors(sectors_up, sectors_down, finals):
    """finals(code→nb)로 섹터 종목 netBuy 치환. 변경 수 반환. 전종목 확정 섹터에 플래그."""
    changed = 0
    for s in (sectors_up or []) + (sectors_down or []):
        stocks = (s.get("stocks") or [])[:TOP]
        for x in stocks:
            nb = finals.get(x.get("code"))
            if nb and any(v is not None for v in nb.values()) and x.get("netBuy") != nb:
                x["netBuy"] = nb
                x["_fin"] = True
                changed += 1
            elif nb:
                x["_fin"] = True
        got = [x for x in stocks if x.get("netBuy")]
        if got and all(x.get("_fin") for x in got):
            if not s.get("netBuyFinal"):
                s["netBuyFinal"] = True
                changed += 1
        for x in stocks:
            x.pop("_fin", None)
    return changed


def main():
    now = datetime.datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    today_c = now.strftime("%Y%m%d")
    try:
        with open(LIVE_PATH, encoding="utf-8") as f:
            live = json.load(f) or {}
    except Exception as e:
        _log(f"라이브 파일 없음 — 종료: {e}")
        return
    if live.get("date") != today:
        _log(f"오늘자 아님(date={live.get('date')}) — 종료")
        return

    codes = sorted({x.get("code")
                    for s in (live.get("sectorsUp") or []) + (live.get("sectorsDown") or [])
                    for x in (s.get("stocks") or [])[:TOP] if x.get("code")})
    if not codes:
        _log("섹터 종목 없음 — 종료")
        return
    with ThreadPoolExecutor(max_workers=8) as pool:
        finals = {c: nb for c, nb in zip(codes, pool.map(
            lambda c: fetch_final(c, today_c), codes)) if nb}
    _log(f"확정 조회: {len(finals)}/{len(codes)}종목")
    if not finals:
        _log("확정 데이터 없음(집계 지연/허브 구버전) — 종료(기존 값 유지)")
        return

    n_live = patch_sectors(live.get("sectorsUp"), live.get("sectorsDown"), finals)
    changed_any = False
    if n_live:
        with open(LIVE_PATH, "w", encoding="utf-8") as f:
            json.dump(live, f, ensure_ascii=False, indent=1)
        changed_any = True
    _log(f"라이브 패치: {n_live}건")

    arch_path = os.path.join(ARCHIVE_DIR, f"{today}.json")
    try:
        with open(arch_path, encoding="utf-8") as f:
            arch = json.load(f) or {}
        rounds = arch.get("rounds") or []
        # 같은 asof 회차(보통 마지막=마감)만 패치 — 과거 회차의 '그 시점 가집계'는 보존
        n_arch = 0
        for rd in rounds:
            if rd.get("asof") == live.get("asof"):
                n_arch = patch_sectors(rd.get("sectorsUp"), rd.get("sectorsDown"), finals)
                break
        if n_arch:
            with open(arch_path, "w", encoding="utf-8") as f:
                json.dump(arch, f, ensure_ascii=False, indent=1)
            changed_any = True
        _log(f"아카이브 패치: {n_arch}건")
    except Exception as e:
        _log(f"아카이브 패치 실패(라이브만): {e}")

    _log("완료 — 변경 " + ("있음(커밋 대상)" if changed_any else "없음"))


if __name__ == "__main__":
    main()
