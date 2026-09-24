#!/usr/bin/env python3
"""KRX 휴장일 캘린더 생성 → data/krx_holidays.json

공공데이터포털 한국천문연구원 특일정보 getRestDeInfo(공휴일·대체공휴일·임시공휴일, 연 단위
1콜)에 KRX 고유 휴장(근로자의날·연말휴장, krx_calendar.krx_extra_holidays)을 더해 올해+내년
목록을 쓴다. Actions(krx_holidays.yml)에서 수동 실행 + 매년 12월 초 자동 실행.

실패 내성: 어느 해의 API 호출이 실패하면 그 해는 기존 파일 값을 유지(없으면 KRX 규칙만),
전부 실패면 파일을 건드리지 않고 종료코드 1. 키 미설정은 종료코드 2.
Actions 해외 IP 접근성: apis.data.go.kr 은 fetch_econ_calendar(관세청)로 실측 완료.

사용: python fetch_krx_holidays.py [--year YYYY] [--out PATH] [--key KEY]
Env:  DATA_GO_KR_KEY (관세청 수출입 통계와 같은 키)
"""
import os
import sys
import json
import argparse
import datetime

import requests

import krx_calendar as kc

RESTDE_URL = "https://apis.data.go.kr/B090041/openapi/service/SpcdeInfoService/getRestDeInfo"
KST = datetime.timezone(datetime.timedelta(hours=9))


def _log(msg):
    print(f"[krx-holidays] {msg}", file=sys.stderr, flush=True)


def _portal_error(resp):
    """공공데이터포털 GW 인증 오류 본문(cmmMsgHeader) → 사유 + 운영자 조치 안내 문자열. 없으면 ""."""
    try:
        h = (resp.json() or {}).get("OpenAPI_ServiceResponse", {}).get("cmmMsgHeader") or {}
    except Exception:
        return ""
    if not h:
        return ""
    code = str(h.get("returnReasonCode") or "")
    hint = {
        "30": "→ DATA_GO_KR_KEY 계정에서 '한국천문연구원_특일 정보' API 활용신청이 필요합니다(관세청 API 와 별도 승인)",
        "20": "→ serviceKey 가 비어 있습니다(DATA_GO_KR_KEY 시크릿 확인)",
        "22": "→ 일일 트래픽 초과",
    }.get(code, "→ 공공데이터포털 마이페이지에서 키·활용신청 상태 확인")
    return f"{h.get('errMsg', '')}({h.get('returnAuthMsg', '')}, code {code}) {hint}"


def fetch_public_holidays(year, key, timeout=20):
    """특일정보 API 1콜(연 단위) → {"YYYY-MM-DD": 이름}. HTTP/인증 오류는 포털 사유를 담아 예외."""
    r = requests.get(RESTDE_URL, params={"serviceKey": key, "solYear": str(year),
                                         "numOfRows": "100", "pageNo": "1", "_type": "json"},
                     timeout=timeout)
    if getattr(r, "status_code", 200) >= 400:
        detail = _portal_error(r)
        try:
            r.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"{e} {detail}".strip()) from None
    return kc.parse_restde(r.json())


def _existing_years(path):
    """기존 파일 → {year: {date: name}} (없거나 깨지면 {})."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f) or {}
        names = doc.get("names") or {}
        out = {}
        for y, days in (doc.get("holidays") or {}).items():
            out[int(y)] = {d: names.get(d, "") for d in (days or [])}
        return out
    except Exception:
        return {}


def build_all(this_year, key, existing_path):
    """올해+내년 휴장일 dict. 반환 ({year: {date: name}}, 실패한 연도 목록)."""
    existing = _existing_years(existing_path)
    years, failed = {}, []
    for y in (this_year, this_year + 1):
        try:
            public = fetch_public_holidays(y, key)
            years[y] = kc.build_year(y, public)
            _log(f"{y}: 공휴일 {len(public)}건 → 휴장일 {len(years[y])}건")
        except Exception as e:
            failed.append(y)
            if y in existing:
                years[y] = existing[y]
                _log(f"{y}: API 실패({e}) — 기존 파일 값 {len(existing[y])}건 유지")
            else:
                years[y] = kc.krx_extra_holidays(y)
                _log(f"{y}: API 실패({e}) — 기존 값 없음, KRX 규칙 {len(years[y])}건만")
    return years, failed


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=datetime.datetime.now(KST).year)
    ap.add_argument("--out", default=kc.DEFAULT_PATH)
    ap.add_argument("--key", default=None)
    a = ap.parse_args(argv)
    key = a.key if a.key is not None else os.environ.get("DATA_GO_KR_KEY", "")
    if not key:
        _log("DATA_GO_KR_KEY 미설정 — 종료(파일 무변경)")
        return 2
    years, failed = build_all(a.year, key, a.out)
    if len(failed) == 2:
        _log("올해·내년 모두 API 실패 — 파일 무변경")
        return 1
    kc.save_holidays(a.out, years, source="data.go.kr 특일정보(getRestDeInfo) + KRX 규칙(근로자의날·연말휴장)")
    _log(f"저장: {a.out} ({', '.join(f'{y}:{len(v)}건' for y, v in sorted(years.items()))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
