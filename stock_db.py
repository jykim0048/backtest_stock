#!/usr/bin/env python3
"""전종목 원본 데이터 저장소(2026-11-11, PLAN_STOCK_DB.md) — 공시 · 일별 수급 · CA 이력을 행 단위로 누적한다.

리포트 저장소(report_db: reports/snapshots = 가공된 JSON)와 달리 **원본을 종목·날짜 키로** 담는다.
커넥션 풀·재시도는 report_db 것을 그대로 쓴다(DATABASE_URL 하나, 풀 하나). 미설정이면 전 기능 비활성.

테이블(init_schema 가 부팅 시 idempotent 생성):
  stock_disclosures (rcept_no)          DART 접수번호가 자연키. 시장 전체. 정정 공시는 접수번호가 새로 나오므로 덮어쓰지 않는다.
  stock_flow_daily  (code, trade_date)  KIS 확정 대금 9주체(백만원) + 네이버 순매매량(주)·외국인 보유율 + 공매도 + 대차.
                                        업서트는 컬럼별 COALESCE — 한 소스가 그날 비어도 다른 소스가 채운 값을 지우지 않는다.
  stock_ca_events   (event_key)         예탁원 CA 마일스톤. first_seen/last_seen 으로 일정의 등장·소멸(취소·변경)을 추적.
  stock_collect_runs (run_date, part)   수집 실행 기록(커버리지·결손 탐지).

쓰기 창구는 railway_server `POST /api/ingest/rows`, 읽기는 `GET /api/stockdata/*`.
행 검증(normalize_*)은 DB 없이 도는 순수 함수 — 화이트리스트에 없는 키는 버리고 형식이 틀린 행은 거부한다.
"""
import datetime
import json
import re

