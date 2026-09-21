"""네이버 수급(외국인·기관 순매수) API Actions IP 접근성 프로브.

Q점수 Flow 팩터(2단계)의 데이터 소스로 m.stock.naver.com 모바일 API 를
쓰려는데, 이 도메인은 파이프라인에서 아직 안 써봤다 — KRX 처럼 해외
데이터센터 IP 를 차단할 가능성을 배제해야 한다. 폴백 후보인
finance.naver.com/item/frgn.naver(HTML)도 함께 확인한다.
결과는 .github/naver_flow_probe_result.json.

2026-09-21 추가 — 장중 1분 투자자 시계열(investorDealTrendTime.naver)이 9/18 부터
전 회차 0행(장중 시황 기관 세부·기타법인 알약·수급 차트 결손). 원인이 ① 페이지
구조 변경(200 인데 행 정규식 불일치)인지 ② IP 차단·오류(요청 실패)인지 가른다.
sources.naver_investor_timeline 과 같은 요청·정규식을 쓰고, 변형(Referer 추가·
bizdate 생략)과 대체 후보(모바일 JSON)도 같이 찍는다.
"""
import json
import datetime
import re

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CODES = ("005930", "247540")   # 대형주(삼성전자, KOSPI) + 코스닥(에코프로비엠)

# sources.naver_investor_timeline 과 동일 (값이 바뀌면 여기도 맞출 것)
ROW_RE = re.compile(r'<tr[^>]*>\s*<td[^>]*>\s*(\d{1,2}:\d{2})\s*</td>(.*?)</tr>', re.S)
NUM_RE = re.compile(r'>\s*(-?[\d,]+)\s*<')
TIMELINE_URL = "https://finance.naver.com/sise/investorDealTrendTime.naver"


def _timeline_case(name, params, headers, res):
    """1분 시계열 HTML 1콜 — 상태·본문 길이·행 수·컬럼 수까지 기록."""
    try:
        r = requests.get(TIMELINE_URL, params=params, headers=headers, timeout=20)
        html = r.content.decode("euc-kr", errors="replace")
        rows = ROW_RE.findall(html)
        cols = [len(NUM_RE.findall(rest)) for _, rest in rows[:3]]
        res[name] = {
            "status": r.status_code,
            "finalUrl": r.url,                      # 리다이렉트(로그인·차단 페이지) 확인
            "size": len(html),
            "rows": len(rows),                      # 0 이면 구조 변경 or 빈 응답
            "colsPerRow": cols,                     # 파서 기대: 10(기타법인 포함)
            "firstTime": rows[0][0] if rows else None,
            "hasTable": "투자자별" in html or "매매동향" in html,
            "blockHint": any(k in html for k in ("차단", "비정상", "captcha", "Access Denied")),
            "head": html[:200].replace("\n", " "),  # 구조 변경 시 눈으로 확인용
        }
    except Exception as e:
        res[name] = {"error": str(e), "rows": 0}


# sources._THEME_ROW_RE 와 동일 — 테마 랭킹 행
THEME_ROW_RE = re.compile(
    r'col_type1"><a href="/sise/sise_group_detail\.naver\?type=theme&no=(\d+)">([^<]+)</a>'
    r'.*?col_type2">\s*<span[^>]*>\s*([+\-]?[\d.]+)%(.*?)</tr>', re.S)
MOBILE = "https://m.stock.naver.com"
# 모바일 번들에서 API 주소 문자열 추출 — 상대(/api/…, /front-api/…) + 절대(*.stock.naver.com)
API_STR_RE = re.compile(
    r'["\'`]((?:https?://[a-z.]*stock\.naver\.com)?/(?:api|front-api)/[^"\'`\s]{2,140}'
    r'|https?://(?:api|polling)[a-z.]*\.naver\.com/[^"\'`\s]{2,140})')
API_KEYWORDS = ("invest", "trend", "deal", "theme", "group", "upjong", "sise", "time",
                "trader", "flow")
NEW_WEB = "https://stock.naver.com"   # 신규 웹(2026-09 개편) — 투자자별 매매동향 시간별 화면:
TRADER_PAGE = "/market/stock/kr/trend/trader"   # 사용자 제공(2026-09-21)


