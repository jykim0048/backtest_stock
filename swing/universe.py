"""업종별 종목 수급 모멘텀 유니버스(2026-10-13, PLAN_UNIVERSE.md §3) — 매일 16:30 크론이 산출해
store `universe/<date>` 에 저장하고 대시보드 「유니버스」 탭이 보여준다.
**후보 소스**: 매트릭스(섹터 시그널 칸)와 **독립 소스**로 나란히 돌린다(병합 아님, 사용자 결정 2026-10-13) —
두 소스 모두 아래 score_stock 의 **같은 모멘텀 점수(−5~+5)** 로 채점·정렬하고(attach_scores / universe_candidates),
진입 규칙만 다르다: 매트릭스 = 칸 종목 전부 분석(점수순, 상한 MAX_NEW_PER_DAY) · 유니버스 = pick() 의 선정 방식
(config.UNI_PICK_MODE, 2026-10-14 A/B/C 시뮬 뒤 사용자 결정 = **roundrobin**: 방향 업종을 1위 점수순으로 돌며 업종당 1개씩,
자리가 남으면 2바퀴째)으로 점수 ≥ UNI_CAND_MIN 인 종목을 보유·대기·매트릭스 후보·쿨다운 제외 후 UNI_NEW_PER_DAY 개.
rank(통합 순위)·sectorcap(통합+업종당 N)은 노브로 남김. 주문·포지션·거래에 source 가 남는다.

파이프라인
  U1 업종 목록      DB증권 USTOCKCOND(K)            → 주간 브리핑 sectorFlow 24행과 이름(norm_sec)으로 조인
  U2 구성종목       DB증권 USTOCKCONDLIST(sort 42)   → 종목·시총(Avls)·당일 거래량·거래대금
  U3 1차 컷         업종별 당일 거래대금 상위 K ∪ 시총 상위 M · 리츠/ETF/우선주 제외
  U4 종목 일별      DB증권 DAYSTOCKTJJ(25영업일 1콜)  → 주간 거래대금·직전 4주 평균·외인/기관 순매수·1W 등락·유동성
  U5 공매도·대차    KIS 허브 /flow?code=&rows=30(업종별 예비 상위 N 만) → 공매도 비중 1W·Δ, 대차잔고 Δ
  U6 점수·Top10     섹터 신호로 방향(상방→Long·하방→Short·혼조/대분류→미산출) → 업종별 Top10

DB증권 토큰은 DB 허브가 Redis `db:token` 에 둔 것을 읽기만 한다(prices.DbHubPrices 와 같은 규칙).
단위: 금액은 전부 억원. DB증권 응답 단위(원/천원/100만원)는 1차 실측 전이라 노브(config.UNI_*_DIV)로 두고
첫 원본 행을 diag.rawSample 에 남겨 /api/swing/universe 로 검증한다.
"""
import concurrent.futures
import datetime
import re
import time
import traceback

from . import config, engine, signals, tradedays
from .prices import DbHubPriceError, DbHubPrices

BIG_SECTORS = ("제조", "금융")               # KIS 대분류 — 중분류(전기·전자·은행 등)와 종목이 겹쳐 산출 제외
UP_SIGNALS, DOWN_SIGNALS = ("동반강세", "수급유입"), ("동반약세", "수급이탈")
_EXCL_NAME = re.compile(r"(리츠|REIT|ETF|ETN|스팩|SPAC|KODEX|TIGER|KBSTAR|HANARO|인버스|레버리지|선물|채권)", re.I)
_PREF_NAME = re.compile(r"(우|우B|우C|\(전환\))$")


# ─────────────────────────── DB증권 조회 클라이언트 ───────────────────────────
class DbQuote:
    """DB증권 REST 조회 전용(시세·업종·종목 TR 4개). 주문 경로는 없다.
    경로별 TPS(USTOCKCOND/LIST·DAYSTOCKTJJ 2, FCODES 3) 를 호출 간격으로 지키고, 응답 헤더
    cont_yn/cont_key 로 연속조회를 따라간다(최대 pages). 응답 헤더를 못 읽는 post(테스트) 는 1페이지."""
    P_IND = "/api/v1/quote/kr-stock/inquiry/industry-cls"
    P_COMP = "/api/v1/quote/kr-stock/inquiry/industry-components"
    P_INV = "/api/v1/quote/kr-stock/inquiry/daily-investor"
    P_FUT = "/api/v1/quote/kr-futureoption/inquiry/future-ticker"
    P_CHART = "/api/v1/quote/kr-chart/day"
    TPS = {P_IND: 2, P_COMP: 2, P_INV: 2, P_FUT: 3, P_CHART: 4}

    def __init__(self, token_source=None, rest_base=None, *, post=None, sleep=time.sleep, now=time.time, log=print):
        self.tok = token_source or DbHubPrices()
        self.rest_base = (rest_base or self.tok.rest_base).rstrip("/")
        self._post, self.sleep, self.now, self.log = post, sleep, now, log
        self._last = {}
        self.calls = 0
        self._token = None

    def token(self):
        if self._token is None:
            self._token = self.tok._token()
        return self._token

    def raw(self, path, body):
        """1페이지 원본 JSON(진단용) — rsp_cd 와 무관하게 응답 그대로."""
        post = self._post
        if post is None:
            import requests
            post = requests.post
        headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self.token()}",
                   "cont_yn": "N", "cont_key": ""}
        r = post(self.rest_base + path, headers=headers, json={"In": body}, timeout=15)
        self.calls += 1
        try:
            data = r.json()
        except Exception:
            data = {"_text": getattr(r, "text", "")[:500]}
        h = getattr(r, "headers", None) or {}
        return {"status": getattr(r, "status_code", None), "cont_yn": h.get("cont_yn"), "cont_key": h.get("cont_key"), "body": data}

    @staticmethod
    def _rows_of(data):
        """응답 → 행 리스트. Out 가 리스트면 그대로, dict 면 그 안의 리스트 값(Out1/Out2 등) 우선, 없으면 [dict]."""
        out = data.get("Out")
        if out is None:
            for k in ("Out2", "Out1", "out", "OUT"):
                if data.get(k) is not None:
                    out = data[k]
                    break
        if isinstance(out, dict):
            lists = [v for v in out.values() if isinstance(v, list)]
            if lists:
                return [x for v in lists for x in v]
            return [out]
        if isinstance(out, list):
            return out
        return []

    def post(self, path, body, pages=5):
        """→ Out 행 리스트(연속조회 합산). rsp_cd != 00000 이면 RuntimeError."""
        post = self._post
        if post is None:
            import requests
            post = requests.post
        rows, cont_yn, cont_key = [], "N", ""
        for _ in range(max(1, pages)):
            gap = 1.0 / self.TPS.get(path, 2) + 0.02 - (self.now() - self._last.get(path, 0.0))
            if gap > 0:
                self.sleep(gap)
            self._last[path] = self.now()
            headers = {"content-type": "application/json; charset=utf-8", "authorization": f"Bearer {self.token()}",
                       "cont_yn": cont_yn, "cont_key": cont_key}
            r = post(self.rest_base + path, headers=headers, json={"In": body}, timeout=15)
            self.calls += 1
            if getattr(r, "status_code", 200) != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            data = r.json()
            if str(data.get("rsp_cd")) != "00000":
                raise RuntimeError(f"{data.get('rsp_cd')} {data.get('rsp_msg')}")
            rows.extend(self._rows_of(data))
            h = getattr(r, "headers", None) or {}
            cont_yn, cont_key = str(h.get("cont_yn", "N")).upper(), str(h.get("cont_key", "") or "")
            if cont_yn != "Y" or not cont_key:
                break
        return rows

    def industry_codes(self, mkt="K"):
        """USTOCKCOND → [{code, name}] (코스피 32업종: 1001 종합 … 1027 제조 · KIS 0001 체계와 다름)."""
        out = []
        for body in ({"InputCondMrktDivCode": mkt, "InputMrktClsCode": "U"}, {"InputCondMrktDivCode": "U", "InputMrktClsCode": mkt},
                     {"InputCondMrktDivCode": mkt}):
            try:
                rows = self.post(self.P_IND, body, pages=3)
            except Exception as e:
                self.log(f"[swing] USTOCKCOND {body}: {e}")
                continue
            for r in rows:
                code, name = _pick_code_name(r)
                if code and name:
                    out.append({"code": code, "name": name})
            if out:
                break
        return out

    def industry_components(self, code, sort="42"):
        """USTOCKCONDLIST(sort 42=거래대금·2=시총 DESC) → 원본 행(Iscd, KorIsnm, Avls(100만원), AcmlVol, AcmlTrPbmn, PrdyCtrt…)."""
        return self.post(self.P_COMP, {"InputBstpIscd": code, "InputRankSortClsCode1": sort,
                                       "InputCondMrktDivCode": "UJ"}, pages=5)

    def daily_investor(self, code, d1, d2):
        """DAYSTOCKTJJ(기간 지정) → 원본 행(Date, Prpr, AcmlVol, AcmlTrPbmn, Orgn/FrgnReg/Prsn × Shnu/Seln × Vol/TrPbmn)."""
        return self.post(self.P_INV, {"InputCondMrktDivCode": "J", "InputIscd1": code,
                                      "InputDate1": d1.replace("-", ""), "InputDate2": d2.replace("-", "")}, pages=3)

    def chart_day(self, code, d1, d2):
        """CHARTDAY 기간 조회(수정주가 미사용, KRX) → 원본 행(Date, Oprc, Hprc, Lprc, Prpr, CntgVol) — 시뮬레이션용."""
        return self.post(self.P_CHART, {"InputOrgAdjPrc": "0", "InputCondMrktDivCode": "J", "InputIscd1": code,
                                        "InputDate1": d1.replace("-", ""), "InputDate2": d2.replace("-", "")}, pages=5)

    def stock_future_underlyings(self):
        """FCODES(JF) → {기초자산 현물코드} — 숏 실주문 전제(주식선물 매도) 플래그용."""
        rows = self.post(self.P_FUT, {"InputCondMrktDivCode": "JF"}, pages=10)
        return {str(r.get("UnasShrnIscd") or "").strip().zfill(6) for r in rows if r.get("UnasShrnIscd")}


