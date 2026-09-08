#!/usr/bin/env python3
"""주간 브리핑 엑셀 생성 — 기준 템플릿(templates/weekly_briefing_template.xlsx) 재현.

사용자 확정 템플릿('(2)' 파일, 2026-09-08)을 로드해 모든 스타일(폰트·색·병합·
열너비·정렬)을 셀 단위로 복사해 쓰고, 최신 주간 데이터(public/weekly_briefing.json)
를 같은 구조에 채워 넣는다 — 매주 데이터가 바뀌어도 템플릿과 동일한 레이아웃.
가변 섹션(지표·일별·코멘트·순매수·관찰·흐름·타임라인·프리뷰)은 템플릿의 해당
행을 프로토타입으로 필요한 수만큼 반복하고, 서술형 행은 병합 폭 기준으로 행높이를
자동 계산한다(잘림 금지). 저장 전 구조 검증(19항)을 통과해야 파일을 쓴다.

출력: public/weekly_briefing.xlsx + public/reports/weekly_briefing/<weekStart>.xlsx
사용: python build_weekly_xlsx.py  (weekly json 생성 직후, CI 16:10)
"""
import os
import sys
import json
from copy import copy

import openpyxl
from openpyxl.utils import get_column_letter

ROOT = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(ROOT, "templates", "weekly_briefing_template.xlsx")
DATA = os.path.join(ROOT, "public", "weekly_briefing.json")
OUT_SNAP = os.path.join(ROOT, "public", "weekly_briefing.xlsx")

SHEET = "주간 브리핑"
RAW_SHEET = "원본 데이터"

# 템플릿 실측 좌표 — 각 스타일 프로토타입 셀 (행, 열). 템플릿이 바뀌면 여기만 갱신.
P = {
    "title": (1, 1), "navy_fill": (1, 2), "meta_label": (1, 8), "meta_val": (1, 9),
    "signal_label": (4, 1), "signal_val": (4, 3),
    "chip": (6, 1), "sect": (6, 2), "cap": (6, 4),
    "hdr": (7, 1), "body_bold": (8, 1), "num_neg": (8, 2), "num_pos": (8, 3),
    "rank_label": (11, 1), "rank_cell": (12, 2),
    "econ_cell": (19, 1), "verdict_ok": (19, 8), "verdict_beat": (20, 8), "verdict_miss": (21, 8),
    "daily_cell": (25, 1), "daily_comment": (25, 7),
    "comment_row": (30, 1),
    "nb_buy_label": (36, 1), "nb_sell_label": (46, 1), "nb_cell": (36, 3),
    "watch_up": (59, 1), "watch_dn": (61, 1), "watch_reason": (59, 3),
    "flow_row": (66, 1),
    "tl_cell": (78, 1), "tl_star": (78, 4), "tl_event": (78, 6), "tl_mkt_row": (77, 2),
    "pv_dn": (96, 1), "pv_sig": (98, 1), "pv_ev": (99, 1), "pv_body": (96, 2),
}

# 병합 폭(wch 합) → 한글 기준 줄당 자수 (열너비 실측: A12.9 B14.9 C13.9 D12.9 E13.9
# F20.9 G14.9 H12.9 I13 J13 — 한글 1자 ≈ 2wch, 안전계수 0.95)
_W = {"A": 12.9, "B": 14.9, "C": 13.9, "D": 12.9, "E": 13.9,
      "F": 20.9, "G": 14.9, "H": 12.9, "I": 13.0, "J": 13.0}


def _cpl(c1, c2):
    total = sum(_W[get_column_letter(c)] for c in range(c1, c2 + 1))
    return max(8, int(total / 2 * 0.95))


