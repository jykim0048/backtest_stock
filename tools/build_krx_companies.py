#!/usr/bin/env python3
"""전 상장 종목 마스터(public/assets/krx_companies.json) 자동 갱신 — KIS 마스터 + DART 이름 병합.

krx_companies.json 은 2026-06-09 KRX 상장법인목록으로 만든 뒤 갱신이 멈춰, 그 뒤의 신규 상장·
분할(영숫자 코드 0220W0 등)·이전상장·상폐가 빠져 있었다. 우선주도 없어 11/9 '한진칼우' 미등재로
주간 브리핑이 실패했다. KRX·pykrx 는 해외 IP 차단이라 Actions 에서 못 쓰므로,
이미 매일 받는 KIS 공개 마스터(build_sector_map_auto._parse_mst)로 상장 목록·시장을 잡고
이름은 기존 파일과 같은 정식 회사명 계열인 DART corp_name 으로 채운다(계획서 PLAN_KRX_COMPANIES_REFRESH.md).

규칙 — 통째 교체가 아니라 '병합'(merge):
  - 신규 상장(KIS 에 있고 파일에 없음) → 추가. 이름 = DART corp_name, 없으면 KIS 이름. 우선주는 KIS 이름 + pref:true.
  - 이전상장(시장 바뀜) → market·ticker 갱신, history.transfers 기록.
  - 사명변경 → --rename kis(기본): KIS 이름이 직전 실행과 달라졌을 때만 갱신(DART 이름 우선).
               --rename dart: DART 이름이 기존 이름과 다르면 갱신(표기 차이가 많으면 쓰지 말 것).
               --rename off: 이름 안 건드림. 옛 이름은 history.renames 에 보존.
  - 상장폐지(파일에 있고 KIS 에 없음) → 2회 연속(서로 다른 날) 없을 때만 삭제, history.delisted 로.
  - 기존 행의 키·순서·형식은 그대로(소비처 무수정). 신규 행은 끝에 붙인다.
안전장치(하나라도 걸리면 파일을 안 쓰고 종료코드 2 — 기존 파일 유지):
  - KIS 파싱: 유가 < 700 · 코스닥 < 1,500 · 합계 < 2,500.
  - 한 번에 추가 > 40 / 삭제 > 20 / 신규 상폐 후보 > 20 / 이름 변경 > 30 (FORCE=1 또는 --force 로 무시).
  - 코넥스 파싱 실패 시 코넥스 행은 손대지 않는다.

실행: DART_API_KEY=… python tools/build_krx_companies.py [--dry-run] [--force] [--rename kis|dart|off]
      theme_map.yml(평일 06:30 KST)·krx_companies.yml(수동)에서 실행. 로컬 PC 에서 직접 호출하지 말 것.
"""
import io
import os
import re
import sys
import json
import zipfile
import argparse
import datetime
import xml.etree.ElementTree as ET
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

OUT = os.path.join(ROOT, "public", "assets", "krx_companies.json")
HISTORY = os.path.join(ROOT, "public", "assets", "krx_companies_history.json")
KST = datetime.timezone(datetime.timedelta(hours=9))

# KIS 마스터 파일 → 기존 파일의 시장 표기
MARKET_OF_FILE = {"kospi_code": "유가", "kosdaq_code": "코스닥", "konex_code": "코넥스"}
SUFFIX = {"유가": ".KS", "코스닥": ".KQ", "코넥스": ".KS"}   # 코넥스는 기존 관례대로 .KS
# 남길 증권 그룹코드 — 기존 파일(KRX 상장법인목록)을 재현하는 조합(P0 실측으로 확정).
#   ST 주권(스팩 포함) · RT 리츠 · IF 인프라투융자 · FS 외국주권 · DR 주식예탁증서 · MF 투자회사 · SC 선박투자
GROUPS = tuple(os.environ.get("KRX_GROUPS", "ST,RT,IF,FS,DR,MF,SC").replace(" ", "").split(","))

MIN_COUNT = {"유가": 700, "코스닥": 1500}
MIN_TOTAL = 2500
MIN_KONEX = 50
LIMITS = {"added": 40, "delisted": 20, "pending": 20, "renamed": 30}


def ticker_of(code, market):
    return code + SUFFIX.get(market, ".KS")


def is_pref(code):
    """우선주 = 코드 끝자리 ≠ 0 (신형 영숫자 보통주 코드도 끝자리 0 — 예 0220W0)."""
    return len(code) == 6 and code[5] != "0"


def new_history():
    return {"renames": [], "delisted": [], "transfers": [], "pendingDelist": {}, "kisNames": {}}