def _html_case(name, url, params, res, row_re=None, needle=None):
    """구형 HTML 페이지 1콜 — 폐지(410)·구조 변경 판별."""
    try:
        r = requests.get(url, params=params, headers=UA, timeout=20)
        html = r.content.decode("euc-kr", errors="replace")
        res[name] = {"status": r.status_code, "finalUrl": r.url, "size": len(html),
                     "rows": len(row_re.findall(html)) if row_re else None,
                     "needle": (needle in html) if needle else None,
                     "head": html[:160].replace("\n", " ")}
    except Exception as e:
        res[name] = {"error": str(e)}


def _discover_mobile_apis(res, pages, base=MOBILE, tag="discover", max_js=60):
    """웹 페이지 → script 번들 → API 주소 문자열 수집(키워드 필터).

    Next.js 류 SPA 라 실제 데이터 호출 주소는 번들 JS 안에만 있다. 페이지별로 번들을
    받아 문자열을 긁고, 키워드(invest/trend/theme…) 포함 것만 남긴다. SSR 이면 HTML 에
    박힌 초기 데이터(__NEXT_DATA__ 등)에서 필드명도 확인할 수 있게 머리 일부를 남긴다."""
    seen_js, found = set(), set()
    info = {}
    for path in pages:
        try:
            r = requests.get(base + path, headers=dict(UA, Referer=base + "/"), timeout=20)
            srcs = re.findall(r'<script[^>]+src="([^"]+\.js)"', r.text)
            nd = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
            info[path] = {"status": r.status_code, "scripts": len(srcs), "size": len(r.text),
                          "nextData": nd.group(1)[:1500] if nd else None}
            for m in API_STR_RE.findall(r.text):          # 인라인 스크립트의 주소도
                if any(k in m.lower() for k in API_KEYWORDS):
                    found.add(m)
        except Exception as e:
            info[path] = {"error": str(e)}
            continue
        for s in srcs:
            u = s if s.startswith("http") else base + s
            if u in seen_js or len(seen_js) >= max_js:
                continue
            seen_js.add(u)
            try:
                js = requests.get(u, headers=UA, timeout=20).text
            except Exception:
                continue
            for m in API_STR_RE.findall(js):
                if any(k in m.lower() for k in API_KEYWORDS):
                    found.add(m)
    res[f"{tag}:pages"] = info
    res[f"{tag}:bundles"] = len(seen_js)
    res[f"{tag}:apis"] = sorted(found)[:300]


def _bundle_context(res, page, needles, base=NEW_WEB, width=400, max_js=90):
    """페이지 번들에서 needle(API 경로) 주변 문맥 추출 — 쿼리 파라미터명 확인용."""
    out = {n: [] for n in needles}
    try:
        html = requests.get(base + page, headers=dict(UA, Referer=base + "/"), timeout=20).text
        srcs = re.findall(r'<script[^>]+src="([^"]+\.js)"', html)[:max_js]
    except Exception as e:
        res["ctx:error"] = str(e)
        return
    for s in srcs:
        u = s if s.startswith("http") else base + s
        try:
            js = requests.get(u, headers=UA, timeout=20).text
        except Exception:
            continue
        for n in needles:
            for mt in re.finditer(re.escape(n), js):
                if len(out[n]) < 4:
                    a = max(0, mt.start() - width)
                    out[n].append(js[a:mt.end() + width])
    res["ctx"] = out


def _trim(o, depth=0):
    """JSON 샘플 축약 — 리스트는 앞 2개, 깊이 4, 문자열 80자."""
    if depth > 4:
        return "…"
    if isinstance(o, dict):
        return {k: _trim(v, depth + 1) for k, v in list(o.items())[:30]}
    if isinstance(o, list):
        return [_trim(v, depth + 1) for v in o[:2]] + ([f"…(+{len(o) - 2})"] if len(o) > 2 else [])
    if isinstance(o, str):
        return o[:80]
    return o


def _json_sample(name, url, params, res, referer=None):
    """응답 구조 샘플 저장 — 파서 작성용(필드명·단위 확인)."""
    try:
        h = dict(UA, Referer=referer) if referer else UA
        r = requests.get(url, params=params, headers=h, timeout=20)
        try:
            data = r.json()
        except Exception:
            data = None
        res[name] = {"status": r.status_code, "url": r.url,
                     "sample": _trim(data) if data is not None else r.text[:300]}
    except Exception as e:
        res[name] = {"error": str(e)}


