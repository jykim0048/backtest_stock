"""하방 촉매 → Short 후보 입구(2026-10-15, PLAN_CATALYST_SHORT 3단계).

main 장중 시황 아카이브(`public/reports/intraday_briefing/<date>.json`, 회차별 catalysts·disclosures)에서 **bearish 촉매**를
종목 단위로 모아 강도(1~5)를 매기고, 보유·대기·쿨다운·다른 소스 후보를 뺀 뒤 공통 모멘텀 채점(universe.attach_scores)을 거쳐
trading_agent Short 분석으로 보낸다(source="catalyst"). 매트릭스·유니버스와 독립된 세 번째 소스.

강도 규칙(main score_catalysts 의 bearScore 가 있으면 그 값 우선):
  규칙 태그 bearish 공시(dilution·distress) 4 · 하방 뉴스 3회차 이상 3 · 하방 뉴스 2 · 둘 다(공시+뉴스) +1(최대 5)
  당일 등락률이 −CAT_MAX_DROP 보다 크게 빠진 종목은 추격 금지(관찰, 사유 기록).
선택: DART_API_KEY 가 있으면 16:30 에 당일 공시를 직접 조회(OpenDART list.json)해 15:30 이후 접수된 자금조달·악재 공시도
당일 후보에 넣는다(main 장중 라운드는 15:07 이 마지막). 규칙 태그는 main analysis/sources.dart_rule_direction 과 같은 표."""
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

from . import config, signals, universe

# ── 공시 제목 규칙 방향(main analysis/sources.py 와 같은 표 — 두 레포가 독립이라 복제, 바꾸면 양쪽 같이) ──
RULE_BEAR = (
    ("유상증자결정", "dilution"), ("전환사채권발행결정", "dilution"), ("신주인수권부사채권발행결정", "dilution"),
    ("교환사채권발행결정", "dilution"), ("영구", "dilution"), ("신종자본증권", "dilution"), ("자기주식처분결정", "dilution"),
    ("감자결정", "distress"), ("관리종목", "distress"), ("불성실공시", "distress"), ("매매거래정지", "distress"),
    ("횡령", "distress"), ("배임", "distress"), ("감사의견", "distress"), ("회생절차", "distress"),
    ("파산", "distress"), ("영업정지", "distress"), ("상장폐지", "distress"), ("담보제공", "distress"),
)
KIND_KR = {"dilution": "희석성 자금조달", "distress": "악재성 공시"}


def rule_direction(title):
    """공시 제목 → ("bearish", kind) | (None, None). 해지·철회·취소 제목은 규칙 적용 안 함."""
    t = "".join(str(title or "").split())
    if not t or any(w in t for w in ("해지", "철회", "취소")):
        return None, None
    if "거래정지" in t and any(w in t for w in ("공급계약", "단일판매", "수주", "무상증자", "합병")):   # 호재 공시에 따른 정지(리플레이 10/14 사례)
        return None, None
    for key, kind in RULE_BEAR:
        if key in t:
            return "bearish", kind
    return None, None


def _norm(s):
    s = "".join(ch for ch in str(s or "") if ch not in " \t·ㆍ・")
    for tok in ("(주)", "주식회사"):
        s = s.replace(tok, "")
    return s.upper()


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_NAMES = None


def name_to_code(name):
    """종목명 → 코드(public/assets/krx_companies.json, 보통주). 우선주 표기(…우/…우B)는 본주."""
    global _NAMES
    if _NAMES is None:
        m = {}
        try:
            with open(os.path.join(_ROOT, "public", "assets", "krx_companies.json"), encoding="utf-8") as f:
                for x in json.load(f):
                    if x.get("name") and x.get("code"):
                        m[_norm(x["name"])] = str(x["code"]).zfill(6)
        except Exception:
            pass
        _NAMES = m
    k = _norm(name)
    for cand in (k, k[:-1] if k.endswith("우") else k, k[:-2] if k.endswith("우B") else k):
        if cand in _NAMES:
            return _NAMES[cand]
    return None