def merge(existing, kis_rows, dart_names, history, today, rename="kis", konex_ok=True,
          delist_now=False):
    """순수 병합. existing: 기존 행 목록, kis_rows: [{code, name, market, pref}] (그룹 필터 후),
    dart_names: {code: corp_name}, history: 이력 dict(변경 안 함 — 사본 반환), today: 'YYYY-MM-DD'.
    delist_now: 누락 종목을 2회 기다리지 않고 바로 상폐 처리(첫 일괄 반영 전용 — 드라이런으로 확인한 뒤).
    반환 (rows, history, report)."""
    h = json.loads(json.dumps(history or {}, ensure_ascii=False))
    for k, v in new_history().items():
        h.setdefault(k, v)
    prev_kis = h["kisNames"]
    pending = h["pendingDelist"]
    kis = {r["code"]: r for r in kis_rows}
    rep = {"added": [], "delisted": [], "pending": [], "renamed": [], "transfers": [],
           "relisted": [], "keptKonex": 0}

    rows = []
    for row in existing:
        code = str(row.get("code", "")).strip()
        k = kis.get(code)
        if k is None:
            if row.get("market") == "코넥스" and not konex_ok:
                rep["keptKonex"] += 1
                rows.append(row)
                continue
            first = pending.get(code)
            if delist_now or (first and first < today):       # 서로 다른 날 2회 연속 누락 → 상폐
                h["delisted"].append({"code": code, "name": row.get("name"),
                                      "market": row.get("market"), "date": today})
                pending.pop(code, None)
                rep["delisted"].append(row)
                continue
            if not first:
                pending[code] = today
                rep["pending"].append(row)
            rows.append(row)
            continue
        pending.pop(code, None)
        new = dict(row)
        if k["market"] != row.get("market"):
            h["transfers"].append({"code": code, "name": row.get("name"),
                                   "from": row.get("market"), "to": k["market"], "date": today})
            rep["transfers"].append({"code": code, "name": row.get("name"),
                                     "from": row.get("market"), "to": k["market"]})
            new["market"] = k["market"]
            new["ticker"] = ticker_of(code, k["market"])
        old_name = (row.get("name") or "").strip()
        cand = None
        if rename == "kis":
            p = prev_kis.get(code)
            if p and p != k["name"]:                          # KIS 이름이 바뀜 = 사명변경
                d = None if k.get("pref") else dart_names.get(code)
                cand = d if d and d != old_name else k["name"]
        elif rename == "dart" and not k.get("pref"):
            cand = dart_names.get(code)
        if not old_name:                                      # 빈 이름은 항상 채움
            cand = cand or (None if k.get("pref") else dart_names.get(code)) or k["name"]
        if cand and cand != old_name:
            if old_name:
                h["renames"].append({"code": code, "old": old_name, "new": cand, "date": today})
            rep["renamed"].append({"code": code, "old": old_name, "new": cand})
            new["name"] = cand
        rows.append(new)

    have = {str(r.get("code", "")).strip() for r in rows} | {d["code"] for d in rep["delisted"]}
    gone = {d["code"] for d in h["delisted"]}
    for code, k in kis.items():
        if code in have:
            continue
        name = k["name"] if k.get("pref") else (dart_names.get(code) or k["name"])
        r = {"name": name, "code": code, "market": k["market"], "ticker": ticker_of(code, k["market"])}
        if k.get("pref"):
            r["pref"] = True
        rows.append(r)
        rep["added"].append(r)
        if code in gone:
            rep["relisted"].append(code)

    h["kisNames"] = {c: k["name"] for c, k in sorted(kis.items())}
    return rows, h, rep


def check_limits(rep, force=False):
    """대량 변경 → 사유 목록(빈 목록이면 통과)."""
    if force:
        return []
    return [f"{k} {len(rep[k])} > {lim}" for k, lim in LIMITS.items() if len(rep[k]) > lim]


def check_counts(kis_rows, konex_ok):
    c = Counter(r["market"] for r in kis_rows)
    bad = [f"{m} {c.get(m, 0)} < {n}" for m, n in MIN_COUNT.items() if c.get(m, 0) < n]
    if sum(c.get(m, 0) for m in MIN_COUNT) < MIN_TOTAL:
        bad.append(f"합계 {sum(c.get(m, 0) for m in MIN_COUNT)} < {MIN_TOTAL}")
    return bad