def _json_case(name, url, params, res):
    """대체 후보(모바일·API JSON) — 응답 형태만 확인."""
    try:
        r = requests.get(url, params=params, headers=UA, timeout=20)
        body = r.text[:300].replace("\n", " ")
        try:
            data = r.json()
        except Exception:
            data = None
        res[name] = {"status": r.status_code, "isJson": data is not None,
                     "type": type(data).__name__ if data is not None else None,
                     "n": len(data) if isinstance(data, (list, dict)) else None,
                     "keys": (list(data)[:8] if isinstance(data, dict)
                              else (list(data[0])[:12] if isinstance(data, list) and data
                                    and isinstance(data[0], dict) else None)),
                     "head": body}
    except Exception as e:
        res[name] = {"error": str(e)}


def main():
    res = {"asof": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    # ① 모바일 JSON API — 1순위 후보
    for code in CODES:
        key = f"trendApi:{code}"
        try:
            r = requests.get(
                f"https://m.stock.naver.com/api/stock/{code}/trend",
                params={"pageSize": 25, "page": 1}, headers=UA, timeout=20)
            rows = r.json() if r.ok else []
            has = (isinstance(rows, list) and len(rows) >= 20
                   and "foreignerPureBuyQuant" in (rows[0] or {}))
            res[key] = {"status": r.status_code, "rows": len(rows) if isinstance(rows, list) else 0,
                        "hasData": has}
        except Exception as e:
            res[key] = {"error": str(e), "hasData": False}

    # ② (삭제 2026-09-21) frgn.naver HTML 폴백 점검 — 신규 웹 개편으로 데이터 표 소멸,
    #    폴백 후보에서 폐기. 모바일 trend API(①)만 사용.

    # ③ 장중 1분 투자자 시계열(2026-09-21) — 현재 호출 그대로 + 변형 + 대체 후보
    bizdate = datetime.datetime.now(
        datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y%m%d")
    res["bizdate"] = bizdate
    ref = dict(UA, Referer="https://finance.naver.com/sise/sise_trans_style.naver")
    _timeline_case("timeline:asis", {"bizdate": bizdate, "sosok": "01", "page": 1}, UA, res)
    _timeline_case("timeline:referer", {"bizdate": bizdate, "sosok": "01", "page": 1}, ref, res)
    _timeline_case("timeline:noBizdate", {"sosok": "01", "page": 1}, ref, res)
    _timeline_case("timeline:kosdaq", {"bizdate": bizdate, "sosok": "02", "page": 1}, ref, res)
    _json_case("altApi:indexTrend",
               "https://m.stock.naver.com/api/index/KOSPI/investorTrend", {}, res)
    _json_case("altApi:indexPrice",
               "https://api.stock.naver.com/index/KOSPI/investor",
               {"pageSize": 10, "page": 1}, res)

    # ④ 테마맵 구형 페이지(2026-09-21) — theme_map.yml 이 9/14·9/21 연속 실패
    #    (build_theme_map: 랭킹 < 50 이면 exit 1). 랭킹·상세 둘 다 확인
    _html_case("theme:ranking", "https://finance.naver.com/sise/theme.naver",
               {"page": 1}, res, row_re=THEME_ROW_RE)
    _html_case("theme:detail", "https://finance.naver.com/sise/sise_group_detail.naver",
               {"type": "theme", "no": "586"}, res, needle="<tr onMouseOver")
    _html_case("upjong:ranking", "https://finance.naver.com/sise/sise_group.naver",
               {"type": "upjong"}, res, needle="sise_group_detail")

    # ⑤ 모바일(Npay 증권) API 탐색 — 번들 문자열 + 유력 후보 직접 호출
    _discover_mobile_apis(res, ["/", "/domestic/index/KOSPI/total",
                                "/domestic/index/KOSPI/investor",
                                "/domestic/theme", "/marketindex"])
    for name, path, params in (
            ("m:indexTrend", "/api/index/KOSPI/trend", {"pageSize": 10, "page": 1}),
            ("m:indexInvestor", "/api/index/KOSPI/investor", {}),
            ("m:indexIntegration", "/api/index/KOSPI/integration", {}),
            ("m:indexInvestorTime", "/api/index/KOSPI/investorTrend/time", {}),
            ("m:frontIndexInvestor", "/front-api/index/investorTrend",
             {"indexCode": "KOSPI"}),
            ("m:themeList", "/api/stocks/theme", {"page": 1, "pageSize": 20}),
            ("m:themeDetail", "/api/stocks/theme/586", {"page": 1, "pageSize": 20}),
            ("m:upjongList", "/api/stocks/upjong", {"page": 1, "pageSize": 20})):
        _json_case(name, MOBILE + path, params, res)

    # ⑥ 신규 웹(stock.naver.com) 투자자별 매매동향 시간별 화면 API 탐색(2026-09-21 사용자 제공)
    #    + 테마 화면 — 번들·인라인·__NEXT_DATA__ 에서 API 주소 수집
    _discover_mobile_apis(res, [TRADER_PAGE, "/market/stock/kr/theme/1",
                                "/market/stock/kr/theme/586"],
                          base=NEW_WEB, tag="newweb", max_js=80)
    for name, path, params in (
            ("nw:traderTime", "/api/domestic/market/trend/trader", {"marketType": "KOSPI"}),
            ("nw:investorTime", "/api/domestic/market/investor/time", {"market": "KOSPI"}),
            ("nw:dealTrendTime", "/api/domestic/index/KOSPI/dealTrend/time", {})):
        _json_sample(name, NEW_WEB + path, params, res, referer=NEW_WEB + TRADER_PAGE)

    # ⑦ 파서 작성용 샘플 — 살아 있는 모바일 JSON(테마 목록·상세, 지수 수급·통합)
    _json_sample("sample:themeList", MOBILE + "/api/stocks/theme", {"page": 1, "pageSize": 3}, res)
    _json_sample("sample:themeDetail", MOBILE + "/api/stocks/theme/586",
                 {"page": 1, "pageSize": 3}, res)
    _json_sample("sample:indexTrend", MOBILE + "/api/index/KOSPI/trend",
                 {"pageSize": 3, "page": 1}, res)
    _json_sample("sample:indexIntegration", MOBILE + "/api/index/KOSPI/integration", {}, res)

    # ⑧ 4차(2026-09-21) — 시간별 API 쿼리 파라미터 확정: 번들 문맥 + 후보 조합 직접 호출
    _bundle_context(res, TRADER_PAGE, ["market/trend/time", "market/trend/chart/time",
                                       "market/trend/daily"])
    ref_tr = NEW_WEB + TRADER_PAGE
    for i, (path, params) in enumerate((
            ("/api/domestic/market/trend/time", {}),
            ("/api/domestic/market/trend/time", {"marketType": "KOSPI"}),
            ("/api/domestic/market/trend/time", {"market": "KOSPI"}),
            ("/api/domestic/market/trend/time", {"koreaIndexType": "KOSPI"}),
            ("/api/domestic/market/trend/time", {"sosok": "01"}),
            ("/api/domestic/market/trend/time", {"marketType": "KOSPI", "bizdate": bizdate}),
            ("/api/domestic/market/trend/time", {"koreaIndexType": "KOSPI", "bizdate": bizdate,
                                                 "page": 1, "pageSize": 10}),
            ("/api/domestic/market/trend/chart/time", {"koreaIndexType": "KOSPI"}),
            ("/api/domestic/market/trend/chart/time", {"marketType": "KOSPI"}),
            ("/api/domestic/market/trend/daily", {"koreaIndexType": "KOSPI"}))):
        _json_sample(f"try:{i}:{path.rsplit('/api/', 1)[1]}?{'&'.join(params)}",
                     NEW_WEB + path, params, res, referer=ref_tr)
    # 테마 상세의 편입 사유 필드(themeItemInfoMap) 전체 구조
    try:
        d = requests.get(MOBILE + "/api/stocks/theme/586", params={"page": 1, "pageSize": 2},
                         headers=UA, timeout=20).json()
        res["themeDetailExtra"] = {k: _trim(d.get(k)) for k in
                                   ("groupInfo", "themeDescription", "themeItemInfoMap",
                                    "totalCount")}
    except Exception as e:
        res["themeDetailExtra"] = {"error": str(e)}

    # ⑨ 5차(2026-09-21) — 규약 확정: 번들 H(): tradeType=KRX|NXT·marketType·bizdate·
    #    startIdx·pageSize. 투자자 코드표(W={8e3:개인,9e3:외국인,9999:기관계,1e3:…})와
    #    marketType 값 목록을 넓은 문맥으로, 실응답은 축약 없이 앞·뒤 행 원문 저장
    _bundle_context(res, TRADER_PAGE, ['9e3:{group:"foreign"', 'marketType:"',
                                       '"KOSDAQ"'], width=1600)
    res["ctx5"] = res.pop("ctx", None)
    for mt in ("KOSPI", "KOSDAQ", "KSP", "KSQ", "0", "1", "STOCK_KOSPI"):
        key = f"trend5:{mt}"
        try:
            r = requests.get(NEW_WEB + "/api/domestic/market/trend/time",
                             params={"tradeType": "KRX", "marketType": mt, "bizdate": bizdate,
                                     "startIdx": 0, "pageSize": 300},
                             headers=dict(UA, Referer=NEW_WEB + TRADER_PAGE), timeout=20)
            d = r.json()
            rows = d.get("content") or []
            res[key] = {"status": r.status_code, "totalElements": d.get("totalElements"),
                        "n": len(rows), "first": rows[0] if rows else None,
                        "last": rows[-1] if rows else None,
                        "times": [x.get("time") for x in rows[:5]] + ["…"]
                                 + [x.get("time") for x in rows[-3:]]}
        except Exception as e:
            res[key] = {"error": str(e)}

    # ⑩ 6차(2026-09-21) — 5차 pageSize=300 전부 400. 페이징 규약(startIdx=행 오프셋?
    #    페이지 번호?)·pageSize 상한·누적 여부 확정. 400 이면 본문(오류 사유) 저장
    for mt, si, ps in (("KOSPI", 0, 20), ("KOSPI", 1, 20), ("KOSPI", 20, 20),
                       ("KOSPI", 0, 50), ("KOSPI", 0, 100), ("KOSPI", 0, 200),
                       ("KOSDAQ", 0, 20)):
        key = f"trend6:{mt}:si{si}:ps{ps}"
        try:
            r = requests.get(NEW_WEB + "/api/domestic/market/trend/time",
                             params={"tradeType": "KRX", "marketType": mt, "bizdate": bizdate,
                                     "startIdx": si, "pageSize": ps},
                             headers=dict(UA, Referer=NEW_WEB + TRADER_PAGE), timeout=20)
            try:
                d = r.json()
            except Exception:
                d = {}
            rows = d.get("content") or [] if isinstance(d, dict) else []
            res[key] = {"status": r.status_code,
                        "body": None if r.ok else r.text[:300],
                        "number": d.get("number") if isinstance(d, dict) else None,
                        "offset": ((d.get("pageable") or {}).get("offset")
                                   if isinstance(d, dict) else None),
                        "totalElements": d.get("totalElements") if isinstance(d, dict) else None,
                        "n": len(rows),
                        "times": [x.get("time") for x in rows[:3]]
                                 + ([x.get("time") for x in rows[-2:]] if len(rows) > 3 else []),
                        # 누적 판정용 — 첫 행(최신)·끝 행 원문(12 투자자 전부)
                        "first": rows[0] if rows and si == 0 else None,
                        "last": rows[-1] if rows and si == 0 and ps >= 100 else None}
        except Exception as e:
            res[key] = {"error": str(e)}

    # ⑪ 7차(2026-09-21) — 교체한 sources 함수 실동작 검증(파이프라인 코드 그대로 import).
    #    pandas/yfinance 는 이 워크플로에 없어 스텁(두 함수는 미사용)
    import sys
    import types
    import os
    sys.path.insert(0, os.getcwd())
    for mod in ("pandas", "yfinance"):
        sys.modules.setdefault(mod, types.ModuleType(mod))
    try:
        from analysis import sources as S
        live = {}
        for mk in ("KOSPI", "KOSDAQ"):
            rows = S.naver_investor_timeline(mk, pages=40)
            ref = {}
            try:
                ref = requests.get(f"{MOBILE}/api/index/{mk}/trend", headers=UA,
                                   timeout=20).json()
            except Exception:
                pass
            live[mk] = {"n": len(rows), "firstTime": rows[0]["time"] if rows else None,
                        "last": rows[-1] if rows else None,
                        "mobileTotals": {k: ref.get(k) for k in
                                         ("personalValue", "foreignValue", "institutionalValue")}}
        live["kospiEqKosdaq"] = (live["KOSPI"]["last"] == live["KOSDAQ"]["last"])
        rk = S.naver_theme_ranking()
        st = S.naver_theme_stocks(rk[0]["no"], limit=500) if rk else []
        live["themes"] = {"ranking": len(rk), "top": rk[:2], "detailN": len(st),
                          "detailSample": st[:2], "reasonFilled": sum(1 for x in st if x["reason"])}
        res["liveCheck"] = live
    except Exception as e:
        import traceback
        res["liveCheck"] = {"error": traceback.format_exc()[-800:]}

    # ⑫ 8차(2026-09-21) — 남은 구형 HTML 2종 점검 + 모바일 대체 후보
    #    board.naver(종목토론방, naver_board: table.type2 > td.title a)
    #    main.naver(밸류에이션, naver_valuation: #_per #_eps #_cns_per #_pbr #_dvr ·
    #    '동일업종 PER 정보'·'투자의견 정보' 표)
    legacy = {}
    for code in CODES:
        for kind, url, needles in (
                ("board", "https://finance.naver.com/item/board.naver",
                 ('class="type2"', 'class="title"')),
                ("main", "https://finance.naver.com/item/main.naver",
                 ('id="_per"', 'id="_pbr"', 'id="_cns_per"', "동일업종 PER 정보",
                  "투자의견 정보"))):
            try:
                r = requests.get(url, params={"code": code},
                                 headers=dict(UA, Referer="https://finance.naver.com/"),
                                 timeout=20)
                r.encoding = r.apparent_encoding or "utf-8"
                t = r.text
                legacy[f"{kind}:{code}"] = {
                    "status": r.status_code, "finalUrl": r.url, "size": len(t),
                    "needles": {n: (n in t) for n in needles},
                    "titleLinks": t.count('<td class="title">') if kind == "board" else None}
            except Exception as e:
                legacy[f"{kind}:{code}"] = {"error": str(e)}
    res["legacyHtml"] = legacy
    # 대체 후보 — 신규 웹 종목 화면 번들에서 토론·밸류 관련 API 주소 수집 + 모바일 샘플
    global API_KEYWORDS
    API_KEYWORDS = API_KEYWORDS + ("discussion", "community", "board", "post", "consensus",
                                   "integration", "finance", "valuation")
    _discover_mobile_apis(res, ["/domestic/stock/005930/total",
                                "/domestic/stock/005930/discussion"],
                          base=NEW_WEB, tag="stockweb", max_js=80)
    _json_sample("sample:stockIntegration", MOBILE + "/api/stock/005930/integration", {}, res)
    _json_sample("sample:stockFinanceAnnual", MOBILE + "/api/stock/005930/finance/annual",
                 {}, res)
    _json_sample("sample:discussionByItem",
                 MOBILE + "/api/community/discussion/posts/by-item",
                 {"discussionType": "domesticStock", "itemCode": "005930",
                  "pageSize": 5, "isHolderOnly": "false", "excludesItemNews": "false",
                  "isBest": "false"}, res)

    res["trendApiOk"] = all(res[f"trendApi:{c}"].get("hasData") for c in CODES)
    # 판정: 410=폐지(Gone) / 그 외 4xx·예외=차단·오류 / 200·행 0=구조 변경
    def _verdict(c):
        st = c.get("status") or 0
        if c.get("error"):
            return "요청실패(오류)"
        if st == 410:
            return "폐지(410 Gone)"
        if st >= 400:
            return f"요청실패({st})"
        return "정상" if (c.get("rows") or c.get("needle")) else "구조변경 의심(200·데이터 없음)"
    res["timelineVerdict"] = _verdict(res["timeline:asis"])
    res["themeVerdict"] = {k: _verdict(res[k]) for k in
                           ("theme:ranking", "theme:detail", "upjong:ranking")}
    res["mobileOk"] = [k for k in res if k.startswith("m:")
                       and res[k].get("status") == 200 and res[k].get("isJson")]
    print(json.dumps(res, indent=2, ensure_ascii=False))
    with open(".github/naver_flow_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
