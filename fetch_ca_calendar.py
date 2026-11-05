"""예탁원(KSD) 기업행위(CA) 일정 수집 -> public/ca_calendar.json

소스: KIS 허브(trading_strategies Railway 서비스) HTTP `GET {FLOW_API_BASE}/ca?raw=0`
  - code 생략 = 시장 전체, 기본 창 = 오늘-14일 ~ 오늘+45일 (허브 기본값).
  - 허브가 KIS 예탁원정보(ksdinfo) TR 12종(유상·무상·배당·주식매수청구·합병/분할·액면교체·
    자본감소·상장정보·공모주청약·실권주·의무예치·주주총회)을 이벤트 단위로 정규화해 돌려준다.
  - 응답: {"status","from","to","asof","counts":{type:n},"errors":{type:msg},
           "events":[{type,label,code,name,recordDate,milestones:[{kind,label,date,end?,note?}],fields}]}

이 스크립트는 이벤트를 '마일스톤(날짜) 단위 행'으로 펼친다 — 캘린더는 날짜 기준이므로
유상증자 1건이 권리락·청약·신주 상장 3행이 된다. 행마다 영향도 importance(1~3) 를 판정한다
(_importance). 종목명은 허브가 대개 null 이라 public/assets/krx_companies.json 으로 채운다.

필드명(fields.*)은 KIS 문서 기준이고 실서비스 미검증 — 전부 방어적으로 다룬다(키 누락·
문자열 숫자·None). 표시용으로 스칼라 필드만 남긴다(_slim_fields).

실패 내성 (fetch_econ_calendar 와 동일 철학 — best-effort, 항상 exit 0):
  - 호출 실패·status=error·events 비정상 → 기존 파일 보존.
  - 일부 타입만 오류(errors 에 있고 이번 응답에 그 타입 이벤트 0건) → 직전 파일의 그 타입
    행을 이월(carried=true) — 타입 하나의 일시 장애로 일정이 사라지지 않게.
  - 장중 30분 회차마다 시장 전체 조회는 허브 부하가 커서, 직전 파일이 CA_MIN_INTERVAL_MIN
    (기본 90분) 이내면 호출을 생략한다(--force 로 무시). CA 일정은 장중에 거의 안 바뀐다.
"""
import os
import sys
import json
import time
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(ROOT, "public", "ca_calendar.json")
KRX_MASTER = os.path.join(ROOT, "public", "assets", "krx_companies.json")

KST = datetime.timezone(datetime.timedelta(hours=9))

FLOW_API_BASE = os.environ.get(
    "FLOW_API_BASE", "https://tradingstrategies-production-09d4.up.railway.app")
TIMEOUT = (10, 170)          # (connect, read) — 시장 전체 조회는 허브 쪽 TR 다건이라 read 를 길게
RETRIES = 1                 # 실패 시 1회 재시도
RETRY_WAIT_S = 5
MIN_INTERVAL_MIN = int(os.environ.get("CA_MIN_INTERVAL_MIN", "90"))
SOURCE = "KIS ksdinfo via hub /ca"

TYPE_LABELS = {
    "paidin": "유상증자", "bonus": "무상증자", "dividend": "배당", "purreq": "주식매수청구",
    "merger": "합병/분할", "revsplit": "액면교체", "capdcrs": "자본감소", "listing": "상장정보",
    "ipo": "공모주청약", "forfeit": "실권주", "deposit": "의무예치", "meeting": "주주총회",
}
_FIELD_MAX = 16             # 행당 fields 키 상한 (표시용 슬림)
_STR_MAX = 60               # 문자열 필드 길이 상한


def _warn(msg):
    print(f"[ca] {msg}")


# -- 정규화 헬퍼 -----------------------------------------------------------------

def _norm_date(s):
    """'YYYY-MM-DD' | 'YYYYMMDD' | 'YYYY.MM.DD' | 'YYYY/MM/DD' → 'YYYY-MM-DD'. 무효면 None."""
    if s is None:
        return None
    t = str(s).strip().replace(".", "-").replace("/", "-")
    if len(t) == 8 and t.isdigit():
        t = f"{t[:4]}-{t[4:6]}-{t[6:]}"
    t = t[:10]
    try:
        datetime.date.fromisoformat(t)
    except ValueError:
        return None
    return t


