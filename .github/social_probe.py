"""해외 peer 소셜 여론 소스(Reddit RSS · StockTwits) Actions IP 접근성 프로브.

딥리서치 peers.reddit 이 Tavily 월 한도 소진(432)+reddit search.json 403(데이터센터 IP)
으로 사실상 항상 비어 있어(2026-09 기준 9/2~9/4 사흘만 채워짐), TradingAgents 의
RSS-first Reddit 수집기와 StockTwits 공개 스트림을 이식하려 한다(P0, 2026-09-18).
둘 다 로컬(거주지 IP)에선 동작을 확인했지만 Actions IP 에서의 상태코드·속도 제한(429)은
미확인이라 여기서 실측한다. 결과는 .github/social_probe_result.json.

측정 항목:
  reddit_rss[*]   : /r/{sub}/search.rss 를 티커 2개 x 서브레딧 3개 순차 호출(1.5s 간격)
                    — status, entries(글 수), 첫 429 발생 시점, Retry-After
  reddit_site_rss : 사이트 전체 /search.rss 1회 (서브레딧 제한 없는 검색)
  reddit_json     : /r/stocks/search.json 1회 (403 예상 — 기록용)
  stocktwits[*]   : /api/2/streams/symbol/{T}.json 티커 3개 — status, messages, 라벨 집계
  stocktwits_browser_ua : 브라우저 UA 로 1회 (로컬은 Cloudflare 403 — Actions 도 같은지)
"""
import json
import time
import datetime
import xml.etree.ElementTree as ET

import requests

# TradingAgents 와 같은 '식별용' UA — Reddit 은 익명 UA(curl/Mozilla 단독)를 막고
# 이런 형태는 RSS 에서 서빙한다. StockTwits 도 이 UA 로 200 (브라우저 UA 는 CF 403).
UA_ID = {"User-Agent": "quant-antigravity/0.1 (+https://github.com/jykim0048/backtest_stock)"}
UA_BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120 Safari/537.36"}
ATOM = {"atom": "http://www.w3.org/2005/Atom"}

TICKERS = ("LLY", "NVDA")
SUBS = ("stocks", "investing", "wallstreetbets")
GAP_S = 1.5


def _rss(url, timeout=20):
    t = time.perf_counter()
    r = requests.get(url, headers=UA_ID, timeout=timeout)
    out = {"status": r.status_code, "ms": int((time.perf_counter() - t) * 1000),
           "size": len(r.content), "entries": 0, "retryAfter": r.headers.get("Retry-After")}
    if r.ok:
        try:
            root = ET.fromstring(r.content)
            out["entries"] = len(root.findall("atom:entry", ATOM))
            out["titles"] = [
                (e.find("atom:title", ATOM).text or "")[:60]
                for e in root.findall("atom:entry", ATOM)[:2]]
        except ET.ParseError as e:
            out["parseError"] = str(e)[:80]
    return out


def main():
    res = {"asof": datetime.datetime.now(datetime.timezone.utc).isoformat(),
           "ua": UA_ID["User-Agent"]}

    # ① Reddit RSS — 서브레딧별 검색, 순차 + 간격(TradingAgents 기본 페이싱)
    rss = {}
    first_429 = None
    n = 0
    for tk in TICKERS:
        for sub in SUBS:
            key = f"{tk}@{sub}"
            try:
                rss[key] = _rss(f"https://www.reddit.com/r/{sub}/search.rss"
                                f"?q={tk}&restrict_sr=on&sort=new&t=month&limit=5")
                if rss[key]["status"] == 429 and first_429 is None:
                    first_429 = {"call": n + 1, "key": key, "retryAfter": rss[key]["retryAfter"]}
            except Exception as e:
                rss[key] = {"error": str(e)[:120]}
            n += 1
            time.sleep(GAP_S)
    res["reddit_rss"] = rss
    res["reddit_first_429"] = first_429
    ok_calls = [v for v in rss.values() if v.get("status") == 200]
    res["redditRssOk"] = bool(ok_calls)
    res["redditRssEntriesTotal"] = sum(v.get("entries", 0) for v in ok_calls)

    # ② 사이트 전체 RSS 검색(서브레딧 무관) — 로컬은 429 빈발
    time.sleep(GAP_S)
    try:
        res["reddit_site_rss"] = _rss("https://www.reddit.com/search.rss?q=Eli+Lilly+stock&sort=new&t=month&limit=5")
    except Exception as e:
        res["reddit_site_rss"] = {"error": str(e)[:120]}

    # ③ search.json — 기록용(403 예상)
    time.sleep(GAP_S)
    try:
        r = requests.get("https://www.reddit.com/r/stocks/search.json?q=LLY&restrict_sr=on&sort=new&t=month&limit=5",
                         headers={**UA_ID, "Accept": "application/json"}, timeout=20)
        res["reddit_json"] = {"status": r.status_code, "size": len(r.content)}
    except Exception as e:
        res["reddit_json"] = {"error": str(e)[:120]}

    # ④ StockTwits 공개 심볼 스트림 — 식별 UA
    st = {}
    for tk in ("LLY", "NVDA", "INCY"):
        try:
            t = time.perf_counter()
            r = requests.get(f"https://api.stocktwits.com/api/2/streams/symbol/{tk}.json",
                             headers={**UA_ID, "Accept": "application/json"}, timeout=20)
            row = {"status": r.status_code, "ms": int((time.perf_counter() - t) * 1000),
                   "size": len(r.content), "messages": 0}
            if r.ok:
                msgs = (r.json() or {}).get("messages") or []
                tally = {"Bullish": 0, "Bearish": 0, "none": 0}
                for m in msgs:
                    s = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
                    tally[s if s in tally else "none"] += 1
                row.update(messages=len(msgs), labels=tally,
                           newest=(msgs[0].get("created_at") if msgs else None),
                           oldest=(msgs[-1].get("created_at") if msgs else None))
            else:
                row["cloudflare"] = "Just a moment" in r.text
            st[tk] = row
        except Exception as e:
            st[tk] = {"error": str(e)[:120]}
        time.sleep(1.0)
    res["stocktwits"] = st
    res["stocktwitsOk"] = any(v.get("status") == 200 and v.get("messages", 0) > 0 for v in st.values())

    # ⑤ StockTwits 브라우저 UA — 로컬은 Cloudflare 챌린지 403
    try:
        r = requests.get("https://api.stocktwits.com/api/2/streams/symbol/LLY.json",
                         headers=UA_BROWSER, timeout=20)
        res["stocktwits_browser_ua"] = {"status": r.status_code, "cloudflare": "Just a moment" in r.text}
    except Exception as e:
        res["stocktwits_browser_ua"] = {"error": str(e)[:120]}

    with open(".github/social_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: res[k] for k in ("redditRssOk", "redditRssEntriesTotal", "reddit_first_429",
                                          "reddit_json", "stocktwitsOk")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
