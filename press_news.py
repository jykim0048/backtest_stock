#!/usr/bin/env python3
"""한경·매경 종목 뉴스 수집 → Railway Postgres (press_news / press_news_stock).

대상: 코스피200 + 코스닥150 구성종목(data/index_constituents.json)에 관련된
한국경제(hankyung.com)·매일경제(mk.co.kr) 기사. 본문은 저장하지 않는다 —
제목·요약·URL·발행시각만(저작권).

수집 경로 3갈래 (PLAN_PRESS_NEWS.md):
  rss       한경 finance/economy, 매경 증권/헤드라인 RSS. 피드당 최신 50건이라
            30분 주기로 돌며 URL dedup 으로 누적 (railway_server 스레드).
  daily     네이버 검색 "{종목명} 한경" / "{종목명} 매경" 1페이지(100건) — RSS 섹션 밖
            기사·RSS 다운 구간 보충. ≈ 350종목 × 2 = 700콜/일.
  backfill  같은 쿼리를 start=1..901 끝까지(네이버 상한 1000건). press_news_backfill
            체크포인트로 이어달리기 — 일 한도(25,000콜, 다른 파이프라인과 공유) 고려해
            --max-calls 로 나눠 돌린다.

종목 매칭: 제목+요약에서 유니버스 종목명 탐색(긴 이름 우선, 매칭 구간 마스킹) +
analysis/news_aliases.json 의 별칭/제외어. 네이버 쿼리로 들어왔는데 제목·요약에
이름이 없으면 matched_in='query'(본문에만 등장) — stock_news VIEW 에서 제외.

CLI (DB 가 내부망 전용이라 컨테이너에서: railway ssh --service backtest_stock_vercel):
  python press_news.py rss
  python press_news.py daily [--max-calls N]
  python press_news.py backfill [--max-calls N] [--codes 005930,000660]
  python press_news.py stats
  python press_news.py match "기사 제목"      # 매칭 디버그 (DB 불필요)

Env: DATABASE_URL, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
"""
import os
import re
import sys
import json
import html
import time
import datetime
import threading
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_PATH = os.path.join(ROOT, "data", "index_constituents.json")
KRX_MASTER = os.path.join(ROOT, "public", "assets", "krx_companies.json")
ALIASES_PATH = os.path.join(ROOT, "analysis", "news_aliases.json")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
KST = datetime.timezone(datetime.timedelta(hours=9))

# 사이트별 UA 고정 (2026-10-19 Railway IP 실측): 한경은 크롬 UA 에 Cloudflare 403,
# 매경은 단순 UA 에 403. 403 이면 그 회차 스킵 — 우회 시도는 하지 않는다.
UA_SIMPLE = "Mozilla/5.0"
UA_BROWSER = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

FEEDS = [  # (press, channel, url, ua)
    ("hankyung", "rss:hk-finance", "https://www.hankyung.com/feed/finance", UA_SIMPLE),
    ("hankyung", "rss:hk-economy", "https://www.hankyung.com/feed/economy", UA_SIMPLE),
    ("mk", "rss:mk-stock", "https://www.mk.co.kr/rss/50200011/", UA_BROWSER),
    ("mk", "rss:mk-headline", "https://www.mk.co.kr/rss/30000001/", UA_BROWSER),
]
PRESS_DOMAINS = {"hankyung": "hankyung.com", "mk": "mk.co.kr"}
# 네이버 검색어 접미사 — '한국경제'/'매일경제' 보다 약칭이 정확도·깊이 모두 높다(실측 77~97%)
PRESS_QUERY = {"hankyung": "한경", "mk": "매경"}