def _num(v):
    """숫자 또는 숫자 문자열('1,234', '12.5%') → float. 실패 None."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def _norm_code(c):
    s = str(c or "").strip()
    if s.upper().startswith("A") and len(s) == 7:      # 'A005930' 류
        s = s[1:]
    if s.isdigit() and len(s) < 6:
        s = s.zfill(6)
    return s


def _slim_fields(fields):
    """스칼라(숫자·짧은 문자열·bool)만, None·빈 문자열·중첩 구조 제거, 키 상한."""
    out = {}
    if not isinstance(fields, dict):
        return out
    for k, v in fields.items():
        if len(out) >= _FIELD_MAX:
            break
        if v is None or isinstance(v, (dict, list, tuple)):
            continue
        if isinstance(v, str):
            v = v.strip()
            if not v:
                continue
            v = v[:_STR_MAX]
        out[str(k)] = v
    return out


KRX_CAPS = os.path.join(ROOT, "public", "assets", "krx_caps.json")


def load_listed(path=KRX_CAPS):
    """코스피·코스닥 상장 종목 코드 집합 — krx_caps.json(매일 갱신, 시가총액 맵). 실패·빈 파일이면 None(필터 안 함).
    krx_companies.json 은 2026-06 이후 갱신이 멈춰 최근 상장사(인텔리빅스 등)가 빠져 있어 상장 판정엔 쓰지 않는다."""
    try:
        with open(path, encoding="utf-8") as f:
            caps = (json.load(f) or {}).get("caps") or {}
        out = {_norm_code(c) for c in caps}
        out.discard("")
        return out or None
    except Exception as e:
        _warn(f"krx_caps.json 로드 실패(상장 필터 생략): {e}")
        return None


def load_names(path=KRX_MASTER):
    """{code: name} — krx_companies.json(전 상장 종목). 실패 시 {}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f) or []
        out = {}
        for e in data:
            code = _norm_code(e.get("code"))
            if code and e.get("name"):
                out[code] = e["name"]
        return out
    except Exception as e:
        _warn(f"krx_companies 로드 실패(종목명 미표기): {e}")
        return {}


# -- 영향도 판정 -----------------------------------------------------------------

def _importance(typ, kind, label, fields):
    """행 영향도 1~3.

    3 = 유상증자 권리락·신주 상장, 의무예치(해제 오버행), 합병/분할·자본감소·액면교체의
        매매정지·재상장, 추가상장(신주 비중 5%+).
    2 = 무상증자 권리락·상장, 배당락(시가배당률 3%+ 또는 미상), 그 외 추가상장, 주식매수청구
        마감, 유상 청약, 합병류 권리락·마감.
    1 = 기준일·지급일·주총·공모청약·실권주 등 나머지.
    """
    kind = kind or ""
    label = label or ""
    f = fields or {}
    if kind in ("record", "pay"):
        return 1
    if typ == "paidin":
        if kind in ("ex", "listing"):
            return 3
        return 2 if kind == "subscribe" else 1
    if typ == "deposit":
        # 허브가 예치일~반환일을 '보호예수 시작'(deposit)·'보호예수 해제'(release)로 나눠 준다(2026-11-05).
        # 해제가 매도 물량 일정 — 총발행 1% 이상이면 3, 그 밖 2. 예치 시작은 1(소음).
        if kind == "release":
            pct = _num(f.get("pctOfTotal"))
            return 3 if (pct is None or pct >= 1) else 2
        return 1
    if typ in ("merger", "capdcrs", "revsplit"):
        if kind == "halt" or kind == "listing" or "재상장" in label or "매매정지" in label:
            return 3
        return 2 if kind in ("ex", "deadline") else 1
    if typ == "listing":
        # 기술적 상장(액면병합·분할·상호변경·재상장 — 주식 수만 바뀌거나 이름만 바뀜)은 물량 100%여도 희석이 아니다(2026-11-05)
        rs = str(f.get("reason") or "")
        if any(w in rs for w in ("액면", "상호변경", "재상장", "통일교체")):
            return 1
        if "무상" in rs:
            return 2
        pct = _num(f.get("newSharesPct"))
        if pct is None:
            pct = _num(f.get("pctOfTotal"))
        return 3 if (pct is not None and pct >= 5) else 2
    if typ == "bonus":
        return 2 if kind in ("ex", "listing") else 1
    if typ == "dividend":
        if kind == "ex":
            y = _num(f.get("cashYieldPct"))          # 허브는 액면배당률(faceDivPct)만 준다 — 시가배당률 미상이면
            if y is None:                            # 고배당 여부(high_divi_gb)로 가른다(2026-11-04 실측)
                return 1 if str(f.get("highDividend") or "").upper() == "N" else 2
            return 2 if y >= 3 else 1
        return 1
    if typ == "purreq":
        return 2 if kind == "deadline" else 1
    return 1   # ipo · forfeit · meeting · 미상 타입


