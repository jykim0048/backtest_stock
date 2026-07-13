#!/usr/bin/env python3
"""KRX 전종목 업종분류 엑셀 → KOSPI 섹터→대표 구성종목 매핑 JSON 생성.

KRX 데이터포털 [업종분류 현황] 다운로드 엑셀(컬럼: 종목코드·종목명·시장구분·업종명·
종가·대비·등락률·시가총액)을 입력받아, 장중 시황의 KIS 업종 섹터 히트에 '관련주'를
붙이기 위한 매핑을 만든다. KIS 업종 지수(FHPUP02140000)와 KRX 업종분류는 같은 KRX
산업 분류라 업종명이 대체로 일치하지만, 표기 차(공백·중점·'기/기기')가 있어 정규화 키로
저장한다(파이프라인이 KIS 업종명도 같은 규칙으로 정규화해 조인).

사용:
  python build_krx_sector_map.py <KOSPI_xlsx> [--out public/assets/krx_sector_map.json]

KRX 분류는 자주 바뀌지 않으므로 산출 JSON 만 커밋하고, 갱신 시 재다운로드→재실행한다.
"""
import os
import re
import sys
import json
import argparse

import openpyxl

ROOT = os.path.dirname(os.path.abspath(__file__))
TOP_PER_SECTOR = 12   # 섹터당 시총 상위 N개 보관(파이프라인이 4개 사용, 여유분 확보)


def norm_sector(name):
    """업종명 정규화 키 — 공백·중점(·・)·괄호 제거. KIS/KRX 표기차 흡수용.
    '의료·정밀기기'/'의료·정밀기' 처럼 '기기/기' 꼬리 차이는 파이프라인의 접두 매칭이 흡수."""
    return re.sub(r"[\s·・()]", "", str(name or "")).strip()


# 시장구분 → 저장 필드. KOSPI 는 표시·지수 정합용(stocks), KOSDAQ 은 테마 매칭
# 확장 전용(kosdaqStocks) — 시총 스케일이 달라 통합 정렬하면 컷(top12)이 왜곡되므로
# 시장별로 각각 top12 를 보관한다(2026-07-13, 기계·장비/제약 케이스).
_MARKET_FIELD = {"KOSPI": "stocks", "KOSDAQ": "kosdaqStocks"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xlsx", nargs="+",
                    help="KRX 업종분류 엑셀 경로(코스피/코스닥, 여러 파일 가능)")
    ap.add_argument("--out", default=os.path.join(ROOT, "public", "assets", "krx_sector_map.json"))
    ap.add_argument("--date", default="")
    args = ap.parse_args()

    # 입력 엑셀에 없는 시장은 기존 맵에서 이월한다 — 예: 코스닥 엑셀만 새로 받아
    # kosdaqStocks 만 갱신하고 기존 KOSPI stocks 는 유지.
    by_sector, covered = {}, set()
    try:
        with open(args.out, encoding="utf-8") as f:
            for k, e in ((json.load(f) or {}).get("sectors") or {}).items():
                by_sector[k] = {fld: list(v) if isinstance(v, list) else v
                                for fld, v in e.items()}
    except Exception:
        pass

    raw = {}   # (key, field) → [{code,name,cap}]
    for path in args.xlsx:
        wb = openpyxl.load_workbook(path, data_only=True)
        for row in wb.active.iter_rows(min_row=2, values_only=True):
            code, name, market, upjong = row[0], row[1], row[2], row[3]
            field = _MARKET_FIELD.get(str(market or "").strip().upper())
            if not field or not code or not upjong:
                continue
            try:
                cap = float(str(row[7]).replace(",", "")) if row[7] is not None else 0.0
            except (ValueError, TypeError):
                cap = 0.0
            key = norm_sector(upjong)
            covered.add(field)
            by_sector.setdefault(key, {"name": str(upjong).strip()})
            raw.setdefault((key, field), []).append(
                {"code": str(code).strip().zfill(6), "name": str(name).strip(), "cap": cap})

    for (key, field), stocks in raw.items():
        stocks.sort(key=lambda s: s["cap"], reverse=True)
        by_sector[key][field] = [{"code": s["code"], "name": s["name"]}
                                 for s in stocks[:TOP_PER_SECTOR]]

    date = args.date
    if not date:
        m = re.search(r"(\d{8})", os.path.basename(args.xlsx[0]))
        date = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}" if m else ""

    out = {"date": date,
           "note": "KRX 업종분류 — KIS 업종 섹터 히트의 관련주 매핑. 정규화 업종명 키. "
                   "stocks=KOSPI 시총상위(표시·지수 정합), kosdaqStocks=KOSDAQ 시총상위(테마 매칭 확장).",
           "sectors": by_sector}
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    n_k = sum(len(e.get("stocks") or []) for e in by_sector.values())
    n_q = sum(len(e.get("kosdaqStocks") or []) for e in by_sector.values())
    print(f"  Updated {args.out} - {len(by_sector)} sectors, "
          f"KOSPI {n_k} / KOSDAQ {n_q} stocks (top {TOP_PER_SECTOR}, 갱신: {sorted(covered)})")


if __name__ == "__main__":
    main()