def fetch_archive(date, *, fetch=None, log=print):
    """main raw `public/reports/intraday_briefing/<date>.json` → dict | None(없음·실패)."""
    try:
        if fetch is not None:
            return fetch(date)
        url = f"{config.MAIN_RAW_BASE}/public/reports/intraday_briefing/{date}.json?t={int(time.time())}"
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "swing-cron"}), timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log(f"[swing] 장중 촉매 아카이브 {date} 로드 실패({type(e).__name__}: {str(e)[:80]})")
        return None


def collect_bearish(arch, extra_disclosures=()):
    """아카이브 전 회차 → 종목별 하방 촉매 {code|n:name: entry}.
    entry = {name, code, market, kind, title, summary, ruleKind, rounds, newsRounds, discl, changePct, bearScore, strength, url}
    - 회차 catalysts 의 direction=bearish(규칙 태그 dirRule 포함)
    - 회차 disclosures(원본 공시 목록)에서 규칙 bearish 인 제목 — LLM 이 촉매로 안 고른 공시도 잡는다(구 아카이브는 dirRule 없음 → 제목 규칙)
    - extra_disclosures: DART 직접 조회분(같은 형식 {corp, stockCode, market, title, url})
    - 16:00 스코어 회차의 changePct·bearScore(있으면)"""
    out = {}

    def ent(name, code, market):
        code = str(code).zfill(6) if code else name_to_code(name)
        key = code or f"n:{_norm(name)}"
        e = out.setdefault(key, {"name": name, "code": code, "market": market, "kind": None, "title": None, "summary": "",
                                 "ruleKind": None, "rounds": 0, "newsRounds": 0, "discl": 0, "changePct": None,
                                 "bearScore": None, "url": None})
        e["market"] = e["market"] or market
        return e

    for rd in (arch or {}).get("rounds") or []:
        if rd.get("scoring"):
            for c in rd.get("catalysts") or []:
                key = (str(c.get("code")).zfill(6) if c.get("code") else name_to_code(c.get("stock"))) or f"n:{_norm(c.get('stock'))}"
                if key in out:
                    if c.get("changePct") is not None:
                        try:
                            out[key]["changePct"] = float(c["changePct"])
                        except (TypeError, ValueError):
                            pass
                    if c.get("bearScore") is not None:
                        out[key]["bearScore"] = int(c["bearScore"])
            continue
        for c in rd.get("catalysts") or []:
            if c.get("market") not in ("KOSPI", "KOSDAQ") or not c.get("stock"):
                continue
            d_rule = c.get("dirRule") or (rule_direction(c.get("title"))[0] if c.get("kind") == "disclosure" else None)
            if c.get("direction") != "bearish" and d_rule != "bearish":
                continue
            e = ent(c["stock"].strip(), c.get("code"), c.get("market"))
            e["rounds"] += 1
            if c.get("kind") == "disclosure":
                e["discl"] += 1
                e["kind"] = "disclosure"
                e["title"] = e["title"] or c.get("title")
                e["ruleKind"] = e["ruleKind"] or c.get("ruleKind") or rule_direction(c.get("title"))[1]
                if c.get("title"):
                    e["amended"] = ("정정" in c["title"]) if e.get("amended") in (None, True) else False
            else:
                e["newsRounds"] += 1
                e["kind"] = e["kind"] or "news"
            s = (c.get("summary") or "").strip()
            if s:
                e["summary"] = s                                      # 마지막 회차 요약
            e["url"] = e["url"] or c.get("url")
        for d in list(rd.get("disclosures") or []):
            _add_disclosure(out, ent, d)
    for d in extra_disclosures or ():
        _add_disclosure(out, ent, d, direct=True)
    for e in out.values():
        e["strength"] = strength(e)
    return out


def _add_disclosure(out, ent, d, direct=False):
    d_rule, kind = d.get("dirRule"), d.get("ruleKind")
    if d_rule is None:
        d_rule, kind = rule_direction(d.get("title"))
    if d_rule != "bearish" or d.get("market") not in ("KOSPI", "KOSDAQ") or not d.get("corp"):
        return
    e = ent(str(d["corp"]).strip(), d.get("stockCode"), d.get("market"))
    if e["discl"] == 0:
        e["rounds"] += 1
    e["discl"] += 1
    e["kind"] = "disclosure"
    e["title"] = e["title"] or d.get("title")
    e["ruleKind"] = e["ruleKind"] or kind
    e["url"] = e["url"] or d.get("url")
    if "정정" in str(d.get("title") or ""):
        e["amended"] = e.get("amended", True)
    else:
        e["amended"] = False                                          # 원 공시가 하나라도 있으면 정정 아님
    if direct:
        e["direct"] = True
    if not e["summary"]:
        e["summary"] = f"{d.get('title')} 공시 — {KIND_KR.get(kind, '악재성 공시')}"


