#!/usr/bin/env python3
"""netbuy_rank 과거 아카이브 재구성 백필 — 허브 /flow(종목당 ~20거래일 시계열)로 결손일 생성.

일별 순매수 랭킹 스냅샷(reports/netbuy_rank/<date>.json)은 2026-09-07 부터 쌓였다.
KIS 가집계 랭킹 TR(FHPTJ04400000)은 당일 전용이라 과거 '그날의 상위 30'은 못 받지만,
종목별 확정 수급 TR(FHPTJ04160001)·공매도·대차는 한 콜에 ~20거래일 시계열을 주므로
유니버스(지수 구성종목 ∪ 기존 아카이브 등재 종목, ~520)를 한 번씩 조회해 결손일의
final(확정 수급 4주체+세분+공매도+대차)과 lists(유니버스 내 외인/기관 순매수·순매도
상위 30 재산출)를 기존 스키마 그대로 만든다(2026-09-10). 파일에 reconstructed=true.

  - 기존 파일은 절대 덮어쓰지 않는다(결손일만).
  - 응답 daily 가 비면(15:40 이전 시간제한·휴장) 그 날은 만들지 않는다(MIN_CODES 미만).
  - 20거래일 창 밖(오늘 기준 ~1개월 전 이전)은 복원 불가 — 조기 실행 권장.
  - 실행: 마감 후(15:40+) Actions workflow_dispatch(backfill_netbuy_history.yml).
    PC 실행 금지(네트워크 규칙). --dry-run 은 파일 미생성, --force 는 시간 가드 무시.

사용: python backfill_netbuy_history.py [--from YYYY-MM-DD] [--to YYYY-MM-DD] [--dry-run]
      (기본: to=직전 거래일, from=to-30일; 환경변수 BACKFILL_FROM/BACKFILL_TO 도 인식)
"""
import os
import sys
import glob
import json
import argparse
import datetime
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import generate_weekly_briefing as g          # _flow_raw 메모이즈·RANK_DIR·KST 재사용

KEYS = ("prsn", "frgn", "orgn", "fund", "scrt", "insu", "ivtr", "pe")
MIN_CODES = 50          # 이 미만이면 휴장/미확정(장중)으로 보고 파일 생성 안 함
TOP_N = 30
LIST_KEYS = (("frgn_buy", "frgn", 1), ("frgn_sell", "frgn", -1),
             ("orgn_buy", "orgn", 1), ("orgn_sell", "orgn", -1))


def _rank_dir():
    return g.RANK_DIR


def load_universe():
    """code -> name. 지수 구성종목(KOSPI200/KOSDAQ150) ∪ 기존 netbuy_rank 등재 종목."""
    names = {}
    try:
        with open(os.path.join(ROOT, "public", "assets", "krx_companies.json"),
                  encoding="utf-8") as f:
            for c in json.load(f):
                code = str(c.get("code") or "").zfill(6)
                if code and c.get("name"):
                    names[code] = c["name"]
    except Exception as ex:
        print(f"[backfill] krx_companies 로드 실패(이름=코드 폴백): {ex}", file=sys.stderr)
    uni = {}
    try:
        with open(os.path.join(ROOT, "public", "assets", "index_constituents.json"),
                  encoding="utf-8") as f:
            raw = json.load(f)
        for k in ("KOSPI200", "KOSDAQ150"):
            for c in raw.get(k) or []:
                code = str(c).zfill(6)
                uni[code] = names.get(code, code)
    except Exception as ex:
        print(f"[backfill] index_constituents 로드 실패: {ex}", file=sys.stderr)
    for path in glob.glob(os.path.join(_rank_dir(), "*.json")):
        if os.path.basename(path) == "index.json":
            continue
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        for rows in (d.get("lists") or {}).values():
            for r in rows or []:
                code = str(r.get("code") or "").zfill(6)
                if code:
                    uni[code] = r.get("name") or uni.get(code) or names.get(code, code)
    return uni


def trading_days(d1, d2):
    out, d = [], d1
    while d <= d2:
        if d.weekday() < 5:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


def fetch_all(codes, workers=6):
    """유니버스 전체 /flow 조회(메모이즈, 실패 종목 제외)."""
    def _one(code):
        try:
            return code, g._flow_raw(code)
        except Exception:
            return code, None
    out = {}
    with ThreadPoolExecutor(max_workers=workers) as tp:
        for code, d in tp.map(_one, codes):
            if d:
                out[code] = d
    return out