class HubFlow:
    """KIS 허브 /flow?code=&rows=N — shorts[{date, qty, volRlim, pbmn, pbmnRlim}] · loans[{date, rmndAmt(억)…}].
    허브 캐시 10분. workers 개 병렬(허브는 종목당 KIS 3콜을 직렬 호출하므로 4 이상은 올리지 않는다)."""

    def __init__(self, base=None, *, get=None, rows=30, workers=4, timeout=20, log=print):
        self.base = (base or config.FLOW_API_BASE).rstrip("/")
        self._get, self.rows, self.workers, self.timeout, self.log = get, rows, workers, timeout, log
        self.calls = 0

    def one(self, code):
        get = self._get
        if get is None:
            import requests
            get = requests.get
        r = get(f"{self.base}/flow", params={"code": code, "rows": self.rows}, timeout=self.timeout)
        self.calls += 1
        if getattr(r, "status_code", 200) != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        d = r.json()
        if d.get("status") != "success":
            raise RuntimeError(f"status={d.get('status')}")
        return {"shorts": d.get("shorts") or [], "loans": d.get("loans") or []}

    def many(self, codes):
        out, errs = {}, []
        if not codes:
            return out, errs
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, self.workers)) as ex:
            fut = {ex.submit(self.one, c): c for c in codes}
            for f in concurrent.futures.as_completed(fut):
                c = fut[f]
                try:
                    out[c] = f.result()
                except Exception as e:
                    errs.append(f"{c}: {type(e).__name__}: {str(e)[:80]}")
        return out, errs


def _pick_code_name(row):
    """업종 행에서 (코드, 이름) — 문서상 Iscd/KorIsnm 이지만 실제 필드명이 다를 수 있어 폴백: 코드 = 첫 3~4자리 숫자 값,
    이름 = 한글이 든 가장 긴 문자열."""
    if not isinstance(row, dict):
        return None, None
    code = str(row.get("Iscd") or row.get("BstpIscd") or row.get("InputBstpIscd") or "").strip()
    name = str(row.get("KorIsnm") or row.get("BstpKorIsnm") or row.get("HtsKorIsnm") or "").strip()
    if not code:
        for v in row.values():
            v = str(v or "").strip()
            if v.isdigit() and 3 <= len(v) <= 4:
                code = v
                break
    if not name:
        cands = [str(v).strip() for v in row.values() if isinstance(v, str) and re.search(r"[가-힣]", v)]
        name = max(cands, key=len) if cands else ""
    return code, name


# ─────────────────────────── 순수 계산 ───────────────────────────
def _num(v, default=None):
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s not in ("", "-", "None") else default
    except (TypeError, ValueError):
        return default


def _r(v, nd=1):
    return None if v is None else round(v, nd)


def is_excluded(code, name):
    """리츠·ETF·ETN·스팩·인버스/레버리지·우선주(코드 끝자리 ≠ 0 또는 이름 '우' 접미) 제외."""
    if not code or len(code) != 6:
        return True
    if _EXCL_NAME.search(name or ""):
        return True
    if code[-1] != "0" or _PREF_NAME.search((name or "").strip()):
        return True
    return False


def direction_of(signal, name):
    if name in BIG_SECTORS:
        return None
    if signal in UP_SIGNALS:
        return "long"
    if signal in DOWN_SIGNALS:
        return "short"
    return None


def day_tv(row):
    """DAYSTOCKTJJ 행의 거래대금(원) — 2026-10-14 실측: `AcmlTrPbmn` 이 빈 문자열로 옴 → 거래량(AcmlVol) × 종가(Prpr) 로 대체."""
    v = _num(row.get("AcmlTrPbmn"), 0.0) or 0.0
    if v > 0:
        return v
    vol, px = _num(row.get("AcmlVol"), 0.0) or 0.0, _num(row.get("Prpr"), 0.0) or 0.0
    return vol * px


def weekly_metrics(rows, date, cap_eok, *, tv_div, flow_div):
    """DAYSTOCKTJJ 행 → 1W/4W 지표(억원). rows 는 날짜 혼합 순서여도 됨. 거래일 5개 미만이면 None."""
    by = {}
    for r in rows:
        d = str(r.get("Date") or r.get("date") or "").strip()
        if len(d) == 8 and d.isdigit():
            by[d] = r
    days = sorted(k for k in by if k <= date.replace("-", ""))
    if len(days) < 5:
        return None
    w1, w4 = days[-5:], days[-25:-5]

    def tv(d):
        return day_tv(by[d]) / tv_div

    def net(d, who):
        buy = _num(by[d].get(f"{who}ShnuTrPbmn"), 0.0) or 0.0
        sell = _num(by[d].get(f"{who}SelnTrPbmn"), 0.0) or 0.0
        return (buy - sell) / flow_div

    tv1w = sum(tv(d) for d in w1)
    tv4w = (sum(tv(d) for d in w4) / (len(w4) / 5.0)) if len(w4) >= 5 else None   # 주평균
    tv20 = (sum(tv(d) for d in days[-20:]) / min(20, len(days))) if days else 0.0
    frgn = sum(net(d, "FrgnReg") for d in w1)
    orgn = sum(net(d, "Orgn") for d in w1)
    prsn = sum(net(d, "Prsn") for d in w1)
    c_last = _num(by[w1[-1]].get("Prpr"))
    c_prev = _num(by[days[-6]].get("Prpr")) if len(days) >= 6 else None
    ret1w = (c_last / c_prev - 1) * 100 if c_last and c_prev else None
    return {"tv1w": _r(tv1w, 0), "tv4w": _r(tv4w, 0), "tv20d": _r(tv20, 1),
            "tvMom": _r(tv1w / tv4w - 1, 3) if tv4w else None,
            "frgn1w": _r(frgn, 0), "orgn1w": _r(orgn, 0), "prsn1w": _r(prsn, 0),
            "flowBp": _r((frgn + orgn) / cap_eok * 1e4, 1) if cap_eok else None,
            "ret1w": _r(ret1w, 2), "close": c_last, "days": len(days)}


