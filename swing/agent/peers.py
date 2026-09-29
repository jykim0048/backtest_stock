"""해외 peer 무인 확정(HANDOFF 3-6, B안) — 스킬의 'Claude 대화형 제안'을 LLM 1콜로 대체.

순서: peers.json 큐레이션 → 저장소 캐시(TTL 이내) → [없으면] LLM 제안 → validate_proposal →
yfinance 5일 시세 검증 → 미국 벨웨더 앞으로 → 유효 2개 미만이면 거절 사유 붙여 1회 재제안 →
그래도 미달이면 업종 기본표 + failedAt 기록(재시도 대기). 진실 원천은 swing 저장소 `peers/dynamic`
(Railway 파일시스템은 휘발성), 판단 직전 kit/memory/peers_dynamic.json 으로 내려써 collect.py 가 읽는다.
어떤 예외도 판단을 막지 않는다(호출측이 감쌈).
"""
import datetime
import json
import os

from . import KIT_DIR, ensure_kit_path

STORE_KEY = "peers/dynamic"
TTL_DAYS = int(os.environ.get("SWING_PEER_TTL_DAYS", "180") or 180)
RETRY_DAYS = int(os.environ.get("SWING_PEER_RETRY_DAYS", "30") or 30)
MEMORY_FILE = os.path.join(KIT_DIR, "memory", "peers_dynamic.json")

# 원본 peers_resolve.py 독스트링의 제안 규칙(= backtest_stock PEER_SYSTEM + 벨웨더) 그대로
SYSTEM = """당신은 한국 상장사의 해외 비교기업(peer)을 고르는 애널리스트다.
규칙:
- 이 한국 종목과 **사업이 가장 유사한 해외 상장** 비교기업 4~5개. 한국 상장사는 제외.
- ticker 는 Yahoo Finance 심볼(미국 AAPL, 일본 6479.T, 대만 3008.TW, 홍콩 2382.HK, 독일 ENR.DE, 영국 BAB.L).
- **앞 2개는 미국 상장(접미사 없는 심볼) 벨웨더** — StockTwits·Reddit 수집 대상이 된다.
- note 는 "왜 peer 인가"를 한국어 한 줄로.
- 실재하지 않거나 확신이 없는 티커는 넣지 않는다.
JSON 으로만 답한다: {"peers":[{"name":"...","ticker":"...","note":"..."}]}"""
SCHEMA = {"type": "object",
          "properties": {"peers": {"type": "array", "items": {
              "type": "object",
              "properties": {"name": {"type": "string"}, "ticker": {"type": "string"},
                             "note": {"type": "string"}},
              "required": ["name", "ticker", "note"]}}},
          "required": ["peers"]}


def _today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).date()


def _age_days(s, today):
    try:
        return (today - datetime.date.fromisoformat(str(s)[:10])).days
    except Exception:
        return 10 ** 6


def _read_asset(name):
    with open(os.path.join(KIT_DIR, "assets", name), encoding="utf-8") as f:
        return json.load(f) or {}


def _user_prompt(stock, profile, rejected):
    lines = [f"종목: {stock.get('name')} ({stock.get('code')}), 야후 티커 {stock.get('ticker') or '-'}",
             f"yfinance 프로필: sector={profile.get('sector')}, industry={profile.get('industry')}, "
             f"industryKey={profile.get('industryKey')}"]
    for k in ("longName", "summary", "longBusinessSummary"):
        if profile.get(k):
            lines.append(f"{k}: {str(profile[k])[:1500]}")
    if rejected:
        lines.append("직전 제안에서 거절된 티커(다시 쓰지 말 것): "
                     + ", ".join(f"{r.get('ticker')}({r.get('reason')})" for r in rejected))
    return "\n".join(lines)


def write_memory(code, entry):
    """kit/memory/peers_dynamic.json 을 이 종목 한 건(또는 빈 dict)으로 덮어쓴다 — collect.py 입력."""
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    doc = {code: entry} if entry and entry.get("peers") else {}
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)


def resolve_peers(stock, store, llm_fn, *, profile_fn=None, yf_check=None, today=None):
    """→ info {source, peers, rejected, llmCalls, model, note}. 캐시·kit 메모리 파일 갱신 포함.
    llm_fn(system, user, max_tokens, schema) -> (data, model)."""
    ensure_kit_path()
    import peers_resolve as pr
    import sources_kr as sources
    today = today or _today()
    profile_fn = profile_fn or sources.yahoo_profile
    yf_check = yf_check or pr.check_yfinance
    code = stock["code"]

    try:
        profile = profile_fn(stock.get("ticker") or "") if stock.get("ticker") else {}
    except Exception:
        profile = {}
    profile = profile or {}
    cache = (store.get(STORE_KEY) if store else None) or {}
    entry = cache.get(code) or {}
    fresh = bool(entry.get("peers")) and _age_days(entry.get("resolved_at"), today) <= TTL_DAYS
    dyn_cfg = {code: entry} if fresh else {}
    peers, source = sources.select_peers(code, profile.get("industryKey"), _read_asset("peers.json"),
                                         _read_asset("industry_peers.json"), dynamic_cfg=dyn_cfg)
    info = {"source": source, "peers": peers, "rejected": [], "llmCalls": 0, "model": None,
            "industry": {k: profile.get(k) for k in ("sector", "industry", "industryKey")}}
    if source == "dynamic":
        info["source"] = entry.get("source") or "dynamic"
        write_memory(code, entry)
        return info
    if source == "curated":
        write_memory(code, None)
        return info
    if entry.get("failedAt") and _age_days(entry["failedAt"], today) <= RETRY_DAYS:
        info["note"] = f"제안 실패 후 재시도 대기(failedAt {entry['failedAt']})"
        write_memory(code, None)
        return info

    valid, rejected = [], []
    for _ in range(2):
        data, model = llm_fn(SYSTEM, _user_prompt(stock, profile, rejected), 1500, SCHEMA)
        info["llmCalls"] += 1
        info["model"] = model
        items = (data or {}).get("peers") if isinstance(data, dict) else data
        accepted, rej = pr.validate_proposal(items or [])
        ok, rej_net = yf_check(accepted)
        rejected += list(rej) + list(rej_net)
        valid = pr.order_bellwethers_first(ok)
        if len(valid) >= 2:
            break
    info["rejected"] = rejected
    if len(valid) >= 2:
        new = {"name": stock.get("name", ""), "resolved_at": today.isoformat(),
               "source": "llm-proposed", "model": info["model"], "peers": valid}
        info.update(source="llm-proposed", peers=valid)
    else:
        new = {"name": stock.get("name", ""), "failedAt": today.isoformat(), "rejected": rejected}
        info["note"] = f"유효 peer {len(valid)}개 — 업종 기본표로 진행"
    if store:
        cache[code] = new
        store.put(STORE_KEY, cache)
    write_memory(code, new if new.get("peers") else None)
    return info