def build_day(date_iso, payloads, names):
    """결손일 하나의 스냅샷 재구성 — 기존 netbuy_rank 스키마(lists/final) + reconstructed."""
    want = date_iso.replace("-", "")
    final, rows = {}, []
    for code, d in payloads.items():
        daily = next((r for r in (d.get("daily") or []) if str(r.get("date")) == want), None)
        if not daily:
            continue
        e = {k: daily.get(k) for k in KEYS}
        sh = next((r for r in (d.get("shorts") or []) if str(r.get("date")) == want), None)
        if sh is not None:
            e["shortAmt"] = sh.get("pbmn")
        loans = d.get("loans") or []                  # 최신순
        for i, lr in enumerate(loans):
            if str(lr.get("date")) == want:
                e["loanAmt"] = lr.get("rmndAmt")
                e["loanChg"] = lr.get("rmndChg")
                if i + 1 < len(loans):
                    e["loanPrev"] = loans[i + 1].get("rmndAmt")
                break
        final[code] = e
        rows.append({"code": code, "name": names.get(code) or code,
                     "price": daily.get("close"), "rate": daily.get("rate"),
                     "frgn": daily.get("frgn"), "orgn": daily.get("orgn"),
                     "fund": daily.get("fund")})
    if len(final) < MIN_CODES:
        return None

    def top(key, sign):
        xs = [r for r in rows if (r.get(key) or 0) * sign > 0]
        xs.sort(key=lambda r: -(r[key] * sign))
        return xs[:TOP_N]

    lists = {lk: top(key, sign) for lk, key, sign in LIST_KEYS}
    return {"date": date_iso, "asof": None, "reconstructed": True,
            "universe": len(final), "lists": lists, "final": final}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", default=os.environ.get("BACKFILL_FROM") or None)
    ap.add_argument("--to", dest="d_to", default=os.environ.get("BACKFILL_TO") or None)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="15:40 시간 가드 무시(테스트)")
    a = ap.parse_args(argv)

    now = datetime.datetime.now(g.KST)
    if not a.force and now.weekday() < 5 and (now.hour, now.minute) < (15, 40):
        print("[backfill] 15:40 이전 — FHPTJ04160001 시간제한으로 daily 가 비어 재구성 불가. 중단",
              file=sys.stderr)
        return 2

    d_to = (datetime.date.fromisoformat(a.d_to) if a.d_to
            else now.date() - datetime.timedelta(days=1))
    while d_to.weekday() >= 5:
        d_to -= datetime.timedelta(days=1)
    d_from = (datetime.date.fromisoformat(a.d_from) if a.d_from
              else d_to - datetime.timedelta(days=30))
    rank_dir = _rank_dir()
    os.makedirs(rank_dir, exist_ok=True)
    targets = [d for d in trading_days(d_from, d_to)
               if not os.path.exists(os.path.join(rank_dir, f"{d.isoformat()}.json"))]
    print(f"[backfill] 창 {d_from} ~ {d_to} · 결손 거래일 {len(targets)}: "
          f"{[d.isoformat() for d in targets]}")
    if not targets:
        return 0

    uni = load_universe()
    print(f"[backfill] 유니버스 {len(uni)}종목 → /flow 조회 (workers={a.workers})")
    payloads = fetch_all(sorted(uni), a.workers)
    n_daily = sum(1 for d in payloads.values() if d.get("daily"))
    print(f"[backfill] 응답 {len(payloads)} · daily 보유 {n_daily}")
    if n_daily < MIN_CODES:
        print("[backfill] daily 시계열이 거의 없음(장중/허브 구버전?) — 중단", file=sys.stderr)
        return 3

    made = []
    for d in targets:
        di = d.isoformat()
        snap = build_day(di, payloads, uni)
        if not snap:
            print(f"[backfill] {di}: 확정 행 {MIN_CODES} 미만 — 휴장/창 밖으로 간주, 생성 안 함")
            continue
        n_sh = sum(1 for e in snap["final"].values() if e.get("shortAmt") is not None)
        n_ln = sum(1 for e in snap["final"].values() if e.get("loanAmt") is not None)
        print(f"[backfill] {di}: final {snap['universe']} · 공매도 {n_sh} · 대차 {n_ln} · "
              f"외인매수1위 {snap['lists']['frgn_buy'][0]['name'] if snap['lists']['frgn_buy'] else '-'}")
        if not a.dry_run:
            with open(os.path.join(rank_dir, f"{di}.json"), "w", encoding="utf-8") as f:
                json.dump(snap, f, ensure_ascii=False, indent=1)
        made.append(di)
    if made and not a.dry_run:
        idx_path = os.path.join(rank_dir, "index.json")
        try:
            with open(idx_path, encoding="utf-8") as f:
                idx = json.load(f)
        except OSError:
            idx = []
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(sorted(set(idx) | set(made), reverse=True), f, indent=1)
    print(f"[backfill] 완료: {len(made)}일 생성{' (dry-run)' if a.dry_run else ''} — {made}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
