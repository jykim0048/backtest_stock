#!/usr/bin/env python3
"""public/ 데이터 JSON 을 Railway ingest API 로 업서트 (dual-write / 백필).

파이프라인(GitHub Actions)이 리포트 파일 생성 직후 호출해 Postgres 에도 쓴다
(git 커밋과 병행 = dual-write 전환기). DB 크레덴셜 없이 HTTP + Bearer 토큰만
쓰므로 워크플로 시크릿은 REPORT_INGEST_TOKEN 하나면 된다. 표준라이브러리만 사용.

사용:
  python push_reports_to_db.py            # 스냅샷 전체 + 오늘(KST) 아카이브
  python push_reports_to_db.py --all      # 백필: 아카이브 전 기간 + 스냅샷 전체
  python push_reports_to_db.py --dry-run  # 전송 없이 대상 목록만 출력

Env:
  INGEST_TOKEN (또는 REPORT_INGEST_TOKEN)  — 필수. 미설정 시 no-op 성공 종료
                                            (시크릿 배포 전 워크플로가 깨지지 않게)
  INGEST_URL — 기본 https://backteststock-production.up.railway.app/api/ingest

경로 → DB 키 매핑은 report_db.classify 와 동일 규칙 (여기 단독 재구현 — 이
스크립트는 psycopg2 없이 어디서든 돌아야 한다):
  public/reports/<date>.json                → reports(kind='daily')
  public/reports/<k...>/<date>.json         → reports(kind='<k...>')
  public/briefing/<date>.json               → reports(kind='briefing')
  그 외 public/**/*.json (assets 제외)       → snapshots(name=상대경로)
  index.json                                → 저장 안 함 (DB 쿼리로 대체)
"""
import os
import re
import sys
import json
import glob
import time
import datetime
import urllib.request
import urllib.error

ROOT = os.path.dirname(os.path.abspath(__file__))
PUBLIC = os.path.join(ROOT, "public")
KST = datetime.timezone(datetime.timedelta(hours=9))

INGEST_URL = os.environ.get(
    "INGEST_URL", "https://backteststock-production.up.railway.app/api/ingest")
TOKEN = os.environ.get("INGEST_TOKEN") or os.environ.get("REPORT_INGEST_TOKEN") or ""

BATCH_BYTES = 3 * 1024 * 1024      # 요청당 payload 상한(직렬화 기준, 대략)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 스냅샷 수집 대상 (public/ 기준 glob) — assets/·reports/ 아카이브·index 제외
_SNAPSHOT_GLOBS = ("*.json", "data/*.json", "briefing/latest.json")


def _rel(path):
    return os.path.relpath(path, PUBLIC).replace("\\", "/")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as ex:
        print(f"  ! skip {_rel(path)}: {ex}", file=sys.stderr)
        return None


def collect(all_dates):
    """(reports, snapshots) — reports: [(kind, date, path)], snapshots: [(name, path)]"""
    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    reports = []

    for path in glob.glob(os.path.join(PUBLIC, "reports", "**", "*.json"), recursive=True):
        rel = _rel(path)
        parts = rel.split("/")
        stem = parts[-1][:-len(".json")]
        if not _DATE_RE.match(stem):
            continue                                    # index.json 등
        kind = "/".join(parts[1:-1]) or "daily"
        if all_dates or stem == today:
            reports.append((kind, stem, path))

    for path in glob.glob(os.path.join(PUBLIC, "briefing", "*.json")):
        stem = os.path.basename(path)[:-len(".json")]
        if _DATE_RE.match(stem) and (all_dates or stem == today):
            reports.append(("briefing", stem, path))

    snapshots = []
    for pat in _SNAPSHOT_GLOBS:
        for path in glob.glob(os.path.join(PUBLIC, pat)):
            snapshots.append((_rel(path), path))
    return reports, snapshots


def _post(body_bytes):
    req = urllib.request.Request(INGEST_URL, data=body_bytes, method="POST")
    req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "push-reports-to-db")
    last = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read()[:200].decode('utf-8', 'replace')}"
            if e.code in (400, 401):        # 재시도 무의미
                break
        except Exception as ex:
            last = str(ex)[:200]
        if attempt < 3:
            time.sleep(5 * attempt)
    raise RuntimeError(f"ingest POST 실패: {last}")


def send_batches(reports, snapshots, dry_run):
    """파일을 읽어 BATCH_BYTES 단위로 묶어 전송. 실패 배치 수 반환."""
    items = [("report", kind, date, path) for kind, date, path in reports] \
          + [("snapshot", name, None, path) for name, path in snapshots]
    total_rep = total_snap = failed = 0
    batch = {"reports": [], "snapshots": []}
    batch_size = 0

    def flush():
        nonlocal batch, batch_size, total_rep, total_snap, failed
        if not batch["reports"] and not batch["snapshots"]:
            return
        n_r, n_s = len(batch["reports"]), len(batch["snapshots"])
        if dry_run:
            print(f"  [dry-run] batch: reports={n_r} snapshots={n_s} ~{batch_size//1024}KB")
        else:
            try:
                res = _post(json.dumps(batch, ensure_ascii=False).encode("utf-8"))
                total_rep += res.get("reports", 0)
                total_snap += res.get("snapshots", 0)
                if res.get("failed"):
                    failed += len(res["failed"])
                    print(f"  ! 서버가 거부한 항목: {res['failed']}", file=sys.stderr)
            except RuntimeError as ex:
                failed += n_r + n_s
                print(f"  ! {ex}", file=sys.stderr)
        batch = {"reports": [], "snapshots": []}
        batch_size = 0

    for what, key, date, path in items:
        payload = _load(path)
        if payload is None:
            failed += 1
            continue
        size = os.path.getsize(path)
        if batch_size and batch_size + size > BATCH_BYTES:
            flush()
        if what == "report":
            batch["reports"].append({"kind": key, "date": date, "payload": payload})
        else:
            batch["snapshots"].append({"name": key, "payload": payload})
        batch_size += size
    flush()

    if not dry_run:
        print(f"[push] 업서트 완료 — reports={total_rep} snapshots={total_snap}"
              + (f" 실패={failed}" if failed else ""))
    return failed


def main():
    args = set(sys.argv[1:])
    all_dates = "--all" in args
    dry_run = "--dry-run" in args

    if not TOKEN and not dry_run:
        print("[push] INGEST_TOKEN 미설정 — DB 업서트 생략 (git 커밋만으로 동작)")
        return 0

    reports, snapshots = collect(all_dates)
    print(f"[push] 대상: 아카이브 {len(reports)}건"
          f"{'(전 기간)' if all_dates else '(오늘)'} + 스냅샷 {len(snapshots)}건"
          f" → {INGEST_URL}")
    if not reports and not snapshots:
        return 0
    failed = send_batches(reports, snapshots, dry_run)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