# -- 펼치기 ----------------------------------------------------------------------

def flatten(events, names=None, listed=None):
    """이벤트 목록 → 마일스톤 행 목록(날짜·영향도 정렬). 비정상 항목은 건너뛴다.

    - 마일스톤이 없고 recordDate 만 있으면 '기준일' 1행.
    - 같은 이벤트에 ex(권리락·배당락 등) 마일스톤이 있으면 record(기준일) 행은 생략(중복 소음).
    - (date, kind, type, code, 라벨) 중복 행 제거.
    """
    names = names or {}
    rows, seen = [], set()
    for ev in events or []:
        if not isinstance(ev, dict):
            continue
        typ = str(ev.get("type") or "").strip()
        if not typ:
            continue
        code = _norm_code(ev.get("code"))
        if listed is not None and code not in listed:      # 비상장·장외(예탁원은 비상장 예탁 종목도 준다 — 네파 신주 25,019% 등)
            continue
        name = ev.get("name") or names.get(code)
        type_label = ev.get("label") or TYPE_LABELS.get(typ) or typ
        fields = _slim_fields(ev.get("fields"))
        ms = [m for m in (ev.get("milestones") or []) if isinstance(m, dict)]
        if not ms and ev.get("recordDate"):
            ms = [{"kind": "record", "label": "기준일", "date": ev.get("recordDate")}]
        has_ex = any((m.get("kind") or "") == "ex" and _norm_date(m.get("date")) for m in ms)
        for m in ms:
            kind = str(m.get("kind") or "").strip()
            date = _norm_date(m.get("date"))
            if not date:
                continue
            if kind == "record" and has_ex:
                continue
            label = m.get("label") or kind or "일정"
            key = (date, kind, typ, code, label)
            if key in seen:
                continue
            seen.add(key)
            row = {"date": date}
            end = _norm_date(m.get("end"))
            if end and end > date:
                row["end"] = end
            row.update({
                "kind": kind, "milestoneLabel": label,
                "type": typ, "typeLabel": type_label,
                "code": code, "name": name,
                "importance": _importance(typ, kind, label, fields),
                "fields": fields,
            })
            if m.get("note"):
                row["note"] = str(m["note"])[:120]
            rows.append(row)
    rows.sort(key=lambda r: (r["date"], -r["importance"], r.get("code") or "", r["kind"]))
    return rows


def carry_errored_types(rows, prev_rows, errors, window_from):
    """오류 타입(이번 응답에 행 0건) 은 직전 파일 행을 이월(carried=true). 창 시작 이전 행은 버림."""
    if not errors or not prev_rows:
        return rows, 0
    have = {r["type"] for r in rows}
    lost = {t for t in errors if t not in have}
    if not lost:
        return rows, 0
    carried = []
    for r in prev_rows:
        if not isinstance(r, dict) or r.get("type") not in lost:
            continue
        last = r.get("end") or r.get("date") or ""
        if window_from and last < window_from:
            continue
        carried.append({**r, "carried": True})
    if not carried:
        return rows, 0
    out = rows + carried
    out.sort(key=lambda r: (r["date"], -(r.get("importance") or 0), r.get("code") or "",
                            r.get("kind") or ""))
    return out, len(carried)