import report_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stock_disclosures (
  rcept_no     text PRIMARY KEY,
  stock_code   text NOT NULL,
  filed_date   date NOT NULL,
  corp_name    text,
  title        text NOT NULL,
  filer        text,
  corp_cls     text,
  rule_dir     text,
  rule_kind    text,
  collected_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS stock_disclosures_code_date ON stock_disclosures (stock_code, filed_date DESC);
CREATE INDEX IF NOT EXISTS stock_disclosures_date ON stock_disclosures (filed_date);

CREATE TABLE IF NOT EXISTS stock_flow_daily (
  code text NOT NULL, trade_date date NOT NULL,
  close numeric, change_pct numeric,
  amt_prsn numeric, amt_frgn numeric, amt_orgn numeric,
  amt_fund numeric, amt_scrt numeric, amt_insu numeric, amt_ivtr numeric, amt_pe numeric, amt_etc numeric,
  qty_prsn bigint, qty_frgn bigint, qty_orgn bigint, hold_ratio numeric,
  short_qty bigint, short_amt numeric, short_vol_pct numeric, short_amt_pct numeric,
  loan_new bigint, loan_rdmp bigint, loan_bal_chg bigint, loan_bal_qty bigint, loan_bal_amt numeric,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (code, trade_date)
);
CREATE INDEX IF NOT EXISTS stock_flow_daily_date ON stock_flow_daily (trade_date);

CREATE TABLE IF NOT EXISTS stock_ca_events (
  event_key   text PRIMARY KEY,
  code        text NOT NULL,
  ca_type     text NOT NULL,
  kind        text,
  event_date  date NOT NULL,
  record_date date,
  importance  int,
  payload     jsonb NOT NULL,
  first_seen  date NOT NULL,
  last_seen   date NOT NULL
);
CREATE INDEX IF NOT EXISTS stock_ca_events_code_date ON stock_ca_events (code, event_date);

CREATE TABLE IF NOT EXISTS stock_collect_runs (
  run_date date NOT NULL, part text NOT NULL,
  universe int, ok int, failed int, rows int, note text,
  finished_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_date, part)
);
"""

_CODE_RE = re.compile(r"^[0-9A-Z]{6}$")            # 0220W0 같은 영문 혼합 코드 포함
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RCEPT_RE = re.compile(r"^\d{14}$")
_KEY_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_ROWS = 20000                                   # 요청 한 건의 행 상한

FLOW_NUM = ("close", "change_pct", "amt_prsn", "amt_frgn", "amt_orgn", "amt_fund", "amt_scrt", "amt_insu",
            "amt_ivtr", "amt_pe", "amt_etc", "hold_ratio", "short_amt", "short_vol_pct", "short_amt_pct", "loan_bal_amt")
FLOW_INT = ("qty_prsn", "qty_frgn", "qty_orgn", "short_qty", "loan_new", "loan_rdmp", "loan_bal_chg", "loan_bal_qty")
FLOW_COLS = FLOW_NUM + FLOW_INT
PARTS = ("dart", "flow", "naver", "ca")


def enabled():
    return report_db.enabled()


def init_schema():
    def _op(cur):
        cur.execute(_SCHEMA)
        return True
    return report_db._run(_op) is True


# ── 행 검증(순수 함수) ───────────────────────────────────────────────────────────────────────
def _date(v):
    """YYYY-MM-DD 또는 YYYYMMDD → YYYY-MM-DD (실재하는 날짜만). 아니면 None."""
    s = str(v or "").strip()
    if re.fullmatch(r"\d{8}", s):
        s = f"{s[:4]}-{s[4:6]}-{s[6:]}"
    if not _DATE_RE.match(s):
        return None
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return None
    return s


def _code(v):
    s = str(v or "").strip().upper()
    if s.isdigit():
        s = s.zfill(6)
    return s if _CODE_RE.match(s) else None


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(str(v).replace(",", "")) if not isinstance(v, (int, float)) else float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _int(v):
    f = _num(v)
    return int(round(f)) if f is not None else None


def _text(v, n):
    s = str(v).strip() if v is not None else ""
    return s[:n] or None


def normalize_flow(row):
    code, date = _code(row.get("code")), _date(row.get("date") or row.get("trade_date"))
    if not code or not date:
        return None
    out = {"code": code, "trade_date": date}
    for k in FLOW_NUM:
        out[k] = _num(row.get(k))
    for k in FLOW_INT:
        out[k] = _int(row.get(k))
    return out if any(out[k] is not None for k in FLOW_COLS) else None       # 값이 하나도 없는 행은 넣지 않는다


def normalize_disclosure(row, tagger=None):
    rno, code, date = str(row.get("rcept_no") or "").strip(), _code(row.get("stock_code") or row.get("code")), _date(row.get("date") or row.get("filed_date"))
    title = _text(row.get("title"), 300)
    if not _RCEPT_RE.match(rno) or not code or not date or not title:
        return None
    rule_dir, rule_kind = (tagger(title) if tagger else (None, None))
    return {"rcept_no": rno, "stock_code": code, "filed_date": date, "corp_name": _text(row.get("corp_name"), 100),
            "title": title, "filer": _text(row.get("filer"), 100), "corp_cls": _text(row.get("corp_cls"), 1),
            "rule_dir": rule_dir, "rule_kind": rule_kind}


def normalize_ca(row):
    key, code, date = str(row.get("event_key") or "").strip(), _code(row.get("code")), _date(row.get("date") or row.get("event_date"))
    typ, seen = _text(row.get("type") or row.get("ca_type"), 30), _date(row.get("seen"))
    payload = row.get("payload")
    if not _KEY_RE.match(key) or not code or not date or not typ or not seen or not isinstance(payload, dict):
        return None
    return {"event_key": key, "code": code, "ca_type": typ, "kind": _text(row.get("kind"), 30), "event_date": date,
            "record_date": _date(row.get("record_date")), "importance": _int(row.get("importance")),
            "payload": json.dumps(payload, ensure_ascii=False), "seen": seen}


def normalize_run(row):
    date, part = _date(row.get("date") or row.get("run_date")), str(row.get("part") or "")
    if not date or part not in PARTS:
        return None
    return {"run_date": date, "part": part, "universe": _int(row.get("universe")), "ok": _int(row.get("ok")),
            "failed": _int(row.get("failed")), "rows": _int(row.get("rows")), "note": _text(row.get("note"), 300)}


def _tagger():
    try:                                           # 서버는 analysis.sources 를 이미 올려 둔다. 없으면(경량 환경) 태그 없이
        from analysis.sources import dart_rule_direction
        return dart_rule_direction
    except Exception:
        return None


# ── 쓰기 ─────────────────────────────────────────────────────────────────────────────────────
def flow_upsert_sql():
    cols = ("code", "trade_date") + FLOW_COLS
    sets = ", ".join(f"{c} = COALESCE(EXCLUDED.{c}, stock_flow_daily.{c})" for c in FLOW_COLS)
    return (f"INSERT INTO stock_flow_daily ({', '.join(cols)}) VALUES %s"
            f" ON CONFLICT (code, trade_date) DO UPDATE SET {sets}, updated_at = now()"), cols


def upsert_rows(table, rows):
    """{"accepted", "rejected"} 또는 실패 시 None. table = flow | disclosures | ca | runs."""
    if table not in ("flow", "disclosures", "ca", "runs") or not isinstance(rows, list) or len(rows) > MAX_ROWS:
        return None
    tagger = _tagger() if table == "disclosures" else None
    norm = {"flow": normalize_flow, "ca": normalize_ca, "runs": normalize_run,
            "disclosures": lambda r: normalize_disclosure(r, tagger)}[table]
    good = []
    for r in rows:
        try:
            n = norm(r) if isinstance(r, dict) else None
        except Exception:
            n = None
        if n:
            good.append(n)
    rejected = len(rows) - len(good)
    if not good:
        return {"accepted": 0, "rejected": rejected}
    # 같은 키가 한 배치에 두 번 오면 ON CONFLICT 가 같은 행을 두 번 건드려 실패한다 — 마지막 것만 남긴다
    pk = {"flow": ("code", "trade_date"), "disclosures": ("rcept_no",), "ca": ("event_key",), "runs": ("run_date", "part")}[table]
    good = list({tuple(g[k] for k in pk): g for g in good}.values())

    def _op(cur):
        from psycopg2.extras import execute_values
        if table == "flow":
            sql, cols = flow_upsert_sql()
            execute_values(cur, sql, [tuple(g[c] for c in cols) for g in good], page_size=500)
        elif table == "disclosures":
            cols = ("rcept_no", "stock_code", "filed_date", "corp_name", "title", "filer", "corp_cls", "rule_dir", "rule_kind")
            execute_values(cur, f"INSERT INTO stock_disclosures ({', '.join(cols)}) VALUES %s ON CONFLICT (rcept_no) DO NOTHING",
                           [tuple(g[c] for c in cols) for g in good], page_size=500)
        elif table == "ca":
            execute_values(cur,
                "INSERT INTO stock_ca_events (event_key, code, ca_type, kind, event_date, record_date, importance, payload, first_seen, last_seen)"
                " VALUES %s ON CONFLICT (event_key) DO UPDATE SET payload = EXCLUDED.payload, importance = EXCLUDED.importance,"
                " first_seen = LEAST(stock_ca_events.first_seen, EXCLUDED.first_seen),"
                " last_seen = GREATEST(stock_ca_events.last_seen, EXCLUDED.last_seen)",
                [(g["event_key"], g["code"], g["ca_type"], g["kind"], g["event_date"], g["record_date"], g["importance"],
                  g["payload"], g["seen"], g["seen"]) for g in good],
                template="(%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)", page_size=500)
        else:
            cols = ("run_date", "part", "universe", "ok", "failed", "rows", "note")
            execute_values(cur, f"INSERT INTO stock_collect_runs ({', '.join(cols)}) VALUES %s"
                           " ON CONFLICT (run_date, part) DO UPDATE SET universe = EXCLUDED.universe, ok = EXCLUDED.ok,"
                           " failed = EXCLUDED.failed, rows = EXCLUDED.rows, note = EXCLUDED.note, finished_at = now()",
                           [tuple(g[c] for c in cols) for g in good])
        return True
    if report_db._run(_op) is not True:
        return None
    return {"accepted": len(good), "rejected": rejected}


# ── 읽기 ─────────────────────────────────────────────────────────────────────────────────────
def _window(frm, to, days=120):
    to_d = _date(to) or datetime.date.today().isoformat()
    frm_d = _date(frm) or (datetime.date.fromisoformat(to_d) - datetime.timedelta(days=days)).isoformat()
    return frm_d, to_d


def _rows(cur, cols):
    out = []
    for r in cur.fetchall():
        d = {}
        for c, v in zip(cols, r):
            if isinstance(v, (datetime.date, datetime.datetime)):
                v = v.isoformat()
            elif v is not None and c not in ("payload",) and not isinstance(v, (int, str, dict, list)):
                v = float(v)                       # Decimal → float
            d[c] = v
        out.append(d)
    return out


def flow(code, frm=None, to=None):
    code = _code(code)
    if not code:
        return None
    f, t = _window(frm, to)
    cols = ("trade_date",) + FLOW_COLS

    def _op(cur):
        cur.execute(f"SELECT {', '.join(cols)} FROM stock_flow_daily WHERE code=%s AND trade_date BETWEEN %s AND %s"
                    " ORDER BY trade_date DESC", (code, f, t))
        return _rows(cur, cols)
    return report_db._run(_op)


def disclosures(code, frm=None, to=None):
    code = _code(code)
    if not code:
        return None
    f, t = _window(frm, to, days=365)
    cols = ("filed_date", "title", "filer", "corp_name", "rcept_no", "rule_dir", "rule_kind")

    def _op(cur):
        cur.execute(f"SELECT {', '.join(cols)} FROM stock_disclosures WHERE stock_code=%s AND filed_date BETWEEN %s AND %s"
                    " ORDER BY filed_date DESC, rcept_no DESC LIMIT 500", (code, f, t))
        return _rows(cur, cols)
    return report_db._run(_op)


def ca(code, frm=None, to=None):
    code = _code(code)
    if not code:
        return None
    today = datetime.date.today()
    f = _date(frm) or (today - datetime.timedelta(days=365)).isoformat()
    t = _date(to) or (today + datetime.timedelta(days=180)).isoformat()
    cols = ("event_date", "ca_type", "kind", "record_date", "importance", "payload", "first_seen", "last_seen")

    def _op(cur):
        cur.execute(f"SELECT {', '.join(cols)} FROM stock_ca_events WHERE code=%s AND event_date BETWEEN %s AND %s"
                    " ORDER BY event_date", (code, f, t))
        rows = _rows(cur, cols)
        cur.execute("SELECT max(last_seen) FROM stock_ca_events")
        latest = cur.fetchone()[0]
        for r in rows:                              # 미래 일정인데 최신 수집에 안 보이면 취소·변경으로 본다
            r["dropped"] = bool(latest and r["last_seen"] < latest.isoformat() and r["event_date"] >= r["last_seen"])
        return rows
    return report_db._run(_op)


def bundle(code, days=120):
    code = _code(code)
    if not code:
        return None
    frm = (datetime.date.today() - datetime.timedelta(days=max(1, min(int(days or 120), 800)))).isoformat()
    return {"code": code, "from": frm, "flow": flow(code, frm), "disclosures": disclosures(code, frm), "ca": ca(code, frm)}


def status():
    """테이블별 행 수·최신 날짜·종목 수 + 최근 실행 기록. DB 비활성·실패면 None."""
    def _op(cur):
        out = {}
        for name, table, dcol, ccol in (("flow", "stock_flow_daily", "trade_date", "code"),
                                        ("disclosures", "stock_disclosures", "filed_date", "stock_code"),
                                        ("ca", "stock_ca_events", "event_date", "code")):
            cur.execute(f"SELECT count(*), min({dcol}), max({dcol}), count(DISTINCT {ccol}) FROM {table}")
            n, lo, hi, codes = cur.fetchone()
            out[name] = {"rows": n, "from": lo.isoformat() if lo else None, "to": hi.isoformat() if hi else None, "codes": codes}
        cur.execute("SELECT count(DISTINCT code) FROM stock_flow_daily WHERE trade_date = (SELECT max(trade_date) FROM stock_flow_daily)")
        out["flow"]["codesLatest"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FILTER (WHERE amt_frgn IS NOT NULL), count(*) FILTER (WHERE qty_frgn IS NOT NULL),"
                    " count(*) FILTER (WHERE short_qty IS NOT NULL), count(*) FILTER (WHERE loan_bal_qty IS NOT NULL) FROM stock_flow_daily")
        k, nv, sh, ln = cur.fetchone()
        out["flow"]["filled"] = {"kis": k, "naver": nv, "short": sh, "loan": ln}
        cur.execute("SELECT run_date, part, universe, ok, failed, rows, note, finished_at FROM stock_collect_runs"
                    " ORDER BY run_date DESC, part LIMIT 12")
        out["runs"] = _rows(cur, ("run_date", "part", "universe", "ok", "failed", "rows", "note", "finished_at"))
        return out
    return report_db._run(_op)
