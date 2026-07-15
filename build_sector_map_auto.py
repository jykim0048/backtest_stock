#!/usr/bin/env python3
"""KIS 마스터파일 기반 섹터맵 자동 생성 — krx_sector_map.json 재생성 (주 1회).

KRX 정보데이터시스템은 해외 IP 차단이라 업종분류 엑셀을 수동 다운로드해야 했다.
KIS 가 공개 CDN 으로 배포하는 종목 마스터(kospi_code.mst / kosdaq_code.mst)에는
전 종목의 '지수업종 대/중분류 코드'(= KIS 산업별 지수의 분류 그 자체)와 전일
시가총액이 들어 있어 인증·엑셀 없이 지수 정합 100% 로 자동화한다(2026-07-13).

파싱은 폭 테이블 대신 앵커 방식 — 고정부 시작의 그룹코드('ST') 위치를 탐지(pad)해
앞쪽 필드(그룹·시총규모·대분류·중분류)를 읽고, 시가총액은 레코드 끝에서 역산
([-15:-6], 그룹사코드3+플래그3 앞 9자리) — 중간 필드 폭 오차와 무관하게 견고하다.

- KOSPI 세부 업종: 중분류 코드(0005 음식료·담배 ~ 0030 오락·문화) → 업종명은
  KIS 허브 /status(sectors code·name) 실측 고정 테이블 + 실행 시 허브 갱신 시도.
- 대분류 금융(0021)·제조(0027): 종목 레벨 '대분류' 코드로 직접 그룹핑 —
  지수 구성 실측(금융 = 금융98+증권29+보험14)과 동일 기준. KB금융처럼 중분류가
  0000 인 종목도 대분류로 잡힌다.
- KOSDAQ: 중분류 코드가 별도 번호 체계(1xxx)라, 기존 맵의 kosdaqStocks 멤버십과
  다수결 대조로 코드→업종명을 부트스트랩(표본 3 미만이면 기존 유지). 중분류
  0000(미분류) 종목은 제외.
- 자동 생성이 못 채운 기존 항목·필드는 보존(안전망) + 기존 맵 대비 일치율 출력.

실행: python build_sector_map_auto.py [--insecure(로컬 테스트용)] [--dry-run]
theme_map.yml(월 06:30 KST)이 테마맵 재생성과 함께 실행한다.
"""
import io
import os
import re
import sys
import json
import zipfile
import argparse
import datetime
from collections import Counter

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "public", "assets", "krx_sector_map.json")
KST = datetime.timezone(datetime.timedelta(hours=9))
TOP = 12
MST_URL = "https://new.real.download.dws.co.kr/common/master/{name}.mst.zip"
HUB_STATUS = os.environ.get(
    "KIS_HUB_URL", "https://tradingstrategies-production-09d4.up.railway.app") + "/status"

# KOSPI 지수업종 코드 → 업종명 (KIS 허브 sectors 실측, 2026-07-13).
# 0021 금융·0027 제조는 '대분류' 코드, 나머지는 '중분류' 코드.
_KOSPI_SECTOR_NAME = {
    "0005": "음식료·담배", "0006": "섬유·의류", "0007": "종이·목재", "0008": "화학",
    "0009": "제약", "0010": "비금속", "0011": "금속", "0012": "기계·장비",
    "0013": "전기·전자", "0014": "의료·정밀기기", "0015": "운송장비·부품", "0016": "유통",
    "0017": "전기·가스", "0018": "건설", "0019": "운송·창고", "0020": "통신",
    "0021": "금융", "0024": "증권", "0025": "보험", "0026": "일반서비스",
    "0027": "제조", "0028": "부동산", "0029": "IT 서비스", "0030": "오락·문화",
}
_BIG_CODES = ("0021", "0027")     # 대분류 코드로 그룹핑할 항목(금융·제조)

_TAIL = {"kospi_code": 228, "kosdaq_code": 222}


