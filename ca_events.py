"""예탁원 CA 캘린더(public/ca_calendar.json) 공용 발췌 — 모닝브리핑·주간 브리핑용 (2026-11-05).

fetch_ca_calendar.py 가 만든 행(마일스톤 단위: date·end·kind·milestoneLabel·type·typeLabel·code·name·
importance·fields)을 브리핑 관점으로 잘라낸다. 무거운 의존성 없음(krx_calendar 만) — 생성기·테스트·
로컬 미리보기에서 그대로 import 한다. 장중 시황 발췌는 generate_intraday_briefing._load_ca_events.

  attach_picks(picks, cal, today)  워치리스트 종목별 CA(오늘~5영업일, ★2+) → pick["ca"] (결정적, LLM 미경유)
  ca_today(cal, today)             오늘(기간 포함) + 다음 영업일 ★3 일정, 악재성 → 물량% 큰 순 상한 12
  llm_view(ca_today_obj)           LLM 입력 축약
  day_pick(rows) / rows_on(cal, d) 주간 브리핑 일별 CA (장중 아카이브 caEvents.today 또는 캘린더 폴백)
  deposit_week(cal, d)             d 가 속한 주에 해제되는 보호예수(장중 시황 카드 — 보호예수는 주 단위만 표기)
  weekly_top(days)                 기간 상위 CA(★3, 악재성 → 물량% 큰 순, 중복 제거)

악재성(adverse) = 유상 권리락·신주상장, 추가상장 중 유상/CB/BW/EB/전환/신주인수권/스톡옵션 사유,
보호예수 해제, 무상감자, 매매정지. 화면은 ★3 + 악재성이면 강조색.
"""
import datetime
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))
CAL_PATH = os.path.join(ROOT, "public", "ca_calendar.json")

HORIZON_BD = 5          # 워치리스트 CA 창: 오늘 ~ 향후 5영업일
PICK_MAX = 4            # 종목당 표시 상한
TODAY_MAX = 12          # 오늘의 CA 상한
DAY_TOP = 8             # 주간 일별 상한
KEY_FIELDS = ("reason", "newSharesPct", "newShares", "issuePrice", "discountPct", "pctOfTotal",
              "shares", "releaseDate", "cashDps", "cashYieldPct", "faceBefore", "faceAfter",
              "direction", "reduceType", "reduceRate", "buyReqPrice", "offerPrice")
_DILUTIVE = re.compile(r"유상|CB|BW|EB|전환|신주인수권|교환|STOCK\s*OPTION|스톡옵션", re.I)
_LISTING_LABEL = {"paidin": "신주상장", "listing": "추가상장", "bonus": "신주상장"}


def load_calendar(path=None):
    """ca_calendar.json → dict. 없음·깨짐이면 {} (섹션 숨김)."""
    try:
        with open(path or CAL_PATH, encoding="utf-8") as f:
            d = json.load(f) or {}
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _holidays(hol):
    if hol is not None:
        return hol
    try:
        import krx_calendar
        return krx_calendar.load_holidays()
    except Exception:
        return set()


def _is_td(d, hol):
    return d.weekday() < 5 and d.isoformat() not in hol


def next_trading_days(d, n, hol=None):
    """d(date) 이후 거래일 n 개."""
    hol = _holidays(hol)
    out, x = [], d
    for _ in range(n * 3 + 15):
        if len(out) >= n:
            break
        x += datetime.timedelta(days=1)
        if _is_td(x, hol):
            out.append(x)
    return out


def bd_diff(today, date_str, hol=None):
    """today(date) → date_str 영업일 차(미래 +, 과거 −, 같은 날 0)."""
    hol = _holidays(hol)
    try:
        t = datetime.date.fromisoformat(str(date_str)[:10])
    except ValueError:
        return None
    lo, hi, sign = (today, t, 1) if t >= today else (t - datetime.timedelta(days=1), today - datetime.timedelta(days=1), -1)
    n, x = 0, lo
    while x < hi:                      # 미래: (today, t] 거래일 수 / 과거: [t, today) 거래일 수
        x += datetime.timedelta(days=1)
        if _is_td(x, hol):
            n += 1
    return sign * n


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except ValueError:
        return None