NAVER_URL = "https://openapi.naver.com/v1/search/news.json"
NAVER_PAGE = 100
NAVER_MAX_START = 901          # start 상한 1000 → 100건 페이지 10개(1,101,...,901)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS press_news (
  url          text PRIMARY KEY,
  press        text        NOT NULL,
  title        text        NOT NULL,
  description  text,
  published_at timestamptz NOT NULL,
  channel      text        NOT NULL,
  collected_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS press_news_stock (
  url        text NOT NULL REFERENCES press_news(url) ON DELETE CASCADE,
  code       text NOT NULL,
  name       text,
  matched_in text NOT NULL,
  PRIMARY KEY (url, code)
);
CREATE INDEX IF NOT EXISTS press_news_stock_code ON press_news_stock(code);
CREATE INDEX IF NOT EXISTS press_news_published ON press_news(published_at);
CREATE TABLE IF NOT EXISTS press_news_state (       -- 서버 스레드 진행 상태(재배포 생존)
  key        text PRIMARY KEY,
  value      text NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS press_news_backfill (
  code       text NOT NULL,
  press      text NOT NULL,
  next_start int  NOT NULL DEFAULT 1,
  done       bool NOT NULL DEFAULT false,
  fetched    int  NOT NULL DEFAULT 0,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (code, press)
);
"""

# railway_server /healthz 노출용 마지막 실행 상태
STATE = {"rss": None, "daily": None, "backfill": None}


def _log(msg):
    print(f"[press_news] {msg}", flush=True)


# ----------------------------------------------------------------------------
# 유니버스 / 매칭
# ----------------------------------------------------------------------------
def load_universe():
    """code -> name (코스피200 ∪ 코스닥150). 이름은 index_constituents.json 의 names
    (네이버 현재명) 우선, 없으면 krx_companies.json."""
    with open(UNIVERSE_PATH, encoding="utf-8") as f:
        uni = json.load(f)
    codes = set(uni.get("KOSPI200", [])) | set(uni.get("KOSDAQ150", []))
    names = dict(uni.get("names") or {})
    missing = [c for c in codes if c not in names]
    if missing:
        try:
            with open(KRX_MASTER, encoding="utf-8") as f:
                master = {x["code"]: x["name"] for x in json.load(f)}
            for c in missing:
                if c in master:
                    names[c] = master[c]
        except Exception as e:
            _log(f"krx_companies 로드 실패: {e}")
    return {c: names[c] for c in sorted(codes) if names.get(c)}


def load_aliases():
    try:
        with open(ALIASES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def query_names(universe, aliases):
    """네이버 검색어용 code -> 이름. exclude_names(그룹명·일반명사)는 검색어 자체가 노이즈라
    (시범: '대상 한경'·'LG 한경' 2,000콜 전부 무관 기사) query_names 에 대체어가 있을 때만 검색."""
    exclude = set(aliases.get("exclude_names") or [])
    override = aliases.get("query_names") or {}
    out = {}
    for code, name in universe.items():
        q = override.get(code) or (None if name in exclude else name)
        if q:
            out[code] = q
    return out


_HANGUL_ALNUM = re.compile(r"[가-힣A-Za-z0-9]")


class Matcher:
    """제목·요약 → {code: matched_in}. 긴 이름부터 찾고 매칭 구간을 마스킹해
    '카카오뱅크' 안의 '카카오', 'LG에너지솔루션' 안의 'LG' 같은 부분 일치를 막는다."""

    def __init__(self, universe, aliases=None):
        aliases = aliases or {}
        exclude = set(aliases.get("exclude_names") or [])
        self.strict = set(aliases.get("strict_names") or [])
        terms = {}
        for code, name in universe.items():
            if name not in exclude:
                terms[name] = (code,)
        for alias, codes in (aliases.get("aliases") or {}).items():
            codes = tuple(c for c in ([codes] if isinstance(codes, str) else codes)
                          if c in universe)          # 값이 리스트면 묶음 별칭(삼전닉스)
            if codes:
                terms[alias] = codes
        self.terms = sorted(terms.items(), key=lambda kv: -len(kv[0]))

    def _ok_boundary(self, text, start, end, term):
        # 왼쪽은 항상 경계 필요('기아' ← '포기아'). 오른쪽은 한글 조사가 붙으므로
        # 영숫자로 끝나는 이름만 검사('SK' ← 'SKC'). strict 이름은 양쪽 모두 한글 불가.
        if start > 0 and _HANGUL_ALNUM.match(text[start - 1]):
            return False
        if end < len(text):
            nxt = text[end]
            if re.match(r"[A-Za-z0-9]", term[-1]) and re.match(r"[A-Za-z0-9]", nxt):
                return False
            if term in self.strict and _HANGUL_ALNUM.match(nxt) \
               and not re.match(r"[은는이가을를의와과도로에만]", nxt):
                return False
        return True

    def find(self, text):
        """text 안의 종목 코드 집합."""
        if not text:
            return set()
        buf = list(text)
        found = set()
        for term, codes in self.terms:
            s = "".join(buf)
            i = s.find(term)
            while i >= 0:
                j = i + len(term)
                if self._ok_boundary(s, i, j, term):
                    found.update(codes)
                    for k in range(i, j):        # 마스킹 — 짧은 이름이 다시 못 잡게
                        buf[k] = "\0"
                    s = "".join(buf)
                i = s.find(term, j)
        return found

    def match(self, title, description):
        out = {}
        for code in self.find(description):
            out[code] = "description"
        for code in self.find(title):
            out[code] = "title"
        return out


# ----------------------------------------------------------------------------
# 정규화 유틸
# ----------------------------------------------------------------------------
_DROP_QS = re.compile(r"^(utm_|ref|from|division|input|sns|svc|type)", re.I)


def normalize_url(url):
    """dedup 키: https + 소문자 호스트, m./모바일 호스트 → www, 추적 파라미터 제거."""
    url = (url or "").strip()
    if not url:
        return ""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    if host in ("m.hankyung.com", "hankyung.com"):
        host = "www.hankyung.com"
    elif host in ("m.mk.co.kr", "mk.co.kr"):
        host = "www.mk.co.kr"
    qs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
          if not _DROP_QS.match(k)]
    path = p.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(qs)), ""))


def press_of(url):
    host = (urlsplit(url).hostname or "").lower()
    for press, dom in PRESS_DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return press
    return None


def _clean(s):
    s = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", s or "", flags=re.S)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    s = re.sub(r"([+-]\d{2}):(\d{2})$", r"\1\2", s)    # 매경 '+09:00' → '+0900'
    try:
        d = parsedate_to_datetime(s)
        return d if d.tzinfo else d.replace(tzinfo=KST)
    except Exception:
        return None


# ----------------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------------
def _connect():
    import psycopg2
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 미설정")
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def init_schema(conn=None):
    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
        conn.commit()
    finally:
        if own:
            conn.close()


def upsert(conn, articles, names=None):
    """articles: [{url, press, title, description, published_at, channel, stocks:{code: matched_in}}]
    → (신규 기사 수, 신규 종목링크 수). 기존 기사는 내용 유지, 종목링크는 더 강한 근거로 갱신
    (title > description > query)."""
    from psycopg2.extras import execute_values
    arts = [a for a in articles if a.get("stocks")]
    if not arts:
        return 0, 0
    by_url = {}
    for a in arts:                                       # 같은 배치 안 중복 병합
        cur = by_url.get(a["url"])
        if cur is None:
            by_url[a["url"]] = dict(a, stocks=dict(a["stocks"]))
        else:
            for c, m in a["stocks"].items():
                cur["stocks"][c] = _stronger(cur["stocks"].get(c), m)
    rows = [(a["url"], a["press"], a["title"], a.get("description") or None,
             a["published_at"], a["channel"]) for a in by_url.values()]
    names = names or {}
    links = [(u, c, names.get(c), m) for u, a in by_url.items() for c, m in a["stocks"].items()]
    with conn.cursor() as cur:
        new_arts = execute_values(cur, """
            INSERT INTO press_news (url, press, title, description, published_at, channel)
            VALUES %s ON CONFLICT (url) DO NOTHING RETURNING url""", rows, fetch=True)
        new_links = execute_values(cur, """
            INSERT INTO press_news_stock (url, code, name, matched_in) VALUES %s
            ON CONFLICT (url, code) DO UPDATE SET matched_in = EXCLUDED.matched_in
              WHERE (CASE press_news_stock.matched_in WHEN 'title' THEN 3
                       WHEN 'description' THEN 2 ELSE 1 END)
                  < (CASE EXCLUDED.matched_in WHEN 'title' THEN 3
                       WHEN 'description' THEN 2 ELSE 1 END)
            RETURNING (xmax = 0)""", links, fetch=True)
    conn.commit()
    return len(new_arts), sum(1 for (ins,) in new_links if ins)


_RANK = {"title": 3, "description": 2, "query": 1}


def _stronger(a, b):
    return a if _RANK.get(a, 0) >= _RANK.get(b, 0) else b


# ----------------------------------------------------------------------------
# RSS
# ----------------------------------------------------------------------------
def fetch_feed(url, ua):
    r = requests.get(url, headers={"User-Agent": ua}, timeout=20)
    r.raise_for_status()
    text = r.content.decode("utf-8", "replace")
    items = []
    # 한경 피드는 XML 이 엄격하지 않아 ElementTree 가 실패 → 정규식 파싱
    for raw in re.findall(r"<item\b[^>]*>(.*?)</item>", text, re.S):
        def g(tag):
            m = re.search(rf"<{tag}\b[^>]*>(.*?)</{tag}>", raw, re.S)
            return m.group(1) if m else ""
        items.append({"title": _clean(g("title")), "link": _clean(g("link")),
                      "description": _clean(g("description")), "pubDate": _clean(g("pubDate"))})
    return items


def run_rss(conn=None):
    universe = load_universe()
    matcher = Matcher(universe, load_aliases())
    own = conn is None
    conn = conn or _connect()
    summary = {}
    try:
        arts = []
        for press, channel, url, ua in FEEDS:
            try:
                items = fetch_feed(url, ua)
            except Exception as e:
                summary[channel] = f"error: {str(e)[:80]}"
                _log(f"{channel} 실패: {e}")
                continue
            hit = 0
            for it in items:
                link = normalize_url(it["link"])
                pub = parse_date(it["pubDate"])
                if not (link and it["title"] and pub) or press_of(link) != press:
                    continue
                stocks = matcher.match(it["title"], it["description"])
                if stocks:
                    hit += 1
                    arts.append({"url": link, "press": press, "title": it["title"],
                                 "description": it["description"], "published_at": pub,
                                 "channel": channel, "stocks": stocks})
            summary[channel] = f"{len(items)}건 중 종목매칭 {hit}"
        new_a, new_l = upsert(conn, arts, universe)
        summary["new_articles"], summary["new_links"] = new_a, new_l
        _log(f"rss {summary}")
        return summary
    finally:
        if own:
            conn.close()


# ----------------------------------------------------------------------------
# 네이버
# ----------------------------------------------------------------------------
class Budget:
    def __init__(self, max_calls):
        self.max_calls, self.used = max_calls, 0

    def take(self):
        if self.max_calls is not None and self.used >= self.max_calls:
            return False
        self.used += 1
        return True


def naver_page(query, start):
    cid, secret = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if not (cid and secret):
        raise RuntimeError("NAVER_CLIENT_ID/SECRET 미설정")
    for attempt in range(3):
        r = requests.get(NAVER_URL, params={"query": query, "display": NAVER_PAGE,
                                            "start": start, "sort": "date"},
                         headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
                         timeout=15)
        if r.status_code == 429:                   # 초당 한도 — 잠깐 쉬고 재시도
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        j = r.json()
        return j.get("total", 0), j.get("items", [])
    raise RuntimeError("네이버 429 반복")


def naver_articles(items, press, code, matcher, channel):
    out = []
    for it in items:
        link = normalize_url(it.get("originallink") or it.get("link"))
        if press_of(link) != press:
            continue
        title, desc = _clean(it.get("title")), _clean(it.get("description"))
        pub = parse_date(it.get("pubDate"))
        if not (title and pub):
            continue
        stocks = matcher.match(title, desc)
        stocks.setdefault(code, "query")          # 본문에만 등장 — 쿼리 근거
        out.append({"url": link, "press": press, "title": title, "description": desc,
                    "published_at": pub, "channel": channel, "stocks": stocks})
    return out


def get_state(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM press_news_state WHERE key=%s", (key,))
        r = cur.fetchone()
    return r[0] if r else None


def set_state(conn, key, value):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO press_news_state (key, value) VALUES (%s, %s)
                       ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()""",
                    (key, str(value)))
    conn.commit()


