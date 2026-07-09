#!/usr/bin/env python3
"""장중 시황 브리핑 생성 — 모닝브리핑의 '장중 실시간' 대칭 버전.

모닝브리핑이 미국장 기반 예습이라면, 이 스크립트는 한국장이 실제로 어떻게
흘러가는지 30분 회차로 복습한다 (intraday_screener.yml 스텝으로 실행):

  1) 한국 급등/급락 테마 (네이버 테마 랭킹 — 각 3개, 동일 비중)
  2) 테마별 주요 밸류체인 (테마 구성종목 등락 상위 + 편입 사유)
  3) 장중 국내 촉매 (DART 당일 촉매성 공시 + '특징주' 뉴스)
  4) LLM 시황 종합 (브리핑 3~4문장 · 테마별 1줄 · 촉매 선별 요약)

산출:
  public/intraday_briefing.json                    (최신 회차 — 대시보드)
  public/reports/intraday_briefing/<date>.json     (회차 누적 rounds[] 아카이브)

LLM 실패 시에도 기계적 수집분(테마·밸류체인·공시·뉴스)만으로 완결된 JSON 을
산출한다(graceful degradation — 모닝브리핑과 동일 원칙).
"""
import os
import sys
import json
import datetime

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from analysis import sources
import llm

KST = datetime.timezone(datetime.timedelta(hours=9))

OUT_PATH = os.environ.get("INTRADAY_BRIEFING_OUT") or os.path.join(
    ROOT, "public", "intraday_briefing.json")
ARCHIVE_DIR = os.path.join(ROOT, "public", "reports", "intraday_briefing")
THEMES_PER_SIDE = int(os.environ.get("BRIEFING_THEMES", "3"))     # 급등/급락 각 N개
STOCKS_PER_THEME = int(os.environ.get("BRIEFING_VC_STOCKS", "4"))  # 테마당 밸류체인 종목


def _warn(msg):
    print(f"[intraday-briefing] {msg}", file=sys.stderr)


