"""네이버 수급(외국인·기관 순매수) API Actions IP 접근성 프로브.

Q점수 Flow 팩터(2단계)의 데이터 소스로 m.stock.naver.com 모바일 API 를
쓰려는데, 이 도메인은 파이프라인에서 아직 안 써봤다 — KRX 처럼 해외
데이터센터 IP 를 차단할 가능성을 배제해야 한다. 폴백 후보인
finance.naver.com/item/frgn.naver(HTML)도 함께 확인한다.
결과는 .github/naver_flow_probe_result.json.
"""
import json
import datetime

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
CODES = ("005930", "247540")   # 대형주(삼성전자, KOSPI) + 코스닥(에코프로비엠)


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

    # ② frgn HTML 페이지 — 폴백 후보
    for code in CODES:
        key = f"frgnHtml:{code}"
        try:
            r = requests.get(
                "https://finance.naver.com/item/frgn.naver",
                params={"code": code}, headers=UA, timeout=20)
            res[key] = {"status": r.status_code, "size": len(r.text),
                        "hasData": "순매매량" in r.text}
        except Exception as e:
            res[key] = {"error": str(e), "hasData": False}

    res["trendApiOk"] = all(res[f"trendApi:{c}"].get("hasData") for c in CODES)
    res["frgnHtmlOk"] = all(res[f"frgnHtml:{c}"].get("hasData") for c in CODES)
    print(json.dumps(res, indent=2, ensure_ascii=False))
    with open(".github/naver_flow_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
