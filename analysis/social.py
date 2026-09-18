"""해외 peer 소셜 여론 RAW 수집기 — StockTwits 공개 스트림(주) + Reddit RSS 검색(보조).

TradingAgents(tradingagents/dataflows/{stocktwits,reddit}.py)의 수집 로직을 이 레포의
규약(구조화 dict 반환, 실패 시 배치 중단 없음, analysis JSON 스키마 친화)에 맞춰 이식
(2026-09-18, 딥리서치 peer 여론 P1). 프롬프트용 문자열이 아니라 리스트/dict 를 돌려
generate_analysis 가 결정적 수치(라벨 집계)는 코드로 채우고 LLM 은 요약만 쓰게 한다.

Actions IP 실측(.github/social_probe_result.json, 2026-09-18):
  - StockTwits /api/2/streams/symbol/<T>.json : 식별 UA 로 전부 200, 100~260ms.
    비인증 한도 시간당 200콜 → 회차당 peer 수십 개면 여유. 티커별 프로세스 캐시.
  - Reddit /r/<sub>/search.rss : 열리지만 신선한 IP 에서도 3번째 콜부터 429(Retry-After
    없음). search.json 은 403. → 실행당 예산제(REDDIT_BUDGET, 기본 2콜) + 첫 429 에서
    그 실행의 레딧을 전부 중단(회로차단). 레딧은 '있으면 보강', StockTwits 가 기본.

TradingAgents 에서 그대로 가져온 원칙:
  - 수집 실패(None/"unavailable")와 글 없음([]/"empty")을 구분해 돌려준다 — 실패를
    '여론 부재'로 LLM 에 넘기면 관측하지 않은 침묵을 신호로 오독한다(#1295).
  - 익명 UA(curl/Mozilla 단독)는 Reddit 이 막으므로 식별용 UA 를 쓴다.
  - StockTwits 메시지의 사용자 라벨(Bullish/Bearish/None)은 코드가 집계하고, 라벨 없는
    메시지는 '무라벨'로 세지 강세/약세 어느 쪽에도 넣지 않는다.
  - 최근 N일 창(fresh_days)으로 잘라 저유동 종목의 수개월 전 글이 섞이지 않게 한다.
"""
import os
import re
import sys
import html
import time
import threading
import datetime
import xml.etree.ElementTree as ET

import requests

UA = {"User-Agent": "quant-antigravity/0.1 (+https://github.com/jykim0048/backtest_stock)"}
KST = datetime.timezone(datetime.timedelta(hours=9))

STOCKTWITS_API = "https://api.stocktwits.com/api/2/streams/symbol/{ticker}.json"
REDDIT_RSS = "https://www.reddit.com/r/{sub}/search.rss"
_ATOM = {"atom": "http://www.w3.org/2005/Atom"}

# ── Reddit 실행당 예산 + 회로차단(프로세스 전역, 스레드 안전) ──────────────────
REDDIT_BUDGET = int(os.environ.get("REDDIT_BUDGET", "2"))     # 실행당 RSS 호출 상한
REDDIT_GAP_S = float(os.environ.get("REDDIT_GAP_S", "1.5"))   # 호출 간 최소 간격
DEFAULT_SUBREDDITS = ("stocks", "investing", "wallstreetbets")
_reddit_lock = threading.Lock()
_reddit_state = {"calls": 0, "blocked": False, "last": 0.0, "reason": ""}

# ── StockTwits 티커별 프로세스 캐시(같은 실행에서 여러 종목이 같은 peer 공유) ────
_st_cache = {}
_st_lock = threading.Lock()
STOCKTWITS_CACHE_TTL = int(os.environ.get("STOCKTWITS_CACHE_TTL", "1800"))   # 30분


def _warn(msg):
    print(f"[social] {msg}", file=sys.stderr)


def is_us_ticker(ticker):
    """StockTwits 대상 = 미국 상장 심볼(접미사 없음). 6479.T / 2382.HK / ENR.DE 등 제외."""
    t = (ticker or "").strip().upper()
    return bool(t) and "." not in t and t.replace("-", "").isalnum()


def reset_reddit_budget():
    """테스트·온디맨드 서버가 실행 단위를 새로 시작할 때 호출."""
    with _reddit_lock:
        _reddit_state.update(calls=0, blocked=False, last=0.0, reason="")


def reddit_status():
    with _reddit_lock:
        return dict(_reddit_state)


