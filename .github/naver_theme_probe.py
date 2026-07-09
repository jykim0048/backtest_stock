"""네이버 테마·급등급락 소스 Actions IP 접근성 프로브 (장중 시황판 사전 검증).

장중 시황 파이프라인(generate_intraday_briefing)의 3개 소스가 GitHub Actions
해외 데이터센터 IP 에서 실데이터를 반환하는지 확인한다 — KRX 는 해외 IP 를
차단하므로 네이버 쪽도 페이지 단위로 실측이 필요하다(도메인 자체는
naver_flow_probe 로 검증됐지만 페이지별 정책이 다를 수 있음).
결과는 .github/naver_theme_probe_result.json.
"""
import json
import re
import datetime

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def main():
    res = {"asof": datetime.datetime.now(datetime.timezone.utc).isoformat()}

    # ① 테마 랭킹 페이지 (EUC-KR HTML) — 테마행(sise_group_detail 링크) 개수로 판정
    theme_no = None
    try:
        r = requests.get("https://finance.naver.com/sise/theme.naver",
                         headers=UA, timeout=20)
        html = r.content.decode("euc-kr", errors="replace")
        links = re.findall(r'sise_group_detail\.naver\?type=theme&no=(\d+)', html)
        res["themeList"] = {"status": r.status_code, "size": len(html),
                            "themeLinks": len(links), "hasData": len(links) >= 20}
        theme_no = links[0] if links else None
    except Exception as e:
        res["themeList"] = {"error": str(e), "hasData": False}

    # ② 테마 상세 (구성종목) — 첫 테마의 종목 링크 개수로 판정
    try:
        no = theme_no or "556"
        r = requests.get("https://finance.naver.com/sise/sise_group_detail.naver",
                         params={"type": "theme", "no": no}, headers=UA, timeout=20)
        html = r.content.decode("euc-kr", errors="replace")
        codes = re.findall(r'/item/main\.naver\?code=(\d{6})', html)
        res["themeDetail"] = {"status": r.status_code, "no": no,
                              "stockLinks": len(set(codes)), "hasData": len(set(codes)) >= 3}
    except Exception as e:
        res["themeDetail"] = {"error": str(e), "hasData": False}

    # ③ 급등/급락 랭킹 API (모바일 JSON)
    for key, path in (("rankUp", "up/KOSPI"), ("rankDown", "down/KOSDAQ")):
        try:
            r = requests.get(f"https://m.stock.naver.com/api/stocks/{path}",
                             params={"page": 1, "pageSize": 10}, headers=UA, timeout=15)
            r.raise_for_status()
            d = r.json()
            rows = d.get("stocks") if isinstance(d, dict) else d
            rows = rows or []
            has = len(rows) >= 5 and "fluctuationsRatio" in (rows[0] or {})
            res[key] = {"status": r.status_code, "rows": len(rows), "hasData": has}
        except Exception as e:
            res[key] = {"error": str(e), "hasData": False}

    res["ok"] = all(v.get("hasData") for k, v in res.items()
                    if isinstance(v, dict) and k != "asof")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    with open(".github/naver_theme_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
