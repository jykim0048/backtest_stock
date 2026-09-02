"""투자주의/경고 표출 범위 회귀 — 경고·위험 지수 필터 제거(NHN 누락, 2026-09-02) (오프라인).

실행: python tests/test_invwarn_scope.py
픽스처는 2026-09-02 KIND menu2 실응답(debug_kind_html_menu2.txt)에서 발췌.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fetch_investment_warning as fi

# KIND 투자경고(menu2) 실구조 발췌: NHN(유가증권·지수 미편입) + 안지오랩(코넥스)
# + 가상의 지수 편입 종목(성호전자, Q150·X300 뱃지) + 해제된 행.
MENU2_HTML = """
<table class="list">
<tbody>
<tr><td class="first txc">8</td>
<td title="NHN"><img src='/images/common/icn_t_yu.gif' alt='유가증권'>
<a id="companysum" href="#" onclick="companysummary_open('18171'); return false;" title='NHN'> NHN</a></td>
<td class="txc">2026-08-21</td><td class="txc">2026-08-24</td><td class="txc">-</td></tr>
<tr><td class="first txc">7</td>
<td title="안지오랩"><img src='/images/common/icn_t_konex.gif' alt='코넥스'>
<a id="companysum" href="#" onclick="companysummary_open('25128'); return false;" title='안지오랩'> 안지오랩</a></td>
<td class="txc">2026-08-20</td><td class="txc">2026-08-21</td><td class="txc">-</td></tr>
<tr><td class="first txc">6</td>
<td title="성호전자"><img src='/images/common/icn_t_ko.gif' alt='코스닥'>
<img src='/x.gif' alt='KOSDAQ150'><img src='/y.gif' alt='KRX300'>
<a id="companysum" href="#" onclick="companysummary_open('04326'); return false;" title='성호전자'> 성호전자</a></td>
<td class="txc">2026-08-18</td><td class="txc">2026-08-19</td><td class="txc">2026-09-02</td></tr>
</tbody>
</table>
"""


def main():
    rows = fi._parse_html(MENU2_HTML, menu_idx="2", default_reason="투자경고")
    assert len(rows) == 3, rows
    by = {r["name"]: r for r in rows}

    # 파싱: NHN 은 지수 뱃지 없음, 성호전자는 Q150 X300, 해제일 정규화("-"→"")
    assert by["NHN"]["code"] == "181710" and by["NHN"]["index"] == ""
    assert by["NHN"]["market"] == "유가증권" and by["NHN"]["release"] == ""
    assert by["성호전자"]["index"] == "Q150 X300"
    assert by["성호전자"]["release"] == "2026-09-02"   # 해제됨 → 현재 지정 필터 대상
    assert by["안지오랩"]["market"] == "코넥스"

    # 경고·위험 범위: 코넥스만 제외 — 지수 미편입 NHN 이 살아남아야 함(핵심 회귀)
    kept = fi._scope_filter(rows, "warning")
    names = {r["name"] for r in kept}
    assert names == {"NHN", "성호전자"}, names
    assert {r["name"] for r in fi._scope_filter(rows, "danger")} == {"NHN", "성호전자"}

    # 투자주의 범위: 지수 편입만 (기존 동작 유지)
    assert {r["name"] for r in fi._scope_filter(rows, "caution")} == {"성호전자"}

    # 현재 지정 중 필터(main ③)와의 결합: 해제된 성호전자는 최종 제외 → NHN 만
    active = [r for r in fi._scope_filter(rows, "warning") if not r.get("release")]
    assert [r["name"] for r in active] == ["NHN"]

    # ── 시총 하한 게이트(③b, 기본 1조) ──────────────────────────────────────
    caps = {"181710.KS": 23000.0, "001210.KS": 800.0, "153890.KQ": None}
    fetch = lambda t: caps.get(t)
    gate_rows = [
        {"name": "NHN", "code": "181710", "ticker": "181710.KS", "index": ""},       # 비편입·대형
        {"name": "금호전기", "code": "001210", "ticker": "001210.KS", "index": ""},   # 비편입·소형
        {"name": "져스텍", "code": "153890", "ticker": "153890.KQ", "index": ""},     # 조회 실패
        {"name": "성호전자", "code": "043260", "ticker": "043260.KQ",
         "index": "Q150 X300"},                                                       # 지수 편입·소형
    ]
    assert fi.MIN_MCAP_EOK == 10000.0   # 기본값(env 미설정 실행 기준)
    kept = fi._mcap_gate([dict(r) for r in gate_rows], "warning", fetch=fetch)
    names = [r["name"] for r in kept]
    # 대형 통과(+mcap 스탬프)·소형 제외·조회 실패 유지(fail-open)·지수 편입 무조건 유지
    assert names == ["NHN", "져스텍", "성호전자"], names
    assert kept[0]["mcap_eok"] == 23000 and "mcap_eok" not in kept[2]

    # 투자주의는 게이트 미적용(원본 그대로), 노브 0 이하 = 비활성
    assert fi._mcap_gate(gate_rows, "caution", fetch=fetch) == gate_rows
    orig = fi.MIN_MCAP_EOK
    try:
        fi.MIN_MCAP_EOK = 0
        assert fi._mcap_gate(gate_rows, "warning", fetch=fetch) == gate_rows
    finally:
        fi.MIN_MCAP_EOK = orig

    print("ALL PASS (invwarn scope filter + mcap gate)")


if __name__ == "__main__":
    main()
