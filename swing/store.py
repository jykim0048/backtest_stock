"""저장소(6단계) — 키-문서(JSON) 저장. 키 예:
  ledger                              원장 전체(engine 스키마)
  decision/<date>/<code>/<purpose>    trading_agent 판단 결과(Decision.to_dict)
  run/<date>                          일일 작업 로그(이벤트·후보·판정 요약)
  signals/<date>                      그날 주간 브리핑 축약본(signals.compact)

구현 3종: FileStore(로컬·테스트), SqlStore(Postgres — Railway, 로컬 테스트는 sqlite3).
Postgres 는 기존 DATABASE_URL 을 공유하되 테이블은 swing_docs 하나로 분리한다.
"""
import datetime
import json
import os


class FileStore:
    def __init__(self, root):
        self.root = root

    def _path(self, key):
        return os.path.join(self.root, *key.split("/")) + ".json"

    def get(self, key, default=None):
        try:
            with open(self._path(key), encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default

    def put(self, key, obj):
        p = self._path(key)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)

    def keys(self, prefix=""):
        out = []
        for dp, _, fs in os.walk(self.root):
            for fn in fs:
                if fn.endswith(".json"):
                    rel = os.path.relpath(os.path.join(dp, fn), self.root)[:-5]
                    k = rel.replace(os.sep, "/")
                    if k.startswith(prefix):
                        out.append(k)
        return sorted(out)


SCHEMA = ("CREATE TABLE IF NOT EXISTS swing_docs ("
          "key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL)")


class SqlStore:
    """DB-API 커넥션 팩토리 기반. paramstyle: psycopg2='%s', sqlite3='?'.
    payload 는 이식성 위해 TEXT(JSON 문자열) — 문서 단위 조회만 하므로 JSONB 불필요."""

    def __init__(self, connect, ph="%s"):
        self.connect, self.ph = connect, ph
        self._exec(SCHEMA)

    def _exec(self, sql, args=(), fetch=False):
        conn = self.connect()
        try:
            cur = conn.cursor()
            cur.execute(sql, args)
            rows = cur.fetchall() if fetch else None
            conn.commit()
            return rows
        finally:
            conn.close()

    def get(self, key, default=None):
        r = self._exec(f"SELECT payload FROM swing_docs WHERE key = {self.ph}", (key,), True)
        return json.loads(r[0][0]) if r else default

    def put(self, key, obj):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        p = self.ph
        self._exec(f"INSERT INTO swing_docs (key, payload, updated_at) VALUES ({p}, {p}, {p}) "
                   "ON CONFLICT (key) DO UPDATE SET payload = excluded.payload, "
                   "updated_at = excluded.updated_at",
                   (key, json.dumps(obj, ensure_ascii=False), now))

    def keys(self, prefix=""):
        r = self._exec(f"SELECT key FROM swing_docs WHERE key LIKE {self.ph} ORDER BY key",
                       (prefix.replace("%", r"\%") + "%",), True)
        return [x[0] for x in r]


def from_env():
    """DATABASE_URL 있으면 Postgres, 없으면 SWING_DATA_DIR(기본 swing/data/local) 파일."""
    dsn = os.environ.get("DATABASE_URL")
    if dsn:
        import psycopg2
        return SqlStore(lambda: psycopg2.connect(dsn), "%s")
    root = os.environ.get("SWING_DATA_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "local")
    return FileStore(root)
