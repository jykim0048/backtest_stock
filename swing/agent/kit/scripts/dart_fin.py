"""DART 재무제표 — 분기·전 계정 수집 + 표준화 + 파생 지표 (backtest_stock `dart_financials` 확장판).

DART `fnlttSinglAcntAll.json` 은 보고서 한 건의 전 계정을 준다. 이 모듈은
  1. 최근 N개 보고기간(1분기·반기·3분기·사업보고서)을 최신부터 거슬러 수집하고
  2. 연결(CFS) 없으면 별도(OFS)로 폴백하며
  3. 재무제표 구분(sj_div: BS/IS/CIS/CF)을 존중해 XBRL 표준 ID → 이름 정규식 순으로 계정을 표준화하고
  4. 손익은 3개월값·누적값을 구분, 4분기는 연간−3분기누적, 현금흐름은 누적 차감으로 분기값을 만든 뒤
  5. 마진·부채비율·유동비율·이자보상배율·FCF·YoY·QoQ·TTM 을 코드로 계산한다.

LLM 에는 표준화 결과(핵심 층)만 넘긴다. 전 계정 원본(전체 층)은 --full 일 때 별도 파일로 저장.

사용:  python dart_fin.py <corp_code> [--periods 5] [--full] [--out DIR]
환경:  DART_API_KEY
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

import requests

from _common import warn, write_json

API = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"
TIMEOUT = 20

# reprt_code → (라벨, 기말 월, 누적 개월)
REPORTS = {
    "11013": ("Q1", 3, 3),
    "11012": ("H1", 6, 6),
    "11014": ("Q3", 9, 9),
    "11011": ("FY", 12, 12),
}
_ORDER = ["11013", "11012", "11014", "11011"]          # 연중 시간순
# 보고서 제출 기한(대략): Q1 5/15, 반기 8/14, 3분기 11/14, 사업보고서 익년 3/31
_DEADLINE = {"11013": (5, 20), "11012": (8, 20), "11014": (11, 20), "11011": (3, 31)}

# 표준 계정: key → (sj_div 후보, account_id 후보, 이름 정규식)
_STD = {
    # 재무상태표
    "assets":              (("BS",), ("ifrs-full_Assets",), r"^자산총계$"),
    "liabilities":         (("BS",), ("ifrs-full_Liabilities",), r"^부채총계$"),
    "equity":              (("BS",), ("ifrs-full_Equity",), r"^자본총계$"),
    "equity_parent":       (("BS",), ("ifrs-full_EquityAttributableToOwnersOfParent",), r"지배기업.*소유주.*지분|지배기업소유주지분"),
    "current_assets":      (("BS",), ("ifrs-full_CurrentAssets",), r"^유동자산$"),
    "current_liabilities": (("BS",), ("ifrs-full_CurrentLiabilities",), r"^유동부채$"),
    "cash":                (("BS",), ("ifrs-full_CashAndCashEquivalents",), r"^현금및현금성자산$"),
    "inventories":         (("BS",), ("ifrs-full_Inventories",), r"^재고자산$"),
    "receivables":         (("BS",), ("ifrs-full_TradeAndOtherCurrentReceivables", "ifrs-full_CurrentTradeReceivables"), r"^매출채권(및기타채권)?$"),
    # 손익계산서(포괄손익계산서 폴백)
    "revenue":             (("IS", "CIS"), ("ifrs-full_Revenue",), r"^(매출액|수익\(매출액\)|영업수익|매출|수익)$"),
    "cost_of_sales":       (("IS", "CIS"), ("ifrs-full_CostOfSales",), r"^매출원가$"),
    "gross_profit":        (("IS", "CIS"), ("ifrs-full_GrossProfit",), r"^매출총이익$"),
    "operating_income":    (("IS", "CIS"), ("dart_OperatingIncomeLoss", "ifrs-full_ProfitLossFromOperatingActivities"), r"^영업이익(\(손실\))?$|^영업손익$"),
    "finance_costs":       (("IS", "CIS"), ("ifrs-full_FinanceCosts",), r"^금융(비용|원가)$"),
    "interest_expense":    (("IS", "CIS"), ("ifrs-full_InterestExpense",), r"^이자비용$"),
    "pretax_income":       (("IS", "CIS"), ("ifrs-full_ProfitLossBeforeTax",), r"법인세(비용)?차감전(순)?(이익|손익)"),
    "net_income":          (("IS", "CIS"), ("ifrs-full_ProfitLoss",), r"^(당기|분기|반기)?순(이익|손익)(\(손실\))?$"),
    "net_income_parent":   (("IS", "CIS"), ("ifrs-full_ProfitLossAttributableToOwnersOfParent",), r"지배기업.*순(이익|손익)"),
    # 현금흐름표
    "cfo":                 (("CF",), ("ifrs-full_CashFlowsFromUsedInOperatingActivities",), r"^영업활동.*현금흐름$"),
    "cfi":                 (("CF",), ("ifrs-full_CashFlowsFromUsedInInvestingActivities",), r"^투자활동.*현금흐름$"),
    "cff":                 (("CF",), ("ifrs-full_CashFlowsFromUsedInFinancingActivities",), r"^재무활동.*현금흐름$"),
    "capex":               (("CF",), ("ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",), r"유형자산의?\s*취득"),
}
_FLOW_KEYS = [k for k, v in _STD.items() if v[0][0] in ("IS", "CF")]   # 기간 흐름 계정
_BS_KEYS = [k for k, v in _STD.items() if v[0] == ("BS",)]

EOK = 100_000_000  # 억원


# ----------------------------------------------------------------------------
# 순수 함수 (오프라인 테스트 대상)
# ----------------------------------------------------------------------------
def to_num(s):
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t in ("", "-", "N/A"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", "", s or "")


def standardize(rows: list[dict]) -> dict:
    """전 계정 rows → {key: {"cur": 당기, "cum": 누적, "prev": 전년동기, "prev_cum": 전년누적, "name": 계정명}}.

    같은 key 후보가 여럿이면 표준 ID 일치 > 이름 정규식 순, 그 안에서는 ord 가 작은 행.
    IS 가 없고 CIS 만 있는 회사는 CIS 로 폴백한다(sj_div 후보 순서).
    """
    by_sj: dict[str, list[dict]] = {}
    for r in rows:
        by_sj.setdefault((r.get("sj_div") or "").strip(), []).append(r)
    out = {}
    for key, (sjs, ids, name_re) in _STD.items():
        pat = re.compile(name_re)
        hit = None
        for sj in sjs:
            cand = by_sj.get(sj) or []
            by_id = [r for r in cand if (r.get("account_id") or "").strip() in ids]
            by_nm = [r for r in cand if pat.search(_norm_name(r.get("account_nm")))]
            pool = by_id or by_nm
            if pool:
                hit = min(pool, key=lambda r: to_num(r.get("ord")) or 9e9)
                break
        if hit is None:
            continue
        out[key] = {
            "name": (hit.get("account_nm") or "").strip(),
            "cur": to_num(hit.get("thstrm_amount")),
            "cum": to_num(hit.get("thstrm_add_amount")),
            "prev": to_num(hit.get("frmtrm_amount")),
            "prev_q": to_num(hit.get("frmtrm_q_amount")),
            "prev_cum": to_num(hit.get("frmtrm_add_amount")),
        }
    return out


def period_label(year: int, reprt: str) -> str:
    return f"{year}{REPORTS[reprt][0]}"


def period_end(year: int, reprt: str) -> str:
    m = REPORTS[reprt][1]
    last = {3: 31, 6: 30, 9: 30, 12: 31}[m]
    return f"{year}-{m:02d}-{last:02d}"


def derive_quarters(periods: list[dict]) -> list[dict]:
    """periods: 최신순 [{year, reprt, std}]. 각 기간에 3개월 손익·분기 현금흐름·누적을 붙인다.

    - IS 3개월: 분기·반기·3분기 보고서는 cur 가 3개월값. 사업보고서는 cur 가 연간 → 같은 해 3분기
      누적(cum)이 있으면 연간−누적으로 4분기값. 없으면 None + note.
    - IS 누적: 분기보고서 cum, 사업보고서는 cur.
    - CF: 보고서 값은 항상 누적 → 같은 해 직전 기간 누적 차감. Q1 은 누적 그대로.
    """
    idx = {(p["year"], p["reprt"]): p for p in periods}
    for p in periods:
        y, rc, std = p["year"], p["reprt"], p["std"]
        q3 = idx.get((y, "11014"))
        prev_rc = _ORDER[_ORDER.index(rc) - 1] if rc != "11013" else None
        prev = idx.get((y, prev_rc)) if prev_rc else None
        q, cum, notes = {}, {}, []
        for k in _FLOW_KEYS:
            v = std.get(k)
            if not v:
                continue
            is_cf = _STD[k][0] == ("CF",)
            if is_cf:
                c = v["cur"]                      # 누적
                cum[k] = c
                if rc == "11013":
                    q[k] = c
                elif prev and prev["std"].get(k, {}).get("cur") is not None and c is not None:
                    q[k] = c - prev["std"][k]["cur"]
                else:
                    q[k] = None
                    if c is not None:
                        notes.append(f"{k}: 직전 기간 누적 부재로 분기값 미산출")
            else:
                if rc == "11011":
                    cum[k] = v["cur"]
                    if q3 and q3["std"].get(k, {}).get("cum") is not None and v["cur"] is not None:
                        q[k] = v["cur"] - q3["std"][k]["cum"]
                    else:
                        q[k] = None
                        if v["cur"] is not None:
                            notes.append(f"{k}: 3분기 누적 부재로 4분기값 미산출(연간만)")
                else:
                    q[k] = v["cur"]
                    cum[k] = v["cum"] if v["cum"] is not None else v["cur"]
        if "revenue" not in std and std.get("operating_income"):
            notes.append("revenue: 매출액 계정 없음(금융업 등은 정상) — 마진·매출 성장률 미산출")
        p["quarter"] = q
        p["cumulative"] = cum
        p["notes"] = notes
    return periods


def _yoy_prev(std: dict, key: str, annual: bool):
    """전년동기 값. 사업보고서는 frmtrm_amount(전년 연간).
    분기·반기·3분기 보고서의 DART 응답은 frmtrm_amount 가 비어 있고 전년동기 3개월값이
    frmtrm_q_amount 에 온다(실측 2026-09-22, 삼성전자·루닛·KB금융 공통). 없으면 frmtrm_amount 폴백."""
    v = std.get(key) or {}
    if annual:
        return v.get("prev")
    return v.get("prev_q") if v.get("prev_q") is not None else v.get("prev")


def _ratio(a, b, pct=True):
    if a is None or b in (None, 0):
        return None
    r = a / b
    return round(r * 100, 2) if pct else round(r, 2)


def derive_metrics(periods: list[dict]) -> dict:
    """최신 기간 기준 파생 지표 + TTM + 성장률."""
    if not periods:
        return {}
    latest = periods[0]
    std, q = latest["std"], latest["quarter"]
    g = lambda k: std.get(k, {}).get("cur")  # noqa: E731
    bs = {k: g(k) for k in _BS_KEYS}
    m = {
        "as_of": period_label(latest["year"], latest["reprt"]),
        "op_margin_pct": _ratio(q.get("operating_income"), q.get("revenue")),
        "net_margin_pct": _ratio(q.get("net_income"), q.get("revenue")),
        "gross_margin_pct": _ratio(q.get("gross_profit"), q.get("revenue")),
        "debt_ratio_pct": _ratio(bs.get("liabilities"), bs.get("equity")),
        "current_ratio_pct": _ratio(bs.get("current_assets"), bs.get("current_liabilities")),
        "cash_to_assets_pct": _ratio(bs.get("cash"), bs.get("assets")),
    }
    # 이자보상배율은 분기값보다 누적(연초~기말) 기준이 안정적 — 누적 우선, 없으면 분기값
    cum = latest["cumulative"]
    ie = next((v for v in (cum.get("interest_expense"), cum.get("finance_costs"),
                           q.get("interest_expense"), q.get("finance_costs")) if v), None)
    op_for_ie = cum.get("operating_income") if (cum.get("interest_expense") or cum.get("finance_costs")) else q.get("operating_income")
    m["interest_coverage_x"] = _ratio(op_for_ie, abs(ie) if ie else None, pct=False)
    m["interest_coverage_basis"] = "cumulative" if (cum.get("interest_expense") or cum.get("finance_costs")) else "quarter"
    cfo, capex = q.get("cfo"), q.get("capex")
    m["fcf_quarter"] = (cfo - abs(capex)) if (cfo is not None and capex is not None) else None

    # YoY: 분기보고서는 3개월값 대 전년동기 3개월값(frmtrm_amount),
    #      사업보고서는 연간 대 전년 연간(frmtrm_amount). QoQ: 직전 기간 3개월값.
    yoy, qoq = {}, {}
    prev_p = periods[1] if len(periods) > 1 else None
    annual = latest["reprt"] == "11011"
    for k in ("revenue", "operating_income", "net_income"):
        cur = std.get(k, {}).get("cur") if annual else q.get(k)
        yoy[k] = _growth(cur, _yoy_prev(std, k, annual))
        qoq[k] = _growth(q.get(k), prev_p["quarter"].get(k)) if prev_p else None
    m["yoy_basis"] = "annual" if annual else "quarter"
    m["yoy_pct"], m["qoq_pct"] = yoy, qoq

    # TTM: 최근 4개 분기 3개월값 합(4개 모두 있을 때만)
    ttm = {}
    for k in ("revenue", "operating_income", "net_income", "cfo"):
        vals = [p["quarter"].get(k) for p in periods[:4]]
        ttm[k] = sum(vals) if len(vals) == 4 and all(v is not None for v in vals) else None
    m["ttm"] = ttm
    if ttm.get("net_income") is not None and bs.get("equity"):
        m["roe_ttm_pct"] = _ratio(ttm["net_income"], bs["equity"])
    return m


def _growth(cur, prev):
    if cur is None or prev in (None, 0):
        return None
    if prev < 0 <= cur:
        return "흑자전환"
    if cur < 0 <= prev:
        return "적자전환"
    if prev < 0 and cur < 0:
        return f"적자{'축소' if cur > prev else '확대'}"
    return round((cur / prev - 1) * 100, 2)


def compact(periods: list[dict], basis: str, metrics: dict, unit: float = EOK) -> dict:
    """LLM 프롬프트용 핵심 층. 금액은 억원, 소수 1자리."""
    def u(v):
        return None if v is None else round(v / unit, 1)
    rows = []
    for p in periods:
        std = p["std"]
        rows.append({
            "period": period_label(p["year"], p["reprt"]),
            "end": period_end(p["year"], p["reprt"]),
            "rcept_no": p.get("rcept_no"),
            "quarter_eok": {k: u(v) for k, v in p["quarter"].items()},
            "cumulative_eok": {k: u(v) for k, v in p["cumulative"].items()},
            "balance_eok": {k: u(std[k]["cur"]) for k in _BS_KEYS if k in std},
            "yoy_prev_quarter_eok": {k: u(_yoy_prev(std, k, p["reprt"] == "11011")) for k in ("revenue", "operating_income", "net_income") if k in std},
            "notes": p["notes"],
        })
    mm = dict(metrics)
    if "fcf_quarter" in mm:
        mm["fcf_quarter_eok"] = u(mm.pop("fcf_quarter"))
    if "ttm" in mm:
        mm["ttm_eok"] = {k: u(v) for k, v in mm.pop("ttm").items()}
    return {"basis": basis, "unit": "억원", "periods": rows, "metrics": mm,
            "caveats": [
                "손익 quarter 는 3개월값, cumulative 는 연초 누적. FY 행의 quarter 는 4분기(연간−3분기누적).",
                "현금흐름은 누적 차감으로 분기값을 만들었으므로 직전 기간 결손 시 None.",
                "12월 결산 가정. 3월·6월 결산 법인은 기말(end) 표기가 실제와 다를 수 있음.",
                "금융업(은행·보험·증권)은 매출액·영업이익 개념이 달라 revenue/operating_income 이 비거나 의미가 다를 수 있음.",
            ]}


# ----------------------------------------------------------------------------
# 네트워크
# ----------------------------------------------------------------------------
def _key():
    k = os.environ.get("DART_API_KEY")
    if not k:
        raise RuntimeError("DART_API_KEY 환경변수가 없습니다.")
    return k


def fetch_report(corp_code: str, year: int, reprt: str, fs_div: str) -> list[dict] | None:
    """rows 리스트, 데이터 없음(status 013)은 [] , 그 외 오류는 None."""
    r = requests.get(API, params={"crtfc_key": _key(), "corp_code": corp_code,
                                  "bsns_year": str(year), "reprt_code": reprt, "fs_div": fs_div},
                     timeout=TIMEOUT)
    r.raise_for_status()
    d = r.json()
    st = d.get("status")
    if st == "000":
        return d.get("list") or []
    if st == "013":
        return []
    raise RuntimeError(f"DART {st}: {d.get('message')}")


def candidate_periods(today: _dt.date, n: int = 10) -> list[tuple[int, str]]:
    """오늘 기준 제출됐을 법한 보고기간을 최신부터 n 개."""
    out = []
    y = today.year
    while len(out) < n and y >= today.year - 4:
        for rc in reversed(_ORDER):
            # 해당 보고서가 제출됐을 시점: 사업보고서는 익년 3/31, 나머지는 같은 해 기한
            dm, dd = _DEADLINE[rc]
            due = _dt.date(y + 1, dm, dd) if rc == "11011" else _dt.date(y, dm, dd)
            if due <= today:
                out.append((y, rc))
        y -= 1
    return out[:n]


def collect(corp_code: str, n_periods: int = 5, today: _dt.date | None = None,
            max_calls: int = 12) -> tuple[dict, list[dict]]:
    """(핵심 층 dict, 원본 rows 목록) 반환. 원본은 기간별 {year, reprt, rows}."""
    today = today or _dt.date.today()
    basis = None
    periods, raw, calls = [], [], 0
    for y, rc in candidate_periods(today, n=n_periods + 5):
        if len(periods) >= n_periods or calls >= max_calls:
            break
        rows = None
        for fs in ([basis] if basis else ["CFS", "OFS"]):
            calls += 1
            try:
                rows = fetch_report(corp_code, y, rc, fs)
            except Exception as e:  # noqa: BLE001
                warn(f"dart_fin {y}/{rc}/{fs} 실패: {e}")
                rows = None
                continue
            if rows:
                basis = basis or fs
                break
        if not rows:
            continue
        std = standardize(rows)
        periods.append({"year": y, "reprt": rc, "std": std,
                        "rcept_no": (rows[0].get("rcept_no") if rows else None)})
        raw.append({"year": y, "reprt": rc, "fs_div": basis, "rows": rows})
    if not periods:
        return {"status": "empty", "basis": None, "periods": [], "calls": calls}, raw
    derive_quarters(periods)
    metrics = derive_metrics(periods)
    out = compact(periods, basis, metrics)
    out["status"] = "ok"
    out["calls"] = calls
    return out, raw


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corp_code")
    ap.add_argument("--periods", type=int, default=5)
    ap.add_argument("--full", action="store_true", help="전 계정 원본을 dart_raw.json 으로 저장")
    ap.add_argument("--out", default=".")
    a = ap.parse_args(argv)
    out = Path(a.out)
    core, raw = collect(a.corp_code, a.periods)
    write_json(out / "dart_financials.json", core)
    if a.full:
        write_json(out / "dart_raw.json", raw)
    print(json.dumps({"status": core.get("status"), "basis": core.get("basis"),
                      "periods": [p["period"] for p in core.get("periods", [])],
                      "calls": core.get("calls")}, ensure_ascii=False))
    return 0 if core.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