def size_pct(row):
    f = row.get("fields") or {}
    v = _num(f.get("newSharesPct"))
    return v if v is not None else _num(f.get("pctOfTotal"))


def covers(row, day):
    """행이 day('YYYY-MM-DD')에 해당 — 당일 또는 기간(date < day <= end)."""
    d = row.get("date") or ""
    return d == day or (d < day <= (row.get("end") or ""))


def is_adverse(row):
    typ, kind = row.get("type"), row.get("kind")
    f = row.get("fields") or {}
    if kind == "halt" or "매매정지" in str(row.get("milestoneLabel") or ""):
        return True
    if typ == "paidin":
        return kind in ("ex", "listing")
    if typ == "listing" and kind == "listing":
        return bool(_DILUTIVE.search(str(f.get("reason") or "")))
    if typ == "deposit":
        return kind == "release" or "해제" in str(row.get("milestoneLabel") or "")
    if typ == "capdcrs":
        return "무상" in str(f.get("reduceType") or f.get("reason") or "")
    if typ == "forfeit":
        return kind == "listing"
    return False


def _fmt_pct(v):
    if v is None:
        return ""
    return f"{v:.2f}".rstrip("0").rstrip(".") + "%"


def badge_text(row, dn):
    """워치리스트 배지 문구 — 예: '오늘 신주상장 9.19%(유상증자)', 'D+2 보호예수 해제 1.9%'."""
    when = "오늘" if dn == 0 else (f"D+{dn}" if dn and dn > 0 else "진행 중")
    typ, kind = row.get("type"), row.get("kind")
    f = row.get("fields") or {}
    label = row.get("milestoneLabel") or row.get("typeLabel") or ""
    if kind == "listing" and typ in _LISTING_LABEL:
        label = "신주상장" if (typ == "paidin" or "유상" in str(f.get("reason") or "")) \
            else _LISTING_LABEL[typ]
    size = size_pct(row)
    txt = f"{when} {label}" + (f" {_fmt_pct(size)}" if size is not None else "")
    if typ == "listing" and f.get("reason"):
        txt += f"({f['reason']})"
    elif typ == "paidin":
        txt += "(유상증자)"
    elif typ == "capdcrs" and f.get("reduceType"):
        txt += f"({f['reduceType']})"
    elif typ in ("merger", "revsplit") and kind == "halt":
        txt += f"({row.get('typeLabel') or typ})"
    return txt


def slim(row, today=None, hol=None):
    """표시·직렬화용 행 — 키 필드만. today(date) 주면 dn·badge 포함."""
    f = row.get("fields") or {}
    out = {k: row.get(k) for k in ("date", "end", "kind", "milestoneLabel", "type", "typeLabel",
                                   "code", "name", "importance") if row.get(k) is not None}
    out["fields"] = {k: f[k] for k in KEY_FIELDS if f.get(k) not in (None, "", "-")}
    sp = size_pct(row)
    if sp is not None:
        out["sizePct"] = sp
    out["adverse"] = is_adverse(row)
    if today is not None:
        d = row.get("date") or ""
        dn = 0 if covers(row, today.isoformat()) else bd_diff(today, d, hol)
        out["dn"] = dn
        out["badge"] = badge_text(row, dn)
    return out


def normalize_deposit(row):
    """보호예수(deposit) 행을 '보호예수 해제'(release) 1행으로 맞춘다 — 그 밖 행은 그대로.
    허브 분리 전 구형 파일은 '예치일~반환일' 기간 1행(kind=deposit, end=반환일, 수년짜리 기간이 매일
    '오늘' 행으로 잡히던 원인) → 반환일을 해제일로. 보호예수 시작(kind=deposit, end 없음)은 None(버림)."""
    if row.get("type") != "deposit":
        return row
    out = {**row, "typeLabel": "보호예수"}
    if row.get("kind") == "release":
        out["milestoneLabel"] = "보호예수 해제"
        return out
    end = row.get("end")
    if row.get("kind") == "deposit" and end:
        out.pop("end", None)
        f = dict(row.get("fields") or {})
        f.setdefault("releaseDate", end)
        out.update({"date": end, "kind": "release", "milestoneLabel": "보호예수 해제", "fields": f})
        return out
    return None