# ── 입출력 ──────────────────────────────────────────────────────────────────
def load_kis(verify=True, probe=False):
    """KIS 마스터 3개 → (전체 행[그룹 무관], 코넥스 성공 여부). 행: {code, name, market, pref, group}."""
    import build_sector_map_auto as bsm
    out, konex_ok = [], True
    for f, market in MARKET_OF_FILE.items():
        try:
            rows = bsm._parse_mst(f, verify, groups=None)
        except Exception as ex:
            print(f"  [kis] {f} 파싱 실패: {ex}", file=sys.stderr)
            rows = []
        if f == "konex_code":
            st = sum(1 for r in rows if r["group"] == "ST")
            print(f"  [kis] konex_code 고정부 폭(자동 탐지) 파싱 {len(rows)}행 (주권 {st})")
            if st < MIN_KONEX:
                konex_ok = False
                print("  [kis] 코넥스 파싱 부족 — 코넥스 행은 손대지 않음", file=sys.stderr)
                continue
        for r in rows:
            out.append({"code": r["code"], "name": r["name"], "market": market,
                        "pref": r["pref"], "group": r["group"]})
    return out, konex_ok


def load_dart_names(key):
    """DART corpCode.xml → {종목코드: corp_name}."""
    import requests
    from build_dart_corp_map import _listed_stock_code
    r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                     params={"crtfc_key": key}, timeout=90)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    names = {}
    for el in root.iter("list"):
        code = _listed_stock_code(el.findtext("stock_code"))
        name = (el.findtext("corp_name") or "").strip()
        if code and name:
            names[code] = name
    return names


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def _write_json(path, obj, compact=False):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        if compact:
            json.dump(obj, f, ensure_ascii=False, indent=1, sort_keys=False)
        else:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _norm(s):
    s = re.sub(r"[\s·ㆍ・]", "", str(s or ""))
    for tok in ("(주)", "주식회사"):
        s = s.replace(tok, "")
    return s.upper()


def diagnose(existing, kis_all, dart, konex_ok):
    """P0 실측 리포트 — 그룹 조합 재현도·DART/KIS 이름 일치율·우선주 수."""
    ex = {str(r["code"]): r for r in existing}
    print("\n── P0 ① 그룹코드별 행 수 (시장/그룹: 전체 · 기존파일에 있음 · 없음[보통주])")
    c_all, c_in = Counter(), Counter()
    for r in kis_all:
        key = (r["market"], r["group"], "우" if r["pref"] else "보")
        c_all[key] += 1
        if r["code"] in ex:
            c_in[key] += 1
    for key in sorted(c_all):
        print(f"   {key[0]:<4} {key[1]} {key[2]}  전체 {c_all[key]:>5}  기존 {c_in[key]:>5}  신규 {c_all[key] - c_in[key]:>5}")
    kis_codes = {r["code"] for r in kis_all}
    miss = [r for r in existing if r["code"] not in kis_codes]
    print(f"   기존 파일 {len(existing)}행 중 KIS 전 그룹 어디에도 없음: {len(miss)} "
          f"({Counter(r['market'] for r in miss)})")
    for r in miss[:40]:
        print(f"      - {r['code']} {r['name']} {r['market']}")
    sel = {r["code"] for r in kis_all if r["group"] in GROUPS and not r["pref"]}
    print(f"   선택 그룹 {GROUPS} 보통주 {len(sel)}행 — 기존과 교집합 {len(sel & set(ex))}, "
          f"기존에만 {len(set(ex) - sel)}, 선택에만 {len(sel - set(ex))}")
    mk = Counter(r["market"] for r in kis_all if r["code"] in ex and ex[r["code"]]["market"] != r["market"])
    print(f"   시장 불일치(이전상장 후보): {sum(mk.values())} {dict(mk)}")

    print("\n── P0 ③ 이름 일치율 (기존 ∩ KIS 보통주)")
    common = [r for r in kis_all if r["code"] in ex and not r["pref"]]
    d_eq = sum(1 for r in common if dart.get(r["code"]) == ex[r["code"]]["name"])
    d_neq = sum(1 for r in common if _norm(dart.get(r["code"])) == _norm(ex[r["code"]]["name"]))
    d_has = sum(1 for r in common if r["code"] in dart)
    k_eq = sum(1 for r in common if r["name"] == ex[r["code"]]["name"])
    n = max(1, len(common))
    print(f"   DART 보유 {d_has}/{len(common)} · DART=기존 {d_eq} ({100 * d_eq / n:.1f}%) · "
          f"정규화 일치 {d_neq} ({100 * d_neq / n:.1f}%) · KIS=기존 {k_eq} ({100 * k_eq / n:.1f}%)")
    diff = [r for r in common if r["code"] in dart and dart[r["code"]] != ex[r["code"]]["name"]]
    for r in diff[:60]:
        print(f"      ≠ {r['code']} 기존 {ex[r['code']]['name']!r} DART {dart[r['code']]!r} KIS {r['name']!r}")
    print(f"\n── P0 ④ 코넥스 파싱 {'성공' if konex_ok else '실패'}")
    pref = [r for r in kis_all if r["pref"] and r["group"] in GROUPS]
    print(f"── P0 ⑤ 우선주(선택 그룹) {len(pref)}행: " + ", ".join(f"{r['code']} {r['name']}" for r in pref[:15]))


