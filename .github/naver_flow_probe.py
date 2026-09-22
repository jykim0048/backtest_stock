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

    # ⑬ 9차(2026-09-21) — 대체 규약 확정. ① integration.totalInfos 전 항목(code·key·value)
    #    — PER/EPS/추정/PBR/BPS/배당/52주 코드명 ② 토론방 API 호스트·파라미터(번들 문맥 +
    #    stock.naver.com 호스트 직접 호출)
    for code in CODES:
        try:
            d = requests.get(f"{MOBILE}/api/stock/{code}/integration", headers=UA,
                             timeout=20).json()
            res[f"totalInfos:{code}"] = [{k: x.get(k) for k in ("code", "key", "value")}
                                         for x in (d.get("totalInfos") or [])]
            res[f"consensus:{code}"] = d.get("consensusInfo")
            res[f"integrationKeys:{code}"] = sorted(d.keys())
        except Exception as e:
            res[f"totalInfos:{code}"] = {"error": str(e)}
    _bundle_context(res, "/domestic/stock/005930/discussion",
                    ["discussion/posts/by-item?", "discussion/posts?itemCode="], width=900)
    res["ctxBoard"] = res.pop("ctx", None)
    ref_d = NEW_WEB + "/domestic/stock/005930/discussion"
    for i, (path, params) in enumerate((
            ("/api/community/discussion/posts/by-item", {"itemCode": "005930"}),
            ("/api/community/discussion/posts/by-item",
             {"discussionType": "domesticStock", "itemCode": "005930", "pageSize": 5}),
            ("/api/community/discussion/posts", {"itemCode": "005930"}),
            ("/api/community/discussion/posts", {"itemCode": "005930", "pageSize": 5}))):
        _json_sample(f"board:{i}:{path.rsplit('/', 1)[1]}?{'&'.join(params)}",
                     NEW_WEB + path, params, res, referer=ref_d)

    # ⑭ 10차(2026-09-21) — 동일업종 PER·등락률 출처 API 찾기. 사용자 제공 화면
    #    stock.naver.com/domestic/stock/005930/price 에 '동일업종 등락률 +3.10%·PER 9.12배'
    #    표시 — integration 엔 없음. 번들 발견 후보를 호출해 응답에 업종 관련 키가 있는지
    #    (industry/upjong/sector/same…) 재귀 탐색 + 상위 구조 샘플
    def _find_keys(o, pat, path="", out=None, depth=0):
        out = [] if out is None else out
        if depth > 6 or len(out) > 40:
            return out
        if isinstance(o, dict):
            for k, v in o.items():
                p = f"{path}.{k}"
                if re.search(pat, k, re.I):
                    out.append({"path": p, "value": _trim(v, 3)})
                _find_keys(v, pat, p, out, depth + 1)
        elif isinstance(o, list):
            for i, v in enumerate(o[:3]):
                _find_keys(v, pat, f"{path}[{i}]", out, depth + 1)
        return out
    ref_p = NEW_WEB + "/domestic/stock/005930/price"
    ind = {}
    for path in ("/api/domestic/detail/005930/price", "/api/domestic/detail/005930/listing",
                 "/api/domestic/detail/005930/traderInfo",
                 "/api/domestic/detail/005930/integration",
                 "/api/domestic/detail/005930/investInfo",
                 "/api/securityService/integration/indicators?itemCode=005930",
                 "/api/securityService/integration/v1/indicators?itemCode=005930",
                 "/api/securityService/integration/price?itemCode=005930"):
        try:
            r = requests.get(NEW_WEB + path, headers=dict(UA, Referer=ref_p), timeout=20)
            try:
                d = r.json()
            except Exception:
                d = None
            ind[path] = {"status": r.status_code,
                         "topKeys": list(d)[:25] if isinstance(d, dict) else type(d).__name__,
                         "industryKeys": _find_keys(d, r"industr|upjong|sector|same|peer"),
                         "has912": "9.12" in r.text, "has310": "3.10" in r.text}
        except Exception as e:
            ind[path] = {"error": str(e)}
    res["industryPer"] = ind
    API_KEYWORDS = API_KEYWORDS + ("detail", "industry", "compare", "upjong")
    _discover_mobile_apis(res, ["/domestic/stock/005930/price"], base=NEW_WEB,
                          tag="pricepage", max_js=90)
    # 교체한 naver_valuation·naver_board 실동작(⑪ 과 같은 방식 — 파이프라인 코드 import)
    try:
        from analysis import sources as S2
        res["liveCheck2"] = {
            "valuation": {c: S2.naver_valuation(c) for c in CODES},
            "board": {c: (lambda b: {"n": len(b), "otherItem": 0, "top": b[:2]})(
                S2.naver_board(c, pages=5)) for c in CODES}}
    except Exception:
        import traceback
        res["liveCheck2"] = {"error": traceback.format_exc()[-800:]}

    # ⑮ 11차(2026-09-21) — 사용자 지적: 동일업종 등락률·PER, 토론 조회수 모두 화면에 있음.
    #    ① 가격 화면 HTML(SSR) 안에 값/문구가 있는지 ② 번들에서 '동일업종'(UTF-8 원문·
    #    \\uXXXX 이스케이프)·조회수 필드 후보(viewCount/readCount/hitCount/views) 주변 문맥
    #    ③ 글 ID 묶음 API(reactions·comment-counts) 실제 호출
    ctx11 = {}
    try:
        html = requests.get(NEW_WEB + "/domestic/stock/005930/price",
                            headers=dict(UA, Referer=NEW_WEB + "/"), timeout=20).text
        for n in ("동일업종", "industryPer", "sameIndustry", "upjongPer", "9.12"):
            i = html.find(n)
            ctx11[f"html:{n}"] = html[max(0, i - 500):i + 500] if i >= 0 else None
    except Exception as e:
        ctx11["html:error"] = str(e)
    res["ctx11html"] = ctx11
    esc = "동일업종".encode("unicode_escape").decode()        # \\ub3d9\\uc77c\\uc5c5\\uc885
    _bundle_context(res, "/domestic/stock/005930/price",
                    ["동일업종", esc, "industryPer", "IndustryPer", "sameIndustry"], width=700)
    res["ctx11price"] = res.pop("ctx", None)
    _bundle_context(res, "/domestic/stock/005930/discussion",
                    ["viewCount", "readCount", "hitCount", "posts/reactions?postIds=",
                     "posts/comment-counts?"], width=500)
    res["ctx11board"] = res.pop("ctx", None)
    try:
        posts = requests.get(NEW_WEB + "/api/community/discussion/posts",
                             params={"itemCode": "005930", "discussionType": "domesticStock",
                                     "isHolderOnly": "false", "excludesItemNews": "false",
                                     "isItemNewsOnly": "false", "pageSize": 3},
                             headers=UA, timeout=20).json().get("posts") or []
        ids = ",".join(str(p.get("id")) for p in posts)
        for name, path in (("reactions", "/api/community/discussion/posts/reactions"),
                           ("commentCounts", "/api/community/discussion/posts/comment-counts"),
                           ("viewCounts", "/api/community/discussion/posts/view-counts")):
            _json_sample(f"batch:{name}", NEW_WEB + path, {"postIds": ids}, res,
                         referer=NEW_WEB + "/domestic/stock/005930/discussion")
        if posts:
            _json_sample("single:post", NEW_WEB + f"/api/community/discussion/posts/{posts[0]['id']}",
                         {"viewerProfileId": ""}, res)
    except Exception as e:
        res["batch:error"] = str(e)

    # ⑯ 12차(2026-09-21) — sameIndustryPer/ChangeRate 를 주는 API 확정.
    #    그 필드를 쓰는 번들 파일에서 API 경로 전부(정적 + "/api/domestic/detail/".concat(x,
    #    "/suffix") 동적 조합) 추출 → 후보 호출해 응답 본문에 sameIndustryPer 포함 여부
    found = {"chunks": [], "paths": set(), "suffixes": set()}
    try:
        html = requests.get(NEW_WEB + "/domestic/stock/005930/price",
                            headers=dict(UA, Referer=NEW_WEB + "/"), timeout=20).text
        for s in re.findall(r'<script[^>]+src="([^"]+\.js)"', html)[:90]:
            u = s if s.startswith("http") else NEW_WEB + s
            try:
                js = requests.get(u, headers=UA, timeout=20).text
            except Exception:
                continue
            if "sameIndustryPer" not in js:
                continue
            found["chunks"].append(u.rsplit("/", 1)[-1])
            found["paths"] |= set(re.findall(r'["\'`](/api/[^"\'`\s]{2,140})', js))
            found["suffixes"] |= set(re.findall(r'\.concat\([^()]{1,30}?,"(/[A-Za-z/]+[^"]*)"\)', js))
    except Exception as e:
        found["error"] = str(e)
    tries = {}
    cands = {f"/api/domestic/detail/005930{sfx.split('?')[0]}" for sfx in found["suffixes"]}
    cands |= {p for p in found["paths"] if "?" not in p and "{" not in p}
    cands |= {f"/api/domestic/detail/005930/{x}" for x in
              ("basic", "info", "invest", "investment", "investmentInfo", "stockInfo",
               "summary", "indicator", "indicators", "fundamental", "overview", "total")}
    for p in sorted(cands)[:60]:
        url = NEW_WEB + (p if p.startswith("/api/domestic/detail/005930") or "005930" in p
                         else p)
        try:
            r = requests.get(url, headers=dict(UA, Referer=NEW_WEB + "/domestic/stock/005930/price"),
                             timeout=15)
            hit = "sameIndustryPer" in r.text
            tries[p] = {"status": r.status_code, "hit": hit,
                        "snippet": (r.text[max(0, r.text.find("sameIndustry") - 300):
                                           r.text.find("sameIndustry") + 300] if hit else None)}
        except Exception as e:
            tries[p] = {"error": str(e)}
    res["sameIndustry"] = {"chunks": found["chunks"], "paths": sorted(found["paths"])[:120],
                           "suffixes": sorted(found["suffixes"])[:80],
                           "hits": {k: v for k, v in tries.items() if v.get("hit")},
                           "tried": {k: v.get("status") for k, v in tries.items()}}
    # 조회수 반영 naver_board 실동작
    try:
        from analysis import sources as S3
        b = S3.naver_board("005930", pages=1)
        res["liveCheck3"] = {"n": len(b), "withViews": sum(1 for x in b if x["views"]),
                             "top": b[:3]}
    except Exception:
        import traceback
        res["liveCheck3"] = {"error": traceback.format_exc()[-600:]}

    # ⑰ 13차(2026-09-22) — 사용자 요청 2건
    #  A. 동일업종 PER·등락률: 가격 화면 RSC 페이로드 + invest-info/detail 후보 호출
    ref_p = NEW_WEB + "/domestic/stock/005930/price"
    a13 = {}
    for name, hdr, url in (
            ("rsc", {"RSC": "1"}, ref_p),
            ("rscQuery", {}, ref_p + "?_rsc=1"),
            ("rscNextUrl", {"RSC": "1", "Next-Url": "/domestic/stock/005930/price"}, ref_p)):
        try:
            r = requests.get(url, headers=dict(UA, Referer=ref_p, **hdr), timeout=20)
            i = r.text.find("sameIndustry")
            a13[name] = {"status": r.status_code, "ctype": r.headers.get("content-type"),
                         "size": len(r.text), "hit": i >= 0,
                         "snippet": r.text[max(0, i - 600):i + 400] if i >= 0 else None}
        except Exception as e:
            a13[name] = {"error": str(e)}
    for p in ("/api/stockDomestic/invest-info/005930", "/api/stockDomestic/invest-info/stock/005930",
              "/api/stockDomestic/005930/invest-info", "/api/stockDomestic/invest-info?itemCode=005930",
              "/api/domestic/detail/stock/005930/KRX/invest", "/api/domestic/detail/005930/invest-info",
              "/api/domestic/detail/005930/investInfo?tradeType=KRX",
              "/api/domestic/stock/005930/invest-info", "/api/domestic/stock/005930/price",
              "/api/domestic/stock/005930/basic", "/api/domestic/stock/005930/integration"):
        try:
            r = requests.get(NEW_WEB + p, headers=dict(UA, Referer=ref_p), timeout=15)
            i = r.text.find("sameIndustry")
            a13[p] = {"status": r.status_code, "hit": i >= 0, "head": r.text[:160],
                      "snippet": r.text[max(0, i - 400):i + 400] if i >= 0 else None}
        except Exception as e:
            a13[p] = {"error": str(e)}
    # 번들 내 invest-info 사용처 문맥(경로·파라미터 확인용)
    _bundle_context(res, "/domestic/stock/005930/price",
                    ["invest-info", "stockDomestic/"], width=400)
    a13["ctx"] = res.pop("ctx", None)
    res["sameIndustry13"] = a13

    #  B. 시장지표: 화면 링크에서 (분류, 코드) 추출 → 실시간 API 호출 샘플
    b13 = {"pages": {}, "pairs": [], "calls": {}}
    pairs = set()
    for page in ("/market/marketindex", "/market/marketindex/exchange",
                 "/market/marketindex/bond", "/market/marketindex/interest",
                 "/market/marketindex/energy", "/market/marketindex/metals"):
        try:
            r = requests.get(NEW_WEB + page, headers=dict(UA, Referer=NEW_WEB + "/"), timeout=20)
            found = set(re.findall(r'/marketindex/([A-Za-z]+)/([^/"?\s]+)/price', r.text))
            b13["pages"][page] = {"status": r.status_code, "finalUrl": r.url, "pairs": len(found),
                                  "apis": sorted(set(re.findall(
                                      r'["\'`](/api/[^"\'`\s]{2,120})', r.text)))[:40]}
            pairs |= found
        except Exception as e:
            b13["pages"][page] = {"error": str(e)}
    b13["pairs"] = sorted(pairs)[:80]
    by_cat = {}
    for cat, code in pairs:
        by_cat.setdefault(cat, []).append(code)
    for cat, codes in by_cat.items():
        _json_sample(f"mi:{cat}", NEW_WEB + f"/api/realtime/marketindex/{cat}/{','.join(codes[:12])}",
                     {}, res, referer=NEW_WEB + "/market/marketindex")
        b13["calls"][cat] = res.pop(f"mi:{cat}")
    _discover_mobile_apis(res, ["/market/marketindex"], base=NEW_WEB, tag="miweb", max_js=90)
    b13["bundleApis"] = [a for a in res.pop("miweb:apis", []) if any(
        k in a.lower() for k in ("marketindex", "bond", "interest", "exchange", "energy", "metal",
                                 "rate"))]
    res.pop("miweb:pages", None)
    res.pop("miweb:bundles", None)
    res["marketindex13"] = b13

    # ⑱ 14차(2026-09-22) — Playwright 캡처로 찾은 API 를 일반 requests 로 호출(쿠키 없이도
    #    되는지) + 파서 작성용 전 항목 덤프
    f14 = {}
    for code in CODES:
        try:
            r = requests.get(f"{NEW_WEB}/api/domestic/detail/{code}/detail",
                             params={"codeType": "KRX"},
                             headers=dict(UA, Referer=f"{NEW_WEB}/domestic/stock/{code}/price"),
                             timeout=20)
            d = r.json()
            f14[f"detail:{code}"] = {
                "status": r.status_code,
                "keys": sorted(d)[:120] if isinstance(d, dict) else None,
                "picked": {k: v for k, v in (d.items() if isinstance(d, dict) else [])
                           if re.search(r"same|industry|upjong|per|pbr|eps|dividend", k, re.I)}}
        except Exception as e:
            f14[f"detail:{code}"] = {"error": str(e)}
    for name in ("majors/rpc", "majors/domesticInterest", "majors/standardInterest",
                 "majors/bond", "energy", "metals"):
        try:
            r = requests.get(f"{NEW_WEB}/api/securityService/marketindex/{name}",
                             headers=dict(UA, Referer=NEW_WEB + "/market/marketindex"), timeout=20)
            d = r.json()
            rows = d if isinstance(d, list) else (d.get("datas") or d.get("items") or [])
            f14[f"mi:{name}"] = {
                "status": r.status_code, "type": type(d).__name__, "n": len(rows),
                "keys": sorted(rows[0])[:60] if rows and isinstance(rows[0], dict) else None,
                "rows": [{k: x.get(k) for k in ("categoryType", "reutersCode", "symbolCode", "name",
                                                 "closePrice", "fluctuations", "fluctuationsRatio",
                                                 "localTradedAt")}
                         | {"dir": (x.get("fluctuationsType") or {}).get("name")}
                         for x in rows if isinstance(x, dict)]}
        except Exception as e:
            f14[f"mi:{name}"] = {"error": str(e)}
    res["api14"] = f14
    # 교체한 naver_valuation(동일업종)·naver_market_indicators 실동작(파이프라인 코드 import)
    try:
        from analysis import sources as S5
        res["liveCheck5"] = {
            "valuation": {c: {k: v for k, v in S5.naver_valuation(c).items()
                              if k in ("per", "estPer", "industryPer", "industryChangePct",
                                       "opinionLabel", "targetPrice")} for c in CODES},
            "marketIndicators": S5.naver_market_indicators()}
    except Exception:
        import traceback
        res["liveCheck5"] = {"error": traceback.format_exc()[-600:]}

    # ⑲ 15차(2026-09-22) — 남은 네이버 경로 전수 점검(조용한 0/빈 값 탐지). 파이프라인과
    #    같은 URL·파싱 기준으로 1콜씩: 폴링 구형 쿼리(기여도 가중)·급등락 랭킹·실적 재무·
    #    K200 fchart(구형)·지수 basic·종목 basic
    a15 = {}

    def _chk(name, url, params=None, parse=None):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            info = {"status": r.status_code, "finalUrl": r.url[:160], "size": len(r.text),
                    "head": r.text[:240]}
            if parse:
                try:
                    info["parsed"] = parse(r)
                except Exception as e:
                    info["parseError"] = str(e)[:200]
            a15[name] = info
        except Exception as e:
            a15[name] = {"error": str(e)[:200]}

    def _poll(r):                        # generate_intraday_briefing._fetch_match_rates 와 동일
        out = {}
        for a in ((r.json() or {}).get("result") or {}).get("areas") or []:
            for it in a.get("datas") or []:
                if it.get("cd") and it.get("cr") is not None:
                    out[str(it["cd"])] = it.get("cr")
        return {"n": len(out), "sample": dict(list(out.items())[:3])}
    _chk("pollingServiceItem",
         "https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:005930,000660,247540",
         parse=_poll)
    _chk("pollingNewStock",               # 신규 웹이 쓰는 형식(대체 후보)
         "https://polling.finance.naver.com/api/realtime/domestic/stock/005930,000660,247540",
         parse=lambda r: {"n": len((r.json() or {}).get("datas") or []),
                          "keys": sorted(((r.json() or {}).get("datas") or [{}])[0])[:40],
                          "sample": [{k: x.get(k) for k in ("itemCode", "closePrice",
                                                             "fluctuationsRatio",
                                                             "compareToPreviousPrice")}
                                     for x in ((r.json() or {}).get("datas") or [])[:3]]})
    _chk("stockRankingUp", "https://m.stock.naver.com/api/stocks/up/KOSPI",
         {"page": 1, "pageSize": 5},
         parse=lambda r: {"n": len((r.json() or {}).get("stocks") or []),
                          "sample": [{k: x.get(k) for k in ("itemCode", "stockName",
                                                             "fluctuationsRatio", "closePrice")}
                                     for x in ((r.json() or {}).get("stocks") or [])[:2]]})
    for kind in ("quarter", "annual"):
        _chk(f"finance:{kind}", f"https://m.stock.naver.com/api/stock/005930/finance/{kind}",
             parse=lambda r: {"rows": len(((r.json() or {}).get("financeInfo") or {})
                                          .get("rowList") or []),
                              "cols": [c.get("key") for c in ((r.json() or {}).get("financeInfo")
                                                              or {}).get("trTitleList") or []]})
    _chk("k200:siseJson", "https://fchart.stock.naver.com/siseJson.naver",
         {"symbol": "KPI200", "requestType": 1, "startTime": "20260901", "endTime": "20260922",
          "timeframe": "day"},
         parse=lambda r: {"lines": r.text.count("\n"), "tail": r.text.strip()[-160:]})
    _chk("k200:indexPrice", "https://m.stock.naver.com/api/index/KPI200/price",
         {"pageSize": 3, "page": 1},
         parse=lambda r: {"n": len(r.json() or []), "first": (r.json() or [{}])[0]})
    _chk("indexBasic", "https://m.stock.naver.com/api/index/KOSPI/basic",
         parse=lambda r: {k: (r.json() or {}).get(k) for k in ("closePrice", "fluctuationsRatio")})
    _chk("stockBasic", "https://m.stock.naver.com/api/stock/005930/basic",
         parse=lambda r: {k: (r.json() or {}).get(k) for k in ("closePrice", "fluctuationsRatio")})
    _chk("indexTrend", "https://m.stock.naver.com/api/index/KOSPI/trend",
         parse=lambda r: r.json())
    res["audit15"] = a15

    # ㉑ 17차(2026-09-22) — 우선주 시총: KIS 마스터 파싱(build_sector_map_auto._parse_mst,
    #    우선주 포함) + 네이버 integration marketValue 폴백 실동작
    try:
        import sys as _sys
        import os as _os
        _sys.path.insert(0, _os.getcwd())
        import build_sector_map_auto as B
        rows = B._parse_mst("kospi_code") + B._parse_mst("kosdaq_code")
        by = {s["code"]: s for s in rows}
        cap17 = {"total": len(rows), "pref": sum(1 for s in rows if s["pref"]),
                 "sample": {c: {k: by[c][k] for k in ("name", "cap", "pref")}
                            for c in ("005930", "005935", "005380", "005385", "005387")
                            if c in by},
                 "prefAlnum": [s["code"] for s in rows if s["pref"] and not s["code"].isdigit()][:5]}
        nv = {}
        for c in ("005935", "005385"):
            j = requests.get(f"{MOBILE}/api/stock/{c}/integration", headers=UA, timeout=20).json()
            nv[c] = next((x.get("value") for x in (j.get("totalInfos") or [])
                          if x.get("code") == "marketValue"), None)
        cap17["naverMarketValue"] = nv
        # 18차(2026-09-22) — 타임라인 섹터 누락 코스닥 8종목의 마스터 분류(big/mid)
        tl_codes = ("393210", "049470", "0007J0", "356680", "140430", "411080", "071200", "201490")
        cap17["tlMissing"] = {c: ({k: by[c][k] for k in ("name", "big", "mid", "pref")}
                                  if c in by else "마스터에 없음") for c in tl_codes}
        res["prefCap17"] = cap17
    except Exception:
        import traceback
        res["prefCap17"] = {"error": traceback.format_exc()[-600:]}
    # 18차 — 섹터맵 생성기 dry-run 로그(미해석 KOSDAQ 중분류 리포트 포함, 파일 미저장)
    try:
        import subprocess
        import sys as _s18
        p = subprocess.run([_s18.executable, "build_sector_map_auto.py", "--dry-run"],
                           capture_output=True, text=True, timeout=240)
        res["sectorMapDryRun"] = {"rc": p.returncode,
                                  "log": (p.stdout + "\n" + p.stderr)[-4000:]}
    except Exception as e:
        res["sectorMapDryRun"] = {"error": str(e)}

    # ⑳ 16차(2026-09-22) — 폴링 신규 형식 전환 검증: 급등·급락 종목을 섞어 구형(SERVICE_ITEM,
    #    rf 4/5=하락)과 신규(api/realtime/domestic/stock, compareToPreviousPrice.code) 등락률을
    #    같은 종목으로 대조(부호 포함 일치 여부)
    try:
        codes = []
        for d_ in ("up", "down"):
            j = requests.get(f"https://m.stock.naver.com/api/stocks/{d_}/KOSPI",
                             params={"page": 1, "pageSize": 15}, headers=UA, timeout=20).json()
            codes += [x["itemCode"] for x in (j.get("stocks") or []) if x.get("itemCode")]
        codes = [c for c in dict.fromkeys(codes) if re.fullmatch(r"\d{6}", c)][:30]
        old, new = {}, {}
        j = requests.get("https://polling.finance.naver.com/api/realtime?query=SERVICE_ITEM:"
                         + ",".join(codes), headers={"User-Agent": "Mozilla/5.0"}, timeout=20).json()
        for a in ((j or {}).get("result") or {}).get("areas") or []:
            for it in a.get("datas") or []:
                if it.get("cd") and it.get("cr") is not None:
                    old[str(it["cd"])] = (-1 if str(it.get("rf")) in ("4", "5") else 1) * float(it["cr"])
        j = requests.get("https://polling.finance.naver.com/api/realtime/domestic/stock/"
                         + ",".join(codes), headers={"User-Agent": "Mozilla/5.0",
                                                     "Referer": "https://stock.naver.com/"},
                         timeout=20).json()
        raw_new = {}
        for it in (j or {}).get("datas") or []:
            cmp_ = it.get("compareToPreviousPrice") or {}
            rate = abs(float(str(it.get("fluctuationsRatioRaw", it.get("fluctuationsRatio"))).replace(",", "")))
            down = str(cmp_.get("code")) in ("4", "5") or cmp_.get("name") in ("FALLING", "LOWER_LIMIT")
            new[str(it["itemCode"])] = -rate if down else rate
            raw_new[str(it["itemCode"])] = [it.get("fluctuationsRatio"), it.get("fluctuationsRatioRaw"),
                                            cmp_.get("code")]
        both = [c for c in codes if c in old and c in new]
        diff = {c: (old[c], new[c]) for c in both if abs(old[c] - new[c]) > 0.011}
        res["pollCompare16"] = {"codes": len(codes), "old": len(old), "new": len(new),
                                "both": len(both), "negatives": sum(1 for c in both if new[c] < 0),
                                "mismatch": diff, "rawSample": dict(list(raw_new.items())[-4:])}
    except Exception:
        import traceback
        res["pollCompare16"] = {"error": traceback.format_exc()[-600:]}

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