# ----------------------------------------------------------------------------
# Phase 1) 수집
# ----------------------------------------------------------------------------
def fetch_indices():
    """코스피/코스닥 지수 — railway_server._naver_indices 와 동일 소스.
    closePrice 는 장전엔 전일 종가, 장중·마감 후엔 당일 (준)실시간/종가."""
    def _num(s):
        try:
            return float(str(s or "").replace(",", "").replace("+", ""))
        except (ValueError, AttributeError):
            return None
    out = {}
    for key, sym in (("kospi", "KOSPI"), ("kosdaq", "KOSDAQ")):
        try:
            r = requests.get(f"https://m.stock.naver.com/api/index/{sym}/basic",
                             headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            d = r.json()
            price, rate = _num(d.get("closePrice")), _num(d.get("fluctuationsRatio"))
            if price is not None:
                out[key] = {"price": price, "rate": rate if rate is not None else 0.0}
        except Exception as e:
            _warn(f"index {sym}: {e}")
    return out


def pick_themes():
    """테마 랭킹에서 급등/급락 각 N개 + 테마별 밸류체인(등락 상위 종목)."""
    ranking = sources.naver_theme_ranking()
    if not ranking:
        return [], []
    ranking.sort(key=lambda t: t["changePct"], reverse=True)
    ups, downs = ranking[:THEMES_PER_SIDE], ranking[-THEMES_PER_SIDE:][::-1]

    def _attach(theme, reverse):
        stocks = sources.naver_theme_stocks(theme["no"], limit=12)
        # 등락률 결측은 뒤로, 급등 테마는 상승 상위·급락 테마는 하락 상위 순
        stocks.sort(key=lambda s: (s["changePct"] is None,
                                   -(s["changePct"] or 0) if reverse else (s["changePct"] or 0)))
        return {**theme, "stocks": stocks[:STOCKS_PER_THEME]}

    return ([_attach(t, True) for t in ups],
            [_attach(t, False) for t in downs])


def fetch_movers():
    """급등/급락 개별종목 (LLM 재료 — 테마 밖 단독 급등주 포착용)."""
    up, down = [], []
    for market in ("KOSPI", "KOSDAQ"):
        up += sources.naver_stock_ranking("up", market, limit=10)
        down += sources.naver_stock_ranking("down", market, limit=10)
    up.sort(key=lambda s: -(s["changePct"] or 0))
    down.sort(key=lambda s: (s["changePct"] or 0))
    return up[:10], down[:10]


def fetch_news(display=20):
    """'특징주' 뉴스 — 장중 급등락 사유가 실시간으로 잡히는 대표 검색어.
    가능하면 오늘(KST) 기사만 남긴다(pubDate 파싱 실패 시 무필터)."""
    items = sources.naver_search("news", "특징주", display=display)
    today = datetime.datetime.now(KST).strftime("%d %b %Y")   # RFC822 "09 Jul 2026"
    todays = [it for it in items if today in (it.get("date") or "")]
    return todays if todays else items


# ----------------------------------------------------------------------------
# Phase 2) LLM 종합
# ----------------------------------------------------------------------------
SYSTEM = (
    "당신은 한국 주식시장 장중 시황을 요약하는 마켓 애널리스트입니다. "
    "제공된 RAW(지수, 급등/급락 테마와 구성종목·편입사유, 급등/급락 개별종목, "
    "당일 공시, 특징주 뉴스)만 근거로 장중 시황 브리핑을 작성하세요.\n"
    "규칙:\n"
    "- 데이터에 없는 수치·사실을 지어내지 말 것. 등락률 등 수치는 입력값을 인용할 것.\n"
    "- briefing 은 3~4개의 완결된 문장(각각이 대시보드 불릿 하나): 지수 흐름 → 주도 테마와 "
    "동인 → 약세 테마 → 관전 포인트 순으로.\n"
    "- themeComments 는 입력의 모든 테마(급등·급락 각각)에 대해 1문장씩 — 왜 움직이는지를 "
    "구성종목 편입사유·공시·뉴스와 연결해 서술하고, name 은 입력 표기 그대로 echo 할 것.\n"
    "- catalysts 는 입력 공시(id: d0,d1,..)와 뉴스(id: n0,n1,..) 중 시장 영향이 큰 3~6건을 "
    "선별해 id 를 그대로 echo 하고 summary 1문장을 쓸 것. 시장 전체 관점에서 중요한 것 우선.\n"
    "- 한국어. 출력은 지정된 JSON 스키마를 엄격히 따를 것."
)

_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "briefing": {"type": "array", "items": _STR},
        "themeComments": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": _STR, "comment": _STR},
            "required": ["name", "comment"]}},
        "catalysts": {"type": "array", "items": {
            "type": "object",
            "properties": {"id": _STR, "summary": _STR},
            "required": ["id", "summary"]}},
    },
    "required": ["briefing", "themeComments", "catalysts"],
}


def _norm(s):
    return "".join(ch for ch in str(s or "") if ch not in " \t·ㆍ・")