def strength(e):
    """하방 강도 1~5 — bearScore(main 스코어러) 우선, 없으면 규칙."""
    if e.get("bearScore"):
        return max(1, min(5, int(e["bearScore"])))
    s = 0
    if e.get("discl") and e.get("ruleKind") in ("dilution", "distress"):
        s = 4
    elif e.get("newsRounds", 0) >= 3:
        s = 3
    elif e.get("newsRounds", 0) >= 1 or e.get("discl"):
        s = 2
    if e.get("discl") and e.get("newsRounds"):
        s += 1
    if s and e.get("amended") and not e.get("newsRounds"):
        s -= 1                                                        # [기재정정] 만 있는 공시 = 새 사건 아님(리플레이: STX·MSDI 연일 재등장)
    return max(1, min(5, s)) if s else 1


def dart_today_bearish(date, *, api_key=None, fetch=None, log=print):
    """OpenDART list.json 당일 공시(상장사) 중 규칙 bearish 제목 → [{corp, stockCode, market, title, url, dirRule, ruleKind}].
    api_key 없으면 []. fetch(params)->dict 로 주입 가능(테스트)."""
    key = api_key or os.environ.get("DART_API_KEY")
    if not key:
        return []
    out = []
    try:
        for page in (1, 2, 3):
            params = {"crtfc_key": key, "bgn_de": date.replace("-", ""), "end_de": date.replace("-", ""),
                      "page_no": page, "page_count": 100, "sort": "date", "sort_mth": "desc"}
            if fetch is not None:
                data = fetch(params)
            else:
                url = "https://opendart.fss.or.kr/api/list.json?" + urllib.parse.urlencode(params)
                with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "swing-cron"}), timeout=20) as r:
                    data = json.loads(r.read().decode("utf-8"))
            if (data or {}).get("status") != "000":
                break
            for it in data.get("list") or []:
                cls = it.get("corp_cls")
                if cls not in ("Y", "K"):
                    continue
                title = (it.get("report_nm") or "").strip()
                d, kind = rule_direction(title)
                if d != "bearish":
                    continue
                out.append({"corp": (it.get("corp_name") or "").strip(), "stockCode": (it.get("stock_code") or "").strip(),
                            "market": "KOSPI" if cls == "Y" else "KOSDAQ", "title": title,
                            "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it.get('rcept_no')}",
                            "dirRule": "bearish", "ruleKind": kind})
            if int(data.get("total_page") or 1) <= page:
                break
    except Exception as e:
        log(f"[swing] DART 당일 공시 조회 실패({type(e).__name__}: {str(e)[:80]}) — 아카이브만 사용")
    return out