def run_daily(max_calls=None, conn=None, resume_key=None):
    """종목·언론사별 1페이지(최신 100건) 보충. resume_key(예: 'daily:2026-10-19')를 주면
    종목 단위 커서를 press_news_state 에 남겨 재배포로 끊겨도 이어서 돈다."""
    universe = load_universe()
    aliases = load_aliases()
    matcher = Matcher(universe, aliases)
    queries = query_names(universe, aliases)
    budget = Budget(max_calls)
    own = conn is None
    conn = conn or _connect()
    tot_a = tot_l = 0
    try:
        after = get_state(conn, resume_key) if resume_key else None
        for code, name in queries.items():
            if after and code <= after:          # 이전 실행이 끝낸 종목(코드 정렬 순)
                continue
            for press, suffix in PRESS_QUERY.items():
                if not budget.take():
                    _log(f"daily 예산 소진({budget.used}콜) — 중단")
                    return _daily_done(budget, tot_a, tot_l)
                try:
                    _, items = naver_page(f"{name} {suffix}", 1)
                except Exception as e:
                    _log(f"daily {name} {suffix} 실패: {e}")
                    continue
                a, l = upsert(conn, naver_articles(items, press, code, matcher, "naver:daily"),
                              universe)
                tot_a, tot_l = tot_a + a, tot_l + l
                time.sleep(0.12)
            if resume_key:
                set_state(conn, resume_key, code)
        if resume_key:
            set_state(conn, resume_key, "~done")     # '~' > 숫자/영문 코드 → 전부 건너뜀
        return _daily_done(budget, tot_a, tot_l)
    finally:
        if own:
            conn.close()