# ----------------------------------------------------------------------------
# StockTwits
# ----------------------------------------------------------------------------
def _parse_iso(s):
    try:
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def stocktwits_stream(ticker, fresh_days=7, max_messages=30, timeout=10, now=None):
    """StockTwits 공개 심볼 스트림 → 라벨 집계 + 메시지 목록(구조화 dict).

    Returns:
      {"ticker", "status": "ok"|"empty"|"unavailable"|"skipped", "error"?,
       "bullish", "bearish", "unlabeled", "total", "labeled", "bullPct"(라벨 기준, None 가능),
       "newest", "oldest", "windowDays", "messages": [{"id","createdAt","user","sentiment","body"}]}
    status 별 의미를 호출측이 구분해 표기한다(unavailable ≠ empty).
    """
    t = (ticker or "").strip().upper()
    base = {"ticker": t, "bullish": 0, "bearish": 0, "unlabeled": 0, "total": 0,
            "labeled": 0, "bullPct": None, "newest": None, "oldest": None,
            "windowDays": fresh_days, "messages": []}
    if not is_us_ticker(t):
        return {**base, "status": "skipped", "error": "non-US ticker"}

    key = (t, fresh_days, max_messages)
    with _st_lock:
        hit = _st_cache.get(key)
        if hit and time.time() - hit[0] < STOCKTWITS_CACHE_TTL:
            return hit[1]

    try:
        r = requests.get(STOCKTWITS_API.format(ticker=t),
                         headers={**UA, "Accept": "application/json"}, timeout=timeout)
        r.raise_for_status()
        data = r.json() or {}
    except Exception as e:
        _warn(f"stocktwits({t}) 실패: {e}")
        return {**base, "status": "unavailable", "error": str(e)[:120]}

    msgs = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(msgs, list):
        return {**base, "status": "unavailable", "error": "unexpected response shape"}

    now = now or datetime.datetime.now(datetime.timezone.utc)
    floor = now - datetime.timedelta(days=fresh_days) if fresh_days else None
    out_msgs = []
    bull = bear = none = 0
    for m in msgs:
        created = _parse_iso(m.get("created_at"))
        if floor is not None and (created is None or created < floor):
            continue
        s = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        if s == "Bullish":
            bull += 1
        elif s == "Bearish":
            bear += 1
        else:
            s, none = None, none + 1
        body = " ".join(str(m.get("body") or "").split())
        if len(body) > 280:
            body = body[:280] + "…"
        out_msgs.append({
            "id": m.get("id"),
            "createdAt": created.astimezone(KST).strftime("%Y-%m-%d %H:%M") if created else "",
            "user": (m.get("user") or {}).get("username") or "?",
            "sentiment": s,                      # "Bullish" | "Bearish" | None
            "body": body,
        })
        if len(out_msgs) >= max_messages:
            break

    total = bull + bear + none
    labeled = bull + bear
    res = {**base, "status": "ok" if total else "empty",
           "bullish": bull, "bearish": bear, "unlabeled": none, "total": total,
           "labeled": labeled, "bullPct": (round(100 * bull / labeled) if labeled else None),
           "newest": out_msgs[0]["createdAt"] if out_msgs else None,
           "oldest": out_msgs[-1]["createdAt"] if out_msgs else None,
           "messages": out_msgs}
    with _st_lock:
        _st_cache[key] = (time.time(), res)
    return res


def stocktwits_for_peers(peer_list, top=2, **kw):
    """peer 목록(name/ticker dict)에서 미국 상장 상위 top 개의 스트림. 대상 없으면 []."""
    out = []
    for p in peer_list or []:
        tk = (p.get("ticker") or "").strip().upper()
        if not is_us_ticker(tk):
            continue
        res = stocktwits_stream(tk, **kw)
        res["name"] = p.get("name") or tk
        out.append(res)
        if len(out) >= top:
            break
    return out


# ----------------------------------------------------------------------------
# Reddit RSS (예산제)
# ----------------------------------------------------------------------------
def _strip_html(content):
    if not content:
        return ""
    if "<!-- SC_OFF -->" in content and "<!-- SC_ON -->" in content:
        content = content.split("<!-- SC_OFF -->")[1].split("<!-- SC_ON -->")[0]
    text = re.sub(r"<[^>]+>", " ", content)
    return " ".join(html.unescape(text).split())