def short_loan_metrics(flow, cap_eok):
    """허브 /flow → 공매도 비중 1W 평균·Δ(직전 4주 대비 pp), 대차잔고 주간 증감(억·bp). 행은 최신순."""
    sh = [x for x in (flow or {}).get("shorts") or [] if x.get("volRlim") is not None]
    out = {"shortRlim1w": None, "shortMom": None, "loanChg": None, "loanBp": None}
    if len(sh) >= 5:
        a = [float(x["volRlim"]) for x in sh[:5]]
        b = [float(x["volRlim"]) for x in sh[5:25]]
        out["shortRlim1w"] = _r(sum(a) / len(a), 2)
        if len(b) >= 5:
            out["shortMom"] = _r(sum(a) / len(a) - sum(b) / len(b), 2)
    ln = [x for x in (flow or {}).get("loans") or [] if x.get("rmndAmt") is not None]
    if len(ln) >= 6:
        chg = float(ln[0]["rmndAmt"]) - float(ln[5]["rmndAmt"])
        out["loanChg"] = _r(chg, 0)
        out["loanBp"] = _r(chg / cap_eok * 1e4, 1) if cap_eok else None
    return out


def score_stock(m, side):
    """방향별 −5~+5. 구성: 거래대금 가속 ±2 · 수급/시총 ±2 · 공매도 비중 Δ ±1 · 대차 Δ ±1 · 1W 가격 일치 +1 ·
    공매도 비중 높음(Short 만) +1. m 의 결측 항목은 0 점."""
    s, why = 0, []
    sign = 1 if side == "long" else -1
    tvm = m.get("tvMom")
    if tvm is not None:
        p = 2 if tvm >= config.UNI_TV_HI else 1 if tvm >= config.UNI_TV_LO else -1 if tvm <= -config.UNI_TV_LO else 0
        s += p
        if p:
            why.append(f"거래대금 {tvm * 100:+.0f}%")
    fb = m.get("flowBp")
    if fb is not None:
        f = fb * sign
        p = 2 if f >= config.UNI_FLOW_HI else 1 if f >= config.UNI_FLOW_LO else -1 if f <= -config.UNI_FLOW_LO else 0
        s += p
        if p:
            why.append(f"외인+기관 {(m.get('frgn1w') or 0) + (m.get('orgn1w') or 0):+,.0f}억")
    sm = m.get("shortMom")
    if sm is not None:
        v = -sm * sign                                   # Long 은 감소가 +, Short 은 증가가 +
        p = 1 if v >= config.UNI_SHORT_DELTA else -1 if v <= -config.UNI_SHORT_DELTA else 0
        s += p
        if p:
            why.append(f"공매도 비중 {sm:+.1f}pp")
    lb = m.get("loanBp")
    if lb is not None:
        v = -lb * sign
        p = 1 if v >= config.UNI_LOAN_BP else -1 if v <= -config.UNI_LOAN_BP else 0
        s += p
        if p:
            why.append(f"대차 {m['loanChg']:+,.0f}억" if m.get("loanChg") is not None else f"대차 {lb:+.0f}bp")
    r1 = m.get("ret1w")
    if r1 is not None and r1 * sign > 0:
        s += 1
        why.append(f"1W {r1:+.1f}%")
    hi = side == "short" and (m.get("shortRlim1w") or 0) >= config.UNI_SHORT_HIGH
    if hi:
        s += 1
        why.append(f"공매도 비중 {m['shortRlim1w']:.1f}%")
    return max(-5, min(5, s)), hi, ", ".join(why)


def prelim_score(m, side):
    """U5(허브) 전 예비 점수 — 거래대금·수급·가격만."""
    s, _, _ = score_stock({k: m.get(k) for k in ("tvMom", "flowBp", "frgn1w", "orgn1w", "ret1w")}, side)
    return s


