#!/usr/bin/env python3
"""KIS 마스터파일 기반 섹터맵 자동 생성 — krx_sector_map.json 재생성 (평일 매일).

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
theme_map.yml(평일 06:30 KST — 2026-09-22 주 1회→매일)이 테마맵 재생성과 함께 실행한다.
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
# 전 종목 code→업종명 맵(컷 없음) — 섹터 알약/엑셀 폴백용(2026-09-09).
# TOP 컷 기반 krx_sector_map 은 지주 분류(한미사이언스→금융계)·소형주(지엘팜텍)가
# 계속 새서, 자르기 전 전 종목을 별도 파일로 보존한다.
OUT_FULL_PATH = os.path.join(ROOT, "public", "assets", "krx_code_sector.json")
# 전 종목 시총(우선주 포함) — 공매도·대차 시총대비 분모(2026-09-22)
OUT_CAPS_PATH = os.path.join(ROOT, "public", "assets", "krx_caps.json")
KST = datetime.timezone(datetime.timedelta(hours=9))
TOP = 30
# 12→30 확대(2026-09-08): 주간 브리핑 순매수 표 섹터 알약이 이 맵을 쓰는데, 업종
# 상위 12 컷 밖 종목(예: OCI홀딩스 — 금융 소속 시총 4조)이 알약 미표기되던 것.
# 소비자들은 전부 상위 K개만 사용(가집계 표시 4~5개 등)이라 확대 무해.
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
    """마스터 파일 → [{code, name, big(대분류), mid(중분류), cap, pref}] (주권만).

    pref=True 는 우선주(코드 끝자리 ≠ 0, 신형 영숫자 코드 포함). 섹터·업종 맵은
    보통주만 쓰고(호출측 필터), 우선주는 전 종목 시총 파일(krx_caps.json)에만 쓴다
    (2026-09-22 — 공매도·대차 시총대비에서 삼성전자우 등 우선주가 시총 결측)."""
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
        if not (len(code) == 6 and code.isalnum()):
            continue
        # 우선주 = 끝자리 ≠ 0(5·7·9·K 등). 신형 영숫자 보통주 코드(예: 인벤테라 0007J0)는
        # 끝자리 0 이라 보통주 — 종전 isdigit 필터가 이들을 전부 빠뜨려 업종·섹터 결측(2026-09-22)
        pref = code[5] != "0"
        if p2[pad:pad + 2] != "ST":                    # 주권만(ETF/ETN/리츠 등 제외)
            continue
        big = p2[pad + 3: pad + 7]                     # 그룹2+시총규모1 다음 4자리
        mid = p2[pad + 7: pad + 11]
        cap_s = p2[tail - 15: tail - 6].strip()        # 끝 역산: 시가총액9 (그룹사3+플래그3 앞)
        try:
            cap = float(cap_s or 0)
        except ValueError:
            cap = 0.0
        out.append({"code": code, "name": kname, "big": big, "mid": mid, "cap": cap,
                    "pref": pref})
    # 진단 프로브(2026-09-09): SECTOR_PROBE=코드,코드 — 원시 필드/고정부 덤프.
    # (지엘팜텍 204840 이 HTS 상 '유통'인데 전 종목 맵에서 빠짐 — 미분류인지
    #  파싱 오프셋 문제인지 클라우드 로그로 판별)
    probes = set(os.environ.get("SECTOR_PROBE", "").replace(" ", "").split(","))
    if probes:
        for s in out:
            if s["code"] in probes:
                print(f"  [probe {name}] {s['code']} {s['name']} big={s['big']!r} "
                      f"mid={s['mid']!r} cap={s['cap']}", file=sys.stderr)
        hit = {s["code"] for s in out}
        for p in probes:
            if p and p not in hit:
                for row in rows:
                    if row[:9].strip() == p:
                        print(f"  [probe {name}] {p} 파싱 제외됨 — p2 앞부분: "
                              f"{row[-tail:][:30]!r}", file=sys.stderr)
                        break
                else:
                    print(f"  [probe {name}] {p} 마스터에 행 없음", file=sys.stderr)
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


NAVER_DETAIL = "https://stock.naver.com/api/domestic/detail/{code}/detail"
NAVER_MIN_N = 3          # 대응표 채택 최소 표본(그 네이버 업종 중 KIS 업종이 있는 종목 수)
NAVER_MIN_SHARE = 0.6    # 최다 KIS 업종 비율 하한 — 미만이면 애매해 채우지 않음


def _naver_upjong(code):
    """네이버 종목 상세 → (upjongCode, upJongName) — 실패 시 (None, None)."""
    try:
        r = requests.get(NAVER_DETAIL.format(code=code), params={"codeType": "KRX"},
                         headers={"User-Agent": "Mozilla/5.0",
                                  "Referer": f"https://stock.naver.com/domestic/stock/{code}/price"},
                         timeout=10)
        r.raise_for_status()
        d = r.json() or {}
        c = str(d.get("upjongCode") or "").strip()
        return (c or None), (str(d.get("upJongName") or "").strip() or None)
    except Exception:
        return None, None


def _naver_map(stocks, full, fetch=None, workers=8):
    """전 종목 네이버 업종 수집 → (대응표 {naver코드: KIS업종}, 종목별 naver코드, 업종명표)."""
    from concurrent.futures import ThreadPoolExecutor
    fetch = fetch or _naver_upjong
    codes = [s["code"] for s in stocks]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        got = dict(zip(codes, ex.map(fetch, codes)))
    nv_of = {c: v[0] for c, v in got.items() if v and v[0]}
    nv_name = {v[0]: v[1] for v in got.values() if v and v[0] and v[1]}
    votes = {}
    for c, nc in nv_of.items():
        if full.get(c):
            votes.setdefault(nc, Counter())[full[c]] += 1
    table = {}
    for nc, cnt in votes.items():
        top, n = cnt.most_common(1)[0]
        total = sum(cnt.values())
        if total >= NAVER_MIN_N and n / total >= NAVER_MIN_SHARE:
            table[nc] = top
    print(f"  네이버 업종 수집: {len(nv_of)}/{len(codes)}종목 · 업종 {len(votes)}개 중 대응 채택 "
          f"{len(table)}개(표본≥{NAVER_MIN_N}·비율≥{NAVER_MIN_SHARE:.0%})")
    return table, nv_of, nv_name


def _fill_from_naver(stocks, full, fetch=None):
    """무분류 종목(full 미등재)만 네이버→KIS 대응표로 채움 — full 제자리 갱신, 채운 수 반환."""
    missing = [s for s in stocks if not full.get(s["code"])]
    if not missing:
        return 0
    table, nv_of, nv_name = _naver_map(stocks, full, fetch=fetch)
    n, skipped = 0, Counter()
    for s in missing:
        nc = nv_of.get(s["code"])
        if nc and table.get(nc):
            full[s["code"]] = table[nc]
            n += 1
        elif nc:
            skipped[nv_name.get(nc) or nc] += 1
    for nm, k in skipped.most_common(8):
        print(f"  [네이버 대응 미채택] {nm}: {k}종목 (표본 부족 또는 KIS 업종 분산)")
    probes = set(os.environ.get("SECTOR_PROBE", "").replace(" ", "").split(",")) - {""}
    for p in sorted(probes):
        print(f"  [probe naver] {p} naver={nv_of.get(p)}({nv_name.get(nv_of.get(p))}) "
              f"→ {full.get(p)}", file=sys.stderr)
    # 대응표·업종명표는 전 종목 맵 파일에 같이 저장 — 주간 브리핑 생성기가 맵 미등재
    # 코드(신규 상장·재빌드 사이)를 네이버 실시간 조회로 즉석 변환하는 데 쓴다(2026-09-22)
    return n, table, nv_name


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
    ap.add_argument("--no-naver", action="store_true",
                    help="네이버 업종 대응 보강 생략(무분류 종목은 맵에서 빠짐)")
    args = ap.parse_args()
    verify = not args.insecure
    if args.insecure:
        requests.packages.urllib3.disable_warnings()

    print("=== Build sector map from KIS master files ===")
    kospi_all = _parse_mst("kospi_code", verify)
    kosdaq_all = _parse_mst("kosdaq_code", verify)
    # 섹터·업종 맵은 종전대로 보통주만(동작 불변) — 우선주는 전 종목 시총 파일에만
    kospi = [s for s in kospi_all if not s["pref"]]
    kosdaq = [s for s in kosdaq_all if not s["pref"]]
    n_pref = len(kospi_all) + len(kosdaq_all) - len(kospi) - len(kosdaq)
    print(f"  마스터: KOSPI {len(kospi)} / KOSDAQ {len(kosdaq)} 보통주 (+우선주 {n_pref})")
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
    kq_mid_name = {}   # KOSDAQ 중분류 코드 → 업종 표시명 (전 종목 맵용)
    for mid, rows in kq_by_mid.items():
        votes = Counter(old_kq[s["code"]] for s in rows if s["code"] in old_kq)
        if not votes:
            continue
        key, cnt = votes.most_common(1)[0]
        if cnt < 3:                                    # 표본 부족 — 기존 유지(안전)
            continue
        sectors.setdefault(key, {"name": (old.get(key) or {}).get("name", key)})
        sectors[key]["kosdaqStocks"] = _top(rows)
        kq_mid_name[mid] = sectors[key].get("name") or key
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

    # ── 전 종목 code→업종명 맵 (TOP 컷 없음 — 알약/엑셀 섹터 표기 폴백) ────────
    # KOSPI: 세부(중분류) 업종명 우선, 미분류(0000 등)는 대분류(금융·제조)로 폴백.
    # KOSDAQ: 부트스트랩으로 해석된 중분류만(미해석·0000 은 제외 — 표기 없음이 정직).
    full = {}
    for s in kospi:
        nm = names.get(s["mid"]) if s["mid"] and s["mid"] != "0000" else None
        nm = nm or names.get(s["big"])
        if nm:
            full[s["code"]] = nm
    # (2026-09-09) '1xxx↔KOSPI 0xxx' 패턴 가설은 일치율 0/964 실측으로 기각 — 폴백
    # 금지. 미해석 중분류는 아래 진단 로그(코호트 예시)로 실제 업종을 파악해
    # 필요 시 고정 테이블로 추가한다.
    # KOSDAQ 대분류(big) 부트스트랩 — mid=0000(중분류 미분류) 종목의 폴백.
    # 실측: 지엘팜텍 big='1011' mid='0000'(HTS 업종 '유통') — mid만 쓰면 누락된다.
    # big 코드명도 기존 맵 멤버십 다수결로 해석(표본 3+), mid 우선/big 폴백.
    kq_by_big = {}
    for s in kosdaq:
        if s["big"] and s["big"] != "0000":
            kq_by_big.setdefault(s["big"], []).append(s)
    kq_big_name = {}
    for bigc, rows_b in kq_by_big.items():
        votes = Counter(old_kq[s["code"]] for s in rows_b if s["code"] in old_kq)
        if votes:
            key, cnt = votes.most_common(1)[0]
            if cnt >= 3:
                kq_big_name[bigc] = ((sectors.get(key) or {}).get("name")
                                     or (old.get(key) or {}).get("name") or key)
    n_bigfill = 0
    unresolved = {}
    for s in kosdaq:
        nm = kq_mid_name.get(s["mid"])
        if not nm and kq_big_name.get(s["big"]):
            nm = kq_big_name[s["big"]]
            n_bigfill += 1
        if nm:
            full[s["code"]] = nm
        elif s["mid"] and s["mid"] != "0000":
            unresolved.setdefault(s["mid"], []).append(s["name"])
    print(f"  KOSDAQ 대분류 폴백: {len(kq_big_name)}개 big 해석, {n_bigfill}종목 충전")
    for mid, ns in sorted(unresolved.items(), key=lambda x: -len(x[1]))[:15]:
        print(f"  [미해석 KOSDAQ mid {mid}] {len(ns)}종목 예: {', '.join(ns[:4])}")
    # ── 네이버 업종 → KIS 업종 대응표로 무분류 종목 보강(2026-09-22) ────────────
    # KIS 마스터에 대·중분류가 모두 0000 인 코스닥 종목(약 13%, 예: 토마토시스템·미투온)은
    # 이름 붙일 코드가 없어 전 종목 맵에서 빠졌다(촉매 타임라인 섹터 결측). 네이버 종목
    # 상세의 업종(upjongCode)을 전 종목 수집해, KIS 업종이 이미 있는 종목들로 '네이버 업종
    # → KIS 업종' 다수결 대응표를 만들고(표본 NAVER_MIN_N·최다 비율 NAVER_MIN_SHARE 이상만)
    # 무분류 종목만 KIS 체계 이름으로 채운다. 애매한 업종은 채우지 않음(정직).
    nv_table, nv_names = {}, {}
    if not args.no_naver:
        n_nv, nv_table, nv_names = _fill_from_naver(kospi + kosdaq, full)
        print(f"  네이버 업종 대응 보강: {n_nv}종목 (대응표 {len(nv_table)}업종 저장)")
    print(f"  전 종목 업종 맵: {len(full)}종목 (KOSPI+KOSDAQ, 컷 없음, "
          f"KOSDAQ 미해석 {sum(len(v) for v in unresolved.values())}종목)")

    out = {
        "asof": datetime.datetime.now(KST).strftime("%Y-%m-%d %H:%M KST"),
        "note": "KIS 마스터파일 자동 생성 — 지수업종 분류 기반(지수 정합). "
                "theme_map.yml 이 평일 매일 06:30 KST 재생성. "
                "stocks=KOSPI 시총상위, kosdaqStocks=KOSDAQ 시총상위(테마 매칭 확장).",
        "sectors": sectors,
    }
    if args.dry_run:
        print("  [dry-run] 저장 생략")
        return
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(out, fp, ensure_ascii=False, indent=1)
    print(f"  Updated {args.out}")
    with open(OUT_FULL_PATH, "w", encoding="utf-8") as fp:
        json.dump({"asof": out["asof"],
                   "note": "전 종목 code→업종명 (TOP 컷 없음 — 알약/엑셀 폴백). "
                           "naverTable=네이버 업종코드→KIS 업종명 다수결 대응표(맵 미등재 코드의 "
                           "실시간 폴백용, 2026-09-22), naverNames=네이버 업종코드→업종명",
                   "map": full, "naverTable": nv_table, "naverNames": nv_names},
                  fp, ensure_ascii=False, indent=0)
    print(f"  Updated {OUT_FULL_PATH}")
    # 전 종목 시총(우선주 포함, TOP 컷 없음) — 주간 브리핑 공매도·대차 시총대비 분모
    # (2026-09-22). 섹터맵은 업종별 상위 30 보통주라 우선주·30위 밖 종목이 결측이었다.
    caps = {s["code"]: s["cap"] for s in kospi_all + kosdaq_all if s.get("cap")}
    with open(OUT_CAPS_PATH, "w", encoding="utf-8") as fp:
        json.dump({"asof": out["asof"], "unit": "억원",
                   "note": "KIS 마스터 전일 시가총액 — 보통주+우선주 전 종목(TOP 컷 없음)",
                   "caps": caps}, fp, ensure_ascii=False, indent=0)
    print(f"  Updated {OUT_CAPS_PATH} ({len(caps)}종목, 우선주 "
          f"{sum(1 for s in kospi_all + kosdaq_all if s['pref'] and s.get('cap'))})")
    print("=== Done ===")


if __name__ == "__main__":
    main()
