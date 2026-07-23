#!/usr/bin/env python3
"""Offline unit test for the follow-up trigger gate (_followup_triggers).

No network calls — feeds fixture raw/flow dicts straight into the gate.
Run: python tests/test_followup_gate.py
"""
import os
import sys
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generate_analysis as ga

TODAY = datetime.datetime.now(ga.KST).date()


def d(days_ago):
    return (TODAY - datetime.timedelta(days=days_ago)).isoformat()


def t(raw=None, flow=None, stock=None, base_px=None):
    return ga._followup_triggers(raw or {}, flow or {}, stock or {}, base_px)


fails = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        fails.append(name)


# 1) recent disclosure with keyword -> hit
trig = t(raw={"dart": {"disclosures": [
    {"date": d(1), "title": "주요사항보고서(유상증자결정)"}]}})
check("disclosure recent keyword", any(x.startswith("disclosure:") for x in trig))

# 2) keyword but older than FOLLOWUP_DISCLOSURE_DAYS -> miss
trig = t(raw={"dart": {"disclosures": [
    {"date": d(30), "title": "주요사항보고서(유상증자결정)"}]}})
check("disclosure old ignored", not trig)

# 3) recent but no keyword -> miss
trig = t(raw={"dart": {"disclosures": [
    {"date": d(0), "title": "기업설명회(IR)개최"}]}})
check("disclosure non-keyword ignored", not trig)

# 4) keyword but unparseable date -> miss, no crash
trig = t(raw={"dart": {"disclosures": [{"date": "", "title": "유상증자결정"}]}})
check("disclosure bad date ignored", not trig)

# 5) short-selling value ratio above threshold -> hit
trig = t(flow={"shorts": [{"pbmnRlim": "12.3"}]})
check("short surge hit", any(x.startswith("short_surge:") for x in trig))

# 6) below threshold -> miss
trig = t(flow={"shorts": [{"pbmnRlim": 4.2}]})
check("short below threshold", not trig)

# 7) garbage values -> no crash
trig = t(flow={"shorts": [{"pbmnRlim": None}, {"pbmnRlim": "n/a"}]})
check("short garbage safe", not trig)

# 8) consensus gap +40% -> hit
trig = t(raw={"research_reports": [{"targetPrice": 150000},
                                   {"targetPrice": 130000}]},
         stock={"basePrice": 100000})
check("consensus gap hit", any(x.startswith("consensus_gap:") for x in trig))

# 9) small gap +10% -> miss
trig = t(raw={"research_reports": [{"targetPrice": 110000}]},
         stock={"basePrice": 100000})
check("consensus small gap", not trig)

# 10) falls back to base_px when stock carries no price (on-demand path)
trig = t(raw={"research_reports": [{"targetPrice": 150000}]}, base_px=100000.0)
check("consensus gap via base_px", any(x.startswith("consensus_gap:") for x in trig))

# 11) negative gap (price above consensus) also triggers via abs()
trig = t(raw={"research_reports": [{"targetPrice": 60000}]},
         stock={"basePrice": 100000})
check("consensus negative gap hit", any(x.startswith("consensus_gap:") for x in trig))

# 12) all three solo trigger classes accumulate — and since the CB disclosure
#     (bear) opposes the +50% consensus gap (bull), a conflict entry is added too
trig = t(raw={"dart": {"disclosures": [{"date": d(2), "title": "전환사채발행결정"}]},
              "research_reports": [{"targetPrice": 150000}]},
         flow={"shorts": [{"pbmnRlim": 15}]}, stock={"basePrice": 100000})
check("combined three solo + conflict",
      len(trig) == 4 and any(x.startswith("conflict:") for x in trig))

# 13) empty inputs -> no trigger, no crash
check("empty inputs safe", not t())


def has_conflict(trig):
    return any(x.startswith("conflict:") for x in trig)


# --- conflict trigger: opposing bull/bear signals gate even below solo thresholds ---

# 14) bull disclosure + soft short surge (8% < solo 10%) -> conflict
#     (공급계약 is also in the solo KW set, so both entries appear)
trig = t(raw={"dart": {"disclosures": [
    {"date": d(1), "title": "단일판매ㆍ공급계약체결"}]}},
         flow={"shorts": [{"pbmnRlim": 8.0}]})
check("conflict bull-disclosure vs soft-short", has_conflict(trig))

# 15) consensus +20% (below solo 30%) + soft short 8% -> conflict, no solo entries
trig = t(raw={"research_reports": [{"targetPrice": 120000}]},
         flow={"shorts": [{"pbmnRlim": 8.0}]}, stock={"basePrice": 100000})
check("conflict gap-up vs soft-short only", has_conflict(trig) and len(trig) == 1)

# 16) bear disclosure (CB) + consensus +20% -> conflict
trig = t(raw={"dart": {"disclosures": [{"date": d(1), "title": "전환사채발행결정"}]},
              "research_reports": [{"targetPrice": 120000}]},
         stock={"basePrice": 100000})
check("conflict bear-disclosure vs gap-up", has_conflict(trig))

# 17) twin institutional/foreign 3-day buying + soft short -> conflict
trend = [{"frgn": 1000, "orgn": 500}, {"frgn": 200, "orgn": 300},
         {"frgn": 50, "orgn": 10}]
trig = t(flow={"shorts": [{"pbmnRlim": 8.0}], "trend": trend})
check("conflict twin-buy vs soft-short", has_conflict(trig))

# 18) twin 3-day selling + bull disclosure -> conflict
trend = [{"frgn": -1000, "orgn": -500}, {"frgn": -200, "orgn": -300},
         {"frgn": -50, "orgn": -10}]
trig = t(raw={"dart": {"disclosures": [{"date": d(1), "title": "무상증자결정"}]}},
         flow={"trend": trend})
check("conflict twin-sell vs bull-disclosure", has_conflict(trig))

# 19) one-sided signals only (bull + bull) -> no conflict
trig = t(raw={"dart": {"disclosures": [{"date": d(1), "title": "무상증자결정"}]},
              "research_reports": [{"targetPrice": 120000}]},
         stock={"basePrice": 100000})
check("no conflict when one-sided", not has_conflict(trig))

# 20) mixed-direction streak (not 3 consecutive) -> no flow signal, no conflict
trend = [{"frgn": 1000, "orgn": 500}, {"frgn": -200, "orgn": 300},
         {"frgn": 50, "orgn": 10}]
trig = t(flow={"shorts": [{"pbmnRlim": 8.0}], "trend": trend})
check("no conflict on broken streak", not has_conflict(trig))

# 21) trend rows with None values -> safely ignored
trend = [{"frgn": None, "orgn": 500}, {"frgn": 200, "orgn": 300},
         {"frgn": 50, "orgn": 10}]
trig = t(flow={"trend": trend})
check("trend None safe", not trig)

# 22) old bull disclosure does not feed conflict either
trig = t(raw={"dart": {"disclosures": [{"date": d(30), "title": "무상증자결정"}]}},
         flow={"shorts": [{"pbmnRlim": 8.0}]})
check("old disclosure excluded from conflict", not has_conflict(trig))

print()
if fails:
    print(f"{len(fails)} FAILED")
    sys.exit(1)
print("ALL 22 PASSED")
