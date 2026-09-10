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
import math
from copy import copy

import openpyxl
from openpyxl.utils import get_column_letter
from openpyxl.styles import Color, PatternFill
from openpyxl.formatting.rule import DataBarRule

ROOT = os.path.dirname(os.path.abspath(__file__))


# ── 종목 → 업종명 (대시보드 섹터 알약과 동일 소스 — 2026-09-09) ────────────────
# 전 종목 맵(krx_code_sector.json, 컷 없음) 우선 + krx_sector_map 보조.
# 이름 키는 krx_companies(code→name)로 조인 — 코드 없는 표(타임라인·수급 관찰)용.
def _load_sector_lookup():
    code_sec, name_sec = {}, {}
    try:
        with open(os.path.join(ROOT, "public", "assets", "krx_code_sector.json"),
                  encoding="utf-8") as f:
            code_sec = dict((json.load(f) or {}).get("map") or {})
    except Exception:
        pass
    try:
        with open(os.path.join(ROOT, "public", "assets", "krx_sector_map.json"),
                  encoding="utf-8") as f:
            sm = json.load(f) or {}
        for e in (sm.get("sectors") or {}).values():
            for x in (e.get("stocks") or []) + (e.get("kosdaqStocks") or []):
                c = str(x.get("code") or "").zfill(6)
                if c and c not in code_sec and e.get("name"):
                    code_sec[c] = e["name"]
    except Exception:
        pass
    try:
        with open(os.path.join(ROOT, "public", "assets", "krx_companies.json"),
                  encoding="utf-8") as f:
            for e in json.load(f):
                c = str(e.get("code") or "").zfill(6)
                nm = (e.get("name") or "").strip()
                if nm and c in code_sec and nm not in name_sec:
                    name_sec[nm] = code_sec[c]
    except Exception:
        pass
    return code_sec, name_sec


_CODE_SEC, _NAME_SEC = _load_sector_lookup()


def _sector_name(name, code=None):
    """종목 → 업종명 (우선주는 보통주 코드 폴백, 미해석은 None)."""
    sec = None
    if code:
        c = str(code).zfill(6)
        sec = _CODE_SEC.get(c) or (_CODE_SEC.get(c[:5] + "0") if c[5] != "0" else None)
    return sec or _NAME_SEC.get(str(name or "").strip())


def _with_sector(name, code=None):
    """'종목명 (업종)' 병기 — 미해석은 이름 그대로."""
    sec = _sector_name(name, code)
    return f"{name} ({sec})" if sec else str(name or "")
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
# 본 시트 실사용 폭 — 종목명/섹터명 분리(2026-09-09 3차 스펙 §9)로 A:K 11열.
# C/D 는 종목명·섹터명이 앉는 자리라 '운송장비·부품'(14wch)까지 수용하게 소폭 확장.
W_MAIN = {**_W, "C": 14.0, "D": 14.0, "K": 13.9}

BUY_RED, SELL_BLUE = "FFD64545", "FF3182F6"   # 상승/매수 적 · 하락/매도 청 (관례)
BUY_BG, SELL_BG, GRAY = "FFFDEEEE", "FFEAF1FD", "FF6B7280"


def fstyle(cell, rgb=None, bold_=None, size=None):
    """셀 폰트 부분 오버라이드(색·굵기·크기) — 프로토 복사 후 강조용."""
    f = copy(cell.font)
    if rgb:
        f.color = Color(rgb=rgb)
    if bold_ is not None:
        f.bold = bold_
    if size:
        f.size = size
    cell.font = f