def short_candidates(entries, exclude=(), decided=None, *, min_strength=None, max_drop=None, sector_of=None, fut=None):
    """하방 촉매 entries(collect_bearish) → Short 후보 목록(signals._candidates 형식 + source/catalyst), 강도 내림차순·당일 등락 오름차순.
    제외: 코드 미해석, exclude(보유·대기·다른 소스 후보), decided(쿨다운), 강도 < min_strength, 당일 하락 > max_drop(추격 금지),
    fut(주식선물 기초자산 집합)이 주어지면 그 밖 종목. 제외 사유는 반환 둘째 값 {code|name: 사유}."""
    min_strength = config.CAT_SHORT_MIN if min_strength is None else min_strength
    max_drop = config.CAT_MAX_DROP if max_drop is None else max_drop
    decided = decided or {}
    out, skipped = [], {}
    for key, e in entries.items():
        code = e.get("code")
        if not code:
            skipped[key] = "코드 미해석"
            continue
        if code in exclude:
            skipped[code] = "보유·대기·다른 소스"
            continue
        if code in decided:
            skipped[code] = f"쿨다운({decided[code]})"
            continue
        if e["strength"] < min_strength:
            skipped[code] = f"강도 {e['strength']} < {min_strength}"
            continue
        if e.get("changePct") is not None and e["changePct"] <= -abs(max_drop):
            skipped[code] = f"당일 {e['changePct']:+.1f}% 급락 — 추격 금지"
            continue
        if e.get("changePct") is not None and e["changePct"] >= config.CAT_RALLY_SKIP:
            skipped[code] = f"당일 {e['changePct']:+.1f}% 급등 — 호재성(제3자배정 등) 가능, 제외"
            continue
        if universe.is_excluded(code, e.get("name")):
            skipped[code] = "리츠·ETF·우선주 제외"
            continue
        if fut is not None and code not in fut:
            skipped[code] = "주식선물 미상장"
            continue
        why = (f"하방 촉매 강도 {e['strength']}/5 · " + (f"{KIND_KR.get(e.get('ruleKind'), '공시')}: {e.get('title')}" if e.get("kind") == "disclosure" else "뉴스")
               + (f" · 하방 뉴스 {e['newsRounds']}회차" if e.get("newsRounds") else "")
               + (f" · 당일 {e['changePct']:+.1f}%" if e.get("changePct") is not None else "")
               + (" · 장 마감 후 공시(직접 조회)" if e.get("direct") else ""))
        out.append({"code": code, "name": e["name"], "sector": (sector_of or {}).get(code), "signal": "하방촉매",
                    "score": None, "reason": why, "source": "catalyst",
                    "catalyst": {k: e.get(k) for k in ("kind", "title", "summary", "ruleKind", "rounds", "newsRounds", "discl",
                                                       "changePct", "bearScore", "strength", "url", "direct", "amended")}})
    # 강도 내림차순 → 당일 등락 내림차순(덜 빠진 종목 먼저 = 추격 리스크 작은 순, 등락 미상은 0)
    out.sort(key=lambda c: (-c["catalyst"]["strength"], -(c["catalyst"]["changePct"] if c["catalyst"]["changePct"] is not None else 0)))
    return out, skipped


class CatalystRunner:
    """daily.run_day 에 꽂는 어댑터 — 아카이브(+선택 DART 직접 조회) → 후보. 결과는 store `catalyst/<date>` 에 남긴다."""

    def __init__(self, store, *, fetch=None, dart_fetch=None, sector_of=None, log=print):
        self.store, self.fetch, self.dart_fetch, self.log = store, fetch, dart_fetch, log
        self._sector_of = sector_of

    def sector_of(self, date):
        if self._sector_of is None:
            m, inv = {}, {}
            try:
                m = universe.load_sector_map(self.store, date, log=self.log)
            except Exception as e:
                self.log(f"[swing] 촉매 업종 맵 로드 실패: {e}")
            for v in m.values():
                for c in v["codes"]:
                    inv.setdefault(c, v["name"])
            self._sector_of = inv
        return self._sector_of

    def build(self, date):
        arch = fetch_archive(date, fetch=self.fetch, log=self.log)
        extra = []
        if config.CAT_DART_DIRECT:
            extra = dart_today_bearish(date, fetch=self.dart_fetch, log=self.log)
        entries = collect_bearish(arch, extra) if (arch or extra) else {}
        doc = {"date": date, "archive": bool(arch), "rounds": len((arch or {}).get("rounds") or []), "direct": len(extra),
               "entries": sorted(entries.values(), key=lambda e: (-e["strength"], e["name"]))}
        try:
            self.store.put(f"catalyst/{date}", doc)
        except Exception as e:
            self.log(f"[swing] catalyst/{date} 저장 실패: {e}")
        return entries, doc

    def short_candidates(self, date, exclude=(), decided=None, *, fut=None):
        entries, doc = self.build(date)
        cands, skipped = short_candidates(entries, exclude, decided, sector_of=self.sector_of(date), fut=fut)
        doc["skipped"] = skipped
        doc["candidates"] = [c["code"] for c in cands]
        try:
            self.store.put(f"catalyst/{date}", doc)
        except Exception:
            pass
        return cands