def synthesize(indices, ups, downs, movers_up, movers_down, disclosures, news):
    raw = {
        "indices": indices,
        "themesUp": [{"name": t["name"], "changePct": t["changePct"],
                      "stocks": [{"name": s["name"], "changePct": s["changePct"],
                                  "reason": (s.get("reason") or "")[:80]}
                                 for s in t["stocks"]]} for t in ups],
        "themesDown": [{"name": t["name"], "changePct": t["changePct"],
                        "stocks": [{"name": s["name"], "changePct": s["changePct"],
                                    "reason": (s.get("reason") or "")[:80]}
                                   for s in t["stocks"]]} for t in downs],
        "moversUp": [{"name": s["name"], "changePct": s["changePct"]} for s in movers_up],
        "moversDown": [{"name": s["name"], "changePct": s["changePct"]} for s in movers_down],
        "disclosures": [{"id": f"d{i}", "corp": d["corp"], "title": d["title"]}
                        for i, d in enumerate(disclosures)],
        "news": [{"id": f"n{i}", "title": n["title"],
                  "description": (n.get("description") or "")[:100]}
                 for i, n in enumerate(news)],
    }
    user = "RAW 데이터(JSON):\n" + json.dumps(raw, ensure_ascii=False)
    synth, model = {}, None
    if llm.configured():
        try:
            synth, model = llm.generate_json(SYSTEM, user, max_tokens=4096,
                                             schema=SCHEMA, return_model=True)
        except Exception as e:
            _warn(f"LLM 종합 실패: {e} — 기계적 결과만 산출")
    else:
        _warn("LLM 미설정 — 기계적 결과만 산출")

    # 테마 코멘트 병합 (표기 차이 허용)
    by_name = {_norm(c.get("name")): (c.get("comment") or "").strip()
               for c in (synth.get("themeComments") or [])}
    for t in ups + downs:
        t["comment"] = by_name.get(_norm(t["name"]), "")

    # 촉매 id → 원본 매칭 (공시 dN / 뉴스 nN)
    catalysts = []
    for c in (synth.get("catalysts") or []):
        cid, summary = str(c.get("id") or ""), (c.get("summary") or "").strip()
        try:
            idx = int(cid[1:])
        except (ValueError, IndexError):
            continue
        if cid.startswith("d") and 0 <= idx < len(disclosures):
            d = disclosures[idx]
            catalysts.append({"kind": "disclosure", "corp": d["corp"],
                              "title": d["title"], "url": d["url"], "summary": summary})
        elif cid.startswith("n") and 0 <= idx < len(news):
            n = news[idx]
            catalysts.append({"kind": "news", "corp": "",
                              "title": n["title"], "url": n.get("link", ""),
                              "summary": summary})
    return [b.strip() for b in (synth.get("briefing") or []) if b and b.strip()], \
        catalysts, model


# ----------------------------------------------------------------------------
# Phase 3) 산출 + 아카이브
# ----------------------------------------------------------------------------
def main():
    print("=== Intraday Market Briefing ===")
    now = datetime.datetime.now(KST)
    indices = fetch_indices()
    ups, downs = pick_themes()
    movers_up, movers_down = fetch_movers()
    disclosures = sources.dart_today_disclosures(limit=40)
    news = fetch_news()
    print(f"  수집: 테마 급등{len(ups)}/급락{len(downs)}, 공시 {len(disclosures)}, "
          f"뉴스 {len(news)}")

    briefing, catalysts, model = synthesize(
        indices, ups, downs, movers_up, movers_down, disclosures, news)

    out = {
        "date": now.strftime("%Y-%m-%d"),
        "asof": now.strftime("%Y-%m-%d %H:%M KST"),
        "generatedBy": model or "mechanical",
        "indices": indices,
        "briefing": briefing,
        "themesUp": ups,
        "themesDown": downs,
        "catalysts": catalysts,
        "disclosures": disclosures[:15],
        "news": news[:10],
        "disclaimer": "네이버 금융·DART·네이버뉴스 기반 자동 생성 시황 — 투자 판단 참고용.",
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  Updated: {OUT_PATH} (model={out['generatedBy']}, "
          f"briefing {len(briefing)}문장, 촉매 {len(catalysts)}건)")

    # 회차 누적 아카이브 — 장 마감 후 하루 흐름 복기용
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    arch_path = os.path.join(ARCHIVE_DIR, f"{out['date']}.json")
    arch = {"date": out["date"], "rounds": []}
    if os.path.exists(arch_path):
        try:
            with open(arch_path, encoding="utf-8") as f:
                arch = json.load(f)
        except Exception:
            pass
    if not isinstance(arch.get("rounds"), list):
        arch["rounds"] = []
    arch["rounds"].append(out)
    with open(arch_path, "w", encoding="utf-8") as f:
        json.dump(arch, f, ensure_ascii=False, indent=2)
    print(f"  Archive: {arch_path} ({len(arch['rounds'])} rounds)")
    print("=== Done ===")


if __name__ == "__main__":
    main()