def _rich_breadth(m):
    """일별 요약 ▲/▼ 셀 — 상승·상한=적, 하락·하한=청 부분 글자색(3차 스펙 §6-7).
    CellRichText 미지원 환경이면 기존 단일 문자열 폴백."""
    if m is None or m.get("up") is None:
        return "데이터 없음"
    up = f"{m['up']}▲" + (f" 상한{m['upLimit']}" if m.get("upLimit") else "")
    dn = f"{m['down']}▼" + (f" 하한{m['downLimit']}" if m.get("downLimit") else "")
    try:
        from openpyxl.cell.rich_text import CellRichText, TextBlock
        from openpyxl.cell.text import InlineFont
        return CellRichText([
            TextBlock(InlineFont(rFont="맑은 고딕", sz=10, color="D64545"), up),
            "/",
            TextBlock(InlineFont(rFont="맑은 고딕", sz=10, color="3182F6"), dn)])
    except Exception:
        return f"{up}/{dn}"


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

    def start(self, ws, widths=None):
        """widths: 시트별 역할 기반 열너비(dict, 미지정 시 본 시트 _W).
        열 수(maxc)도 widths 로 정해진다 — 상세 탭의 A:K 등 가변 폭 지원."""
        self.ws = ws
        self.r = 1
        self.merges = []
        self.heights = {}
        self.w = dict(widths or _W)
        self.maxc = len(self.w)
        for col, w in self.w.items():
            ws.column_dimensions[col].width = w

    def cell(self, c, v, proto, num=None, wrap=False, fmt=None, align=None):
        """proto 스타일 복사 셀. num='sign'이면 부호색, wrap=True 면 줄바꿈 강제.
        fmt=숫자서식 오버라이드(값은 숫자 그대로 유지), align=수평정렬 오버라이드."""
        if num == "sign" and isinstance(v, (int, float)):
            proto = "num_pos" if v > 0 else "num_neg" if v < 0 else "econ_cell"
        p = self.styles[proto]
        d = self.ws.cell(self.r, c, v if v is not None else "")
        d.font, d.fill, d.border = copy(p.font), copy(p.fill), copy(p.border)
        d.alignment, d.number_format = copy(p.alignment), p.number_format
        if fmt:
            d.number_format = fmt
        if align:
            a = copy(d.alignment); a.horizontal = align; d.alignment = a
        if wrap and not d.alignment.wrap_text:
            a = copy(d.alignment); a.wrapText = True; d.alignment = a
        return d

    def fill_row(self, c1, c2, proto):
        for c in range(c1, c2 + 1):
            self.cell(c, "", proto)

    def mg(self, c1, c2):
        self.merges.append((self.r, c1, self.r, c2))

    def wrap_h(self, text, c1, c2):
        total = sum(self.w[get_column_letter(c)] for c in range(c1, c2 + 1))
        h = _hpt(text, max(8, int(total / 2 * 0.95)))
        self.heights[self.r] = max(self.heights.get(self.r, 15.0), h)

    def nl(self, n=1, height=None):
        if height:
            self.heights[self.r] = height
        self.r += n

    def chip(self, no, title, cap="", cap_col=None, cap_right=False):
        self.cell(1, no, "chip")
        self.cell(2, title, "sect")
        for c in range(3, self.maxc + 1):
            self.cell(c, "", "sect")
        if cap:
            col = cap_col or (self.maxc if cap_right else 4)
            self.cell(col, cap, "cap")
        self.nl()

    def hdr_row(self, labels, merge_last_to=None, align=None):
        for i, h in enumerate(labels):
            self.cell(i + 1, h, "hdr", align=align)
        last = len(labels)
        end = merge_last_to or last
        for c in range(last + 1, end + 1):
            self.cell(c, "", "hdr")
        if merge_last_to and merge_last_to > last:
            self.merges.append((self.r, last, self.r, merge_last_to))
        self.nl()

    def autofix(self):
        """잘림 자동 교정 — 비wrap 잘림 셀은 wrap 전환, wrap 셀은 필요 행높이 반영.
        validate 와 동일 기준이라 이 패스 후 잘림 검증은 항상 통과한다."""
        ws = self.ws
        merged_at, inside = {}, set()
        for (r1, c1, r2, c2) in self.merges:
            merged_at[(r1, c1)] = (c1, c2)
            for c in range(c1 + 1, c2 + 1):
                inside.add((r1, c))
        for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=self.maxc):
            for cell in row:
                v = cell.value
                if v in (None, "") or (cell.row, cell.column) in inside:
                    continue
                c1, c2 = merged_at.get((cell.row, cell.column),
                                       (cell.column, cell.column))
                span = sum(self.w[get_column_letter(c)] for c in range(c1, c2 + 1))
                need = _disp_w(v)
                if need <= span:
                    continue
                if not cell.alignment.wrap_text:
                    nxt = ws.cell(cell.row, c2 + 1).value if c2 < self.maxc else None
                    if nxt in (None, ""):
                        continue            # 오른쪽이 비어 overflow 표시 — 잘림 아님
                    a = copy(cell.alignment); a.wrapText = True; cell.alignment = a
                lines = max(1, math.ceil(need / span))
                h = lines * 13.5 + 4
                self.heights[cell.row] = max(self.heights.get(cell.row, 15.0), h)

    def finish(self):
        self.autofix()
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
    b.start(ws, W_MAIN)      # 종목명/섹터명 분리로 A:K 11열(3차 스펙 §9)
    syn = d.get("synthesis") or {}

    def eok(v):
        return "" if v is None else round(v / 100)

    # ── Header ──────────────────────────────────────────────────────────
    b.cell(1, "WEEKLY MARKET BRIEF", "title")
    for c in range(2, 8):
        b.cell(c, "", "navy_fill")
    b.merges.append((1, 1, 2, 7))
    b.cell(8, "기간", "meta_label")
    b.cell(9, d.get("weekStart", "") + " ~ " + d.get("weekEnd", ""), "meta_val")
    b.cell(10, "", "meta_val"); b.cell(11, "", "meta_val"); b.mg(9, 11)   # 잘림 금지
    b.nl(1, 20.25)
    b.cell(8, "생성 기준", "meta_label"); b.cell(9, d.get("asof", ""), "meta_val")
    b.cell(10, "", "meta_val"); b.cell(11, "", "meta_val"); b.mg(9, 11)
    b.nl(1, 20.25); b.nl()
    b.cell(1, "MARKET SIGNAL", "signal_label"); b.cell(2, "", "signal_label"); b.mg(1, 2)
    b.cell(3, syn.get("headline", ""), "signal_val", wrap=True)
    for c in range(4, 12):
        b.cell(c, "", "signal_val")
    b.mg(3, 11); b.wrap_h(syn.get("headline", ""), 3, 11)
    if self_h := b.heights.get(b.r):
        b.heights[b.r] = max(17.25, self_h)
    b.nl(); b.nl()

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

    # 섹터 순위 표 — 순위는 행 유지, 섹터명/누적수익률 컬럼 분리(3차 스펙 §1-5).
    # 1위·2위·3위를 가로로 펼치지 않는다(금지 구조). 수익률은 숫자 그대로 두고
    # 표시 서식만 +x.x% — 양수 적/음수 청.
    b.hdr_row(["구분", "순위", "섹터명", "누적수익률"])
    usw, krw = d.get("usSectorWeekly") or {}, d.get("sectorFlow") or {}
    kr_rows = (krw.get("rows") or [])
    def top3(rows, key, rev):
        return sorted([r for r in rows], key=lambda x: -x[key] if rev else x[key])[:3]
    table = [("US 상승 섹터", [(s["name"], s["chg"]) for s in (usw.get("up") or [])[:3]]),
             ("US 하락 섹터", [(s["name"], s["chg"]) for s in (usw.get("down") or [])[:3]]),
             ("KR 상승 섹터", [(s["name"], s["chgPct"]) for s in top3(kr_rows, "chgPct", True)]),
             ("KR 하락 섹터", [(s["name"], s["chgPct"]) for s in top3(kr_rows, "chgPct", False) if s["chgPct"] < 0])]
    F_PCTS = '+0.0"%";-0.0"%";0.0"%"'
    for label, items in table:
        for i in range(3):
            b.cell(1, label, "rank_label")
            b.cell(2, f"{i + 1}위", "rank_cell", align="center")
            if i < len(items):
                nm, ch = items[i]
                b.cell(3, nm, "rank_cell")
                cc = b.cell(4, ch, "rank_cell", fmt=F_PCTS, align="right")
                if isinstance(ch, (int, float)) and ch:
                    fstyle(cc, rgb=BUY_RED if ch > 0 else SELL_BLUE)
            else:
                b.cell(3, "데이터 없음", "rank_cell")
                b.cell(4, "—", "rank_cell", align="right")
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
        if not vlabel:                       # 파생 판정 — 예상값 우선, 없으면 이전값
            a = e.get("actual")
            for ref, word in ((e.get("forecast"), "예상"), (e.get("previous"), "이전比")):
                if isinstance(a, (int, float)) and isinstance(ref, (int, float)):
                    vlabel = f"{word} " + ("상회" if a > ref else "하회" if a < ref else
                                           ("부합" if word == "예상" else "동일"))
                    verdict = "beat" if a > ref else "miss" if a < ref else "inline"
                    break
        proto = {"beat": "verdict_beat", "miss": "verdict_miss"}.get(verdict, "verdict_ok")
        b.cell(1, date, "econ_cell"); b.cell(2, e.get("nation", ""), "econ_cell")
        b.cell(3, e.get("name", ""), "econ_cell", wrap=True)
        b.wrap_h(e.get("name", ""), 3, 3)
        b.cell(4, e.get("actual", ""), "econ_cell"); b.cell(5, e.get("forecast", ""), "econ_cell")
        b.cell(6, e.get("previous", ""), "econ_cell")
        b.cell(7, (e.get("unitScale") or "") + (e.get("unit") or ""), "econ_cell")
        b.cell(8, vlabel or "데이터 없음", proto)
        b.nl()
    b.nl()

    # ── 04 일별 요약 ────────────────────────────────────────────────────
    b.chip("04", "일별 요약")
    b.hdr_row(["날짜", "코스피(%)", "코스피 ▲/▼", "코스닥(%)", "코스닥 ▲/▼", "주도 섹터",
               "전일 미국장 → 한국장 반응"], merge_last_to=11)
    brM = {x.get("date"): x for x in ((d.get("breadth") or {}).get("days") or [])}
    dc = {x.get("date"): x.get("note") for x in (syn.get("dailyContext") or []) if x}
    for day in d.get("days") or []:
        ks = (day.get("indices") or {}).get("kospi") or {}
        kq = (day.get("indices") or {}).get("kosdaq") or {}
        br = brM.get(day.get("date")) or {}
        note = dc.get(day.get("date"), "")
        b.cell(1, day.get("date"), "daily_cell")
        # 장전 재생성(당일 장중 데이터 이전 — 지수·섹터 미확정)은 '—' 표기.
        # 빈칸이면 04 검증(결측)에 걸려 xlsx 저장이 통째로 실패한다(2026-09-09 실측:
        # 장전 수동 dispatch 에서 exit 1). 16:10 정규 실행이 실값으로 덮는다.
        if ks.get("rate") is None:
            b.cell(2, "—", "daily_cell")
        else:
            b.cell(2, ks.get("rate"), "num_pos", num="sign")
        # ▲/▼ 셀 — 상승·상한 적 / 하락·하한 청 부분 글자색(3차 스펙 §6-7)
        b.cell(3, _rich_breadth(br.get("kospi")), "daily_cell")
        if kq.get("rate") is None:
            b.cell(4, "—", "daily_cell")
        else:
            b.cell(4, kq.get("rate"), "num_pos", num="sign")
        b.cell(5, _rich_breadth(br.get("kosdaq")), "daily_cell")
        lead = ", ".join(s.get("name", "") for s in (day.get("sectorsUp") or [])[:2]) or "—"
        b.cell(6, lead, "daily_cell", wrap=True)
        b.wrap_h(lead, 6, 6)
        b.cell(7, note, "daily_comment")
        for c in range(8, 12):
            b.cell(c, "", "daily_comment")
        b.mg(7, 11); b.wrap_h(note, 7, 11)
        b.nl()
    b.nl()

    # ── 05 주간 종합 코멘트 ─────────────────────────────────────────────
    if syn.get("weeklyComment"):
        b.chip("05", "주간 종합 코멘트 — 매크로·수급")
        b.hdr_row(["No.", "Comment"], merge_last_to=11)
        for cm in syn["weeklyComment"]:
            t = "- " + str(cm)
            b.cell(1, t, "comment_row")
            for c in range(2, 12):
                b.cell(c, "", "comment_row")
            b.mg(1, 11); b.wrap_h(t, 1, 11); b.nl()
        b.nl()

    # ── 06 투자자 합산 순매수 ───────────────────────────────────────────
    total = (d.get("netbuyCum") or {}).get("total") or {}
    if total.get("top") or total.get("bottom"):
        b.chip("06", "투자자 합산 순매수 상위/하위", cap="외인+기관계, 억원", cap_col=10)
        # 종목명/섹터명 분리(3차 스펙 §8) — 한 셀 병기 금지, 섹터는 구조화 맵에서
        b.hdr_row(["구분", "순위", "종목명", "섹터명", "합산", "외인", "기관", "개인",
                   "공매도", "대차잔고", "대차증감"])
        for label, proto, rows in (("순매수 상위", "nb_buy_label", total.get("top")),
                                   ("순매도 상위", "nb_sell_label", total.get("bottom"))):
            for i, e in enumerate(rows or []):
                b.cell(1, label, proto)
                b.cell(2, i + 1, "nb_cell")
                b.cell(3, e.get("name", ""), "nb_cell")
                sec_c = b.cell(4, _sector_name(e.get("name"), e.get("code")) or "", "nb_cell")
                fstyle(sec_c, rgb=GRAY)
                for c, k in ((5, "amt"), (6, "frgn"), (7, "orgn"), (8, "prsn")):
                    b.cell(c, eok(e.get(k)), "num_pos", num="sign")
                b.cell(9, e.get("shortSum", ""), "nb_cell")
                b.cell(10, e.get("loanAmt", ""), "nb_cell")
                b.cell(11, e.get("loanChg", ""), "nb_cell")
                b.nl()
        b.nl()

    # ── 07 섹터 시그널 종목 관찰 (4-Matrix, 2026-09-09) ──────────────────
    # 생성기 sectorScreen 저장값만 렌더(웹과 동일 결과 §28). 2x2: 좌 A:E/우 G:K,
    # 각 종목 = [종목명|섹터명|Score] + 선정 이유(카드 폭 병합). 구 아카이브
    # (sectorScreen 없음)는 기존 수급 상방/하방 관찰 폴백.
    ssm = (d.get("sectorScreen") or {}).get("matrix")
    if ssm is not None:
        SS_ST = {"동반강세": "FFD64545", "수급유입": "FF0B7F8C",
                 "수급이탈": "FFB45309", "동반약세": "FF3182F6"}
        sub = (d.get("sectorScreen") or {}).get("sub") or {}
        b.chip("07", "섹터 시그널 종목 관찰",
               cap="섹터 상태 x 수급 x 공매도·대차 x 촉매", cap_col=7)
        for pair in (("동반강세", "수급유입"), ("수급이탈", "동반약세")):
            spans = {0: (1, 5), 1: (7, 11)}
            # 카드 타이틀 행
            for j, sig in enumerate(pair):
                c1, c2 = spans[j]
                tc = b.cell(c1, f"● {sig} — {sub.get(sig, '')}", "sect")
                fstyle(tc, rgb=SS_ST[sig], bold_=True)
                for c in range(c1 + 1, c2 + 1):
                    b.cell(c, "", "sect")
                b.mg(c1, c2)
            b.nl()
            n = max(len(ssm.get(s) or []) for s in pair) or 1
            for i in range(n):
                for j, sig in enumerate(pair):
                    c1, c2 = spans[j]
                    arr = ssm.get(sig) or []
                    if i == 0 and not arr:
                        b.cell(c1, "해당 섹터·조건 충족 종목 없음", "econ_cell")
                        for c in range(c1 + 1, c2 + 1):
                            b.cell(c, "", "econ_cell")
                        b.mg(c1, c2)
                        continue
                    if i >= len(arr):
                        continue
                    e = arr[i]
                    nm_c = b.cell(c1, e.get("name", ""), "body_bold")
                    sec_c = b.cell(c1 + 1, e.get("sector") or "", "econ_cell")
                    fstyle(sec_c, rgb=GRAY)
                    sc = e.get("score")
                    sc_c = b.cell(c1 + 2, sc if sc is not None else "", "econ_cell",
                                  fmt="+0;-0;0", align="right")
                    fstyle(sc_c, rgb=SS_ST[sig], bold_=True)
                    for c in range(c1 + 3, c2 + 1):
                        b.cell(c, "", "econ_cell")
                b.nl()
                for j, sig in enumerate(pair):        # 선정 이유 행(카드 폭 병합)
                    c1, c2 = spans[j]
                    arr = ssm.get(sig) or []
                    if i >= len(arr):
                        continue
                    rs = arr[i].get("reason", "")
                    b.cell(c1, rs, "watch_reason", wrap=True)
                    for c in range(c1 + 1, c2 + 1):
                        b.cell(c, "", "watch_reason")
                    b.mg(c1, c2); b.wrap_h(rs, c1, c2)
                b.nl()
            b.nl()
        un = (d.get("sectorScreen") or {}).get("universeNote") or ""
        if un:
            uc = b.cell(1, un + " · 관찰 우선순위이며 매매 권유 아님", "cap", wrap=True)
            for c in range(2, 12):
                b.cell(c, "", "cap")
            b.mg(1, 11); b.wrap_h(str(uc.value), 1, 11); b.nl()
        b.nl()
    else:
        # ── 폴백(구 아카이브): 07 수급 관찰 — '종목명|섹터명|사유' 분리 양식
        wn = syn.get("watchNotes") or {}
        if wn.get("long") or wn.get("short"):
            b.chip("07", "수급 관찰")
            b.hdr_row(["방향", "종목명", "섹터명", "관찰 사유"], merge_last_to=11)
            for label, proto, rows in (("상방 관찰", "watch_up", wn.get("long")),
                                       ("하방 관찰", "watch_dn", wn.get("short"))):
                for e in rows or []:
                    b.cell(1, label, proto)
                    b.cell(2, e.get("name", ""), "nb_cell")
                    sec_c = b.cell(3, _sector_name(e.get("name")) or "", "nb_cell")
                    fstyle(sec_c, rgb=GRAY)
                    basis = e.get("basis", "")
                    b.cell(4, basis, "watch_reason")
                    for c in range(5, 12):
                        b.cell(c, "", "watch_reason")
                    b.mg(4, 11); b.wrap_h(basis, 4, 11); b.nl()
            b.nl()

    # ── 08 / 09 서술형 흐름 ─────────────────────────────────────────────
    for no, title, hdr2, items in (("08", "주간 시장 흐름", "Market Flow", syn.get("weekNarrative")),
                                   ("09", "섹터·테마 흐름", "", syn.get("sectorRotation"))):
        if not items:
            continue
        b.chip(no, title)
        if hdr2:
            b.hdr_row(["No.", hdr2], merge_last_to=11)
        for t in items:
            t = "- " + str(t)
            b.cell(1, t, "flow_row")
            for c in range(2, 12):
                b.cell(c, "", "flow_row")
            b.mg(1, 11); b.wrap_h(t, 1, 11); b.nl()
        b.nl()

    # ── 10 촉매 타임라인 ────────────────────────────────────────────────
    # 시장 이벤트 행(종목 없음)은 04 일별 표 반응 코멘트와 중복 — 종목 행만 +
    # 첫 행 컬럼명 (2026-09-09 사용자 요청)
    tl = [t for t in (syn.get("catalystTimeline") or []) if t.get("stock")]
    if tl:
        b.chip("10", "주간 촉매 타임라인", cap="등락률 = 주간 누적", cap_col=10)
        # 섹터 컬럼(2026-09-09): 생성기 저장값(t.sector) 우선, 구 데이터는 로컬 룩업
        b.hdr_row(["날짜", "종목명", "시장", "섹터명", "별점", "등락률(%)", "핵심 촉매"],
                  merge_last_to=11)
        for t in tl:
            stock = t.get("stock")
            sec = t.get("sector") or _sector_name(stock, t.get("code")) or "—"
            b.cell(1, t.get("date", ""), "tl_cell")
            # 재등장 병합(2026-09-09): 종목명에 (n회), 촉매 셀에 이전 촉매 병기
            if (t.get("appearCount") or 1) > 1 and stock:
                stock = f"{stock} ({t['appearCount']}회)"
            b.cell(2, stock if stock else "—",
                   "tl_mkt_row" if not stock else "nb_cell")
            b.cell(3, t.get("market", ""), "tl_cell")
            sec_c = b.cell(4, sec, "tl_cell")
            fstyle(sec_c, rgb=GRAY)
            b.cell(5, "★" * (t.get("star") or 0), "tl_star")
            b.cell(6, t.get("changePct", ""), "num_pos", num="sign")
            ev = t.get("event", "")
            for hrow in (t.get("history") or []):
                ev += f"\n({str(hrow.get('date', ''))[5:]}) {hrow.get('event', '')}"
            b.cell(7, ev, "tl_event")
            for c in range(8, 12):
                b.cell(c, "", "tl_event")
            b.mg(7, 11); b.wrap_h(ev, 7, 11); b.nl()
        b.nl()

    # ── 11 다음 주 프리뷰 — 08 주간 시장 흐름과 동일한 서술형(2026-09-09):
    # 구분(상승/하방/판별/이벤트) 라벨 삭제, '- ' 접두 문장 풀폭 병합 나열
    nw = syn.get("nextWeek") or {}
    if nw:
        pv = (list(nw.get("upside") or []) + list(nw.get("downside") or [])
              + ([nw["signal"]] if nw.get("signal") else [])
              + list(nw.get("events") or []))
    else:
        pv = [t for _, t in _classify_preview(syn.get("nextWeekPreview"))]
    pv = [t for t in pv if str(t).strip()]
    if pv:
        b.chip("11", "다음 주 프리뷰")
        for t in pv:
            t = "- " + str(t)
            b.cell(1, t, "flow_row")
            for c in range(2, 12):
                b.cell(c, "", "flow_row")
            b.mg(1, 11); b.wrap_h(t, 1, 11); b.nl()

    b.finish()
    add_detail_sheets(wb, b, d)     # 접힘(토글) 섹션 상세 — 별도 탭 4종(2026-09-09)
    del wb["_tpl"]

    # 원본 데이터(평탄화 스트림) 탭 삭제 — 2026-09-09 사용자 요청. 상세 탭
    # 삽입 위치 계산(add_detail_sheets 의 RAW_SHEET index)이 끝난 뒤 제거한다.
    if RAW_SHEET in wb.sheetnames:
        del wb[RAW_SHEET]
    return wb, ws