def _daily_done(budget, a, l):
    s = {"calls": budget.used, "new_articles": a, "new_links": l}
    _log(f"daily {s}")
    return s


def run_backfill(max_calls=None, codes=None, conn=None):
    """체크포인트(press_news_backfill) 기반 전체 페이지 수집. 중단·재개 가능."""
    universe = load_universe()
    aliases = load_aliases()
    matcher = Matcher(universe, aliases)
    queries = query_names(universe, aliases)
    targets = [c for c in queries if not codes or c in codes]
    budget = Budget(max_calls)
    own = conn is None
    conn = conn or _connect()
    tot_a = tot_l = 0
    try:
        init_schema(conn)
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO press_news_backfill (code, press)
                           SELECT c, p FROM unnest(%s::text[]) c, unnest(%s::text[]) p
                           ON CONFLICT DO NOTHING""", (targets, list(PRESS_QUERY)))
            cur.execute("""SELECT code, press, next_start FROM press_news_backfill
                           WHERE NOT done AND code = ANY(%s) ORDER BY code, press""", (targets,))
            todo = cur.fetchall()
        conn.commit()
        _log(f"backfill 대상 {len(todo)}건 (종목×언론사), 예산 {max_calls or '무제한'}콜")
        for code, press, start in todo:
            query = f"{queries[code]} {PRESS_QUERY[press]}"
            while start <= NAVER_MAX_START:
                if not budget.take():
                    _log(f"backfill 예산 소진({budget.used}콜) — 다음 실행에서 이어감")
                    return _bf_done(budget, tot_a, tot_l)
                try:
                    total, items = naver_page(query, start)
                except Exception as e:
                    _log(f"backfill {query} start={start} 실패: {e} — 다음 실행에서 재시도")
                    break
                a, l = upsert(conn, naver_articles(items, press, code, matcher, "naver:backfill"),
                              universe)
                tot_a, tot_l = tot_a + a, tot_l + l
                start += NAVER_PAGE
                done = len(items) < NAVER_PAGE or start > min(total, NAVER_MAX_START)
                with conn.cursor() as cur:
                    cur.execute("""UPDATE press_news_backfill SET next_start=%s, done=%s,
                                   fetched=fetched+%s, updated_at=now()
                                   WHERE code=%s AND press=%s""",
                                (start, done, len(items), code, press))
                conn.commit()
                time.sleep(0.12)
                if done:
                    break
        return _bf_done(budget, tot_a, tot_l)
    finally:
        if own:
            conn.close()


def _bf_done(budget, a, l):
    s = {"calls": budget.used, "new_articles": a, "new_links": l}
    _log(f"backfill {s}")
    return s


def stats(conn=None):
    own = conn is None
    conn = conn or _connect()
    try:
        with conn.cursor() as cur:
            for title, q in [
                ("언론사·채널별", """SELECT press, channel, count(*), min(published_at)::date,
                                    max(published_at)::date FROM press_news GROUP BY 1,2 ORDER BY 1,2"""),
                ("매칭 근거별", "SELECT matched_in, count(*) FROM press_news_stock GROUP BY 1 ORDER BY 2 DESC"),
                ("백필 진행", """SELECT press, count(*) FILTER (WHERE done), count(*), sum(fetched)
                                FROM press_news_backfill GROUP BY 1"""),
            ]:
                cur.execute(q)
                print(f"-- {title}")
                for r in cur.fetchall():
                    print("  ", r)
    finally:
        if own:
            conn.close()


# ----------------------------------------------------------------------------
# railway_server 연동 — RSS 30분, 네이버 일일 보충 22:30 KST
# ----------------------------------------------------------------------------
RSS_INTERVAL = int(os.environ.get("PRESS_RSS_INTERVAL", "1800"))
DAILY_AT = os.environ.get("PRESS_DAILY_AT", "22:30")
DAILY_MAX_CALLS = int(os.environ.get("PRESS_DAILY_MAX_CALLS", "1000"))
# 일일 보충 뒤 이어 도는 백필 하루 예산 — 끝나면(전 종목 done) 자동으로 0콜
BACKFILL_DAILY_CALLS = int(os.environ.get("PRESS_BACKFILL_DAILY_CALLS", "3000"))
_job_lock = threading.Lock()


def loop():
    """서버 데몬 스레드. DATABASE_URL 없으면 즉시 종료."""
    if not DATABASE_URL:
        _log("DATABASE_URL 미설정 — 수집 스레드 비활성")
        return
    try:
        init_schema()
    except Exception as e:
        _log(f"스키마 준비 실패: {e} — 수집 스레드 종료")
        return
    _log(f"수집 스레드 시작 (RSS {RSS_INTERVAL}s, 네이버 보충·백필 {DAILY_AT} KST~)")
    last_rss, last_check = 0.0, 0.0
    while True:
        try:
            now = datetime.datetime.now(KST)
            if time.time() - last_rss >= RSS_INTERVAL:
                last_rss = time.time()
                STATE["rss"] = {"at": now.isoformat(timespec="seconds"), **run_rss()}
            # 22:30 이후 5분마다 확인 — 진행 상태는 DB 에 있어 재배포(≈30분 주기)로
            # 스레드가 죽어도 다음 부팅에서 남은 분량만 이어 돈다.
            if now.strftime("%H:%M") >= DAILY_AT and time.time() - last_check >= 300                and not _job_lock.locked():
                last_check = time.time()
                threading.Thread(target=_night_job, args=(now.strftime("%Y-%m-%d"),),
                                 daemon=True).start()
        except Exception as e:
            _log(f"loop error: {e}")
        time.sleep(30)


def _night_job(today):
    if not _job_lock.acquire(blocking=False):
        return
    conn = None
    try:
        conn = _connect()
        dkey = f"daily:{today}"
        if get_state(conn, dkey) != "~done":
            STATE["daily"] = {"date": today, **run_daily(max_calls=DAILY_MAX_CALLS,
                                                         conn=conn, resume_key=dkey)}
        if get_state(conn, dkey) != "~done":
            return                                 # 보충 미완 — 백필은 다음 확인 때
        bkey = f"backfill_calls:{today}"
        used = int(get_state(conn, bkey) or 0)
        if used < BACKFILL_DAILY_CALLS:
            r = run_backfill(max_calls=min(500, BACKFILL_DAILY_CALLS - used), conn=conn)
            set_state(conn, bkey, used + r["calls"])
            STATE["backfill"] = {"date": today, "callsToday": used + r["calls"], **r}
    except Exception as e:
        STATE["daily"] = {"date": today, "error": str(e)[:200]}
        _log(f"night job 실패: {e}")
    finally:
        if conn is not None:
            conn.close()
        _job_lock.release()


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="한경·매경 종목 뉴스 수집")
    ap.add_argument("cmd", choices=["rss", "daily", "backfill", "stats", "init", "match"])
    ap.add_argument("text", nargs="?")
    ap.add_argument("--max-calls", type=int)
    ap.add_argument("--codes", help="쉼표 구분 종목코드(backfill 대상 제한)")
    a = ap.parse_args(argv)
    if a.cmd == "match":
        m = Matcher(load_universe(), load_aliases())
        uni = load_universe()
        print({c: uni[c] for c in m.find(a.text or "")})
        return
    if a.cmd == "init":
        init_schema()
    elif a.cmd == "rss":
        init_schema()
        run_rss()
    elif a.cmd == "daily":
        init_schema()
        run_daily(max_calls=a.max_calls)
    elif a.cmd == "backfill":
        run_backfill(max_calls=a.max_calls,
                     codes=set(a.codes.split(",")) if a.codes else None)
    elif a.cmd == "stats":
        stats()


if __name__ == "__main__":
    main(sys.argv[1:])
