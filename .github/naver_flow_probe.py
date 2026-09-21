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

    res["trendApiOk"] = all(res[f"trendApi:{c}"].get("hasData") for c in CODES)
    res["frgnHtmlOk"] = all(res[f"frgnHtml:{c}"].get("hasData") for c in CODES)
    # 판정: 요청은 되는데 행이 0 → 구조 변경 / 요청 실패·리다이렉트 → 차단 의심
    tl = res["timeline:asis"]
    res["timelineVerdict"] = (
        "요청실패(차단·오류 의심)" if tl.get("error") or (tl.get("status") or 0) >= 400
        else "정상(행 수집됨)" if tl.get("rows")
        else "구조변경 의심(200·행 0)")
    print(json.dumps(res, indent=2, ensure_ascii=False))
    with open(".github/naver_flow_probe_result.json", "w", encoding="utf-8") as f:
        json.dump(res, f, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
