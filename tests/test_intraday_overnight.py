"""장중 첫 회차 DART 조회 범위 확장(직전 거래일 + 당일)·전일 아카이브 중복 제거 회귀(오프라인, 2026-10-15).

실행: python tests/test_intraday_overnight.py
"""
import datetime
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import generate_intraday_briefing as g
from analysis import sources


def main():
    tmp = tempfile.mkdtemp()
    g.ARCHIVE_DIR = tmp
    KST = datetime.timezone(datetime.timedelta(hours=9))
    now = datetime.datetime(2026, 10, 14, 8, 37, tzinfo=KST)        # 수요일 첫 회차
    # 직전 거래일: 10/13(화). 주말 건너뜀: 10/13(월 아님) → 10/12 월요일 08:37 이면 10/10(금)
    assert g._prev_trading_day(datetime.date(2026, 10, 14)) == datetime.date(2026, 10, 13)
    assert g._prev_trading_day(datetime.date(2026, 10, 12)) == datetime.date(2026, 10, 9) or \
        g._prev_trading_day(datetime.date(2026, 10, 12)).weekday() < 5
    # 당일 아카이브 없음 → 첫 회차 → bgn = 전일
    assert g._overnight_bgn(now) == "20261013"
    # 당일 아카이브에 회차가 있으면 None(당일만)
    with open(os.path.join(tmp, "2026-10-14.json"), "w", encoding="utf-8") as f:
        json.dump({"date": "2026-10-14", "rounds": [{"asof": "x"}]}, f)
    assert g._overnight_bgn(now) is None
    os.remove(os.path.join(tmp, "2026-10-14.json"))
    # 전일 아카이브의 공시 url 은 중복 제외, 그 외 전일 접수분은 overnight 표시
    with open(os.path.join(tmp, "2026-10-13.json"), "w", encoding="utf-8") as f:
        json.dump({"date": "2026-10-13", "rounds": [{"disclosures": [{"url": "u-dup"}],
                                                      "catalysts": [{"kind": "disclosure", "url": "u-cat"}]}]}, f)
    rows = [{"corp": "A", "date": "20261013", "url": "u-dup", "title": "유상증자결정"},
            {"corp": "B", "date": "20261013", "url": "u-cat", "title": "전환사채권발행결정"},
            {"corp": "C", "date": "20261013", "url": "u-new", "title": "주요사항보고서(교환사채권발행결정)"},
            {"corp": "D", "date": "20261014", "url": "u-today", "title": "단일판매공급계약"}]
    orig = sources.dart_today_disclosures
    sources.dart_today_disclosures = lambda limit=40, bgn_de=None, **k: [dict(r) for r in rows] if bgn_de else [rows[-1]]
    try:
        out = g._fetch_disclosures(now)
        assert [r["corp"] for r in out] == ["C", "D"], out
        assert out[0].get("overnight") is True and not out[1].get("overnight")
        # 두 번째 회차(아카이브 있음)는 당일만
        with open(os.path.join(tmp, "2026-10-14.json"), "w", encoding="utf-8") as f:
            json.dump({"date": "2026-10-14", "rounds": [{"asof": "x"}]}, f)
        out2 = g._fetch_disclosures(now.replace(hour=9, minute=7))
        assert [r["corp"] for r in out2] == ["D"], out2
    finally:
        sources.dart_today_disclosures = orig
    print("OK test_intraday_overnight")


if __name__ == "__main__":
    main()