def _iso_epoch(s):
    d = _parse_iso(s)
    return d.timestamp() if d else None


def _reddit_take_slot():
    """예산·회로차단 검사 후 슬롯 확보(간격 대기 포함). 실패 사유 문자열 또는 None."""
    with _reddit_lock:
        if _reddit_state["blocked"]:
            return f"blocked: {_reddit_state['reason']}"
        if _reddit_state["calls"] >= REDDIT_BUDGET:
            return f"budget exhausted ({REDDIT_BUDGET} calls/run)"
        wait = REDDIT_GAP_S - (time.time() - _reddit_state["last"])
        if wait > 0:
            time.sleep(wait)
        _reddit_state["calls"] += 1
        _reddit_state["last"] = time.time()
        return None


def reddit_rss(query, sub="stocks", limit=5, period="month", timeout=10, fresh_days=None):
    """서브레딧 검색 RSS → 글 목록. 반환: list(성공, 빈 리스트 = 글 없음) | None(수집 실패/예산 소진).

    항목: {"title","url","subreddit","author","date","createdUtc","content","score":None,
           "num_comments":None,"source":"rss"} — score/댓글수는 RSS 에 없어 None(가짜 0 금지).
    """
    reason = _reddit_take_slot()
    if reason:
        _warn(f"reddit_rss({query}@{sub}) 생략 — {reason}")
        return None
    try:
        r = requests.get(REDDIT_RSS.format(sub=sub),
                         params={"q": query, "restrict_sr": "on", "sort": "new",
                                 "t": period, "limit": limit},
                         headers=UA, timeout=timeout)
        if r.status_code == 429:
            with _reddit_lock:
                _reddit_state.update(blocked=True, reason="429 rate limited")
            _warn(f"reddit_rss({query}@{sub}) 429 — 이 실행의 Reddit 수집 중단")
            return None
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        _warn(f"reddit_rss({query}@{sub}) 실패: {e}")
        return None

    floor = (time.time() - fresh_days * 86400) if fresh_days else None
    out = []
    for e in root.findall("atom:entry", _ATOM)[:limit]:
        def _t(tag):
            el = e.find(f"atom:{tag}", _ATOM)
            return (el.text or "") if el is not None else ""
        link = e.find("atom:link", _ATOM)
        author = e.find("atom:author/atom:name", _ATOM)
        cat = e.find("atom:category", _ATOM)
        created = _iso_epoch(_t("published") or _t("updated"))
        if floor is not None and created is not None and created < floor:
            continue
        out.append({
            "title": " ".join(_t("title").split()),
            "url": (link.get("href") if link is not None else "") or "",
            "subreddit": (cat.get("label") if cat is not None and cat.get("label") else f"r/{sub}"),
            "author": (author.text or "") if author is not None else "",
            "date": datetime.datetime.fromtimestamp(created, datetime.timezone.utc).strftime("%Y-%m-%d") if created else "",
            "createdUtc": created,
            "content": _strip_html(_t("content"))[:500],
            "score": None, "num_comments": None, "source": "rss",
        })
    return out


def reddit_for_peers(peer_list, subs=("stocks",), limit=5, **kw):
    """peer 상위부터 미국 티커로 RSS 검색(예산 내). 반환 {"posts": [...], "status": "ok"|"empty"|"unavailable", "queried": [...]}.

    예산(기본 2콜)이 작으므로 서브레딧은 r/stocks 하나, peer 는 예산만큼만 본다.
    한 번도 성공한 콜이 없으면 unavailable, 성공했지만 0건이면 empty.
    """
    posts, queried, ok_calls = [], [], 0
    for p in peer_list or []:
        tk = (p.get("ticker") or "").strip().upper()
        if not is_us_ticker(tk):
            continue
        for sub in subs:
            st = reddit_status()
            if st["blocked"] or st["calls"] >= REDDIT_BUDGET:
                break
            res = reddit_rss(tk, sub=sub, limit=limit, **kw)
            queried.append(f"{tk}@{sub}")
            if res is None:
                continue
            ok_calls += 1
            posts.extend(res)
    seen, uniq = set(), []
    for x in posts:
        if x["url"] and x["url"] not in seen:
            seen.add(x["url"])
            uniq.append(x)
    status = "unavailable" if ok_calls == 0 else ("ok" if uniq else "empty")
    return {"posts": uniq, "status": status, "queried": queried, "reddit": reddit_status()}