def _parse_mst(name, verify=True):
    """마스터 파일 → [{code, name, big(대분류), mid(중분류), cap}] (보통주·주권만)."""
    r = requests.get(MST_URL.format(name=name), timeout=60, verify=verify)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        raw = z.read(f"{name}.mst").decode("cp949", errors="replace")
    tail = _TAIL[name]
    rows = [row for row in raw.splitlines() if len(row) > tail + 21]
    # 그룹코드('ST') 시작 오프셋(pad) 탐지 — 폭 테이블 오차와 무관하게 정렬
    c = Counter()
    for row in rows[:500]:
        p2 = row[-tail:]
        for pad in (0, 1, 2):
            if p2[pad:pad + 2] == "ST":
                c[pad] += 1
    if not c:
        return []
    pad = c.most_common(1)[0][0]

    out = []
    for row in rows:
        p1, p2 = row[:-tail], row[-tail:]
        code = p1[0:9].strip()
        kname = p1[21:].strip()
        if not (len(code) == 6 and code.isdigit() and code.endswith("0")):
            continue                                   # 보통주(끝자리 0)만
        if p2[pad:pad + 2] != "ST":                    # 주권만(ETF/ETN/리츠 등 제외)
            continue
        big = p2[pad + 3: pad + 7]                     # 그룹2+시총규모1 다음 4자리
        mid = p2[pad + 7: pad + 11]
        cap_s = p2[tail - 15: tail - 6].strip()        # 끝 역산: 시가총액9 (그룹사3+플래그3 앞)
        try:
            cap = float(cap_s or 0)
        except ValueError:
            cap = 0.0
        out.append({"code": code, "name": kname, "big": big, "mid": mid, "cap": cap})
    return out


def _kospi_names():
    """업종코드→업종명 — 허브 /status 갱신 시도, 실패 시 고정 테이블."""
    try:
        r = requests.get(HUB_STATUS, timeout=10)
        m = {str(s.get("code")).zfill(4): s.get("name")
             for s in (r.json() or {}).get("sectors") or []
             if s.get("code") and s.get("name")}
        fresh = {c: n for c, n in m.items() if "코스피" not in n}   # 합성 코스피 지수 제외
        if len(fresh) >= 20:
            return {**_KOSPI_SECTOR_NAME, **fresh}
    except Exception:
        pass
    return dict(_KOSPI_SECTOR_NAME)


def _norm(name):
    return re.sub(r"[\s·・()]", "", str(name or "")).strip()


