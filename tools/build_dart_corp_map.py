#!/usr/bin/env python3
"""Build the static stock_code -> corp_code map from DART corpCode.xml.

The deep-research code (analysis/sources.py) maps a 6-digit KRX stock code to its
8-digit DART corp_code. The live source is corpCode.xml — a ~tens-of-MB zip of ALL
DART-registered corps. Downloading it on every Vercel cold start would blow the
serverless time budget, so we pre-build a small JSON map of just the LISTED stocks
and bundle it at public/assets/dart_corp_map.json (loaded by sources._load_corp_map).

Run once locally (or in CI) with your DART key, then commit the output:

    # PowerShell
    $env:DART_API_KEY="xxxx"; python tools/build_dart_corp_map.py
    # bash
    DART_API_KEY=xxxx python tools/build_dart_corp_map.py

Re-run after KRX listing changes (new IPOs / delistings) to refresh.
"""
import io
import os
import sys
import json
import zipfile
import xml.etree.ElementTree as ET

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "public", "assets", "dart_corp_map.json")
OUT_NAMES = os.path.join(ROOT, "public", "assets", "krx_listed_names.json")


def _norm_listed_name(s):
    """상장사명 정규화 — generate_intraday_briefing._norm_listed_name 과 동일 규칙 유지."""
    s = "".join(ch for ch in str(s or "") if ch not in " \t·ㆍ・")
    for tok in ("(주)", "주식회사"):
        s = s.replace(tok, "")
    return s.upper()


def main():
    key = os.environ.get("DART_API_KEY")
    if not key:
        print("ERROR: DART_API_KEY environment variable not set", file=sys.stderr)
        sys.exit(1)

    print("Downloading corpCode.xml from DART ...")
    r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                     params={"crtfc_key": key}, timeout=60)
    r.raise_for_status()

    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml = zf.read(zf.namelist()[0])
    root = ET.fromstring(xml)

    m, names = {}, {}
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        name = (el.findtext("corp_name") or "").strip()
        if stock and corp and stock.isdigit():   # only listed stocks have a stock_code
            m[stock.zfill(6)] = corp
            if name:                              # 정규화명 → 종목코드 (상장 검증용)
                names[_norm_listed_name(name)] = stock.zfill(6)

    if not m:
        print("ERROR: no listed stock codes parsed — aborting", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    print(f"Wrote {len(m)} listed stock_code->corp_code entries to {OUT}")
    # 상장사 정규화명→종목코드 맵 — 장중 촉매의 '한국 상장 종목' 결정적 검증용
    # (generate_intraday_briefing 이 뉴스발 촉매의 해외 종목 혼입을 차단하는 데 사용).
    with open(OUT_NAMES, "w", encoding="utf-8") as f:
        json.dump(names, f, ensure_ascii=False)
    print(f"Wrote {len(names)} listed name->stock_code entries to {OUT_NAMES}")


if __name__ == "__main__":
    main()
