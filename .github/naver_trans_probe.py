"""네이버 투자자별 매매동향(sise_trans_style) 실페이지 구조 프로브 — 임시.

naver_investor_timeline 파서가 실페이지에서 0행을 반환(2026-07-13 실측, 모의 HTML
로만 검증했던 정규식 불일치). Actions IP 에서 실제 HTML 을 채집해 커밋하면 로컬에서
구조를 보고 정규식을 고친다(로컬 PC 는 네이버 직접 접근 불가). 수정 확정 후 제거.

결과: .github/naver_trans_probe_result.json
"""
import json
import re
import datetime

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
OUT = ".github/naver_trans_probe_result.json"


def main():
    res = {"asof": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        r = requests.get("https://finance.naver.com/sise/sise_trans_style.naver",
                         params={"sosok": "01", "page": 1}, headers=UA, timeout=20)
        html = r.content.decode("euc-kr", errors="replace")
        res["status"] = r.status_code
        res["length"] = len(html)
        res["iframes"] = re.findall(r'<iframe[^>]+src="([^"]+)"', html)[:10]
        # 시간(HH:MM) 셀 주변 원문 — 파서 정규식 교정용
        m = re.search(r"\d{1,2}:\d{2}", html)
        res["firstTimeAt"] = m.start() if m else None
        if m:
            res["aroundFirstTime"] = html[max(0, m.start() - 1500): m.start() + 2500]
        # '시간별' 표제 주변
        j = html.find("시간별")
        if j >= 0:
            res["aroundHeader"] = html[j: j + 1500]
        res["headSnippet"] = html[:1200]
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"

    # iframe 페이지가 실데이터를 갖는 구조면 그쪽도 채집
    for src in res.get("iframes", []):
        if "trans" in src or "invest" in src:
            try:
                url = src if src.startswith("http") else "https://finance.naver.com" + src
                r2 = requests.get(url, headers=UA, timeout=20)
                h2 = r2.content.decode("euc-kr", errors="replace")
                m2 = re.search(r"\d{1,2}:\d{2}", h2)
                res["iframeProbe"] = {
                    "url": url, "status": r2.status_code, "length": len(h2),
                    "aroundFirstTime": h2[max(0, m2.start() - 1500): m2.start() + 2500] if m2 else None,
                }
            except Exception as e:
                res["iframeProbe"] = {"url": src, "error": str(e)[:200]}
            break

    # 종목별 수급(trend) API — pageSize=1 이 유효한지(enrich 의 flow 전멸 원인 후보).
    for label, ps in (("trend_ps1", 1), ("trend_ps25", 25)):
        try:
            r3 = requests.get("https://m.stock.naver.com/api/stock/051910/trend",
                              params={"pageSize": ps, "page": 1}, headers=UA, timeout=15)
            rows = r3.json() if r3.ok else None
            row0 = rows[0] if isinstance(rows, list) and rows else None
            res[label] = {"status": r3.status_code,
                          "isList": isinstance(rows, list),
                          "rows": len(rows) if isinstance(rows, list) else None,
                          "row0Keys": sorted(row0.keys())[:20] if isinstance(row0, dict) else None,
                          "bodyHead": (r3.text or "")[:200] if not isinstance(rows, list) else None}
        except Exception as e:
            res[label] = {"error": str(e)[:200]}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print(f"probe written: {OUT} (status={res.get('status')}, len={res.get('length')})")


if __name__ == "__main__":
    main()