# ─────────────────────────── 빌드 ───────────────────────────
def build_universe(date, wb, *, dbq, hub, ledger=None, decided=None, log=print, now=None, store=None, sector_map=None):
    """→ universe 문서. 네트워크 실패는 업종/종목 단위로 건너뛰고 diag.errors 에 남긴다. 토큰 문제만 예외."""
    t0 = time.time()
    diag = {"errors": [], "unmatched": [], "rawSample": {}, "calls": {}}
    knobs = {"topkTv": config.UNI_TOPK_TV, "topkCap": config.UNI_TOPK_CAP, "topN": config.UNI_TOPN,
             "minTvEok": config.UNI_MIN_TV_EOK, "flowPerSector": config.UNI_FLOW_PER_SECTOR,
             "candMin": config.UNI_CAND_MIN, "tvDiv": config.UNI_TV_DIV, "flowDiv": config.UNI_FLOW_DIV,
             "rankBasis": config.UNI_RANK_BASIS, "totalMin": config.UNI_TOTAL_MIN, "liqPool": config.UNI_LIQ_POOL}
    by_total = config.UNI_RANK_BASIS == "total"
    sig = signals.sector_signals(wb)                       # {norm name: signal}
    sig_rows = {signals.norm_sec(r.get("name")): r for r in ((wb or {}).get("sectorFlow") or {}).get("rows") or []}
    ledger = ledger or engine.new_ledger()
    held = {p["code"]: p.get("side", "long") for p in ledger.get("positions", [])}
    pend = {o["code"]: o.get("side", "long") for o in ledger.get("orders", [])}
    decided = decided or {}

    # U1 업종 목록 ↔ sectorFlow 조인 (USTOCKCOND 가 비면 구성종목 겹침으로 해석)
    inds, how = sector_codes(dbq, store, date, sector_map=sector_map, log=log)
    diag["calls"]["industry"] = 1
    diag["sectorSource"] = how
    diag["industries"] = [f"{i['code']} {i['name']}" for i in inds]
    sectors = []
    for ind in inds:
        k = signals.norm_sec(ind["name"])
        if k not in sig_rows:
            diag["unmatched"].append(ind["name"])
            continue
        row = sig_rows[k]
        sectors.append({"name": row.get("name") or ind["name"], "dbCode": ind["code"], "signal": sig.get(k) or "혼조",
                        "dir": direction_of(sig.get(k), row.get("name") or ind["name"]),
                        "big": (row.get("name") or ind["name"]) in BIG_SECTORS,
                        "chgPct": row.get("chgPct"), "frgn": row.get("frgn"), "orgn": row.get("orgn"),
                        "m1": row.get("m1"), "ytd": row.get("ytd")})
    try:
        fut = dbq.stock_future_underlyings()
    except Exception as e:
        fut = set()
        diag["errors"].append(f"FCODES: {type(e).__name__}: {str(e)[:80]}")

    # 영업일 창: 25영업일(1W 5 + 4W 20)
    start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=45)).isoformat()
    tdays = [d for d in tradedays.trading_days(start, date)]
    d1 = tdays[-25] if len(tdays) >= 25 else tdays[0]
    pxc = PriceCache(store, dbq, log=log)

    out_secs, n_cut, n_top, n_long, n_short, n_fut = [], 0, 0, 0, 0, 0
    for s in sectors:
        try:
            comp = dbq.industry_components(s["dbCode"], "42")
        except Exception as e:
            diag["errors"].append(f"{s['name']} USTOCKCONDLIST: {type(e).__name__}: {str(e)[:80]}")
            out_secs.append({**s, "n": 0, "nCut": 0, "top": [], "error": "구성종목 조회 실패"})
            continue
        if comp and "components" not in diag["rawSample"]:
            diag["rawSample"]["components"] = comp[0]
        stocks = []
        for r in comp:
            code = str(r.get("Iscd") or "").strip().zfill(6)
            name = str(r.get("KorIsnm") or "").strip()
            if is_excluded(code, name):
                continue
            stocks.append({"code": code, "name": name, "cap": _r((_num(r.get("Avls"), 0.0) or 0.0) / config.UNI_CAP_DIV, 0),
                           "tvDay": _r((_num(r.get("AcmlTrPbmn"), 0.0) or 0.0) / config.UNI_TV_DIV, 1),
                           "chgDay": _num(r.get("PrdyCtrt"))})
        sec = {**s, "n": len(stocks), "top": []}
        # U3 1차 컷
        by_tv = sorted(stocks, key=lambda x: -(x["tvDay"] or 0))[:config.UNI_TOPK_TV]
        by_cap = sorted(stocks, key=lambda x: -(x["cap"] or 0))[:config.UNI_TOPK_CAP]
        cut, seen = [], set()
        for x in by_tv + by_cap:
            if x["code"] not in seen:
                seen.add(x["code"])
                cut.append(x)
        sec["nCut"] = len(cut)
        n_cut += len(cut)
        sec["secTvDay"] = _r(sum(x["tvDay"] or 0 for x in stocks), 0)
        if s.get("big"):
            # 대분류(제조·금융): 산출 제외 — 당일 거래대금 상위 3 을 지표 없이 남긴다
            for i, x in enumerate(by_tv[:3]):
                sec["top"].append({**x, "rank": i + 1, "side": None, "score": None, "flags": [], "reason": ""})
            out_secs.append(sec)
            continue
        # U4 종목 일별 — 혼조 업종도 조회해 업종 거래대금 가속은 채운다(2026-10-14, 종목 점수·후보는 방향 업종만)
        rows_m = []
        for x in cut:
            try:
                rows = dbq.daily_investor(x["code"], d1, date)
            except Exception as e:
                diag["errors"].append(f"{x['code']} DAYSTOCKTJJ: {type(e).__name__}: {str(e)[:80]}")
                continue
            if rows and "investor" not in diag["rawSample"]:
                diag["rawSample"]["investor"] = rows[0]
            m = weekly_metrics(rows, date, x["cap"], tv_div=config.UNI_TV_DIV, flow_div=config.UNI_FLOW_DIV)
            if not m:
                continue
            if (m["tv20d"] or 0) < config.UNI_MIN_TV_EOK:
                continue
            rows_m.append({**x, **m})
        sec["secTv1w"] = _r(sum(x["tv1w"] or 0 for x in rows_m), 0)
        tv4 = [x["tv4w"] for x in rows_m if x.get("tv4w")]
        sec["secTvMom"] = _r(sum(x["tv1w"] for x in rows_m if x.get("tv4w")) / sum(tv4) - 1, 3) if tv4 else None
        if not s["dir"]:
            # 혼조: 관찰만 — 주간 거래대금 상위 3(주간 지표 포함), 점수·후보 없음
            obs = sorted(rows_m, key=lambda x: -(x["tv1w"] or 0))[:3] or by_tv[:3]
            for i, x in enumerate(obs):
                sec["top"].append({**x, "rank": i + 1, "side": None, "score": None, "flags": [], "reason": ""})
            out_secs.append(sec)
            continue
        # U5 허브 보강(예비 상위 N)
        rows_m.sort(key=lambda x: (-prelim_score(x, s["dir"]), -(x["tv1w"] or 0)))
        pre = rows_m if by_total else rows_m[:config.UNI_FLOW_PER_SECTOR]   # 종합 기준 = 1차 컷 전 종목(시뮬 T2)
        flows, errs = hub.many([x["code"] for x in pre])
        diag["errors"].extend(f"{s['name']} /flow {e}" for e in errs[:3])
        # U6 점수
        scored = []
        for x in pre:
            x.update(short_loan_metrics(flows.get(x["code"]), x["cap"]))
            pd_, pc_ = pxc.closes(x["code"], date)                   # 멀티기간 수익률(2026-10-14)
            x.update(period_returns(pd_, pc_, date))
            sc, hi, why = score_stock(x, s["dir"])
            flags = []
            if x["code"] in held:
                flags.append("held")
            if x["code"] in pend:
                flags.append("pending")
            if x["code"] in fut:
                flags.append("fut")
            if hi:
                flags.append("shortHigh")
            if x["code"] in decided:
                flags.append("done")
            x.update(side=s["dir"], score=sc, shortHigh=hi, reason=why, hasStockFuture=x["code"] in fut,
                     held=x["code"] in held, pending=x["code"] in pend, done=decided.get(x["code"]), flags=flags)
            scored.append(x)
        scored.sort(key=lambda x: (-x["score"], -(x["tv1w"] or 0)))
        if by_total:
            sec["top"] = scored                                    # 전 종목 — 종합 채점 후 Top N 으로 자름
        else:
            for i, x in enumerate(scored[:config.UNI_TOPN]):
                x["rank"] = i + 1
                sec["top"].append(x)
        out_secs.append(sec)

    # 주가·유동성·수급·종합 점수 — 종합 기준이면 1차 컷 전 종목 풀에서 채점 후 종합순 Top N(시뮬 T2 와 같은 계산)
    annotate_composite(out_secs)
    basis = config.UNI_RANK_BASIS
    for sec in out_secs:
        if not sec.get("dir"):
            continue
        if by_total:
            sec["top"].sort(key=lambda x: (x.get("totalScore") is None, -(x.get("totalScore") or 0), -(x.get("score") or 0)))
            sec["top"] = sec["top"][:config.UNI_TOPN]
            for i, x in enumerate(sec["top"]):
                x["rank"] = i + 1
        n_top += len(sec["top"])
        c = [x for x in sec["top"] if is_candidate(x)]
        if sec["dir"] == "long":
            n_long += len(c)
        else:
            n_short += len(c)
            n_fut += sum(1 for x in c if x.get("hasStockFuture"))
    order = {"long": 0, "short": 1, None: 2}
    out_secs.sort(key=lambda x: (order[x.get("dir")], -max([basis_value(y, basis) for y in x["top"]] or [-9]),
                                 -(x.get("secTvMom") or -9)))
    rank_long, rank_short = integrated_rank(out_secs, "long", basis=basis), integrated_rank(out_secs, "short", basis=basis)
    diag["calls"].update(db=dbq.calls, hub=hub.calls, px=pxc.calls)
    diag["secs"] = round(time.time() - t0, 1)
    doc = {"date": date, "asof": (now or datetime.datetime.now()).isoformat(timespec="seconds"),
           "wbAsof": (wb or {}).get("asof"), "mode": "observe",
           "basis": {"tv": "DB증권 DAYSTOCKTJJ 거래대금 5영업일 합(억, AcmlTrPbmn 비면 거래량×종가) · Δ = 직전 4주 주평균 대비",
                     "flow": "DB증권 DAYSTOCKTJJ 외국인·기관계 매수−매도(억) · bp = ÷시총",
                     "short": "KIS /flow 공매도 volRlim 1W 평균(%) · Δ = 직전 4주 평균 대비(pp)",
                     "loan": "KIS /flow 대차잔고(억) 5영업일 증감", "cap": "DB증권 Avls(시총)",
                     "composite": f"주가·수급 점수 = 같은 방향 Top 풀 안 백분위(0~100) · 유동성 점수 = "
                                  f"{'Long·Short 합산' if config.UNI_LIQ_POOL == 'all' else '같은 방향'} Top 풀 안 20일 대금 백분위 · 종합 = {config.UNI_W_PX:.2f}·주가 + {config.UNI_W_FLOW:.2f}·수급 · "
                                  f"유동성점수 < {config.UNI_LIQ_GATE:.0f} 이면 종합 공란 · 수익률 = DB증권 CHARTDAY 종가(1M 21·3M 63·6M 126·12M 252영업일, YTD 전년말)"},
           "knobs": knobs,
           "summary": {"sectors": len(out_secs), "signaled": sum(1 for x in out_secs if x.get("dir")),
                       "mixed": sum(1 for x in out_secs if not x.get("dir") and not x.get("big")),
                       "big": sum(1 for x in out_secs if x.get("big")), "cut": n_cut, "top": n_top,
                       "long": n_long, "short": n_short, "shortFut": n_fut,
                       "heldOverlap": sum(1 for x in out_secs for y in x["top"] if y.get("held"))},
           "sectors": out_secs, "rankLong": rank_long, "rankShort": rank_short, "diag": diag}
    log(f"[swing] universe {date}: 업종 {len(out_secs)}(신호 {doc['summary']['signaled']}) · 1차 컷 {n_cut} · "
        f"Top {n_top} · Long {n_long} · Short {n_short}(선물 {n_fut}) · DB {dbq.calls}콜 · 허브 {hub.calls}콜 · "
        f"{diag['secs']}s · 오류 {len(diag['errors'])}")
    return doc


