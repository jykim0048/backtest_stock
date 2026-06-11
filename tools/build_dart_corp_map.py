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

    m = {}
    for el in root.iter("list"):
        stock = (el.findtext("stock_code") or "").strip()
        corp = (el.findtext("corp_code") or "").strip()
        if stock and corp and stock.isdigit():   # only listed stocks have a stock_code
            m[stock.zfill(6)] = corp

    if not m:
        print("ERROR: no listed stock codes parsed — aborting", file=sys.stderr)
        sys.exit(1)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False)
    print(f"Wrote {len(m)} listed stock_code->corp_code entries to {OUT}")


if __name__ == "__main__":
    main()