def _hpt(text, cpl):
    lines = max(1, -(-len(str(text or "")) // cpl))
    return 15.0 if lines == 1 else lines * 13.5 + 4


class Builder:
    def __init__(self, tpl_ws):
        self.t = tpl_ws
        self.styles = {}
        for k, (r, c) in P.items():
            self.styles[k] = self.t.cell(r, c)

    def start(self, ws):
        self.ws = ws
        self.r = 1
        self.merges = []
        self.heights = {}
        for col, w in _W.items():
            ws.column_dimensions[col].width = w

    def cell(self, c, v, proto, num=None):
        """proto 스타일 복사 셀. num='sign'이면 부호에 따라 양/음수 프로토 색."""
        if num == "sign" and isinstance(v, (int, float)):
            proto = "num_pos" if v > 0 else "num_neg" if v < 0 else "econ_cell"
        p = self.styles[proto]
        d = self.ws.cell(self.r, c, v if v is not None else "")
        d.font, d.fill, d.border = copy(p.font), copy(p.fill), copy(p.border)
        d.alignment, d.number_format = copy(p.alignment), p.number_format
        return d

    def fill_row(self, c1, c2, proto):
        for c in range(c1, c2 + 1):
            self.cell(c, "", proto)

    def mg(self, c1, c2):
        self.merges.append((self.r, c1, self.r, c2))

    def wrap_h(self, text, c1, c2):
        h = _hpt(text, _cpl(c1, c2))
        self.heights[self.r] = max(self.heights.get(self.r, 15.0), h)

    def nl(self, n=1, height=None):
        if height:
            self.heights[self.r] = height
        self.r += n

    def chip(self, no, title, cap="", cap_col=None, cap_right=False):
        self.cell(1, no, "chip")
        self.cell(2, title, "sect")
        for c in range(3, 11):
            self.cell(c, "", "sect")
        if cap:
            col = cap_col or (10 if cap_right else 4)
            self.cell(col, cap, "cap")
        self.nl()

    def hdr_row(self, labels, merge_last_to=None):
        for i, h in enumerate(labels):
            self.cell(i + 1, h, "hdr")
        last = len(labels)
        end = merge_last_to or last
        for c in range(last + 1, end + 1):
            self.cell(c, "", "hdr")
        if merge_last_to and merge_last_to > last:
            self.merges.append((self.r, last, self.r, merge_last_to))
        self.nl()

    def finish(self):
        ws = self.ws
        for (r1, c1, r2, c2) in self.merges:
            ws.merge_cells(start_row=r1, start_column=c1, end_row=r2, end_column=c2)
        for r, h in self.heights.items():
            ws.row_dimensions[r].height = h


def _fmt_pct(v):
    return "" if v is None else v


def _classify_preview(items):
    """nextWeekPreview 불릿 → (구분, 본문). 판별/이벤트는 접두어로, 시나리오는
    결과절 키워드로 상승/하방 분류(오분류 방지: 조건절이 아니라 귀결로 판단)."""
    out = []
    for b in items or []:
        t = str(b)
        if "판별 신호" in t[:12]:
            out.append(("판별 신호", t))
        elif "핵심 이벤트" in t[:12] or t.startswith("이벤트"):
            out.append(("핵심 이벤트", t))
        else:
            tail = t.split("면")[-1] if "면" in t else t
            neg = any(k in tail for k in ("후퇴", "차익", "하락", "조정", "출회", "부담", "리스크", "약세", "축소"))
            pos = any(k in tail for k in ("지속", "상승", "유입", "우호", "랠리", "회복", "확대", "해소"))
            out.append(("상승 시나리오" if pos and not neg else "하방 시나리오", t))
    return out


def build(d):
    wb = openpyxl.load_workbook(TEMPLATE)
    tpl = wb[SHEET]
    b = Builder(tpl)
    # 템플릿 시트는 스타일 소스로만 쓰고, 동일 이름 새 시트에 재작성
    idx = wb.sheetnames.index(SHEET)
    tpl.title = "_tpl"
    ws = wb.create_sheet(SHEET, idx)
    b.start(ws)
    syn = d.get("synthesis") or {}

    def eok(v):
        return "" if v is None else round(v / 100)

    # ── Header ──────────────────────────────────────────────────────────
    b.cell(1, "WEEKLY MARKET BRIEF", "title")
    for c in range(2, 8):
        b.cell(c, "", "navy_fill")
    b.merges.append((1, 1, 2, 7))
    b.cell(8, "기간", "meta_label"); b.cell(9, d.get("weekStart", "") + " ~ " + d.get("weekEnd", ""), "meta_val")
    b.nl(1, 20.25)
    b.cell(8, "생성 기준", "meta_label"); b.cell(9, d.get("asof", ""), "meta_val")
    b.nl(1, 20.25); b.nl()
    b.cell(1, "MARKET SIGNAL", "signal_label"); b.cell(2, "", "signal_label"); b.mg(1, 2)
    b.cell(3, syn.get("headline", ""), "signal_val")
    for c in range(4, 11):
        b.cell(c, "", "signal_val")
    b.mg(3, 10); b.nl(1, 17.25); b.nl()

    # ── 01 + 02 (좌우) ──────────────────────────────────────────────────
    b.cell(1, "01", "chip"); b.cell(2, "주간 누적 수급", "sect"); b.cell(3, "", "sect")
    b.cell(4, "단위: 억원", "cap")
    b.cell(5, "02", "chip"); b.cell(6, "US 미국 주간", "sect"); b.cell(7, "", "sect")
    b.cell(8, "모닝브리핑 전일 기준 누적, %", "cap"); b.cell(9, "", "cap"); b.cell(10, "", "cap")
    b.nl()
    us = d.get("usWeekly") or {}
    b.hdr_row(["시장", "개인", "외국인", "기관", "S&P500", "다우", "나스닥", "VIX 변동성", "필라델피아 반도체", ""])
    inv = {"kospi": {}, "kosdaq": {}}
    for day in d.get("days") or []:
        for mk in inv:
            for k, v in ((day.get("investors") or {}).get(mk) or {}).items():
                if isinstance(v, (int, float)):
                    inv[mk][k] = inv[mk].get(k, 0) + v
    for i, mk in enumerate(("kospi", "kosdaq")):
        m = inv[mk]
        b.cell(1, mk.upper(), "body_bold")
        for c, k in ((2, "individual"), (3, "foreign"), (4, "institution")):
            b.cell(c, round(m.get(k)) if m.get(k) is not None else "", "num_pos", num="sign")
        if i == 0:
            for c, k in ((5, "S&P500"), (6, "다우"), (7, "나스닥"), (8, "VIX 변동성"), (9, "필라델피아 반도체")):
                b.cell(c, us.get(k, ""), "num_pos", num="sign")
        b.nl()
    b.nl()

    # 섹터 순위 표
    b.hdr_row(["구분", "1위", "2위", "3위"])
    usw, krw = d.get("usSectorWeekly") or {}, d.get("sectorFlow") or {}
    kr_rows = (krw.get("rows") or [])
    def top3(rows, key, rev):
        return sorted([r for r in rows], key=lambda x: -x[key] if rev else x[key])[:3]
    table = [("US 상승 섹터", [(s["name"], s["chg"]) for s in (usw.get("up") or [])[:3]]),
             ("US 하락 섹터", [(s["name"], s["chg"]) for s in (usw.get("down") or [])[:3]]),
             ("KR 상승 섹터", [(s["name"], s["chgPct"]) for s in top3(kr_rows, "chgPct", True)]),
             ("KR 하락 섹터", [(s["name"], s["chgPct"]) for s in top3(kr_rows, "chgPct", False) if s["chgPct"] < 0])]
    for label, items in table:
        b.cell(1, label, "rank_label")
        for i in range(3):
            v = ""
            if i < len(items):
                nm, ch = items[i]
                v = f"{nm} {'+' if ch > 0 else ''}{ch}%"
            b.cell(2 + i, v, "rank_cell")
        b.nl()
    b.nl()

    # ── 03 경제지표 ─────────────────────────────────────────────────────
    b.chip("03", "주간 주요 경제지표 — 시장 반영")
    b.hdr_row(["날짜", "국가", "지표", "실제", "예상", "이전", "단위", "판정"])
    econ = []
    for day in d.get("days") or []:
        for e in (day.get("usEcon") or []) + (day.get("koEcon") or []):
            econ.append((day.get("date"), e))
    for date, e in econ:
        sp = e.get("surprise") or {}
        verdict, vs = sp.get("verdict"), sp.get("vs")
        vlabel = {"beat": "예상 상회", "miss": "예상 하회", "inline": "예상 부합"}.get(verdict, "")
        if vs == "previous" and vlabel:
            vlabel = vlabel.replace("예상", "이전比")
        proto = {"beat": "verdict_beat", "miss": "verdict_miss"}.get(verdict, "verdict_ok")
        b.cell(1, date, "econ_cell"); b.cell(2, e.get("nation", ""), "econ_cell")
        b.cell(3, e.get("name", ""), "econ_cell")
        b.cell(4, e.get("actual", ""), "econ_cell"); b.cell(5, e.get("forecast", ""), "econ_cell")
        b.cell(6, e.get("previous", ""), "econ_cell")
        b.cell(7, (e.get("unitScale") or "") + (e.get("unit") or ""), "econ_cell")
        b.cell(8, vlabel, proto)
        b.nl()
    b.nl()

    # ── 04 일별 요약 ────────────────────────────────────────────────────
    b.chip("04", "일별 요약")
    b.hdr_row(["날짜", "코스피(%)", "코스피 ▲/▼", "코스닥(%)", "코스닥 ▲/▼", "주도 섹터",
               "전일 미국장 → 한국장 반응"], merge_last_to=10)
    brM = {x.get("date"): x for x in ((d.get("breadth") or {}).get("days") or [])}
    dc = {x.get("date"): x.get("note") for x in (syn.get("dailyContext") or []) if x}
    for day in d.get("days") or []:
        ks = (day.get("indices") or {}).get("kospi") or {}
        kq = (day.get("indices") or {}).get("kosdaq") or {}
        br = brM.get(day.get("date")) or {}
        def adr(m):
            m = m or {}
            return "" if m.get("up") is None else f"{m['up']}▲/{m['down']}▼" + (f" 상한{m['upLimit']}" if m.get("upLimit") else "")
        note = dc.get(day.get("date"), "")
        b.cell(1, day.get("date"), "daily_cell")
        b.cell(2, ks.get("rate"), "num_pos", num="sign")
        b.cell(3, adr(br.get("kospi")), "daily_cell")
        b.cell(4, kq.get("rate"), "num_pos", num="sign")
        b.cell(5, adr(br.get("kosdaq")), "daily_cell")
        b.cell(6, ", ".join(s.get("name", "") for s in (day.get("sectorsUp") or [])[:2]), "daily_cell")
        b.cell(7, note, "daily_comment")
        for c in range(8, 11):
            b.cell(c, "", "daily_comment")
        b.mg(7, 10); b.wrap_h(note, 7, 10)
        b.nl()
    b.nl()

    # ── 05 주간 종합 코멘트 ─────────────────────────────────────────────
    if syn.get("weeklyComment"):
        b.chip("05", "주간 종합 코멘트 — 매크로·수급")
        b.hdr_row(["No.", "Comment"], merge_last_to=10)
        for cm in syn["weeklyComment"]:
            t = "- " + str(cm)
            b.cell(1, t, "comment_row")
            for c in range(2, 11):
                b.cell(c, "", "comment_row")
            b.mg(1, 10); b.wrap_h(t, 1, 10); b.nl()
        b.nl()

    # ── 06 투자자 합산 순매수 ───────────────────────────────────────────
    total = (d.get("netbuyCum") or {}).get("total") or {}
    if total.get("top") or total.get("bottom"):
        b.chip("06", "투자자 합산 순매수 상위/하위", cap="외인+기관계, 억원", cap_col=10)
        b.hdr_row(["구분", "순위", "종목", "합산", "외인", "기관", "개인", "공매도", "대차잔고", "대차증감"])
        for label, proto, rows in (("순매수 상위", "nb_buy_label", total.get("top")),
                                   ("순매도 상위", "nb_sell_label", total.get("bottom"))):
            for i, e in enumerate(rows or []):
                b.cell(1, label, proto)
                b.cell(2, i + 1, "nb_cell")
                b.cell(3, e.get("name", ""), "nb_cell")
                for c, k in ((4, "amt"), (5, "frgn"), (6, "orgn"), (7, "prsn")):
                    b.cell(c, eok(e.get(k)), "num_pos", num="sign")
                b.cell(8, e.get("shortSum", ""), "nb_cell")
                b.cell(9, e.get("loanAmt", ""), "nb_cell")
                b.cell(10, e.get("loanChg", ""), "nb_cell")
                b.nl()
        b.nl()

    # ── 07 수급 관찰 ────────────────────────────────────────────────────
    wn = syn.get("watchNotes") or {}
    if wn.get("long") or wn.get("short"):
        b.chip("07", "수급 관찰")
        b.hdr_row(["방향", "종목", "관찰 사유"], merge_last_to=10)
        for label, proto, rows in (("상방 관찰", "watch_up", wn.get("long")),
                                   ("하방 관찰", "watch_dn", wn.get("short"))):
            for e in rows or []:
                b.cell(1, label, proto)
                b.cell(2, e.get("name", ""), "nb_cell")
                basis = e.get("basis", "")
                b.cell(3, basis, "watch_reason")
                for c in range(4, 11):
                    b.cell(c, "", "watch_reason")
                b.mg(3, 10); b.wrap_h(basis, 3, 10); b.nl()
        b.nl()

    # ── 08 / 09 서술형 흐름 ─────────────────────────────────────────────
    for no, title, hdr2, items in (("08", "주간 시장 흐름", "Market Flow", syn.get("weekNarrative")),
                                   ("09", "섹터·테마 흐름", "", syn.get("sectorRotation"))):
        if not items:
            continue
        b.chip(no, title)
        if hdr2:
            b.hdr_row(["No.", hdr2], merge_last_to=10)
        for t in items:
            t = "- " + str(t)
            b.cell(1, t, "flow_row")
            for c in range(2, 11):
                b.cell(c, "", "flow_row")
            b.mg(1, 10); b.wrap_h(t, 1, 10); b.nl()
        b.nl()

    # ── 10 촉매 타임라인 ────────────────────────────────────────────────
    tl = syn.get("catalystTimeline") or []
    if tl:
        b.chip("10", "주간 촉매 타임라인", cap="등락률 = 주간 누적", cap_col=10)
        for t in tl:
            stock = t.get("stock")
            b.cell(1, t.get("date", ""), "tl_cell")
            b.cell(2, stock or "—", "tl_mkt_row" if not stock else "nb_cell")
            b.cell(3, t.get("market", ""), "tl_cell")
            b.cell(4, "★" * (t.get("star") or 0), "tl_star")
            b.cell(5, t.get("changePct", ""), "num_pos", num="sign")
            ev = t.get("event", "")
            b.cell(6, ev, "tl_event")
            for c in range(7, 11):
                b.cell(c, "", "tl_event")
            b.mg(6, 10); b.wrap_h(ev, 6, 10); b.nl()
        b.nl()

    # ── 11 다음 주 프리뷰 ───────────────────────────────────────────────
    pv = _classify_preview(syn.get("nextWeekPreview"))
    if pv:
        b.chip("11", "다음 주 프리뷰")
        b.hdr_row(["구분", "내용"], merge_last_to=10)
        proto_map = {"상승 시나리오": "watch_up", "하방 시나리오": "pv_dn",
                     "판별 신호": "pv_sig", "핵심 이벤트": "pv_ev"}
        for label, text in pv:
            b.cell(1, label, proto_map.get(label, "pv_sig"))
            b.cell(2, text, "pv_body")
            for c in range(3, 11):
                b.cell(c, "", "pv_body")
            b.mg(2, 10); b.wrap_h(text, 2, 10); b.nl()

    b.finish()
    del wb["_tpl"]

    # ── 원본 데이터 시트 재작성 (보존용 평탄화 스트림) ───────────────────
    raw = wb[RAW_SHEET]
    raw.delete_rows(1, raw.max_row)
    def row(*vals):
        raw.append(list(vals))
    row("QUANT ANTIGRAVITY 주간 브리핑", d.get("weekStart", "") + " ~ " + d.get("weekEnd", ""))
    row("생성 기준", d.get("asof", ""))
    row("헤드라인", syn.get("headline", ""))
    row()
    row("1. 주간 누적 수급 (억원)")
    row("시장", "개인", "외국인", "기관")
    for mk in ("kospi", "kosdaq"):
        m = inv[mk]
        row(mk.upper(), round(m.get("individual", 0)), round(m.get("foreign", 0)), round(m.get("institution", 0)))
    row()
    row("2. US 미국 주간 (%)")
    for k, v in us.items():
        row(k, v)
    row()
    row("3. 경제지표")
    for date, e in econ:
        row(date, e.get("nation"), e.get("name"), e.get("actual"), e.get("forecast"), e.get("previous"))
    row()
    row("4. 일별 요약")
    for day in d.get("days") or []:
        ks = (day.get("indices") or {}).get("kospi") or {}
        row(day.get("date"), ks.get("rate"), dc.get(day.get("date"), ""))
    row()
    row("5. 주간 종합 코멘트")
    for cm in syn.get("weeklyComment") or []:
        row("-", cm)
    row("6. 투자자 합산 순매수 (억원)")
    for label, rows_ in (("순매수 상위", total.get("top")), ("순매도 상위", total.get("bottom"))):
        for i, e in enumerate(rows_ or []):
            row(label, i + 1, e.get("name"), eok(e.get("amt")), eok(e.get("frgn")), eok(e.get("orgn")),
                eok(e.get("prsn")), e.get("shortSum"), e.get("loanAmt"), e.get("loanChg"))
    row("7. 수급 관찰")
    for lab, rows_ in (("상방 관찰", wn.get("long")), ("하방 관찰", wn.get("short"))):
        for e in rows_ or []:
            row(lab, e.get("name"), e.get("basis"))
    row("8. 주간 시장 흐름")
    for t in syn.get("weekNarrative") or []:
        row("-", t)
    row("9. 섹터·테마 흐름")
    for t in syn.get("sectorRotation") or []:
        row("-", t)
    row("10. 촉매 타임라인")
    for t in tl:
        row(t.get("date"), t.get("stock") or "—", t.get("market"), t.get("star"), t.get("changePct"), t.get("event"))
    row("11. 다음 주 프리뷰")
    for label, text in pv:
        row(label, text)
    return wb, ws


def validate(ws, d):
    """저장 전 구조 검증 — 실패 시 (False, 사유)."""
    errs = []
    if ws.max_column > 10:
        errs.append(f"A:J 초과 (열 {ws.max_column})")
    # 01/02 좌우 배치: 같은 행에 '01'과 '02' 칩
    chips = {}
    for r in range(1, ws.max_row + 1):
        for c in (1, 5):
            v = ws.cell(r, c).value
            if isinstance(v, str) and v in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11"):
                chips.setdefault(v, (r, c))
    if not ("01" in chips and "02" in chips and chips["01"][0] == chips["02"][0]):
        errs.append("01/02 좌우 배치 아님")
    order = [chips[k][0] for k in ("03", "04", "06", "10") if k in chips]
    if order != sorted(order):
        errs.append("섹션 순서 어긋남")
    # 서술형 병합 존재
    mg = {(m.min_row, m.min_col, m.max_col) for m in ws.merged_cells.ranges}
    daily_r = chips.get("04", (0, 0))[0]
    if daily_r and not any(r > daily_r and c1 == 7 and c2 == 10 for r, c1, c2 in mg):
        errs.append("일별 코멘트 G:J 병합 없음")
    # 행높이: wrap 셀 잘림 방지 확인 (긴 코멘트 행 높이 > 15)
    for m in ws.merged_cells.ranges:
        v = ws.cell(m.min_row, m.min_col).value
        if isinstance(v, str) and len(v) > 60 and (ws.row_dimensions[m.min_row].height or 15) <= 15:
            errs.append(f"{m.min_row}행 긴 텍스트 행높이 미조정")
            break
    return (not errs), errs


def main():
    with open(DATA, encoding="utf-8") as f:
        d = json.load(f)
    wb, ws = build(d)
    ok, errs = validate(ws, d)
    if not ok:
        print(f"[xlsx] 검증 실패 — 저장 안 함: {errs}", file=sys.stderr)
        return 1
    wb.save(OUT_SNAP)
    week_path = os.path.join(ROOT, "public", "reports", "weekly_briefing",
                             f"{d.get('weekStart')}.xlsx")
    wb.save(week_path)
    print(f"[xlsx] 저장: {OUT_SNAP} (+{os.path.basename(week_path)}) — 검증 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