def week_range(day_str):
    """day 가 속한 주(월~일) → ('YYYY-MM-DD' 월요일, 일요일)."""
    d = datetime.date.fromisoformat(str(day_str)[:10])
    mon = d - datetime.timedelta(days=d.weekday())
    return mon.isoformat(), (mon + datetime.timedelta(days=6)).isoformat()


def in_week(row, day_str):
    """보호예수는 '이번 주에 해제되는 종목'만 표기(2026-11-05 사용자 결정) — 해제일이 day 와 같은 주면 True.
    다른 유형은 항상 True."""
    if row.get("type") != "deposit":
        return True
    lo, hi = week_range(day_str)
    return lo <= (row.get("date") or "") <= hi


def deposit_week(cal, day_str, top=20):
    """day 가 속한 주에 보호예수가 해제되는 행 — 날짜 → 물량% 큰 순. 반환 (상위 top, 전체 건수)."""
    rows = [r for r in _rows(cal) if r.get("type") == "deposit" and in_week(r, day_str)]
    rows.sort(key=lambda r: (r.get("date") or "", -(size_pct(r) if size_pct(r) is not None else -1),
                             r.get("code") or ""))
    return rows[:top], len(rows)


def _noise(row):
    """보호예수는 해제(release)만 매도 물량 일정 — normalize_deposit 뒤에도 해제가 아닌 행은 제외.
    기술적 상장(액면병합·분할·상호변경·재상장 — 물량 100%여도 희석이 아님)도 브리핑 발췌에서 뺀다(2026-11-05)."""
    if row.get("type") == "deposit" and row.get("kind") != "release":
        return True
    rs = str(((row.get("fields") or {}).get("reason")) or "")
    return row.get("type") == "listing" and any(w in rs for w in ("액면", "상호변경", "재상장", "통일교체"))


def _rows(cal):
    out = []
    for r in (cal or {}).get("rows") or []:
        if not isinstance(r, dict) or not r.get("date"):
            continue
        r = normalize_deposit(r)
        if r is not None and not _noise(r):
            out.append(r)
    return out


def attach_picks(picks, cal, today_str, hol=None, horizon_bd=HORIZON_BD, min_imp=2, per_pick=PICK_MAX):
    """picks 각 종목에 코드 일치 CA(오늘~향후 horizon_bd 영업일, 중요도 min_imp+) → pick["ca"].
    정렬 = 날짜 → 중요도 내림차순. 붙인 종목 수를 반환. 캘린더 없으면 0(picks 불변)."""
    rows = _rows(cal)
    if not rows or not picks:
        return 0
    hol = _holidays(hol)
    today = datetime.date.fromisoformat(today_str)
    days = next_trading_days(today, horizon_bd, hol)
    end = days[-1].isoformat() if days else today_str
    by_code = {}
    for r in rows:
        if (r.get("importance") or 0) < min_imp or not in_week(r, today_str):
            continue
        if covers(r, today_str) or today_str < r["date"] <= end:
            by_code.setdefault(str(r.get("code") or ""), []).append(r)
    n = 0
    for p in picks:
        if not isinstance(p, dict):
            continue
        hit = by_code.get(str(p.get("code") or "").zfill(6))
        if not hit:
            continue
        hit = sorted(hit, key=lambda r: (r["date"], -(r.get("importance") or 0)))[:per_pick]
        p["ca"] = [slim(r, today, hol) for r in hit]
        n += 1
    return n


def _rank_key(r):
    sp = size_pct(r)
    return (-(r.get("importance") or 0), not is_adverse(r), -(sp if sp is not None else -1),
            r.get("code") or "")