RANK_KEYS = ("code", "name", "rank", "score", "reason", "cap", "tv1w", "tvMom", "frgn1w", "orgn1w", "flowBp",
             "shortRlim1w", "shortMom", "loanChg", "ret1w", "hasStockFuture", "shortHigh", "held", "pending", "done", "flags",
             "tv20d", "ret1m", "ret3m", "ret6m", "ret12m", "retYtd", "pxScore", "liqScore", "flowScore", "totalScore", "liqGate")


def basis_value(x, basis):
    """정렬 기준 값 — score(모멘텀 −5~+5) 또는 total(종합 0~100, 없으면 최하)."""
    if basis == "total":
        return x.get("totalScore") if x.get("totalScore") is not None else -9
    return x.get("score") if x.get("score") is not None else -9


def is_candidate(x, basis=None):
    """후보 적격 점수 — total: 종합 ≥ UNI_TOTAL_MIN(게이트 통과), score: 모멘텀 ≥ UNI_CAND_MIN."""
    basis = basis or config.UNI_RANK_BASIS
    if x.get("score") is None:
        return False
    if basis == "total":
        return x.get("totalScore") is not None and x["totalScore"] >= config.UNI_TOTAL_MIN
    return x["score"] >= config.UNI_CAND_MIN


def integrated_rank(sectors, side, n=None, basis="score"):
    """전 업종 통합 롱/숏 순위 — 방향 업종들의 Top 을 합쳐 기준 점수(basis: score 모멘텀 · total 종합) → 주간 거래대금 순.
    rankAll = 통합 순위, rank = 업종 내 순위."""
    xs = [dict({k: x.get(k) for k in RANK_KEYS}, sector=s.get("name"), signal=s.get("signal"))
         for s in sectors if s.get("dir") == side for x in (s.get("top") or []) if x.get("score") is not None]
    xs.sort(key=lambda x: (-basis_value(x, basis), -(x.get("tv1w") or 0)))
    xs = xs[:n or config.UNI_RANK_N]
    for i, x in enumerate(xs):
        x["rankAll"] = i + 1
    return xs


def recent_decisions(store, date, days=3):
    """최근 days 영업일 안에 entry 판단이 있는 종목 → {code: 'YYYY-MM-DD'} (쿨다운 표시용)."""
    out = {}
    start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=days * 3)).isoformat()
    for d in tradedays.trading_days(start, date)[-days:]:
        for k in store.keys(f"decision/{d}/"):
            parts = k.split("/")
            if len(parts) >= 4 and parts[3].startswith("entry"):
                out[parts[2]] = d
    return out


def run_universe(date, store, wb, *, dbq=None, hub=None, force=False, log=print):
    """크론 진입: 휴장일·이미 산출(force 아님)이면 건너뜀. 토큰 오류는 DbHubPriceError 로 올린다."""
    if not tradedays.is_trading_day(date):
        return {"skip": "휴장일"}
    if store.get(f"universe/{date}") and not force:
        return {"skip": "이미 산출됨"}
    dbq = dbq or DbQuote(log=log)
    hub = hub or HubFlow(log=log)
    try:
        doc = build_universe(date, wb, dbq=dbq, hub=hub, ledger=store.get("ledger"),
                             decided=recent_decisions(store, date, config.UNI_COOLDOWN_DAYS), log=log)
    except DbHubPriceError:
        raise
    except Exception as e:
        traceback.print_exc()
        return {"error": f"{type(e).__name__}: {e}"}
    store.put(f"universe/{date}", doc)
    return {"ok": True, "summary": doc["summary"], "secs": doc["diag"]["secs"]}


# ─────────────────────────── 공통 채점 · 후보 소스(2026-10-13, 매트릭스와 독립 소스) ───────────────────────────
def load_caps(store, date, *, fetch=None, log=print):
    """main `public/assets/krx_caps.json`(KIS 마스터 시총, 억원, 보통주+우선주 전 종목) → {code: cap}.
    하루 1회 store `uni_cache/caps` 에 캐시. 실패하면 이전 캐시(없으면 {})."""
    cached = store.get("uni_cache/caps") or {}
    if cached.get("date") == date and cached.get("caps"):
        return cached["caps"]
    try:
        if fetch is None:
            import json as _json
            import urllib.request
            url = f"{config.MAIN_RAW_BASE}/public/assets/krx_caps.json?t={int(time.time())}"
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "swing-cron"}), timeout=20) as r:
                raw = _json.loads(r.read().decode("utf-8"))
        else:
            raw = fetch()
        caps = next((v for v in raw.values() if isinstance(v, dict)), {}) if isinstance(raw, dict) else {}
        caps = {str(k).zfill(6): float(v) for k, v in caps.items() if v}
        if caps:
            store.put("uni_cache/caps", {"date": date, "asof": raw.get("asof"), "caps": caps})
            return caps
    except Exception as e:
        log(f"[swing] krx_caps 로드 실패({type(e).__name__}: {str(e)[:80]}) — 캐시 {len(cached.get('caps') or {})}종목 사용")
    return cached.get("caps") or {}