def _print_report(rep):
    print(f"\n── 변경 요약: 추가 {len(rep['added'])} (우선주 {sum(1 for r in rep['added'] if r.get('pref'))}) · "
          f"상폐 {len(rep['delisted'])} · 상폐 후보(1회 누락) {len(rep['pending'])} · "
          f"이전 {len(rep['transfers'])} · 사명변경 {len(rep['renamed'])} · 코넥스 보존 {rep['keptKonex']}")
    for r in rep["added"]:
        print(f"   + {r['code']} {r['name']} {r['market']}{' (우)' if r.get('pref') else ''}")
    for r in rep["delisted"]:
        print(f"   - {r['code']} {r.get('name')} {r.get('market')}")
    for r in rep["pending"]:
        print(f"   ? {r['code']} {r.get('name')} {r.get('market')} (1회 누락)")
    for t in rep["transfers"]:
        print(f"   → {t['code']} {t['name']} {t['from']}→{t['to']}")
    for t in rep["renamed"]:
        print(f"   ✎ {t['code']} {t['old']!r} → {t['new']!r}")


def summary_line(rep):
    parts = [f"추가 {len(rep['added'])}", f"상폐 {len(rep['delisted'])}",
             f"이전 {len(rep['transfers'])}", f"사명 {len(rep['renamed'])}"]
    return " · ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="쓰지 않고 P0 진단 + 변경 목록만 출력")
    ap.add_argument("--force", action="store_true", help="대량 변경 한도 무시(첫 실행용, FORCE=1 과 같음)")
    ap.add_argument("--rename", choices=("kis", "dart", "off"), default=os.environ.get("KRX_RENAME", "kis"))
    ap.add_argument("--delist-now", action="store_true",
                    help="누락 종목 즉시 상폐(첫 일괄 반영 전용 — 드라이런 목록 확인 뒤)")
    ap.add_argument("--insecure", action="store_true", help="로컬 테스트용 SSL 검증 생략")
    args = ap.parse_args()
    force = args.force or os.environ.get("FORCE") == "1"
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    print(f"=== krx_companies 갱신 {today} (rename={args.rename}, force={force}, dry_run={args.dry_run}, "
          f"delist_now={args.delist_now}) ===")

    kis_all, konex_ok = load_kis(verify=not args.insecure)
    kis_rows = [r for r in kis_all if r["group"] in GROUPS]
    print(f"  KIS: {dict(Counter(r['market'] for r in kis_rows))} (그룹 {GROUPS}, 우선주 "
          f"{sum(1 for r in kis_rows if r['pref'])})")
    bad = check_counts(kis_rows, konex_ok)
    if bad:
        print("::error::KIS 마스터 파싱 부족 — 기존 파일 유지: " + ", ".join(bad), file=sys.stderr)
        sys.exit(2)

    key = os.environ.get("DART_API_KEY")
    dart = {}
    if key:
        try:
            dart = load_dart_names(key)
            print(f"  DART 상장사 이름 {len(dart)}")
        except Exception as ex:
            print(f"  [dart] 이름 로드 실패(KIS 이름 사용): {ex}", file=sys.stderr)
    else:
        print("  [dart] DART_API_KEY 없음 — 신규 종목 이름은 KIS 약명", file=sys.stderr)
    if not dart and args.rename == "dart":
        args.rename = "off"

    existing = _read_json(OUT, [])
    history = _read_json(HISTORY, new_history())
    if args.dry_run:
        diagnose(existing, kis_all, dart, konex_ok)
    rows, h, rep = merge(existing, kis_rows, dart, history, today, rename=args.rename, konex_ok=konex_ok,
                         delist_now=args.delist_now)
    _print_report(rep)

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"summary={summary_line(rep)}\n")
    if args.dry_run:
        lim = check_limits(rep, force=False)
        print("\n(dry-run — 파일 안 씀)" + (f" 실제 실행이면 한도 초과로 중단: {', '.join(lim)}" if lim else ""))
        return
    lim = check_limits(rep, force)
    if lim:
        print("::error::대량 변경 — 기존 파일 유지(검토 후 FORCE=1 수동 실행): " + ", ".join(lim), file=sys.stderr)
        sys.exit(2)
    if rows != existing:
        _write_json(OUT, rows)
        print(f"  wrote {OUT} ({len(rows)}행)")
    if h != history:
        _write_json(HISTORY, h, compact=True)
        print(f"  wrote {HISTORY}")


if __name__ == "__main__":
    main()