def ca_today(cal, today_str, hol=None, max_n=TODAY_MAX):
    """오늘의 CA — 오늘(기간 포함) + 다음 영업일의 중요도 3 일정. 물량% 큰 순 상한 max_n.
    반환 {"date","nextDate","today":[...],"next":[...],"total","asof"} | {} (없음)."""
    rows = _rows(cal)
    if not rows:
        return {}
    hol = _holidays(hol)
    today = datetime.date.fromisoformat(today_str)
    nd = next_trading_days(today, 1, hol)
    nxt = nd[0].isoformat() if nd else None
    cand = []
    for r in rows:
        if (r.get("importance") or 0) < 3 or not in_week(r, today_str):
            continue
        if covers(r, today_str):
            cand.append(("today", r))
        elif nxt and r["date"] == nxt:
            cand.append(("next", r))
    if not cand:
        return {}

    def k(c):
        sp = size_pct(c[1])
        # 악재성(희석·해제·감자·정지) 먼저, 그 안에서 물량% 큰 순 — 액면병합·무상 100% 같은 기술적 상장이
        # 희석성 신주상장을 밀어내지 않게
        return (not is_adverse(c[1]), -(sp if sp is not None else -1), c[0] != "today",
                c[1].get("code") or "")
    cand.sort(key=k)
    picked = cand[:max_n]
    return {"date": today_str, "nextDate": nxt, "asof": cal.get("asof"), "total": len(cand),
            "today": [slim(r, today, hol) for w, r in picked if w == "today"],
            "next": [slim(r, today, hol) for w, r in picked if w == "next"]}


def llm_view(cat):
    """LLM 입력용 — 종목·일정·핵심 수치만(수치 재생성 유인 제거)."""
    out = []
    for when in ("today", "next"):
        for r in (cat or {}).get(when) or []:
            x = {"when": "오늘" if when == "today" else "다음 영업일", "date": r.get("date"),
                 "stock": r.get("name") or r.get("code"), "code": r.get("code"),
                 "event": r.get("typeLabel"), "milestone": r.get("milestoneLabel")}
            if r.get("end"):
                x["end"] = r["end"]
            if r.get("sizePct") is not None:
                x["sizePct"] = r["sizePct"]
            f = {k: v for k, v in (r.get("fields") or {}).items()
                 if k in ("reason", "issuePrice", "discountPct", "releaseDate", "reduceType")}
            if f:
                x["fields"] = f
            out.append(x)
    return out


def rows_on(cal, day):
    """캘린더 폴백 — day 에 해당하는 행(기간 포함)."""
    return [r for r in _rows(cal) if covers(r, day)]


def day_pick(rows, top=DAY_TOP, min_imp=2):
    """일별 CA — 중요도 min_imp+ 를 중요도 → 물량% 순으로 상한 top. 슬림 행."""
    # 그날 행 목록이라 구형 보호예수 기간 행(반환일이 그날이 아님)은 _noise 로 버리고, 해제 행만 라벨 통일
    rows = [normalize_deposit(r) for r in rows or [] if isinstance(r, dict) and not _noise(r)]
    rows = [r for r in rows if r and (r.get("importance") or 0) >= min_imp]
    rows.sort(key=_rank_key)
    return [slim(r) for r in rows[:top]]


def weekly_top(days, top=10, min_imp=3):
    """기간 상위 CA — days[].ca 를 모아 중요도 min_imp+, 물량% 큰 순, (종목·유형·마일스톤·날짜) 중복 제거."""
    seen, out = set(), []
    for d in days or []:
        for r in d.get("ca") or []:
            if (r.get("importance") or 0) < min_imp:
                continue
            key = (r.get("code"), r.get("type"), r.get("kind"), r.get("milestoneLabel"), r.get("date"))
            if key in seen:
                continue
            seen.add(key)
            out.append({**r, "day": d.get("date")})
    out.sort(key=lambda r: (not r.get("adverse"),
                            -(r.get("sizePct") if r.get("sizePct") is not None else -1),
                            -(r.get("importance") or 0), r.get("day") or ""))
    return out[:top]


def weekly_llm_view(top_rows):
    return [{"date": r.get("date"), "stock": r.get("name") or r.get("code"),
             "event": r.get("typeLabel"), "milestone": r.get("milestoneLabel"),
             **({"sizePct": r["sizePct"]} if r.get("sizePct") is not None else {}),
             **({"reason": (r.get("fields") or {}).get("reason")}
                if (r.get("fields") or {}).get("reason") else {})}
            for r in top_rows or []]