def load_sector_map(store, date, *, fetch=None, log=print):
    """main `public/assets/krx_sector_map.json`(KIS 업종지수명 → 시총 상위 종목) → {norm 업종명: {"name", "codes": set}}.
    DB증권 업종 코드를 종목 겹침으로 KIS 업종명에 매핑하는 데 쓴다. 7일 캐시 store `uni_cache/sector_map`."""
    cached = store.get("uni_cache/sector_map") or {}
    fresh = cached.get("date") and (datetime.date.fromisoformat(date) - datetime.date.fromisoformat(cached["date"])).days < 7
    if fresh and cached.get("map"):
        return {k: {"name": v["name"], "codes": set(v["codes"])} for k, v in cached["map"].items()}
    try:
        if fetch is None:
            import json as _json
            import urllib.request
            url = f"{config.MAIN_RAW_BASE}/public/assets/krx_sector_map.json?t={int(time.time())}"
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "swing-cron"}), timeout=20) as r:
                raw = _json.loads(r.read().decode("utf-8"))
        else:
            raw = fetch()
        m = {}
        for key, sec in (raw.get("sectors") or {}).items():
            name = sec.get("name") or key
            codes = {str(x.get("code")).zfill(6) for x in (sec.get("stocks") or []) + (sec.get("kosdaqStocks") or []) if x.get("code")}
            if codes:
                m[signals.norm_sec(name)] = {"name": name, "codes": codes}
        if m:
            store.put("uni_cache/sector_map", {"date": date, "asof": raw.get("asof"),
                                               "map": {k: {"name": v["name"], "codes": sorted(v["codes"])} for k, v in m.items()}})
            return m
    except Exception as e:
        log(f"[swing] krx_sector_map 로드 실패({type(e).__name__}: {str(e)[:80]}) — 캐시 {len(cached.get('map') or {})}업종 사용")
    return {k: {"name": v["name"], "codes": set(v["codes"])} for k, v in (cached.get("map") or {}).items()}


def resolve_db_sectors(dbq, sector_map, *, codes=None, min_overlap=0.3, log=print):
    """DB증권 업종 코드 → KIS 업종명 매핑을 **구성종목 겹침**으로 만든다(USTOCKCOND 가 빈 응답을 주는 문제 우회, 2026-10-13).
    각 후보 코드의 USTOCKCONDLIST 구성종목과 섹터 맵 종목의 겹침 비율(DB 구성종목 기준)이 가장 큰 KIS 업종에 붙인다.
    → [{"code", "name", "overlap", "n"}] (name = KIS 업종명). 대분류(제조·금융 등 겹침이 넓은 코드)는 겹침 최대 업종이
    여러 개라 min_overlap 미달로 빠지거나 가장 큰 쪽에 붙는데, 한 KIS 업종에는 겹침이 가장 큰 코드 하나만 남긴다."""
    codes = list(codes or [str(c) for c in range(1001, 1061)])
    found = []
    for code in codes:
        try:
            rows = dbq.industry_components(code, "2")
        except Exception as e:
            log(f"[swing] 업종 {code} 구성종목 실패: {str(e)[:60]}")
            continue
        comp = {str(r.get("Iscd") or "").strip().zfill(6) for r in rows if r.get("Iscd")}
        if len(comp) < 3:
            continue
        best, best_ov = None, 0.0
        for k, v in sector_map.items():
            ov = len(comp & v["codes"]) / len(comp)
            if ov > best_ov:
                best, best_ov = k, ov
        if best and best_ov >= min_overlap:
            found.append({"code": code, "name": sector_map[best]["name"], "key": best, "overlap": round(best_ov, 2), "n": len(comp)})
    found.sort(key=lambda x: -x["overlap"])
    out, used = [], set()
    for f in found:
        if f["key"] in used:
            continue
        used.add(f["key"])
        out.append({"code": f["code"], "name": f["name"], "overlap": f["overlap"], "n": f["n"]})
    return out


def sector_codes(dbq, store, date, *, sector_map=None, log=print):
    """업종 코드 목록 — ① store 캐시(7일) ② USTOCKCOND ③ 구성종목 겹침 해석(resolve_db_sectors). 반환 [{"code","name"}]."""
    cached = store.get("uni_cache/db_sectors") if store is not None else None
    if cached and cached.get("date") and (datetime.date.fromisoformat(date) - datetime.date.fromisoformat(cached["date"])).days < 7 \
            and cached.get("sectors"):
        return cached["sectors"], "cache"
    inds, how = [], "industry"
    try:
        inds = dbq.industry_codes("K")
    except Exception as e:
        log(f"[swing] USTOCKCOND 실패: {e}")
    if not inds:
        sm = sector_map if sector_map is not None else (load_sector_map(store, date, log=log) if store is not None else {})
        if sm:
            inds, how = resolve_db_sectors(dbq, sm, log=log), "overlap"
    if inds and store is not None:
        store.put("uni_cache/db_sectors", {"date": date, "how": how, "sectors": inds})
    return inds, how


def score_codes(items, side, date, *, dbq, hub, caps=None, log=print):
    """임의 종목 목록에 유니버스와 **같은 모멘텀 점수**를 매긴다 → {code: {tv1w…, shortRlim1w…, score, shortHigh, reason}}.
    매트릭스 후보를 유니버스와 공통 척도로 정렬하기 위한 것. 시총은 caps(krx_caps) → 없으면 수급/시총 항목 0점."""
    caps = caps or {}
    start = (datetime.date.fromisoformat(date) - datetime.timedelta(days=45)).isoformat()
    tdays = tradedays.trading_days(start, date)
    d1 = tdays[-25] if len(tdays) >= 25 else tdays[0]
    out, codes = {}, []
    for it in items:
        code = str(it["code"]).zfill(6)
        try:
            rows = dbq.daily_investor(code, d1, date)
        except Exception as e:
            log(f"[swing] score {code} DAYSTOCKTJJ 실패: {type(e).__name__}: {str(e)[:80]}")
            continue
        m = weekly_metrics(rows, date, caps.get(code) or it.get("cap"), tv_div=config.UNI_TV_DIV, flow_div=config.UNI_FLOW_DIV)
        if not m:
            continue
        out[code] = m
        codes.append(code)
    flows, errs = hub.many(codes)
    for e in errs[:3]:
        log(f"[swing] score /flow {e}")
    for code in codes:
        m = out[code]
        m.update(short_loan_metrics(flows.get(code), caps.get(code)))
        sc, hi, why = score_stock(m, side)
        m.update(score=sc, shortHigh=hi, reason=why, cap=caps.get(code))
    return out


def attach_scores(cands, side, date, *, dbq, hub, caps=None, log=print):
    """매트릭스 후보(signals._candidates 형식) 에 공통 점수 부여(제자리) — score=모멘텀, matrixScore=생성기 점수,
    metrics=지표, reason 뒤에 '모멘텀 +3: …' 추가. 채점 실패 종목은 score None(정렬 뒤로)."""
    m = score_codes(cands, side, date, dbq=dbq, hub=hub, caps=caps, log=log)
    for c in cands:
        c["source"] = "matrix"
        c["matrixScore"] = c.get("score")
        x = m.get(str(c["code"]).zfill(6))
        if x:
            c["score"] = x["score"]
            c["metrics"] = {k: x.get(k) for k in ("tv1w", "tvMom", "frgn1w", "orgn1w", "flowBp", "shortRlim1w",
                                                  "shortMom", "loanChg", "ret1w", "shortHigh", "cap")}
            c["reason"] = (c.get("reason") or "") + f" · 모멘텀 {x['score']:+d}/5" + (f"({x['reason']})" if x["reason"] else "")
        else:
            c["score"], c["metrics"] = None, None
    cands.sort(key=score_key)
    return cands


