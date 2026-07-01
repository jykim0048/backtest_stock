#!/usr/bin/env python3
"""Batch: sector & theme analysis -> public/sector_analysis.json (+ archive).

Reads the morning briefing (public/briefing/latest.json), derives the US surge
themes' top KR names from its flow[], and runs the 5-phase sector-theme workflow
(analysis.sector.analyze_sectors) to produce the dashboard's sector_analysis.json.

Mirrors generate_report.py conventions: KST date, ensure_ascii=False JSON,
dated archive + index.json. Env knobs:
  SECTOR_BRIEFING  input briefing json   (default public/briefing/latest.json)
  SECTOR_OUT       latest output json    (default public/sector_analysis.json)
  SECTOR_ARCHIVE   "0" to skip archive   (default archive on)
"""
import os
import sys
import json
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from analysis import sector

KST = datetime.timezone(datetime.timedelta(hours=9))

BRIEFING_PATH = os.environ.get(
    "SECTOR_BRIEFING", os.path.join(ROOT, "public", "briefing", "latest.json"))
OUT_PATH = os.environ.get(
    "SECTOR_OUT", os.path.join(ROOT, "public", "sector_analysis.json"))
ARCHIVE = os.environ.get("SECTOR_ARCHIVE", "1") != "0"
REPORTS_DIR = os.path.join(ROOT, "public", "reports", "sector")
KRX_MASTER = os.path.join(ROOT, "public", "assets", "krx_companies.json")


# --- KRX master resolver (same shape as railway_server.resolve) --------------
def _load_master():
    by_code, by_name = {}, {}
    try:
        with open(KRX_MASTER, encoding="utf-8") as f:
            for c in json.load(f):
                code = str(c.get("code", "")).zfill(6)
                name = (c.get("name") or "").strip()
                if not code or not name:
                    continue
                raw = c.get("market", "") or ""
                market = "KOSPI" if "유가" in raw else ("KOSDAQ" if "코스닥" in raw else "KOSPI")
                entry = {"code": code, "name": name, "market": market}
                by_code[code] = entry
                by_name.setdefault(name, entry)
    except Exception as ex:
        print(f"[sector] master load failed: {ex}", file=sys.stderr)
    return by_code, by_name


def make_resolver():
    by_code, by_name = _load_master()

    def resolve(q):
        q = (q or "").strip()
        if not q:
            return None
        if q.isdigit():
            return by_code.get(q.zfill(6))
        if q in by_name:
            return by_name[q]
        ql = q.lower()
        cands = [e for n, e in by_name.items() if ql in n.lower()]
        if cands:
            cands.sort(key=lambda e: len(e["name"]))
            return cands[0]
        return None

    return resolve


def generate_sector():
    print("=== Sector & Theme Analysis ===")
    with open(BRIEFING_PATH, encoding="utf-8") as f:
        briefing = json.load(f)
    print(f"  Briefing      : {BRIEFING_PATH} (date={briefing.get('date')})")

    today = datetime.datetime.now(KST).strftime("%Y-%m-%d")
    result = sector.analyze_sectors(briefing, resolve_fn=make_resolver())
    result["date"] = result.get("date") or today

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"  Updated       : {OUT_PATH}  (regime={result['macro']['regime']}, "
          f"F&G={result['sentiment']['score']}, model={result['generatedBy']})")

    if ARCHIVE:
        os.makedirs(REPORTS_DIR, exist_ok=True)
        dated_path = os.path.join(REPORTS_DIR, f"{result['date']}.json")
        with open(dated_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"  Saved archive : {dated_path}")

        index_path = os.path.join(REPORTS_DIR, "index.json")
        index = []
        if os.path.exists(index_path):
            with open(index_path, encoding="utf-8") as f:
                index = json.load(f)
        if result["date"] not in index:
            index.append(result["date"])
            index.sort(reverse=True)
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        print(f"  Updated index : {index_path} ({len(index)} entries)")
    print("=== Done ===")


if __name__ == "__main__":
    generate_sector()