def _top(rows):
    # cap(전일 시가총액, 마스터파일 원값)은 섹터→테마 매칭의 기여도 가중용 —
    # 상대 비중만 쓰므로 단위 무관. 구버전 맵(cap 없음)은 기여항이 자동 비활성.
    rows = sorted(rows, key=lambda s: s["cap"], reverse=True)
    return [{"code": s["code"], "name": s["name"], "cap": s["cap"]} for s in rows[:TOP]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--insecure", action="store_true", help="로컬 테스트용 SSL 검증 생략")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--out", default=OUT_PATH)
    args = ap.parse_args()
    verify = not args.insecure
    if args.insecure:
        requests.packages.urllib3.disable_warnings()

    print("=== Build sector map from KIS master files ===")
    kospi = _parse_mst("kospi_code", verify)
    kosdaq = _parse_mst("kosdaq_code", verify)
    print(f"  마스터: KOSPI {len(kospi)} / KOSDAQ {len(kosdaq)} 보통주")
    if len(kospi) < 500 or len(kosdaq) < 800:
        print("  마스터 파싱 부족 — 기존 맵 유지, 중단", file=sys.stderr)
        sys.exit(1)

    names = _kospi_names()
    try:
        with open(args.out, encoding="utf-8") as fp:
            old = (json.load(fp) or {}).get("sectors") or {}
    except Exception:
        old = {}

    sectors = {}

    # ── KOSPI 세부 업종(중분류) + 대분류(금융·제조) ──────────────────────────
    by_mid, by_big = {}, {}
    for s in kospi:
        by_mid.setdefault(s["mid"], []).append(s)
        by_big.setdefault(s["big"], []).append(s)
    for code, name in names.items():
        rows = by_big.get(code) if code in _BIG_CODES else by_mid.get(code)
        if rows:
            sectors.setdefault(_norm(name), {"name": name})["stocks"] = _top(rows)

    # ── KOSDAQ: 기존 맵 멤버십 다수결로 중분류 코드 → 업종 부트스트랩 ─────────
    old_kq = {s.get("code"): key for key, e in old.items()
              for s in (e.get("kosdaqStocks") or [])}
    kq_by_mid = {}
    for s in kosdaq:
        if s["mid"] and s["mid"] != "0000":            # 미분류 제외
            kq_by_mid.setdefault(s["mid"], []).append(s)
    n_boot = 0
    for mid, rows in kq_by_mid.items():
        votes = Counter(old_kq[s["code"]] for s in rows if s["code"] in old_kq)
        if not votes:
            continue
        key, cnt = votes.most_common(1)[0]
        if cnt < 3:                                    # 표본 부족 — 기존 유지(안전)
            continue
        sectors.setdefault(key, {"name": (old.get(key) or {}).get("name", key)})
        sectors[key]["kosdaqStocks"] = _top(rows)
        n_boot += 1

    # 자동 생성이 못 채운 기존 항목·필드 보존(안전망 — 금융·제조 KOSDAQ 합성 포함)
    for key, e in old.items():
        sectors.setdefault(key, dict(e))
        for fld in ("stocks", "kosdaqStocks"):
            if e.get(fld) and not sectors[key].get(fld):
                sectors[key][fld] = e[fld]

    # 시총(cap) 백필 — 보존 경로로 들어온 항목(자동생성 미커버 업종: 오락·문화 등 15개
    # 실측, 2026-07-16)은 cap 이 없어 기여도 가중이 비활성된다. 마스터파일에는 전 종목
    # cap 이 있으므로 코드 조회로 전체 백필(자동생성분 포함 최신값으로 갱신).
    cap_by_code = {s["code"]: s["cap"] for s in kospi + kosdaq if s.get("cap")}
    n_fill = 0
    for e in sectors.values():
        for fld in ("stocks", "kosdaqStocks"):
            for s in e.get(fld) or []:
                c = cap_by_code.get(s.get("code"))
                if c:
                    s["cap"] = c
                    n_fill += 1
    print(f"  시총 백필: {n_fill}종목 (마스터 코드 조회)")

    n_k = sum(len(e.get("stocks") or []) for e in sectors.values())
    n_q = sum(len(e.get("kosdaqStocks") or []) for e in sectors.values())
    print(f"  생성: {len(sectors)} 섹터, KOSPI {n_k} / KOSDAQ {n_q} "
          f"(코스닥 부트스트랩 {n_boot}업종)")

    # 검증 리포트 — 기존 맵과의 일치율(급변 시 사람이 확인)
    for fld, label in (("stocks", "KOSPI"), ("kosdaqStocks", "KOSDAQ")):
        agree = total = 0
        for key, e in old.items():
            old_codes = {s["code"] for s in e.get(fld) or []}
            new_codes = {s["code"] for s in (sectors.get(key) or {}).get(fld) or []}
            if old_codes:
                total += len(old_codes)
                agree += len(old_codes & new_codes)
        if total:
            print(f"  기존 맵 대비 {label} 일치율: {agree}/{total} ({agree / total * 100:.0f}%)")

    out = {
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "note": "KIS 마스터파일 자동 생성 — 지수업종 분류 기반(지수 정합). "
                "theme_map.yml 이 주 1회(월 06:30 KST) 재생성. "
                "stocks=KOSPI 시총상위, kosdaqStocks=KOSDAQ 시총상위(테마 매칭 확장).",
        "sectors": sectors,
    }
    if args.dry_run:
        print("  [dry-run] 저장 생략")
        return
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    print(f"  Updated {args.out}")
    print("=== Done ===")


if __name__ == "__main__":
    main()
