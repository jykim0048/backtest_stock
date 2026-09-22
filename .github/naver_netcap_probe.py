"""네이버 신규 웹(stock.naver.com) 화면이 실제로 호출하는 API 를 헤드리스 브라우저로 캡처.

번들 문자열 탐색(naver_flow_probe.py 1~13차)으로는 지연 로드 청크의 호출 주소를 못 찾았다
(2026-09-22 — 동일업종 PER·등락률, 시장지표 환율·유가·금리). Playwright 로 화면을 열어
네트워크 요청(URL·상태·응답 일부)을 그대로 기록한다. 결과: .github/naver_netcap_result.json.
"""
import json
import datetime

from playwright.sync_api import sync_playwright

PAGES = {
    # 동일업종 등락률·PER 이 '투자정보' 패널에 표시되는 화면(사용자 제공, 2026-09-22)
    "stockPrice": ("https://stock.naver.com/domestic/stock/005930/price",
                   ("sameIndustry", "9.39", "동일업종")),
    # 환율·국채·기준금리·에너지·금속 전부 한 화면(사용자 제공)
    "marketindex": ("https://stock.naver.com/market/marketindex",
                    ("FX_USDKRW", "USDKRW", "1,361", "DXY", "WTI", "국고채", "CD")),
}


def capture(page, url, needles):
    seen = []

    def on_response(resp):
        u = resp.url
        if "/api/" not in u or len(seen) >= 150:
            return
        rec = {"url": u, "status": resp.status, "method": resp.request.method}
        try:
            body = resp.text()
            rec["size"] = len(body)
            rec["hits"] = [n for n in needles if n in body]
            rec["head"] = body[:600]
        except Exception as e:
            rec["bodyError"] = str(e)[:120]
        seen.append(rec)

    page.on("response", on_response)
    page.goto(url, wait_until="networkidle", timeout=60000)
    # 스크롤로 지연 로드 섹션(투자정보·금리 표) 유도
    for _ in range(6):
        page.mouse.wheel(0, 1500)
        page.wait_for_timeout(700)
    page.wait_for_timeout(2500)
    page.remove_listener("response", on_response)
    return seen


def main():
    out = {"asof": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    with sync_playwright() as p:
        br = p.chromium.launch()
        ctx = br.new_context(locale="ko-KR", viewport={"width": 1400, "height": 1000},
                             user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128 Safari/537.36")
        for key, (url, needles) in PAGES.items():
            pg = ctx.new_page()
            try:
                reqs = capture(pg, url, needles)
                out[key] = {"n": len(reqs),
                            "withHits": [r for r in reqs if r.get("hits")],
                            "all": [{k: r.get(k) for k in ("url", "status", "size", "hits")}
                                    for r in reqs]}
            except Exception as e:
                out[key] = {"error": str(e)[:300]}
            pg.close()
        br.close()
    print(json.dumps({k: (v if k == "asof" else {"n": v.get("n"), "hits": len(v.get("withHits") or [])})
                      for k, v in out.items()}, ensure_ascii=False, indent=1))
    with open(".github/naver_netcap_result.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