def score_key(c):
    s = c.get("score")
    return (0 if s is not None else 1, -(s or 0), -((c.get("metrics") or {}).get("tv1w") or 0))


def pick(doc, side, mode, ok, cap=None, sector_cap=None, basis="score"):
    """후보 선정 공통 함수(운용·시뮬 공용, 2026-10-14). doc = {sectors[], rankLong[], rankShort[]} (universe/<date> 또는 시뮬 as-of 문서).
    ok(x) = 종목 적격 판정(점수·보유·쿨다운 등). mode:
      rank       = 전 업종 통합 순위(rankLong/rankShort) 위에서부터
      sectorcap  = rank 와 같되 업종당 최대 sector_cap
      roundrobin = 방향 업종을 1위(첫 적격 종목) 점수순으로 돌며 업종당 1개씩, 자리가 남으면 2바퀴째(업종별 Top 풀)
    cap 이 None 이면 적격 종목 전부(호출측이 자름). 반환 행에는 sector·signal·rankAll 이 들어간다."""
    doc = doc or {}
    key = "rankLong" if side == "long" else "rankShort"
    ranked = doc.get(key)
    if ranked is None:
        ranked = integrated_rank(doc.get("sectors") or [], side, basis=basis)
    rank_all = {str(x.get("code")).zfill(6): x.get("rankAll") for x in ranked}
    out = []
    if mode in ("rank", "sectorcap"):
        per, n = (sector_cap if sector_cap is not None else config.UNI_SECTOR_CAP), {}
        for x in ranked:
            if not ok(x):
                continue
            if mode == "sectorcap":
                if n.get(x.get("sector"), 0) >= per:
                    continue
                n[x.get("sector")] = n.get(x.get("sector"), 0) + 1
            out.append(dict(x))
            if cap is not None and len(out) >= cap:
                break
        return out
    lists = []
    for s in doc.get("sectors") or []:
        if s.get("dir") != side:
            continue
        xs = [dict(x, sector=s.get("name"), signal=s.get("signal"), rankAll=rank_all.get(str(x.get("code")).zfill(6)))
              for x in (s.get("top") or []) if ok(x)]
        if xs:
            lists.append(xs)
    if basis == "total":                                           # 업종 안도 종합순(문서 Top 순서가 이미 종합순이지만 방어)
        for xs in lists:
            xs.sort(key=lambda x: -basis_value(x, basis))
    lists.sort(key=lambda xs: (-basis_value(xs[0], basis), -(xs[0].get("tv1w") or 0)))   # 업종 순서 = 첫 적격 종목 기준 점수
    i = 0
    while any(len(xs) > i for xs in lists) and (cap is None or len(out) < cap):
        for xs in lists:
            if len(xs) > i and (cap is None or len(out) < cap):
                x = dict(xs[i])
                x["rrRound"] = i + 1
                out.append(x)
        i += 1
    return out


def universe_candidates(doc, side, exclude=(), decided=None, fut_only=False, mode=None):
    """유니버스 문서 → 후보 [{code,name,sector,signal,score,reason,source:"universe",metrics}].
    선정 방식 = mode(기본 config.UNI_PICK_MODE = roundrobin, 2026-10-14 사용자 결정): pick() 참조. 적격 = 점수 ≥ UNI_CAND_MIN,
    보유·대기·매트릭스 후보(exclude)·최근 판단(decided, 쿨다운) 아님. 상한(UNI_NEW_PER_DAY)은 호출측이 자른다."""
    decided, seen = decided or {}, set(exclude)
    mode = mode or config.UNI_PICK_MODE
    basis = ((doc or {}).get("knobs") or {}).get("rankBasis") or config.UNI_RANK_BASIS   # 문서를 만든 기준 우선

    def ok(x):
        code = str(x.get("code") or "").zfill(6)
        if (not is_candidate(x, basis) or code in seen or code in decided
                or (fut_only and not x.get("hasStockFuture"))):
            return False
        seen.add(code)
        return True
    label = "롱" if side == "long" else "숏"
    out = []
    for i, x in enumerate(pick(doc, side, mode, ok, basis=basis)):
        code = str(x.get("code") or "").zfill(6)
        if mode == "roundrobin":
            how = f"업종 라운드로빈 {i + 1}번째({x.get('rrRound')}바퀴 · 업종 {x.get('sector')} {x.get('signal')} {x.get('rank')}위 · 통합 {x.get('rankAll')}위)"
        elif mode == "sectorcap":
            how = f"통합 {label} 순위 {x.get('rankAll')}위(업종당 ≤{config.UNI_SECTOR_CAP} · 업종 {x.get('sector')} {x.get('signal')} {x.get('rank')}위)"
        else:
            how = f"전 업종 통합 {label} 모멘텀 {x.get('rankAll')}위(업종 {x.get('sector')} {x.get('signal')} {x.get('rank')}위)"
        out.append({"code": code, "name": x.get("name"), "sector": x.get("sector"), "signal": x.get("signal"),
                    "score": x["score"], "source": "universe", "rankAll": x.get("rankAll"), "pickMode": mode, "rankBasis": basis,
                    "reason": how + (f" · 종합 {x['totalScore']:.0f}(주가 {x.get('pxScore') or 0:.0f}·수급 {x.get('flowScore') or 0:.0f})"
                                     if basis == "total" and x.get("totalScore") is not None else "")
                              + f" · 모멘텀 {x['score']:+d}/5" + (f"({x.get('reason')})" if x.get("reason") else ""),
                    "metrics": {k: x.get(k) for k in ("tv1w", "tvMom", "frgn1w", "orgn1w", "flowBp", "shortRlim1w",
                                                      "shortMom", "loanChg", "ret1w", "shortHigh", "cap", "hasStockFuture",
                                                      "ret1m", "ret3m", "ret6m", "ret12m", "retYtd", "pxScore", "liqScore",
                                                      "flowScore", "totalScore")}})
    return out


class UniverseRunner:
    """daily.run_day 에 꽂는 유니버스 어댑터 — 산출(또는 그날 문서 재사용)·공통 채점. DB증권/허브 클라이언트는 처음
    쓸 때 만든다(토큰 없음 등은 호출 시점에 예외 → run_day 가 노트로 남기고 계속)."""

    def __init__(self, store, dbq=None, hub=None, *, caps_loader=None, force=False, log=print):
        self.store, self._dbq, self._hub, self.force, self.log = store, dbq, hub, force, log
        self._caps_loader, self._caps = caps_loader, None

    @property
    def dbq(self):
        if self._dbq is None:
            self._dbq = DbQuote(log=self.log)
        return self._dbq

    @property
    def hub(self):
        if self._hub is None:
            self._hub = HubFlow(log=self.log)
        return self._hub

    def caps(self, date):
        if self._caps is None:
            self._caps = self._caps_loader(date) if self._caps_loader else load_caps(self.store, date, log=self.log)
        return self._caps

    def build_or_load(self, date, wb, ledger):
        doc = self.store.get(f"universe/{date}")
        if doc and not self.force:
            return doc
        doc = build_universe(date, wb, dbq=self.dbq, hub=self.hub, ledger=ledger, store=self.store,
                             decided=recent_decisions(self.store, date, config.UNI_COOLDOWN_DAYS), log=self.log)
        self.store.put(f"universe/{date}", doc)
        return doc

    def attach_scores(self, cands, side, date):
        if not cands:
            return cands
        return attach_scores(cands, side, date, dbq=self.dbq, hub=self.hub, caps=self.caps(date), log=self.log)


# ─────────────────────────── 멀티기간 수익률 · 백분위 점수(2026-10-14, 브레인 리포트 양식 반영) ───────────────────────────
HORIZONS = (("ret1m", 21), ("ret3m", 63), ("ret6m", 126), ("ret12m", 252))


