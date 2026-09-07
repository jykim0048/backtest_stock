"""report_db 경로 매핑 회귀 테스트 (오프라인 — DB 연결 없이 classify/paths_for 만).

대시보드 fetch 경로 ↔ DB 키 매핑이 어긋나면 리포트가 조용히 raw 폴백으로
흘러 마이그레이션이 무효가 되므로, 실제 사용 중인 모든 경로 패턴을 고정한다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import report_db


def test_classify_daily_archive():
    assert report_db.classify("reports/2026-09-07.json") == ("report", "daily", "2026-09-07")
    assert report_db.classify("reports/index.json") == ("index", "daily", None)


def test_classify_kind_archives():
    cases = {
        "reports/intraday/2026-09-07.json": ("report", "intraday", "2026-09-07"),
        "reports/intraday_briefing/2026-09-07.json": ("report", "intraday_briefing", "2026-09-07"),
        "reports/eod/2026-09-07.json": ("report", "eod", "2026-09-07"),
        "reports/down/2026-09-07.json": ("report", "down", "2026-09-07"),
        "reports/intraday_down/2026-09-07.json": ("report", "intraday_down", "2026-09-07"),
        "reports/invwarn/2026-09-07.json": ("report", "invwarn", "2026-09-07"),
        "reports/sector/2026-09-07.json": ("report", "sector", "2026-09-07"),
        "reports/selection/2026-09-07.json": ("report", "selection", "2026-09-07"),
        "reports/selection/intraday/2026-09-07.json": ("report", "selection/intraday", "2026-09-07"),
        "reports/selection/intraday/index.json": ("index", "selection/intraday", None),
    }
    for rel, want in cases.items():
        assert report_db.classify(rel) == want, rel


def test_classify_briefing():
    assert report_db.classify("briefing/2026-09-07.json") == ("report", "briefing", "2026-09-07")
    assert report_db.classify("briefing/index.json") == ("index", "briefing", None)
    # latest.json 은 날짜가 아니므로 스냅샷으로 취급
    assert report_db.classify("briefing/latest.json") == ("snapshot", "briefing/latest.json", None)


def test_classify_snapshots():
    for rel in ("intraday_report.json", "daily_market_report.json",
                "intraday_briefing.json", "sector_analysis.json",
                "econ_calendar.json", "earnings_calendar.json", "us_catalysts.json",
                "data/investment_warning.json"):
        assert report_db.classify(rel) == ("snapshot", rel, None), rel


def test_classify_excluded():
    # 에셋(준정적)·비 JSON·이상 파일명은 DB 비대상
    assert report_db.classify("assets/krx_sector_map.json") is None
    assert report_db.classify("index.html") is None
    assert report_db.classify("reports/notadate.json") is None
    assert report_db.classify("data/debug_kind_html_menu3.txt") is None


def test_paths_for_roundtrip():
    # (kind, date) → 경로 → classify 왕복이 원래 키로 돌아와야 캐시 무효화가 맞다
    for kind in ("daily", "briefing", "intraday", "selection/intraday"):
        report_path, index_path = report_db.paths_for(kind, "2026-09-07")
        assert report_db.classify(report_path) == ("report", kind, "2026-09-07"), kind
        assert report_db.classify(index_path) == ("index", kind, None), kind