def build(resp, prev, now_kst, names=None, listed=None):
    """허브 응답 → 저장 payload. 저장하지 말아야 하면(실패) None."""
    if not isinstance(resp, dict):
        _warn("응답이 JSON 객체가 아님 — 기존 파일 보존")
        return None
    if resp.get("status") != "success":
        _warn(f"status={resp.get('status')!r} — 기존 파일 보존 (errors={resp.get('errors')})")
        return None
    events = resp.get("events")
    if not isinstance(events, list):
        _warn("events 가 리스트가 아님 — 기존 파일 보존")
        return None
    errors = resp.get("errors") if isinstance(resp.get("errors"), dict) else {}
    if not events and errors:
        _warn(f"events 0건 + 오류 {len(errors)}타입 — 기존 파일 보존")
        return None
    rows = flatten(events, names, listed)
    n_unlisted = 0 if listed is None else sum(1 for e in events if isinstance(e, dict) and _norm_code(e.get("code")) not in listed)
    w_from = _norm_date(resp.get("from"))
    w_to = _norm_date(resp.get("to"))
    rows, n_carry = carry_errored_types(rows, (prev or {}).get("rows") or [], errors, w_from)
    if n_carry:
        _warn(f"오류 타입 {sorted(errors)} — 직전 행 {n_carry}건 이월")
    row_counts = {}
    for r in rows:
        row_counts[r["type"]] = row_counts.get(r["type"], 0) + 1
    return {
        "asof": now_kst.strftime("%Y-%m-%d %H:%M KST"),
        "hubAsof": resp.get("asof"),
        "from": w_from,
        "to": w_to,
        "rows": rows,
        "counts": resp.get("counts") if isinstance(resp.get("counts"), dict) else {},
        "rowCounts": row_counts,
        "errors": errors,
        "unlistedSkipped": n_unlisted,        # 상장 종목만(2026-11-05) — 거른 이벤트 수
        "source": SOURCE,
    }


# -- I/O -------------------------------------------------------------------------

def _load_prev():
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _fresh(prev, now_kst, minutes):
    """직전 파일 asof 가 오늘이고 minutes 분 이내면 True."""
    if minutes <= 0:
        return False
    try:
        at = datetime.datetime.strptime(str(prev.get("asof", ""))[:16], "%Y-%m-%d %H:%M") \
            .replace(tzinfo=KST)
    except ValueError:
        return False
    return at.date() == now_kst.date() and (now_kst - at) < datetime.timedelta(minutes=minutes)


def fetch_hub():
    import requests
    url = f"{FLOW_API_BASE.rstrip('/')}/ca"
    params = {"raw": "0"}
    for k in ("from", "to", "types"):        # 선택 오버라이드 (CA_FROM=YYYYMMDD 등)
        v = os.environ.get(f"CA_{k.upper()}")
        if v:
            params[k] = v
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT,
                             headers={"User-Agent": "backtest-stock-ca-calendar"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            _warn(f"허브 /ca 호출 실패({attempt + 1}/{RETRIES + 1}): {e}")
            if attempt < RETRIES:
                time.sleep(RETRY_WAIT_S)
    raise RuntimeError(f"허브 /ca 호출 실패: {last}")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    now_kst = datetime.datetime.now(KST)
    prev = _load_prev()
    if "--force" not in argv and _fresh(prev, now_kst, MIN_INTERVAL_MIN):
        _warn(f"직전 파일 {prev.get('asof')} 가 {MIN_INTERVAL_MIN}분 이내 — 호출 생략")
        return 0
    try:
        resp = fetch_hub()
    except Exception as e:
        _warn(f"{e} — 기존 ca_calendar.json 보존")
        return 0
    payload = build(resp, prev, now_kst, load_names(), load_listed())
    if payload is None:
        return 0
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    n_hi = sum(1 for r in payload["rows"] if r["importance"] >= 3)
    _warn(f"저장: 이벤트 {len(resp.get('events') or [])}건 → 행 {len(payload['rows'])}건 "
          f"(★3 {n_hi}건, {payload['from']}~{payload['to']}, 오류 {len(payload['errors'])}타입) "
          f"-> {os.path.relpath(OUT_PATH, ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