def _iso(d8):
    d8 = str(d8 or "").strip()
    return f"{d8[:4]}-{d8[4:6]}-{d8[6:8]}" if len(d8) == 8 and d8.isdigit() else d8[:10]


class PriceCache:
    """종목별 종가 이력 — store `uni_cache/px/<code>` {dates[], closes[]} 에 두고 CHARTDAY 로 증분 조회(마지막 날짜 이후만).
    첫 조회는 keep 영업일(≈15개월) 범위 1콜, 이후 매일 1콜(수 행). store 가 없으면 메모리만."""

    def __init__(self, store, dbq, *, keep=None, log=print):
        self.store, self.dbq, self.keep, self.log = store, dbq, keep or config.UNI_PX_KEEP, log
        self._mem, self.calls = {}, 0

    def _get(self, code):
        if code in self._mem:
            return self._mem[code]
        doc = (self.store.get(f"uni_cache/px/{code}") if self.store is not None else None) or {"dates": [], "closes": []}
        self._mem[code] = doc
        return doc

    def closes(self, code, date):
        """→ (dates, closes) — date 이하, 오름차순. 조회 실패 시 캐시된 범위만."""
        doc = self._get(code)
        last = doc["dates"][-1] if doc["dates"] else None
        if last is None or last < date:
            d1 = ((datetime.date.fromisoformat(last) + datetime.timedelta(days=1)).isoformat() if last
                  else (datetime.date.fromisoformat(date) - datetime.timedelta(days=int(self.keep * 1.6))).isoformat())
            try:
                rows = self.dbq.chart_day(code, d1, date)
                self.calls += 1
            except Exception as e:
                self.log(f"[swing] px {code} CHARTDAY 실패: {str(e)[:60]}")
                rows = None
            if rows is not None:
                m = dict(zip(doc["dates"], doc["closes"]))
                for r in rows:
                    d, c = _iso(r.get("Date")), _num(r.get("Prpr"))
                    if len(d) == 10 and c:
                        m[d] = c
                ds = sorted(m)[-self.keep:]
                doc = {"dates": ds, "closes": [m[d] for d in ds], "asof": date}
                self._mem[code] = doc
                if self.store is not None:
                    try:
                        self.store.put(f"uni_cache/px/{code}", doc)
                    except Exception as e:
                        self.log(f"[swing] px {code} 캐시 저장 실패: {str(e)[:60]}")
        n = sum(1 for d in doc["dates"] if d <= date)
        return doc["dates"][:n], doc["closes"][:n]


def period_returns(dates, closes, date):
    """as-of date 멀티기간 수익률(%) — 1M=21·3M=63·6M=126·12M=252 영업일 전 종가 대비, YTD=전년 마지막 종가 대비. 부족하면 None."""
    out = {k: None for k, _ in HORIZONS}
    out["retYtd"] = None
    n = sum(1 for d in dates if d <= date)
    if n < 2 or not closes[n - 1]:
        return out
    last = closes[n - 1]
    for k, lag in HORIZONS:
        if n - 1 - lag >= 0 and closes[n - 1 - lag]:
            out[k] = round((last / closes[n - 1 - lag] - 1) * 100, 1)
    y = date[:4]
    prev = [i for i in range(n) if dates[i] < f"{y}-01-01"]
    if prev and closes[prev[-1]]:
        out["retYtd"] = round((last / closes[prev[-1]] - 1) * 100, 1)
    return out


def percentile(values, v):
    """0~100 — values 중 v 보다 작은 비율(동률 절반). 단일 값이면 50."""
    xs = [x for x in values if x is not None]
    if v is None or not xs:
        return None
    if len(xs) == 1:
        return 50.0
    lt = sum(1 for x in xs if x < v)
    eq = sum(1 for x in xs if x == v)
    return round((lt + 0.5 * eq) / len(xs) * 100, 1)


def annotate_composite(sectors, *, w_px=None, w_flow=None, liq_gate=None, liq_pool=None):
    """업종 Top 종목에 주가·유동성·수급·종합 점수(0~100, 같은 방향 풀 안 백분위)를 붙인다(제자리).
    주가점수 = 1M/3M/6M/12M 수익률 백분위 평균(방향 부호 반영: Short 은 하락이 높게, 2개 이상 기간 필요)
    유동성점수 = 20일 평균 거래대금 백분위 — 풀은 config.UNI_LIQ_POOL(all = Long·Short 합산, side = 방향별) · 수급점수 = (수급/시총, −공매도 비중Δ, −대차Δ, 거래대금 가속) 백분위 평균(방향 부호)
    종합점수 = w_px·주가 + w_flow·수급 (이미지 역산 0.55/0.45) — 유동성점수 < liq_gate(30) 면 공란(None)."""
    w_px = config.UNI_W_PX if w_px is None else w_px
    w_flow = config.UNI_W_FLOW if w_flow is None else w_flow
    liq_gate = config.UNI_LIQ_GATE if liq_gate is None else liq_gate
    liq_pool = (liq_pool or config.UNI_LIQ_POOL)
    liq_all = [x.get("tv20d") for s in sectors if s.get("dir") in ("long", "short")
               for x in (s.get("top") or []) if x.get("score") is not None]
    for side in ("long", "short"):
        sg = 1 if side == "long" else -1
        pool = [x for s in sectors if s.get("dir") == side for x in (s.get("top") or []) if x.get("score") is not None]
        if not pool:
            continue
        ret_cols = {k: [None if x.get(k) is None else sg * x[k] for x in pool] for k, _ in HORIZONS}
        liq_col = liq_all if liq_pool == "all" else [x.get("tv20d") for x in pool]
        flow_cols = {"flowBp": [None if x.get("flowBp") is None else sg * x["flowBp"] for x in pool],
                     "shortMom": [None if x.get("shortMom") is None else -sg * x["shortMom"] for x in pool],
                     "loanBp": [None if x.get("loanBp") is None else -sg * x["loanBp"] for x in pool],
                     "tvMom": [x.get("tvMom") for x in pool]}
        for x in pool:
            ps = [percentile(ret_cols[k], None if x.get(k) is None else sg * x[k]) for k, _ in HORIZONS]
            ps = [p for p in ps if p is not None]
            x["pxScore"] = round(sum(ps) / len(ps), 1) if len(ps) >= 2 else None
            x["liqScore"] = percentile(liq_col, x.get("tv20d"))
            fs = [percentile(flow_cols["flowBp"], None if x.get("flowBp") is None else sg * x["flowBp"]),
                  percentile(flow_cols["shortMom"], None if x.get("shortMom") is None else -sg * x["shortMom"]),
                  percentile(flow_cols["loanBp"], None if x.get("loanBp") is None else -sg * x["loanBp"]),
                  percentile(flow_cols["tvMom"], x.get("tvMom"))]
            fs = [p for p in fs if p is not None]
            x["flowScore"] = round(sum(fs) / len(fs), 1) if fs else None
            ok = (x["pxScore"] is not None and x["flowScore"] is not None and x["liqScore"] is not None
                  and x["liqScore"] >= liq_gate)
            x["totalScore"] = round(w_px * x["pxScore"] + w_flow * x["flowScore"], 1) if ok else None
            x["liqGate"] = bool(x["liqScore"] is not None and x["liqScore"] < liq_gate)
    return sectors


COMPOSITE_KEYS = ("ret1m", "ret3m", "ret6m", "ret12m", "retYtd", "pxScore", "liqScore", "flowScore", "totalScore", "liqGate", "tv20d")