def add_detail_sheets(wb, b, d):
    """대시보드에서 토글로 접힌 섹션들을 별도 탭으로 — 본 시트 디자인 시스템 준수.
    (2026-09-09 사용자 스펙: 시트별 역할 기반 열너비·#,##0 숫자서식·정렬 규칙·
    틀 고정·자동 필터·조건부 강조(근접도 단계/동반 수급/대차 방향/신호). 값은
    숫자 그대로(문자열 변환·단위 변환 금지), 데이터 없는 섹션의 탭은 만들지 않음."""
    F_EOK = "+#,##0;-#,##0;0"            # 부호 있는 억원 정수 (순매수 흐름)
    F_INT = "#,##0"                      # 무부호 정수 (공매도 누적·대차잔고)
    F_PCT = "+0.0;-0.0;0.0"              # 부호 있는 % (저장값 그대로, 표시만)
    F_EOK1 = "#,##0.0"                   # 억원 소수 1자리
    F_EOK1S = "+#,##0.0;-#,##0.0;0.0"
    TEAL, TEAL_BG = "FF0B7F8C", "FFEAF8FA"   # 본 시트 섹션 강조색과 동일 계열

    # 역할 기반 열너비 — 종목명/섹터명 분리(3차 스펙), 숫자 중간, 마지막 열들은
    # 기간(23자) 표시 폭 확보 겸용. len(dict)=시트 열 수(maxc).
    W_NH = {"A": 18.5, "B": 9.0, "C": 14.0, "D": 10.5, "E": 10.5, "F": 10.5,
            "G": 10.5, "H": 10.5, "I": 11.5, "J": 11.5, "K": 13.0, "L": 13.5}
    # 카드 그리드(2026-09-09 2차 스펙): 투자자별=2x2 카드(BUY|SELL 좌우),
    # 공매도·대차=3카드 가로 — 대시보드 토글 실화면(4카드 1행/3카드 1행) 기반,
    # 엑셀 폭 제약으로 투자자별만 2x2(스펙 §4 예시). 색상=대시보드(매수 적/매도 청).
    # 3차 스펙: 카드 내부도 종목명/섹터명 컬럼 분리 — 투자자별 A:Q, 공매도 A:P.
    W_INV = {"A": 4.5, "B": 18.0, "C": 12.5, "D": 10.5, "E": 4.5, "F": 18.0,
             "G": 12.5, "H": 10.5, "I": 2.0, "J": 4.5, "K": 18.0, "L": 12.5,
             "M": 10.5, "N": 4.5, "O": 18.0, "P": 12.5, "Q": 10.5}   # 종목명 18(한화에어로스페이스 폭)
    W_SL = {"A": 4.5, "B": 16.0, "C": 12.5, "D": 12.0, "E": 2.0, "F": 4.5,
            "G": 16.0, "H": 12.5, "I": 10.5, "J": 10.0, "K": 2.0, "L": 4.5,
            "M": 16.0, "N": 12.5, "O": 10.5, "P": 10.0}
    W_SEC = {"A": 20.5, "B": 12.0, "C": 10.5, "D": 10.5, "E": 10.5, "F": 10.5,
             "G": 12.0, "H": 12.9, "I": 13.0, "J": 13.0}
    W_SEC10 = {"A": 20.5, "B": 11.5, "C": 9.5, "D": 9.5, "E": 9.5, "F": 10.5,
               "G": 9.5, "H": 9.5, "I": 13.0, "J": 13.0}   # 기관 세분 10컬럼(F=투신(사모))

    def eok(v):
        return "" if v is None else round(v / 100)

    def bold(cell, color=None):
        f = copy(cell.font)
        f.bold = True
        if color:
            f.color = Color(rgb=color)
        cell.font = f

    period = d.get("weekStart", "") + " ~ " + d.get("weekEnd", "")

    def new_sheet(name, widths, meta=None):
        """meta=(기간 라벨 열, 기간 값 시작 열) — 기간 전체 표시가 가능한 위치로
        시트별 오버라이드(기본: maxc-2, maxc-1). 값은 시작 열~maxc 병합."""
        idx = wb.sheetnames.index(RAW_SHEET)   # 원본 데이터 앞에 순서대로 삽입
        ws2 = wb.create_sheet(name, idx)
        b.start(ws2, widths)
        mc = b.maxc
        label_c, val_c = meta or (mc - 2, mc - 1)
        # 공통 헤더 바 — 시트명 좌측 + 기간 우측(병합 폭 확보, 잘림 금지)
        b.cell(1, name, "title")
        for c in range(2, label_c):
            b.cell(c, "", "navy_fill")
        b.merges.append((1, 1, 1, label_c - 1))
        b.cell(label_c, "기간", "meta_label")
        b.cell(val_c, period, "meta_val")
        for c in range(val_c + 1, mc + 1):
            b.cell(c, "", "meta_val")
        b.mg(val_c, mc)
        b.nl(1, 20.25); b.nl()
        return ws2

    def footnote(text):
        """하단 각주 — 작은 회색 글씨, 전체 폭, wrap (2차 스펙 §21)."""
        cc = b.cell(1, text, "econ_cell", wrap=True)
        fstyle(cc, rgb=GRAY, size=8)
        for c in range(2, b.maxc + 1):
            b.cell(c, "", "econ_cell")
        b.mg(1, b.maxc); b.wrap_h(text, 1, b.maxc); b.nl()

    def note(text):
        b.cell(1, text, "cap")
        for c in range(2, b.maxc + 1):
            b.cell(c, "", "cap")
        b.mg(1, b.maxc); b.nl()

    # ── 탭 1: 52주 신고가 근접 — 랭킹/스크리너 표 (A:K) ─────────────────────
    nh = ((d.get("breadth") or {}).get("newHighs") or {}).get("stocks") or []
    if nh:
        ws2 = new_sheet("신고가 근접", W_NH)
        b.chip("D1", "52주 신고가 근접", cap="단위: 억원, %", cap_col=10)
        # 컬럼 그룹 헤더 — 종목 정보 / 수급 / 신고가 정보 (네이비 밴드로 구분)
        for c1, c2, t in ((1, 3, "종목 정보"), (4, 10, "수급 (주간 누적, 억원)"),
                          (11, 12, "신고가 정보")):
            b.cell(c1, t, "chip")
            for c in range(c1 + 1, c2 + 1):
                b.cell(c, "", "chip")
            b.mg(c1, c2)
        b.nl()
        # 종목명/시장/섹터명 분리 — 순서는 대시보드와 동일(2026-09-10)
        b.hdr_row(["종목명", "시장", "섹터명", "합산", "외인", "기관", "개인", "공매도",
                   "대차잔고", "대차증감", "주간등락(%)", "신고가대비(%)"], align="center")
        for s in nh:
            frgn, orgn = s.get("frgn"), s.get("orgn")
            total = ((frgn or 0) + (orgn or 0)
                     if (frgn is not None or orgn is not None) else None)
            near = s.get("nearRate")
            name_c = b.cell(1, s.get("name", ""), "nb_cell")
            b.cell(2, s.get("market") or "", "econ_cell", align="center")
            sec_c = b.cell(3, _sector_name(s.get("name"), s.get("code")) or "", "nb_cell")
            fstyle(sec_c, rgb=GRAY)
            tot_c = b.cell(4, eok(total) if total is not None else "",
                           "econ_cell", num="sign", fmt=F_EOK)
            b.cell(5, eok(frgn), "econ_cell", num="sign", fmt=F_EOK)
            b.cell(6, eok(orgn), "econ_cell", num="sign", fmt=F_EOK)
            b.cell(7, eok(s.get("prsn")), "econ_cell", num="sign", fmt=F_EOK)
            b.cell(8, s.get("shortSum") if s.get("shortSum") is not None else "",
                   "econ_cell", fmt=F_INT, align="right")
            b.cell(9, s.get("loanAmt") if s.get("loanAmt") is not None else "",
                   "econ_cell", fmt=F_INT, align="right")
            b.cell(10, s.get("loanChg") if s.get("loanChg") is not None else "",
                   "econ_cell", num="sign", fmt=F_EOK1S)
            b.cell(11, s.get("weekChgPct") if s.get("weekChgPct") is not None else "",
                   "econ_cell", num="sign", fmt=F_PCT)
            near_c = b.cell(12, -near if near else (0 if near == 0 else ""),
                            "econ_cell", fmt=F_PCT, align="right")
            # 근접도 단계 강조 (nearRate=신고가까지 남은 %): 0~2% 최강, 2~5% 중간
            if isinstance(near, (int, float)):
                if near <= 2:
                    bold(near_c, TEAL)
                    near_c.fill = PatternFill("solid", fgColor=TEAL_BG)
                    bold(name_c)
                elif near <= 5:
                    bold(near_c)
            # 외인·기관 동반 순매수/순매도 — 합산 굵게 (부호색은 num='sign')
            if isinstance(frgn, (int, float)) and isinstance(orgn, (int, float)) \
                    and (frgn > 0) == (orgn > 0) and frgn != 0 and orgn != 0:
                bold(tot_c)
            b.nl()
        data_end = b.r - 1
        b.nl()
        note("최신 거래일 기준 · 52주 신고가 대비 -10% 이내 (KIS 랭킹) · ETF·ETN 제외 · "
             "수급=주간 누적 확정(억원) · 동반 순매수(외인·기관 동일 방향)=합산 굵게")
        b.finish()
        ws2.freeze_panes = "A6"                       # 표 헤더(5행)까지 고정
        ws2.auto_filter.ref = f"A5:L{data_end}"

    # ── 탭 2: 투자자별 순매수 — 2x2 카드 그리드 (카드 내 BUY|SELL 좌우) ─────
    # 대시보드 토글 실화면: 투자자 4카드 1행 · 카드=순매수(적)/순매도(청) 목록.
    # 엑셀 폭 제약으로 2x2(2차 스펙 §4 예시), BUY/SELL 은 좌우 배치(§3).
    nc = d.get("netbuyCum") or {}
    # 2x4(2026-09-10): 1·2행 외국인·기관계 / 외인+기관(total, 합산 카드와 동일)·개인,
    # 3·4행 금융투자·투신(사모) / 연기금·보험. 구 아카이브(세분 없음)는 종전 2x2.
    has_det = bool(nc.get("finInv") or nc.get("insur") or nc.get("trust"))
    if has_det:
        inv_secs = [("F1", "외국인", nc.get("frgn")), ("F2", "기관계", nc.get("orgn")),
                    ("F3", "외인+기관", nc.get("total")), ("F4", "개인", nc.get("prsn"))]
    else:
        inv_secs = [("F1", "외국인", nc.get("frgn")), ("F2", "기관계", nc.get("orgn")),
                    ("F3", "연기금", nc.get("fund")), ("F4", "개인", nc.get("prsn"))]
    inv_secs2 = [("F5", "금융투자", nc.get("finInv")), ("F6", "투신(사모)", nc.get("trust")),
                 ("F7", "연기금", nc.get("fund")), ("F8", "보험", nc.get("insur"))]
    if any(v and ((v.get("top") or v.get("bottom"))) for _, _, v in inv_secs):
        ws2 = new_sheet("투자자별 순매수", W_INV, meta=(14, 15))
        # 외인·기관 동반 매수/매도 = 두 리스트 동시 등재 (실데이터 교집합만)
        def keyset(v, side):
            return {(e.get("code") or e.get("name"))
                    for e in ((v or {}).get(side) or [])}
        both = (keyset(nc.get("frgn"), "top") & keyset(nc.get("orgn"), "top")) \
            | (keyset(nc.get("frgn"), "bottom") & keyset(nc.get("orgn"), "bottom"))

        def inv_cards(pair):
            """카드 2개(좌 A~H, 우 J~Q)를 같은 행 대역에 나란히 그린다 —
            카드 내부 컬럼: 순위|종목명|섹터명|금액 x BUY/SELL(3차 스펙 §14)."""
            cols = [1, 10]
            # 카드 헤더 — 'F1 외국인' + 단위 캡션(우측), 네이비 바
            for (no, label, v), c0 in zip(pair, cols):
                b.cell(c0, f"{no} {label}", "meta_label")
                for c in range(c0 + 1, c0 + 4):
                    b.cell(c, "", "meta_label")
                b.mg(c0, c0 + 3)
                cap_c = b.cell(c0 + 4, "주간 누적 · 억원", "meta_val", align="right")
                fstyle(cap_c, size=8)
                for c in range(c0 + 5, c0 + 8):
                    b.cell(c, "", "meta_val")
                b.mg(c0 + 4, c0 + 7)
            b.nl(1, 18)
            # BUY / SELL 라벨 — 대시보드 색(매수 적/매도 청)
            for _, c0 in zip(pair, cols):
                lb = b.cell(c0, "순매수 상위", "econ_cell", align="center")
                fstyle(lb, rgb=BUY_RED, bold_=True)
                lb.fill = PatternFill("solid", fgColor=BUY_BG)
                for c in range(c0 + 1, c0 + 4):
                    x = b.cell(c, "", "econ_cell"); x.fill = PatternFill("solid", fgColor=BUY_BG)
                b.mg(c0, c0 + 3)
                ls = b.cell(c0 + 4, "순매도 상위", "econ_cell", align="center")
                fstyle(ls, rgb=SELL_BLUE, bold_=True)
                ls.fill = PatternFill("solid", fgColor=SELL_BG)
                for c in range(c0 + 5, c0 + 8):
                    x = b.cell(c, "", "econ_cell"); x.fill = PatternFill("solid", fgColor=SELL_BG)
                b.mg(c0 + 4, c0 + 7)
            b.nl()
            for _, c0 in zip(pair, cols):
                for j, t in enumerate(["순위", "종목명", "섹터명", "금액",
                                       "순위", "종목명", "섹터명", "금액"]):
                    b.cell(c0 + j, t, "hdr", align="center")
            b.nl()
            n = max([len((v or {}).get("top") or []) for _, _, v in pair]
                    + [len((v or {}).get("bottom") or []) for _, _, v in pair] + [1])
            for i in range(n):
                for (no, label, v), c0 in zip(pair, cols):
                    for side, off, rgb in (("top", 0, BUY_RED), ("bottom", 4, SELL_BLUE)):
                        rows_ = (v or {}).get(side) or []
                        if i >= len(rows_):
                            continue
                        e = rows_[i]
                        b.cell(c0 + off, i + 1, "econ_cell", align="center")
                        name_c = b.cell(c0 + off + 1, e.get("name", ""), "nb_cell")
                        sec_c = b.cell(c0 + off + 2,
                                       _sector_name(e.get("name"), e.get("code")) or "",
                                       "nb_cell")
                        fstyle(sec_c, rgb=GRAY)
                        if no in ("F1", "F2") and \
                                (e.get("code") or e.get("name")) in both:
                            bold(name_c)
                        amt_c = b.cell(c0 + off + 3, eok(e.get("amt")), "econ_cell",
                                       fmt="+#,##0;-#,##0;0", align="right")
                        fstyle(amt_c, rgb=rgb, bold_=True)
                b.nl()

        inv_cards(inv_secs[0:2])
        b.nl()
        inv_cards(inv_secs[2:4])
        b.nl()
        if has_det:
            inv_cards(inv_secs2[0:2])
            b.nl()
            inv_cards(inv_secs2[2:4])
            b.nl()
        footnote("외인·기관 동시 등재 종목=종목명 굵게(동반 매수/매도) · "
                 "netbuy_rank 일별 아카이브 합산 — 상위 30 리스트 등재일만 반영되는 근사치"
                 + (" · 금융투자=증권, 투신(사모)=투자신탁+사모펀드 (확정 병합분) · "
                    "외인+기관=합산 카드와 동일" if has_det else ""))
        b.finish()
        ws2.freeze_panes = "A3"                       # 타이틀 바(기간) 고정

    # ── 탭 3: 공매도·대차 — 3카드 가로 비교 (대시보드 3열 카드 동일) ────────
    slw = d.get("shortLoan") or {}
    if (slw.get("shortTop") or slw.get("loanUp") or slw.get("loanDown")):
        ws2 = new_sheet("공매도·대차", W_SL, meta=(13, 14))
        # 카드 내부 컬럼: 순위|종목명|섹터명|... (3차 스펙 §15)
        cards = [("S1 공매도 누적 상위(억)", 1, slw.get("shortTop"),
                  ["순위", "종목명", "섹터명", "공매도 누적"],
                  lambda e: [(e.get("amt"), "#,##0", "amt")]),
                 ("S2 대차잔고 증가(억)", 6, slw.get("loanUp"),
                  ["순위", "종목명", "섹터명", "증감", "잔고"],
                  lambda e: [(e.get("chg"), F_EOK1S, "chg"), (e.get("amt"), F_EOK1, "sec")]),
                 ("S3 대차잔고 감소 · 숏커버 추정(억)", 12, slw.get("loanDown"),
                  ["순위", "종목명", "섹터명", "증감", "잔고"],
                  lambda e: [(e.get("chg"), F_EOK1S, "chg"), (e.get("amt"), F_EOK1, "sec")])]
        # 카드 헤더(네이비 바) — 3카드 같은 행
        for title, c0, rows_, hdr, _vals in cards:
            b.cell(c0, title, "meta_label")
            for c in range(c0 + 1, c0 + len(hdr)):
                b.cell(c, "", "meta_label")
            b.mg(c0, c0 + len(hdr) - 1)
        b.nl(1, 18)
        for title, c0, rows_, hdr, _vals in cards:
            for j, t in enumerate(hdr):
                b.cell(c0 + j, t, "hdr", align="center")
        b.nl()
        n = max(len(slw.get(k) or []) for k in ("shortTop", "loanUp", "loanDown"))
        for i in range(n):
            for title, c0, rows_, hdr, vals in cards:
                if i >= len(rows_ or []):
                    continue
                e = rows_[i]
                b.cell(c0, i + 1, "econ_cell", align="center")
                b.cell(c0 + 1, e.get("name", ""), "nb_cell")
                sec_c = b.cell(c0 + 2, _sector_name(e.get("name")) or "", "nb_cell")
                fstyle(sec_c, rgb=GRAY)
                for j, (v, fm, kind) in enumerate(vals(e)):
                    cc = b.cell(c0 + 3 + j, v if v is not None else "", "econ_cell",
                                fmt=fm, align="right")
                    if kind == "amt":                 # S1 Primary — 굵게
                        fstyle(cc, bold_=True)
                    elif kind == "chg":               # 증감 Primary — 방향색+굵게
                        if isinstance(v, (int, float)) and v:
                            fstyle(cc, rgb=BUY_RED if v > 0 else SELL_BLUE, bold_=True)
                        else:
                            fstyle(cc, bold_=True)
                    else:                             # 잔고 Secondary — 회색
                        fstyle(cc, rgb=GRAY)
            b.nl()
        b.nl()
        footnote("랭킹 유니버스(순매수 상위 30 등재 종목) 한정 · 대차 증감=주초 대비 최신 잔고")
        b.finish()
        ws2.freeze_panes = "A3"                       # 타이틀 바(기간) 고정

    # ── 탭 4: 섹터 x 수급 매트릭스 — 등락률 정렬 유지, 신호는 저장값 렌더 ────
    # V1(2026-09-09): 신호 판정은 generate_weekly_briefing.classify_sector_signal
    # 이 JSON 에 저장한 값을 그대로 사용 — 웹/엑셀 이중 구현 금지(§16-17).
    sf = ((d.get("sectorFlow") or {}).get("rows")) or []
    if sf:
        has_v1 = any("signal" in r0 for r0 in sf)
        has_det = any(r0.get("finInv") is not None for r0 in sf)
        if has_v1:
            W = {"A": 20.5, "B": 8.2, "C": 8.2, "D": 8.2, "E": 8.2, "F": 9.5,
                 "G": 9.5, "H": 10.0, "I": 9.5, "J": 10.5, "K": 9.5, "L": 9.5,
                 "M": 9.5, "N": 11.5}
            ws2 = new_sheet("섹터x수급", W, meta=(11, 12))
            b.chip("M1", "섹터 x 수급 매트릭스", cap="가격 %, 수급 억원", cap_col=12)
            # 세분 순서 = 투자자별 2행(금융투자·투신(사모)·연기금·보험, 2026-09-10)
            hdr = ["업종", "YTD", "3M", "1M", "1W", "외인", "기관(합)", "외인+기관",
                   "금융투자", "투신(사모)", "연기금", "보험", "개인", "신호"]
            b.hdr_row(hdr, align="center")
            # 신호별 강조(배지 셀만, 행 전체 채색 금지 §13)
            SIG_ST = {"동반강세": ("FFD64545", "FFFDEEEE"),
                      "수급이탈": ("FFB45309", "FFFFF4E0"),
                      "수급유입": ("FF0B7F8C", "FFEAF8FA"),
                      "동반약세": ("FF3182F6", "FFEAF1FD"),
                      "데이터부족": ("FF6B7280", None)}
            for r0 in sf:
                b.cell(1, r0.get("name", ""), "nb_cell")
                for j, k in enumerate(("ytd", "m3", "m1", "chgPct")):
                    v = r0.get(k)
                    cc = b.cell(2 + j, v if v is not None else "", "econ_cell",
                                fmt=F_PCT, align="right")
                    if isinstance(v, (int, float)) and v:
                        fstyle(cc, rgb=BUY_RED if v > 0 else SELL_BLUE)
                for j, k in enumerate(("frgn", "orgn", "coreFlow",
                                       "finInv", "trust", "fund", "insur", "prsn")):
                    v = r0.get(k)
                    cc = b.cell(6 + j, v if v is not None else "", "econ_cell",
                                fmt=F_EOK, align="right")
                    if isinstance(v, (int, float)) and v:
                        fstyle(cc, rgb=BUY_RED if v > 0 else SELL_BLUE)
                sig = r0.get("signal") or ""
                if sig == "혼조":                 # 혼조는 저장만, 표기 생략
                    sig = ""
                sc = b.cell(14, sig, "econ_cell", align="center")
                if sig in SIG_ST:
                    rgb, fill = SIG_ST[sig]
                    fstyle(sc, rgb=rgb, bold_=True)
                    if fill:
                        sc.fill = PatternFill("solid", fgColor=fill)
                b.nl()
            data_end = b.r - 1
            b.nl()
            note("가격추세=YTD·3M·1M·1W(%) 중 3개 이상 동일 방향 · 핵심수급(외인+기관)=주간 누적 · KIS 업종(KRX 산업분류)")
            note("동반강세=가격강세+수급유입 · 수급이탈=가격강세+수급이탈 · 수급유입=가격약세+수급유입 · "
                 "동반약세=가격약세+수급이탈 · 공란=혼조(가격방향 불명확) · 데이터부족=일부 기간 결측")
            b.finish()
            ws2.freeze_panes = "A5"
            ws2.auto_filter.ref = f"A4:N{data_end}"
            ws2.conditional_formatting.add(          # 1W 데이터 막대(값 표시 유지)
                f"E5:E{data_end}",
                DataBarRule(start_type="min", end_type="max", color="19B6C9", showValue=True))
        else:
            # ── 폴백(구 아카이브, 1W 기반 근사) — '과열주의' 용어 폐기 → 수급이탈
            ws2 = new_sheet("섹터x수급", W_SEC10 if has_det else W_SEC)
            b.chip("M1", "섹터 x 수급 매트릭스", cap="주간 등락 vs 투자자 순매수(억)", cap_col=8)
            if has_det:
                hdr = ["업종", "주간등락(%)", "외인", "기관(합)", "금융투자", "투신(사모)",
                       "연기금", "보험", "개인", "신호"]
                keys = ("frgn", "orgn", "finInv", "trust", "fund", "insur", "prsn")
            else:
                hdr = ["업종", "주간등락(%)", "외인", "기관", "연기금", "개인", "신호"]
                keys = ("frgn", "orgn", "fund", "prsn")
            sig_c = len(hdr)
            b.hdr_row(hdr, align="center")
            for r0 in sf:
                smart = (r0.get("frgn") or 0) + (r0.get("orgn") or 0)
                sig = ("수급이탈" if (r0.get("chgPct") or 0) >= 1.5 and smart < 0
                       else "수급유입" if (r0.get("chgPct") or 0) <= -1.5 and smart > 0 else "")
                b.cell(1, r0.get("name", ""), "nb_cell")
                b.cell(2, r0.get("chgPct", ""), "econ_cell", num="sign", fmt=F_PCT)
                for j, k in enumerate(keys):
                    b.cell(3 + j, r0.get(k, ""), "econ_cell", num="sign", fmt=F_EOK)
                b.cell(sig_c, sig, "watch_up" if sig == "수급유입" else "watch_dn" if sig
                       else "econ_cell", align="center")
                b.nl()
            data_end = b.r - 1
            b.nl()
            note("KIS 업종(KRX 산업분류) 일별 확정 합산 · 수급이탈=주가 상승+수급 이탈 · 수급유입=주가 하락+수급 유입"
                 + (" · 기관 세분(금융투자·보험·투신(사모)·연기금)은 기관(합)의 하위 항목" if has_det else ""))
            b.finish()
            ws2.freeze_panes = "A5"
            ws2.auto_filter.ref = f"A4:{get_column_letter(sig_c)}{data_end}"
            ws2.conditional_formatting.add(
                f"B5:B{data_end}",
                DataBarRule(start_type="min", end_type="max", color="19B6C9", showValue=True))


