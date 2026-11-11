#!/usr/bin/env python3
"""전종목 원본 수집 → Railway Postgres (2026-11-11, PLAN_STOCK_DB.md).

마감 후 하루 1회(Railway 스케줄러 18:10 KST → stock_data_collect.yml). 리포트 파이프라인(LLM·대시보드)과 무관한
별도 단계다 — 가공하지 않은 원본을 종목·날짜 행으로 쌓는다. 결과는 `POST /api/ingest/rows` 로 보낸다(DB 크레덴셜 없음).

  dart  : DART list.json 시장 전체(유가 Y · 코스닥 K) 최근 N일 → stock_disclosures   (종목별 호출 없음, 하루 ~10콜)
  flow  : KIS 허브 /flow × 유니버스 → stock_flow_daily 의 확정 대금 9주체·공매도·대차  (종목당 1콜, 허브가 KIS 4~5 TR)
  naver : 네이버 trend × 유니버스 → stock_flow_daily 의 순매매량(주)·외국인 보유율·종가   (종목당 1콜)
  ca    : KIS 허브 /ca 시장 전체 → stock_ca_events                                   (1콜, 이벤트 키로 누적)

수급 소스는 매번 수십 거래일을 주므로 그 창 전체를 다시 업서트한다(서버가 컬럼별 COALESCE) — 하루 이틀 빠져도 다음 실행이 메운다.
PC 실행 금지(네트워크 규칙) — Actions 에서만. 로컬에서는 --dry-run 도 외부 호출을 하므로 테스트(tests/test_stock_data_collect.py)만 돌린다.

사용:
  python collect_stock_data.py                              # 전 파트, 유니버스 = 지수 구성 ∪ 아카이브 등재 ∪ 워치리스트
  python collect_stock_data.py --parts dart,ca              # 일부만
  python collect_stock_data.py --dry-run --limit 5          # 전송 없이 5종목만 건수 출력
  python collect_stock_data.py --parts dart --dart-from 2025-11-11 --dart-to 2026-11-10   # 공시 백필(90일씩 끊어 조회)
  python collect_stock_data.py --parts ca --ca-from 20251111 --ca-to 20270511             # CA 과거 창(60일 이상은 36일 창으로 나눠 조회)

Env: DART_API_KEY · INGEST_TOKEN(또는 REPORT_INGEST_TOKEN) · FLOW_API_BASE(선택) · INGEST_ROWS_URL(선택)
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.abspath(__file__))
KST = datetime.timezone(datetime.timedelta(hours=9))
FLOW_API_BASE = os.environ.get("FLOW_API_BASE", "https://tradingstrategies-production-09d4.up.railway.app").rstrip("/")
INGEST_ROWS_URL = os.environ.get("INGEST_ROWS_URL", "https://backteststock-production.up.railway.app/api/ingest/rows")
TOKEN = os.environ.get("INGEST_TOKEN") or os.environ.get("REPORT_INGEST_TOKEN") or ""
DART_KEY = os.environ.get("DART_API_KEY", "")
UA = {"User-Agent": "Mozilla/5.0 (backtest-stock stock-data-collect)"}
BATCH = 2000                    # ingest 요청 한 건의 행 수
MIN_COVERAGE = 0.8              # 유니버스 대비 수급 성공 종목 비율이 이보다 낮으면 실패 종료
KIS_KEYS = ("prsn", "frgn", "orgn", "fund", "scrt", "insu", "ivtr", "pe", "etc")


def log(msg):
    print(f"[stockdata] {msg}", flush=True)


def _get_json(url, timeout=30, retries=2, headers=None):
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers or UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{url.split('?')[0]}: {last}")


# ── 유니버스 ─────────────────────────────────────────────────────────────────────────────────
def load_listed(root=ROOT):
    """현재 상장 종목 코드 집합 — public/assets/krx_caps.json(매일 갱신, 보통주+우선주 전 종목). 못 읽으면 None(필터 안 함)."""
    try:
        with open(os.path.join(root, "public", "assets", "krx_caps.json"), encoding="utf-8") as f:
            caps = (json.load(f) or {}).get("caps") or {}
        out = {str(c).zfill(6) for c in caps}
        return out if len(out) >= 1000 else None     # 비정상적으로 작으면 깨진 파일로 보고 필터하지 않는다
    except Exception as e:                           # noqa: BLE001
        log(f"krx_caps 로드 실패(상장 필터 생략): {e}")
        return None


def build_universe(root=ROOT, mode="archive"):
    """수급 수집 대상 코드 집합. index = 지수 구성(KOSPI200 ∪ KOSDAQ150), archive(기본) = + netbuy_rank 아카이브 등재 ∪ 워치리스트.
    아카이브에는 상장폐지 종목이 남아 있으므로(메리츠화재 000060 등) 현재 상장 종목(krx_caps)만 남긴다 — 2026-11-11."""
    uni = set()
    try:
        with open(os.path.join(root, "public", "assets", "index_constituents.json"), encoding="utf-8") as f:
            raw = json.load(f)
        for k in ("KOSPI200", "KOSDAQ150"):
            uni.update(str(c).zfill(6) for c in raw.get(k) or [])
    except Exception as e:                           # noqa: BLE001
        log(f"index_constituents 로드 실패: {e}")
    if mode == "index":
        return sorted(uni)
    for path in glob.glob(os.path.join(root, "public", "reports", "netbuy_rank", "*.json")):
        if os.path.basename(path) == "index.json":
            continue
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:                            # noqa: BLE001
            continue
        uni.update(str(c).zfill(6) for c in (d.get("final") or {}))
        for rows in (d.get("lists") or {}).values():
            uni.update(str(r.get("code") or "").zfill(6) for r in rows or [] if r.get("code"))
    for name in ("watchlist.json", "watchlist_down.json", "intraday_watchlist.json", "intraday_watchlist_down.json"):
        try:
            with open(os.path.join(root, name), encoding="utf-8") as f:
                uni.update(str(s.get("code") or "").zfill(6) for s in json.load(f) if isinstance(s, dict) and s.get("code"))
        except Exception:                            # noqa: BLE001
            pass
    uni = {c for c in uni if len(c) == 6}
    listed = load_listed(root)
    if listed is not None:
        gone = uni - listed
        if gone:
            log(f"상장폐지·미상장 {len(gone)}종목 제외(예: {', '.join(sorted(gone)[:5])})")
        uni &= listed
    return sorted(uni)


# ── 응답 → 행(순수 함수) ─────────────────────────────────────────────────────────────────────
def _iso(d):
    s = str(d or "").strip()
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) == 8 and s.isdigit() else (s if len(s) == 10 else None)


def flow_rows(code, payload):
    """허브 /flow 응답 → {날짜: 행}. daily = KIS 확정 대금(백만원) 9주체·종가·등락률, shorts = 공매도, loans = 대차."""
    out = {}

    def row(d):
        iso = _iso(d)
        return out.setdefault(iso, {"code": code, "date": iso}) if iso else None
    for r in (payload or {}).get("daily") or []:
        x = row(r.get("date"))
        if x is None:
            continue
        for k in KIS_KEYS:
            if r.get(k) is not None:
                x[f"amt_{k}"] = r.get(k)
        if r.get("close") is not None:
            x["close"] = r.get("close")
        if r.get("rate") is not None:
            x["change_pct"] = r.get("rate")
    for r in (payload or {}).get("shorts") or []:
        x = row(r.get("date"))
        if x is None:
            continue
        x.update({k: v for k, v in (("short_qty", r.get("qty")), ("short_amt", r.get("pbmn")),
                                    ("short_vol_pct", r.get("volRlim")), ("short_amt_pct", r.get("pbmnRlim"))) if v is not None})
    for r in (payload or {}).get("loans") or []:
        x = row(r.get("date"))
        if x is None:
            continue
        x.update({k: v for k, v in (("loan_new", r.get("newQty")), ("loan_rdmp", r.get("rdmpQty")), ("loan_bal_chg", r.get("rmndChg")),
                                    ("loan_bal_qty", r.get("rmndQty")), ("loan_bal_amt", r.get("rmndAmt"))) if v is not None})
    return out


def _qty(v):
    try:
        return int(str(v).replace(",", "").replace("+", ""))
    except (TypeError, ValueError):
        return None


def naver_rows(code, items):
    """네이버 trend 응답(list) → {날짜: 행}. 순매매량(주)·외국인 보유율(%)·종가·등락률(전일 대비)."""
    out = {}
    for it in items if isinstance(items, list) else []:
        iso = _iso(it.get("bizdate"))
        if not iso:
            continue
        x = {"code": code, "date": iso}
        for src, dst in (("foreignerPureBuyQuant", "qty_frgn"), ("organPureBuyQuant", "qty_orgn"), ("individualPureBuyQuant", "qty_prsn")):
            q = _qty(it.get(src))
            if q is not None:
                x[dst] = q
        try:
            x["hold_ratio"] = float(str(it.get("foreignerHoldRatio") or "").rstrip("%"))
        except ValueError:
            pass
        close, chg = _qty(it.get("closePrice")), _qty(it.get("compareToPreviousClosePrice")) or 0
        if close:
            x["close"] = close
            if close - chg:
                x["change_pct"] = round(chg / (close - chg) * 100, 2)
        if len(x) > 2:
            out[iso] = x
    return out


def merge_rows(*maps):
    """{날짜: 행} 여러 개를 날짜별로 합친다 — 앞 소스(KIS)의 값이 있으면 뒤(네이버)의 같은 키로 덮지 않는다."""
    out = {}
    for m in maps:
        for d, r in (m or {}).items():
            x = out.setdefault(d, {})
            for k, v in r.items():
                if v is not None and x.get(k) is None:
                    x[k] = v
    return [out[d] for d in sorted(out)]


def dart_rows(items, corp_cls):
    """DART list.json 의 list → 행. 상장 종목 코드가 없는 건(비상장·펀드)은 버린다."""
    out = []
    for it in items or []:
        code, rno, date = (it.get("stock_code") or "").strip(), (it.get("rcept_no") or "").strip(), _iso(it.get("rcept_dt"))
        if not code or not rno or not date:
            continue
        out.append({"rcept_no": rno, "stock_code": code, "date": date, "corp_name": it.get("corp_name"),
                    "title": " ".join(str(it.get("report_nm") or "").split()), "filer": it.get("flr_nm"), "corp_cls": corp_cls})
    return out


def ca_key(row):
    """CA 마일스톤 행의 누적 키 — 같은 일정은 같은 키. 날짜가 바뀌면 새 키가 되고 옛 키는 last_seen 이 멈춘다(= 변경·취소)."""
    raw = "|".join(str(row.get(k) or "") for k in ("type", "code", "kind", "date", "milestoneLabel"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def ca_rows(cal_rows, seen):
    """fetch_ca_calendar.flatten 의 행 → ingest 행. seen = 수집일(YYYY-MM-DD)."""
    out = []
    for r in cal_rows or []:
        if not r.get("code") or not r.get("date") or not r.get("type"):
            continue
        out.append({"event_key": ca_key(r), "code": r["code"], "type": r["type"], "kind": r.get("kind"), "date": r["date"],
                    "importance": r.get("importance"), "seen": seen,
                    "payload": {k: r.get(k) for k in ("date", "end", "kind", "milestoneLabel", "type", "typeLabel", "code", "name",
                                                      "importance", "fields", "note") if r.get(k) is not None}})
    return out


# ── 수집(외부 호출) ──────────────────────────────────────────────────────────────────────────
def fetch_flow(code, rows):
    return _get_json(f"{FLOW_API_BASE}/flow?code={code}&rows={rows}", timeout=30,
                     headers={"User-Agent": "backtest-stock-stock-data"})


def fetch_naver(code, page_size, pages):
    items = []
    for page in range(1, pages + 1):
        got = _get_json(f"https://m.stock.naver.com/api/stock/{code}/trend?pageSize={page_size}&page={page}", timeout=12, retries=1)
        if not isinstance(got, list) or not got:
            break
        items.extend(got)
        if len(got) < page_size:
            break
        time.sleep(0.15)
    return items


def collect_flow(codes, parts, flow_rows_n, naver_size, naver_pages, workers=6):
    """→ (행 목록, 파트별 {ok, failed, rows}). 종목 실패는 건너뛴다."""
    stat = {p: {"ok": 0, "failed": 0, "rows": 0} for p in ("flow", "naver") if p in parts}

    def one(code):
        kis = nav = None
        if "flow" in parts:
            try:
                kis = flow_rows(code, fetch_flow(code, flow_rows_n))
            except Exception as e:                   # noqa: BLE001
                kis = e
        if "naver" in parts:
            try:
                nav = naver_rows(code, fetch_naver(code, naver_size, naver_pages))
                time.sleep(0.1)
            except Exception as e:                   # noqa: BLE001
                nav = e
        return code, kis, nav

    rows, errs = [], []
    with ThreadPoolExecutor(max_workers=workers) as tp:
        for code, kis, nav in tp.map(one, codes):
            for part, got in (("flow", kis), ("naver", nav)):
                if part not in stat:
                    continue
                if isinstance(got, Exception) or not got:
                    stat[part]["failed"] += 1
                    if isinstance(got, Exception) and len(errs) < 5:
                        errs.append(f"{part} {code}: {got}")
                else:
                    stat[part]["ok"] += 1
                    stat[part]["rows"] += len(got)
            rows.extend(merge_rows(kis if isinstance(kis, dict) else None, nav if isinstance(nav, dict) else None))
    for e in errs:
        log(f"실패 예: {e}")
    return rows, stat


def collect_dart(bgn, end, on_chunk=None, chunk_days=30):
    """[bgn, end](date) 구간 시장 전체 공시 → (행, 호출 수). corp_code 없는 조회는 3개월 창 제한이라 끊어서 조회한다.
    on_chunk(rows) 를 주면 창 하나(chunk_days)가 끝날 때마다 넘기고 비운다 — 긴 백필이 중간에 끊겨도 앞부분은 적재된다."""
    if not DART_KEY:
        raise RuntimeError("DART_API_KEY 미설정")
    rows, calls, d = [], 0, bgn
    while d <= end:
        e = min(end, d + datetime.timedelta(days=chunk_days - 1))
        for cls in ("Y", "K"):
            page = 1
            while True:
                data = _get_json("https://opendart.fss.or.kr/api/list.json?crtfc_key=" + DART_KEY +
                                 f"&bgn_de={d:%Y%m%d}&end_de={e:%Y%m%d}&corp_cls={cls}&page_count=100&page_no={page}", timeout=25)
                calls += 1
                st = data.get("status")
                if st == "013":                      # 조회된 데이터 없음
                    break
                if st != "000":
                    raise RuntimeError(f"DART status {st}: {data.get('message')}")
                rows.extend(dart_rows(data.get("list"), cls))
                if page >= int(data.get("total_page") or 1):
                    break
                page += 1
                time.sleep(0.12)
        if on_chunk is not None:
            on_chunk(rows, d, e)
            rows = []
        d = e + datetime.timedelta(days=1)
    return rows, calls


CA_SPAN_MAX = 60        # 허브 시장 전체 /ca 는 120초 시간 상한 — 기본 창(−14 ~ +45일 = 59일)까지는 한 번에 받는다
CA_WINDOW_DAYS = 36     # 그보다 넓으면(백필) 이 길이의 창으로 나눠 조회한다
CA_STEP_DAYS = 28       # 창 간격 — 허브의 의무예치 조회는 '창 시작 +7 ~ +35일' 해제분만 훑으므로 28일 간격으로 겹쳐야 해제일이 이어진다
CA_LEAD_DAYS = 7        # 같은 이유로 첫 창은 from 보다 7일 앞에서 시작한다


def ca_windows(ca_from=None, ca_to=None):
    """[from, to](YYYYMMDD) → 허브에 보낼 창 [(from, to)]. 한쪽이 없거나 CA_SPAN_MAX 일보다 좁으면 그대로 한 번.
    넓은 구간을 한 번에 물으면 허브가 시간 상한(120초)·종류별 호출 상한(30)에서 잘라 뒷부분이 조용히 빠진다."""
    if not (ca_from and ca_to):
        return [(ca_from, ca_to)]
    a = datetime.datetime.strptime(ca_from, "%Y%m%d").date()
    b = datetime.datetime.strptime(ca_to, "%Y%m%d").date()
    if (b - a).days < CA_SPAN_MAX:
        return [(ca_from, ca_to)]
    out, d = [], a - datetime.timedelta(days=CA_LEAD_DAYS)
    while True:
        e = min(b, d + datetime.timedelta(days=CA_WINDOW_DAYS - 1))
        out.append((d.strftime("%Y%m%d"), e.strftime("%Y%m%d")))
        if e >= b:
            return out
        d += datetime.timedelta(days=CA_STEP_DAYS)


def collect_ca(today, ca_from=None, ca_to=None):
    """허브 /ca 한 번 → (행, 이벤트 수, 오류 종류, 잘림 기록 {종류: [구간…]}).
    잘림 = 허브가 시간·호출 상한이나 KIS 하루 100행 상한에 걸려 다 못 받은 구간(diag.truncated)."""
    import fetch_ca_calendar as cal                  # flatten·상장 필터·종목명 규칙을 캘린더와 똑같이
    url = f"{FLOW_API_BASE}/ca?raw=0" + (f"&from={ca_from}" if ca_from else "") + (f"&to={ca_to}" if ca_to else "")
    # 허브 시간 상한이 120초라 그보다 길게 기다린다(종전 90초는 창이 무거우면 응답 전에 끊겼다). 재시도는 허브 6시간 캐시가 받는다.
    resp = _get_json(url, timeout=170, retries=1, headers={"User-Agent": "backtest-stock-stock-data"})
    if resp.get("status") not in (None, "success"):
        raise RuntimeError(f"허브 /ca status={resp.get('status')}")
    try:
        names, listed = cal.load_names(), cal.load_listed()
    except Exception:                                # noqa: BLE001
        names, listed = {}, None
    flat = cal.flatten(resp.get("events") or [], names=names, listed=listed)
    trunc = {k: v["truncated"] for k, v in (resp.get("diag") or {}).items() if isinstance(v, dict) and v.get("truncated")}
    return ca_rows(flat, today), len(resp.get("events") or []), sorted((resp.get("errors") or {}).keys()), trunc


# ── 전송 ─────────────────────────────────────────────────────────────────────────────────────
def post_rows(table, rows, dry=False):
    """→ (accepted, rejected). dry 면 전송 없이 건수만."""
    if dry or not rows:
        return len(rows), 0
    if not TOKEN:
        raise RuntimeError("INGEST_TOKEN 미설정")
    acc = rej = 0
    for i in range(0, len(rows), BATCH):
        body = json.dumps({"table": table, "rows": rows[i:i + BATCH]}, ensure_ascii=False).encode("utf-8")
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(INGEST_ROWS_URL, data=body, method="POST",
                                             headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    out = json.loads(r.read().decode("utf-8"))
                acc += out.get("accepted", 0)
                rej += out.get("rejected", 0)
                last = None
                break
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}"
                if e.code in (400, 401, 404):
                    break
            except Exception as e:                   # noqa: BLE001
                last = str(e)
            time.sleep(3 * (attempt + 1))
        if last:
            raise RuntimeError(f"ingest {table} 실패: {last}")
    return acc, rej


def _is_trading_day(d):
    try:
        import krx_calendar
        return krx_calendar.is_trading_day(d, krx_calendar.load_holidays())
    except Exception:                                # noqa: BLE001
        return d.weekday() < 5


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--parts", default="dart,flow,naver,ca")
    ap.add_argument("--universe", default="archive", choices=("index", "archive"))
    ap.add_argument("--limit", type=int, default=0, help="유니버스 앞 N종목만(시험용)")
    ap.add_argument("--flow-rows", type=int, default=30, help="허브 /flow daily 거래일 수(공매도·대차는 허브가 max(rows,20))")
    ap.add_argument("--naver-size", type=int, default=20)
    ap.add_argument("--naver-pages", type=int, default=1)
    ap.add_argument("--dart-days", type=int, default=3, help="오늘 포함 최근 N일 공시(주말·누락 보정)")
    ap.add_argument("--dart-from")
    ap.add_argument("--dart-to")
    ap.add_argument("--ca-from", help="YYYYMMDD (허브 기본 = 오늘−14일)")
    ap.add_argument("--ca-to", help="YYYYMMDD (허브 기본 = 오늘+45일)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="장 마감(15:40) 전·휴장일에도 수급 파트 실행")
    a = ap.parse_args(argv)
    parts = [p for p in (x.strip() for x in a.parts.split(",")) if p in ("dart", "flow", "naver", "ca")]
    now = datetime.datetime.now(KST)
    today = now.date()
    iso = today.isoformat()
    runs, failed_parts = [], []

    # 수급: 거래일 15:40 이후에만(그 전에는 KIS 확정 대금이 비고 '오늘' 커버리지가 거짓이 된다). 휴장일은 건너뜀.
    flow_parts = [p for p in ("flow", "naver") if p in parts]
    if flow_parts and not a.force:
        if not _is_trading_day(today):
            log(f"{iso} 휴장일 — 수급 파트 생략(--force 로 무시)")
            flow_parts = []
        elif (now.hour, now.minute) < (15, 40):
            log(f"{now:%H:%M} KST 장 마감 전 — 수급 파트 생략(--force 로 무시)")
            flow_parts = []

    if flow_parts:
        codes = build_universe(mode=a.universe)
        if a.limit:
            codes = codes[:a.limit]
        log(f"수급 유니버스 {len(codes)}종목 · 파트 {flow_parts} · flow rows {a.flow_rows} · naver {a.naver_size}×{a.naver_pages}")
        t = time.time()
        rows, stat = collect_flow(codes, flow_parts, a.flow_rows, a.naver_size, a.naver_pages, a.workers)
        acc, rej = post_rows("flow", rows, a.dry_run)
        log(f"수급 {len(rows)}행 → accepted {acc} · rejected {rej} · {time.time() - t:.0f}s · {stat}")
        for p, s in stat.items():
            runs.append({"date": iso, "part": p, "universe": len(codes), "ok": s["ok"], "failed": s["failed"], "rows": s["rows"],
                         "note": f"accepted {acc}/{len(rows)}"})
            if codes and s["ok"] / len(codes) < MIN_COVERAGE:
                failed_parts.append(f"{p} 커버리지 {s['ok']}/{len(codes)}")

    if "dart" in parts:
        try:
            end = datetime.date.fromisoformat(a.dart_to) if a.dart_to else today
            bgn = datetime.date.fromisoformat(a.dart_from) if a.dart_from else end - datetime.timedelta(days=max(1, a.dart_days) - 1)
            tot = {"rows": 0, "acc": 0, "rej": 0}

            def _send(chunk, d0, d1):                # 창 하나씩 바로 적재(백필이 끊겨도 앞부분은 남는다)
                acc, rej = post_rows("disclosures", chunk, a.dry_run)
                tot["rows"] += len(chunk); tot["acc"] += acc; tot["rej"] += rej
                if (end - bgn).days > 31:
                    log(f"공시 {d0}~{d1} {len(chunk)}행 → accepted {acc} · rejected {rej}")
            _, calls = collect_dart(bgn, end, on_chunk=_send)
            log(f"공시 {bgn}~{end} {tot['rows']}행({calls}콜) → accepted {tot['acc']} · rejected {tot['rej']}")
            runs.append({"date": iso, "part": "dart", "ok": 1, "failed": 0, "rows": tot["rows"], "note": f"{bgn}~{end} {calls}콜 accepted {tot['acc']}"})
        except Exception as e:                       # noqa: BLE001
            log(f"공시 실패: {e}")
            failed_parts.append("dart")
            runs.append({"date": iso, "part": "dart", "ok": 0, "failed": 1, "rows": 0, "note": str(e)[:200]})

    if "ca" in parts:
        wins = ca_windows(a.ca_from, a.ca_to)        # 넓은 구간(백필)은 창으로 나눠 창마다 바로 적재
        tot = {"rows": 0, "ev": 0, "acc": 0, "rej": 0, "trunc": 0}
        errs_all, bad, last_err = set(), [], ""
        for i, (f, t) in enumerate(wins):
            try:
                rows, n_ev, errs, trunc = collect_ca(iso, f, t)
                acc, rej = post_rows("ca", rows, a.dry_run)
                tot["rows"] += len(rows); tot["ev"] += n_ev; tot["acc"] += acc; tot["rej"] += rej
                tot["trunc"] += sum(len(v) for v in trunc.values())
                errs_all.update(errs)
                log(f"CA {f or '기본'}~{t or '기본'} 이벤트 {n_ev}건 → {len(rows)}행 → accepted {acc} · rejected {rej}"
                    + (f" · 허브 오류 타입 {errs}" if errs else "")
                    + (f" · 잘림 {json.dumps(trunc, ensure_ascii=False)[:400]}" if trunc else ""))
            except Exception as e:                   # noqa: BLE001
                last_err = str(e)[:160]
                bad.append(f"{f}~{t}")
                log(f"CA 실패 {f or '기본'}~{t or '기본'}: {e}")
            if i + 1 < len(wins):
                time.sleep(2)
        if len(wins) > 1:
            log(f"CA 합계 {len(wins)}창(실패 {len(bad)}) 이벤트 {tot['ev']}건 → {tot['rows']}행 → accepted {tot['acc']} · rejected {tot['rej']}"
                f" · 잘린 구간 {tot['trunc']}")
        note = (f"events {tot['ev']}" + (f" {a.ca_from}~{a.ca_to} {len(wins)}창" if len(wins) > 1 else "")
                + (f" errors {','.join(sorted(errs_all))}" if errs_all else "") + (f" truncated {tot['trunc']}" if tot["trunc"] else "")
                + (f" 실패창 {','.join(bad)[:80]} {last_err}" if bad else ""))
        if len(wins) == 1:                           # 매일 실행 — 종전 기록 형식 그대로(ok 1/0, failed = 오류 종류 수)
            runs.append({"date": iso, "part": "ca", "ok": 0 if bad else 1, "failed": 1 if bad else len(errs_all),
                         "rows": tot["rows"], "note": (last_err if bad else note)[:200]})
        else:                                        # 백필 — ok/failed = 창 수
            runs.append({"date": iso, "part": "ca", "ok": len(wins) - len(bad), "failed": len(bad), "rows": tot["rows"], "note": note[:200]})
        if bad:
            failed_parts.append("ca" if len(wins) == 1 else f"ca {len(bad)}/{len(wins)}창")

    if runs and not a.dry_run:
        try:
            post_rows("runs", runs)
        except Exception as e:                       # noqa: BLE001
            log(f"실행 기록 전송 실패: {e}")
    if failed_parts:
        log(f"실패: {failed_parts}")
        return 1
    log("완료" + (" (dry-run — 전송 없음)" if a.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
