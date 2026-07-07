"""FnGuide Actions IP 접근성 프로브.

GitHub Actions(해외 데이터센터 IP)에서 wcomp.fnguide.com 이 실데이터를
반환하는지 확인한다 — KRX 는 해외 IP 를 차단하므로(빈 응답) FnGuide 도
같은 정책일 가능성을 배제해야 analysis/sources.fnguide_valuation 을
파이프라인에서 신뢰할 수 있다. 결과는 .github/fnguide_probe_result.json.
"""
import json
import datetime

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://wcomp.fnguide.com/",
}
# 페이지별 '실데이터 포함' 마커 — 차단/빈 껍데기 응답과 구분
MARKERS = {"Invest": 'id="h_per"', "FinanceRatio": "rtoAccumulate"}


def main():
    res = {"asof": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    for page, marker in MARKERS.items():
        for code in ("051910", "078140"):   # 대형주(LG화학) + 스몰캡(대봉엘에스)
            key = f"{page}:{code}"
            try:
                r = requests.get(
                    f"https://wcomp.fnguide.com/CompanyInfo/{page}",
                    params={"c_id": "AA", "menu_type": "01", "cmp_cd": code},
                    headers=HEADERS, timeout=20,
                )
                res[key] = {"status": r.status_code, "size": len(r.text),
                            "hasData": marker in r.text}
            except Exception as e:
                res[key] = {"error": str(e)}
    res["ok"] = all(v.get("hasData") for k, v in res.items()
                    if isinstance(v, dict) and k != "asof")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    with open(".github/fnguide_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