def _disp_w(text):
    """표시 폭(wch) 추정 — 한글/전각 2, 그 외 1.1(여유계수)."""
    return sum(2 if ord(ch) > 0x2E80 else 1.1 for ch in str(text or ""))


def validate(ws, d):
    """저장 전 검증 — 레이아웃(19항) + 섹션별 필수값 + 전 시트 텍스트 잘림 검사.
    실패 시 (False, 사유목록) — 파일을 저장하지 않는다."""
    errs = []
    if ws.max_column > 11:              # 종목명/섹터명 분리로 A:K (3차 스펙 §9)
        errs.append(f"A:K 초과 (열 {ws.max_column})")

    chips = {}
    for r in range(1, ws.max_row + 1):
        for c in (1, 5):
            v = ws.cell(r, c).value
            if isinstance(v, str) and v in ("01", "02", "03", "04", "05", "06",
                                            "07", "08", "09", "10", "11"):
                chips.setdefault(v, r)
    if not ("01" in chips and "02" in chips and chips["01"] == chips["02"]):
        errs.append("01/02 좌우 배치 아님")
    order = [chips[k] for k in ("03", "04", "06", "10") if k in chips]
    if order != sorted(order):
        errs.append("섹션 순서 어긋남")

    def _blank(v):
        return v is None or str(v).strip() == ""

    # 01 주간 누적 수급 — KOSPI/KOSDAQ x 개인/외인/기관 + 02 US 5지수
    r01 = chips.get("01")
    if r01:
        for dr, mk in ((2, "KOSPI"), (3, "KOSDAQ")):
            for c in (2, 3, 4):
                if _blank(ws.cell(r01 + dr, c).value):
                    errs.append(f"01 {mk} 수급 결측(열{c})")
        for c in range(5, 10):
            if _blank(ws.cell(r01 + 2, c).value):
                errs.append(f"02 US 지수 결측(열{c})")
        # 섹터 순위 — 그룹 4종 x 순위 행(1~3위) x [순위|섹터명|누적수익률] 컬럼
        # (3차 스펙: 순위를 가로로 펼치지 않고 행 유지, 섹터명/수익률 분리)
        rr = r01 + 6                    # +5=순위표 헤더행, 데이터는 +6부터
        seen = {}
        for i in range(12):
            lab = str(ws.cell(rr + i, 1).value or "")
            if lab not in ("US 상승 섹터", "US 하락 섹터", "KR 상승 섹터", "KR 하락 섹터"):
                break
            seen[lab] = seen.get(lab, 0) + 1
            for c, nm in ((2, "순위"), (3, "섹터명"), (4, "수익률")):
                if _blank(ws.cell(rr + i, c).value):
                    errs.append(f"섹터 순위 {rr+i}행 {nm} 결측")
        for need in ("US 상승 섹터", "US 하락 섹터", "KR 상승 섹터", "KR 하락 섹터"):
            if not seen.get(need):
                errs.append(f"섹터 순위 '{need}' 없음")

    # 03 경제지표 — 판정 포함 필수열
    r03, r04 = chips.get("03"), chips.get("04")
    if r03 and r04:
        for r in range(r03 + 2, r04 - 1):
            if _blank(ws.cell(r, 1).value):
                continue
            for c, nm in ((2, "국가"), (3, "지표"), (4, "실제"), (8, "판정")):
                if _blank(ws.cell(r, c).value):
                    errs.append(f"03 {r}행 {nm} 결측")

    # 04 일별 요약 — 7컬럼 (▲▼는 '데이터 없음' 허용, 빈칸 불가)
    r05 = chips.get("05") or chips.get("06") or ws.max_row
    if r04:
        for r in range(r04 + 2, r05 - 1):
            if _blank(ws.cell(r, 1).value):
                continue
            for c in (2, 3, 4, 5, 6, 7):
                if _blank(ws.cell(r, c).value):
                    errs.append(f"04 {r}행 열{c} 결측")

    # 10 촉매 타임라인 — 종목 행 필수 필드 (시장 이벤트 행 '—' 예외)
    r10, r11 = chips.get("10"), chips.get("11")
    if r10:
        for r in range(r10 + 1, (r11 or ws.max_row + 2) - 1):
            if _blank(ws.cell(r, 1).value):
                continue
            if str(ws.cell(r, 2).value or "") == "—":
                continue
            for c, nm in ((2, "종목"), (3, "시장"), (4, "섹터"), (5, "별점"),
                          (6, "등락률"), (7, "내용")):
                if _blank(ws.cell(r, c).value):
                    errs.append(f"10 {r}행 {nm} 결측")

    # 11 프리뷰 — 서술형('- ' 접두) 행 1개 이상, 구분 라벨 없음(2026-09-09)
    if r11:
        n11, r = 0, r11 + 1
        while r <= ws.max_row and not _blank(ws.cell(r, 1).value):
            v = str(ws.cell(r, 1).value)
            if not v.startswith("- "):
                errs.append(f"11 {r}행 '-' 접두 없음: {v[:20]}")
            n11 += 1
            r += 1
        if n11 == 0:
            errs.append("11 프리뷰 행 없음")

    # 전 시트 텍스트 잘림 검사 — wrap 셀은 행높이, 비wrap 셀은 인접 셀 충돌 폭
    merged_at = {}
    for m in ws.merged_cells.ranges:
        merged_at[(m.min_row, m.min_col)] = (m.min_col, m.max_col)
    inside = set()
    for m in ws.merged_cells.ranges:
        for c in range(m.min_col + 1, m.max_col + 1):
            inside.add((m.min_row, c))
    widths = {c: (W_MAIN[get_column_letter(c)]) for c in range(1, 12)}
    for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=11):
        for cell in row:
            v = cell.value
            if _blank(v) or (cell.row, cell.column) in inside:
                continue
            c1, c2 = merged_at.get((cell.row, cell.column), (cell.column, cell.column))
            span = sum(widths[c] for c in range(c1, c2 + 1))
            need = _disp_w(v)
            if cell.alignment.wrap_text:
                lines = max(1, math.ceil(need / span))
                h = ws.row_dimensions[cell.row].height or 15.0
                if lines > 1 and h < lines * 12.5:
                    errs.append(f"{cell.row}행 열{c1} wrap 행높이 부족({h}<{lines}줄)")
            else:
                nxt = ws.cell(cell.row, c2 + 1).value if c2 < 11 else None
                if need > span and not _blank(nxt):
                    errs.append(f"{cell.row}행 열{c1} 잘림({str(v)[:14]}…)")
    return (not errs), sorted(set(errs))[:15]


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
