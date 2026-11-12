"""krx_companies.json 자동 갱신(tools/build_krx_companies.merge) — 오프라인.

실행: python -m unittest tests.test_krx_companies_refresh
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import build_krx_companies as b          # noqa: E402
import build_sector_map_auto as bsm      # noqa: E402

EXISTING = [
    {"name": "현대자동차", "code": "005380", "market": "유가", "ticker": "005380.KS"},
    {"name": "한진칼", "code": "180640", "market": "유가", "ticker": "180640.KS"},
    {"name": "옛사명", "code": "111110", "market": "코스닥", "ticker": "111110.KQ"},
    {"name": "이전상장", "code": "222220", "market": "코스닥", "ticker": "222220.KQ"},
    {"name": "상폐예정", "code": "333330", "market": "코스닥", "ticker": "333330.KQ"},
    {"name": "코넥스사", "code": "444440", "market": "코넥스", "ticker": "444440.KS"},
]
DART = {"005380": "현대자동차", "180640": "한진칼", "111110": "새사명", "222220": "이전상장",
        "0220W0": "한화머시너리앤서비스홀딩스", "180645": "한진칼"}


def kis(*extra, drop=()):
    rows = [
        {"code": "005380", "name": "현대차", "market": "유가", "pref": False},
        {"code": "180640", "name": "한진칼", "market": "유가", "pref": False},
        {"code": "111110", "name": "새사명", "market": "코스닥", "pref": False},
        {"code": "222220", "name": "이전상장", "market": "유가", "pref": False},
        {"code": "333330", "name": "상폐예정", "market": "코스닥", "pref": False},
        {"code": "444440", "name": "코넥스사", "market": "코넥스", "pref": False},
    ] + list(extra)
    return [r for r in rows if r["code"] not in drop]


class TestMerge(unittest.TestCase):
    def test_new_listing_alnum_and_pref(self):
        rows, h, rep = b.merge(EXISTING, kis(
            {"code": "0220W0", "name": "한화M&S", "market": "유가", "pref": False},
            {"code": "180645", "name": "한진칼우", "market": "유가", "pref": True}),
            DART, b.new_history(), "2026-11-13")
        by = {r["code"]: r for r in rows}
        self.assertEqual(by["0220W0"], {"name": "한화머시너리앤서비스홀딩스", "code": "0220W0",
                                        "market": "유가", "ticker": "0220W0.KS"})
        # 우선주: DART 이름(보통주 회사명)이 아니라 KIS 이름, 보통주와 같은 시장, pref:true
        self.assertEqual(by["180645"], {"name": "한진칼우", "code": "180645", "market": "유가",
                                        "ticker": "180645.KS", "pref": True})
        self.assertEqual(len(rep["added"]), 2)
        self.assertEqual(rows[:len(EXISTING)][0], EXISTING[0])          # 기존 행·순서 보존

    def test_existing_format_preserved_and_kis_short_name_ignored(self):
        rows, h, rep = b.merge(EXISTING, kis(), DART, b.new_history(), "2026-11-13")
        self.assertEqual(rows[0], EXISTING[0])                          # 현대차(KIS 약명)로 안 바뀜
        self.assertEqual(list(rows[0]), ["name", "code", "market", "ticker"])
        self.assertEqual(h["kisNames"]["005380"], "현대차")

    def test_transfer(self):
        rows, h, rep = b.merge(EXISTING, kis(), DART, b.new_history(), "2026-11-13")
        r = next(x for x in rows if x["code"] == "222220")
        self.assertEqual((r["market"], r["ticker"]), ("유가", "222220.KS"))
        self.assertEqual(h["transfers"][0]["from"], "코스닥")

    def test_rename_kis_mode_needs_previous_kis_name(self):
        # 첫 실행(직전 KIS 이름 없음) → 사명변경 안 함
        rows, h, rep = b.merge(EXISTING, kis(), DART, b.new_history(), "2026-11-13")
        self.assertEqual(next(x for x in rows if x["code"] == "111110")["name"], "옛사명")
        self.assertEqual(rep["renamed"], [])
        # 다음 실행에서 KIS 이름이 바뀌면 DART 이름으로 갱신 + 옛 이름 보존
        h["kisNames"]["111110"] = "옛사명"
        rows2, h2, rep2 = b.merge(rows, kis(), DART, h, "2026-11-14")
        self.assertEqual(next(x for x in rows2 if x["code"] == "111110")["name"], "새사명")
        self.assertEqual(h2["renames"][-1], {"code": "111110", "old": "옛사명", "new": "새사명",
                                             "date": "2026-11-14"})

    def test_rename_dart_and_off(self):
        rows, h, rep = b.merge(EXISTING, kis(), DART, b.new_history(), "2026-11-13", rename="dart")
        self.assertEqual(next(x for x in rows if x["code"] == "111110")["name"], "새사명")
        rows, h, rep = b.merge(EXISTING, kis(), DART, {"kisNames": {"111110": "옛사명"}},
                               "2026-11-13", rename="off")
        self.assertEqual(rep["renamed"], [])

    def test_delist_needs_two_runs_on_different_days(self):
        rows, h, rep = b.merge(EXISTING, kis(drop={"333330"}), DART, b.new_history(), "2026-11-13")
        self.assertIn("333330", {r["code"] for r in rows})             # 1회 누락 → 유지
        self.assertEqual(h["pendingDelist"], {"333330": "2026-11-13"})
        rows_same, h_same, _ = b.merge(rows, kis(drop={"333330"}), DART, h, "2026-11-13")
        self.assertIn("333330", {r["code"] for r in rows_same})        # 같은 날 재실행 → 유지
        rows2, h2, rep2 = b.merge(rows, kis(drop={"333330"}), DART, h, "2026-11-14")
        self.assertNotIn("333330", {r["code"] for r in rows2})
        self.assertEqual(h2["delisted"][0]["code"], "333330")
        self.assertEqual(h2["pendingDelist"], {})

    def test_delist_now_first_bulk_run(self):
        rows, h, rep = b.merge(EXISTING, kis(drop={"333330"}), DART, b.new_history(), "2026-11-13",
                               delist_now=True)
        self.assertNotIn("333330", {r["code"] for r in rows})
        self.assertEqual([d["code"] for d in h["delisted"]], ["333330"])
        self.assertEqual(h["pendingDelist"], {})

    def test_reappear_clears_pending(self):
        rows, h, _ = b.merge(EXISTING, kis(drop={"333330"}), DART, b.new_history(), "2026-11-13")
        rows2, h2, rep2 = b.merge(rows, kis(), DART, h, "2026-11-14")
        self.assertEqual(h2["pendingDelist"], {})
        self.assertIn("333330", {r["code"] for r in rows2})

    def test_konex_failure_keeps_konex_rows(self):
        rows, h, rep = b.merge(EXISTING, kis(drop={"444440"}), DART, b.new_history(), "2026-11-13",
                               konex_ok=False)
        self.assertIn("444440", {r["code"] for r in rows})
        self.assertEqual(h["pendingDelist"], {})
        self.assertEqual(rep["keptKonex"], 1)

    def test_history_input_not_mutated(self):
        hist = b.new_history()
        b.merge(EXISTING, kis(drop={"333330"}), DART, hist, "2026-11-13")
        self.assertEqual(hist, b.new_history())

    def test_limits(self):
        many = [{"code": f"9{i:04d}0", "name": f"신규{i}", "market": "코스닥", "pref": False}
                for i in range(41)]
        rows, h, rep = b.merge(EXISTING, kis(*many), {}, b.new_history(), "2026-11-13")
        self.assertEqual(b.check_limits(rep), ["added 41 > 40"])
        self.assertEqual(b.check_limits(rep, force=True), [])

    def test_counts(self):
        self.assertTrue(b.check_counts(kis(), True))                    # 소량 → 중단 사유 있음
        big = ([{"code": "x", "market": "유가"}] * 800) + ([{"code": "y", "market": "코스닥"}] * 1700)
        self.assertEqual(b.check_counts(big, True), [])

    def test_is_pref(self):
        self.assertTrue(b.is_pref("005935"))
        self.assertTrue(b.is_pref("00680K"))
        self.assertFalse(b.is_pref("0220W0"))
        self.assertFalse(b.is_pref("005930"))


def _mst_line(code, name, group, tail):
    """KIS 마스터 한 행 흉내 — 코드9 + 표준코드12 + 종목명 + 고정부(tail, 첫 2자리 그룹코드)."""
    fixed = (group + "0" * tail)[:tail]
    return code.ljust(9) + ("KR7" + code + "000")[:12].ljust(12) + name + fixed


class TestParseMst(unittest.TestCase):
    def test_groups_filter_and_group_field(self):
        raw = "\n".join([_mst_line("005930", "삼성전자", "ST", 228),
                         _mst_line("005935", "삼성전자우", "ST", 228),
                         _mst_line("330590", "롯데리츠", "RT", 228),
                         _mst_line("069500", "KODEX 200", "EF", 228)])
        st = bsm._parse_mst("kospi_code", raw=raw)
        self.assertEqual([r["code"] for r in st], ["005930", "005935"])  # 기본 = 주권만(동작 불변)
        allg = bsm._parse_mst("kospi_code", raw=raw, groups=None)
        self.assertEqual({r["code"]: r["group"] for r in allg},
                         {"005930": "ST", "005935": "ST", "330590": "RT", "069500": "EF"})
        self.assertEqual(next(r for r in allg if r["code"] == "005935")["pref"], True)

    def test_unknown_tail_autodetect(self):
        lines = [_mst_line(f"{i:05d}0", "코넥스" + "가" * (i % 7), "ST", 184) for i in range(40)]
        self.assertEqual(bsm._detect_tail(lines), 184)
        rows = bsm._parse_mst("konex_code", raw="\n".join(lines))
        self.assertEqual(len(rows), 40)
        self.assertTrue(rows[3]["name"].startswith("코넥스"))


if __name__ == "__main__":
    unittest.main()
