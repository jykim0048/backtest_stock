#!/usr/bin/env python3
"""리포트 Postgres 저장소 (Railway Postgres).

대시보드 데이터 JSON 의 1차 저장소. 기존 'GitHub 커밋 = DB' 구조를 대체하되,
읽기 실패 시 호출측(railway_server)이 GitHub raw 로 폴백하므로 이 모듈의 모든
함수는 예외 대신 None/False 를 돌려주는 방향으로 방어적으로 동작한다.

스키마 (init_schema 가 부팅 시 idempotent 생성):
  reports   (kind, report_date) PK — 일자별 아카이브.
             kind 는 public/ 하위 경로에서 유도: reports/<date>.json → 'daily',
             reports/intraday/<date>.json → 'intraday',
             reports/selection/intraday/<date>.json → 'selection/intraday',
             briefing/<date>.json → 'briefing'
  snapshots (name) PK — 최신 스냅샷. name = public/ 기준 상대경로
             (예: 'intraday_report.json', 'data/investment_warning.json',
              'briefing/latest.json')

index.json 은 저장하지 않는다 — 날짜 목록은 reports 테이블 쿼리로 대체
(인덱스 파일 갱신 누락 버그 원천 제거). 단 DB 에 해당 kind 가 한 건도 없으면
None 을 돌려 호출측 raw 폴백을 살린다(백필 전 부분 목록 노출 방지).

Env: DATABASE_URL (Railway Postgres 참조 변수). 미설정 시 전 기능 비활성.
"""
import os
import re
import json
import threading
import datetime
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool = None
_pool_lock = threading.Lock()
_state = {"configured": bool(DATABASE_URL), "ok": None,
          "note": "DATABASE_URL 미설정" if not DATABASE_URL else "unconnected"}

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KIND_RE = re.compile(r"^[A-Za-z0-9_\-/]{1,80}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  kind        text        NOT NULL,
  report_date date        NOT NULL,
  payload     jsonb       NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (kind, report_date)
);
CREATE TABLE IF NOT EXISTS snapshots (
  name       text        PRIMARY KEY,
  payload    jsonb       NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
"""


def enabled():
    return bool(DATABASE_URL)


def status():
    return dict(_state)


def _get_pool():
    global _pool
    if not DATABASE_URL:
        return None
    with _pool_lock:
        if _pool is None:
            from psycopg2.pool import ThreadedConnectionPool
            _pool = ThreadedConnectionPool(1, 5, dsn=DATABASE_URL)
        return _pool


@contextmanager
def _conn():
    """풀에서 커넥션 하나. 예외 시 rollback + (연결성 오류면) 폐기."""
    import psycopg2
    pool = _get_pool()
    conn = pool.getconn()
    broken = False
    try:
        yield conn
        conn.commit()
    except Exception as ex:
        try:
            conn.rollback()
        except Exception:
            broken = True
        if isinstance(ex, (psycopg2.OperationalError, psycopg2.InterfaceError)):
            broken = True
        raise
    finally:
        pool.putconn(conn, close=broken)


def _run(op, *args):
    """op(cursor, *args) 를 실행 — 연결 끊김이면 1회 재시도. 실패 시 None."""
    if not DATABASE_URL:
        return None
    try:
        import psycopg2
    except ImportError:
        _state.update(ok=False, note="psycopg2 미설치 (requirements.txt 확인)")
        return None
    for attempt in (1, 2):
        try:
            with _conn() as conn:
                with conn.cursor() as cur:
                    out = op(cur, *args)
            _state.update(ok=True, note="ok")
            return out
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as ex:
            _state.update(ok=False, note=f"conn: {str(ex)[:120]}")
            if attempt == 2:
                return None
        except Exception as ex:
            _state.update(ok=False, note=f"query: {str(ex)[:120]}")
            return None


def init_schema():
    """부팅 시 1회 — 테이블 idempotent 생성. 성공 여부 반환."""
    def _op(cur):
        cur.execute(_SCHEMA)
        return True
    return _run(_op) is True


# ---------------------------------------------------------------------------
# 경로 <-> (종류, kind, date) 매핑 — 대시보드 fetch 경로를 그대로 DB 키로 해석
# ---------------------------------------------------------------------------
def classify(rel):
    """public/ 기준 상대경로 → ('report', kind, date) | ('index', kind, None)
    | ('snapshot', name, None) | None (DB 비대상: assets 등)."""
    rel = rel.replace("\\", "/").lstrip("/")
    if not rel.endswith(".json") or rel.startswith("assets/"):
        return None
    parts = rel.split("/")
    stem = parts[-1][:-len(".json")]
    if parts[0] == "reports":
        kind = "/".join(parts[1:-1]) or "daily"
        if stem == "index":
            return ("index", kind, None)
        if _DATE_RE.match(stem):
            return ("report", kind, stem)
        return None
    if parts[0] == "briefing" and len(parts) == 2:
        if stem == "index":
            return ("index", "briefing", None)
        if _DATE_RE.match(stem):
            return ("report", "briefing", stem)
    return ("snapshot", rel, None)


def paths_for(kind, date):
    """(kind, date) → 대시보드가 fetch 하는 상대경로(리포트, index) — 캐시 무효화용."""
    if kind == "daily":
        return (f"reports/{date}.json", "reports/index.json")
    if kind == "briefing":
        return (f"briefing/{date}.json", "briefing/index.json")
    return (f"reports/{kind}/{date}.json", f"reports/{kind}/index.json")


# ---------------------------------------------------------------------------
# 읽기 (railway_server 서빙 경로) — 반환은 UTF-8 JSON bytes, 실패/부재는 None
# ---------------------------------------------------------------------------
def fetch(rel):
    c = classify(rel)
    if c is None:
        return None
    what, key, date = c

    if what == "report":
        def _op(cur):
            cur.execute("SELECT payload::text FROM reports"
                        " WHERE kind=%s AND report_date=%s", (key, date))
            row = cur.fetchone()
            return row[0].encode("utf-8") if row else None
        return _run(_op)

    if what == "index":
        def _op(cur):
            cur.execute("SELECT report_date FROM reports WHERE kind=%s"
                        " ORDER BY report_date DESC", (key,))
            rows = cur.fetchall()
            if not rows:            # 백필 전 빈/부분 목록 노출 방지 → raw 폴백
                return None
            return json.dumps([r[0].isoformat() for r in rows]).encode("utf-8")
        return _run(_op)

    def _op(cur):
        cur.execute("SELECT payload::text FROM snapshots WHERE name=%s", (key,))
        row = cur.fetchone()
        return row[0].encode("utf-8") if row else None
    return _run(_op)


# ---------------------------------------------------------------------------
# 쓰기 (ingest API / 백필) — 성공 True / 실패 False
# ---------------------------------------------------------------------------
def upsert_report(kind, date, payload):
    if not _KIND_RE.match(kind or "") or not _DATE_RE.match(date or ""):
        return False
    text = json.dumps(payload, ensure_ascii=False)
    def _op(cur):
        cur.execute(
            "INSERT INTO reports (kind, report_date, payload, updated_at)"
            " VALUES (%s, %s, %s::jsonb, now())"
            " ON CONFLICT (kind, report_date)"
            " DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
            (kind, date, text))
        return True
    return _run(_op) is True


def upsert_snapshot(name, payload):
    name = (name or "").replace("\\", "/").lstrip("/")
    if not name.endswith(".json") or ".." in name or len(name) > 200:
        return False
    text = json.dumps(payload, ensure_ascii=False)
    def _op(cur):
        cur.execute(
            "INSERT INTO snapshots (name, payload, updated_at)"
            " VALUES (%s, %s::jsonb, now())"
            " ON CONFLICT (name)"
            " DO UPDATE SET payload = EXCLUDED.payload, updated_at = now()",
            (name, text))
        return True
    return _run(_op) is True
