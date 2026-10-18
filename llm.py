"""Provider-agnostic LLM JSON helper with an ordered fallback chain.

One model getting its free quota / billing exhausted should not break the batch.
`generate_json()` walks an ordered chain of `provider:model` links and, when a
link fails in a *switchable* way (missing key, quota/billing exhausted, rate
limit that won't clear, auth error, server error), falls through to the next
link automatically. Only if every link fails does it raise `LLMError`.

Configure the chain with the LLM_CHAIN env var (comma-separated, highest
priority first); a link whose API key is missing is skipped silently:

    LLM_CHAIN=gemini:gemini-2.5-flash,gemini:gemini-2.0-flash,anthropic:claude-sonnet-4-6

Default chain (all Gemini, separate per-model quotas). 무료 티어 실측(2026-09-03,
프로브)상 3.1-flash-lite 만 일일 한도가 크고(500/일) 나머지는 전부 20/일이라,
용량 큰 lite 를 1순위로 두고 20/일 모델들은 '비상 폴백'으로 보존한다(구 1순위
3.5-flash 는 야간 세션에서 20회 소진 후 상시 429 → 재시도 낭비 + 폴백 쿼터 잠식):
    gemini-3.1-flash-lite -> gemini-3.5-flash-lite -> gemini-3.8-flash -> gemini-3.7-flash
    -> gemini-3.6-flash -> gemini-3.5-flash -> gemini-3-flash-preview
2026-10-18: 2.5-flash·2.5-flash-lite 가 신규 사용자 404(폐기)라 체인에서 뺐다. 10-16 swing 런에서 1순위 lite 가
503(구글 과부하)를 내자 20/일 폴백 2개가 앞 3~4종목에 바로 바닥나고 404 두 개만 남아 후보 13개가 판단 실패 →
(1) 신규 모델로 폴백 층을 늘리고 (2) 1순위 링크 5xx 는 길게 기다리며 (3) 한도 소진(429)·폐기(404) 링크는
일정 시간 건너뛴다(같은 프로세스 안에서 매 호출 재시도·대기 낭비 방지).

Keys: GEMINI_API_KEY (or GOOGLE_API_KEY) for gemini, ANTHROPIC_API_KEY for
anthropic. Every link returns parsed JSON via response_mime_type=application/json
(Gemini) or output_config.format json_schema (Anthropic), so callers never parse
free-form model text.
"""
import os
import sys
import json
import time
import random

DEFAULT_CHAIN = ("gemini:gemini-3.1-flash-lite,"
                 "gemini:gemini-3.5-flash-lite,"
                 "gemini:gemini-3.8-flash,"
                 "gemini:gemini-3.7-flash,"
                 "gemini:gemini-3.6-flash,"
                 "gemini:gemini-3.5-flash,"
                 "gemini:gemini-3-flash-preview")

MAX_RETRY = 3                                   # transient retries within one link
RETRYABLE = {429, 500, 502, 503, 504, 529}      # HTTP codes worth retrying / then switching

# 워스트케이스 꼬리 제어(브리핑 등 폴백 산출물이 있는 호출자용) — 기본값은 기존과 동일.
#   LLM_TIMEOUT_S : 요청당 HTTP 타임아웃(초). 120s×링크당 재시도 3회×5링크가 슬로우
#                   회차 꼬리를 만들 수 있어, 기계적 폴백이 있는 스텝은 짧게 잡는다.
#   LLM_MAX_LINKS : 폴백 체인 길이 상한(0=무제한). 하위 링크까지 다 도는 최악 경로 차단.
TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "120"))
MAX_LINKS = int(os.environ.get("LLM_MAX_LINKS", "0"))

# 1순위 링크 일시 장애(5xx) 인내(2026-10-18) — 짧은 재시도(MAX_RETRY)가 다 실패하면 폴백(하루 한도 작음)으로
# 넘어가기 전에 LLM_UNAVAIL_WAITS 초씩 더 기다려 본다. 그래도 실패하면 LLM_UNAVAIL_COOLDOWN_S 동안은 긴 대기를
# 생략(장애가 길 때 호출마다 몇 분씩 묶이지 않게). "" 이면 끔.
UNAVAIL_WAITS = [float(x) for x in os.environ.get("LLM_UNAVAIL_WAITS", "30,60,90").split(",") if x.strip()]
UNAVAIL_COOLDOWN_S = float(os.environ.get("LLM_UNAVAIL_COOLDOWN_S", "600"))
TRANSIENT_5XX = {500, 502, 503, 504, 529}
# 한도 소진(429 재시도 후에도) 링크는 LLM_EXHAUSTED_COOLDOWN_S 동안 건너뛴다. 404(모델 폐기·없음)는 프로세스 끝까지.
EXHAUSTED_COOLDOWN_S = float(os.environ.get("LLM_EXHAUSTED_COOLDOWN_S", "3600"))
_DEAD = {}                                      # (provider, model) -> (until_monotonic, reason)
_UNAVAIL_UNTIL = {}                             # (provider, model) -> monotonic; 긴 대기 생략 구간

_CLIENTS = {}                                   # (provider, key) -> SDK client (reused)


class LLMError(Exception):
    """Raised only when every link in the chain fails."""


class _SkipLink(Exception):
    """This link can't run (e.g. missing key) — try the next one immediately."""


class _Retryable(Exception):
    """Transient failure — retry this link, then switch if it persists."""

    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code


class _SwitchLink(Exception):
    """This link failed for good (auth/quota/billing) — switch to the next."""


def _warn(msg):
    print(f"[llm] {msg}", file=sys.stderr)


def _parse_json(text):
    """LLM 응답 JSON 파싱 — 엄격 우선, 실패 시 관용(lenient) 파싱.

    모델이 유효한 JSON 뒤에 여분 텍스트를 덧붙이는 사례("Extra data", 2026-09-03
    장중 회차·딥리서치 실측)에서 유효분까지 통째로 버려져 링크가 실패하던 것 →
    raw_decode 로 첫 번째 완전한 JSON 값만 취하고 꼬리는 무시한다.

    반환 (data, ignored_chars). 정상 응답이면 ignored_chars=0.
    앞부분부터 깨진 응답은 JSONDecodeError 전파(기존대로 링크 전환).
    """
    try:
        return json.loads(text), 0
    except json.JSONDecodeError:
        stripped = text.lstrip()
        data, end = json.JSONDecoder().raw_decode(stripped)
        return data, len(stripped) - end


def _backoff(attempt):
    return min(2 ** attempt + random.uniform(0, 1), 20)


# ---------------------------------------------------------------------------
# Adapters: each returns a JSON string (or raises _SkipLink/_Retryable/_SwitchLink)
# ---------------------------------------------------------------------------
def _gemini(model, system, user, max_tokens, schema):
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise _SkipLink("GEMINI_API_KEY missing")
    from google import genai
    from google.genai import errors as gerr

    client = _CLIENTS.get(("gemini", key))
    if client is None:
        # SDK 기본 HTTP 타임아웃이 무제한이라 정체된 연결 하나가 호출을 무한 대기시킨다
        # (2026-07-13 마감 시황 재실행이 생성 스텝에서 30분+ 걸린 원인). 기본 120초 제한
        # (LLM_TIMEOUT_S 로 조정) — 초과 시 transport 예외 → _Retryable → 재시도/링크 전환.
        client = genai.Client(api_key=key,
                              http_options=genai.types.HttpOptions(
                                  timeout=int(TIMEOUT_S * 1000)))
        _CLIENTS[("gemini", key)] = client

    # Gemini 는 Anthropic 의 output_config 같은 스키마 강제 수단을 쓰지 않으므로
    # 스키마를 시스템 프롬프트에 명시한다. 이게 없으면 모델이 키 이름을 매 호출
    # 추측해 실행마다 다른 구조가 나온다 (섹터분석 summary 가 간헐적으로 빈 값이 되던 원인).
    if schema is not None:
        system = (system + "\n\n출력 JSON 스키마 — 아래 키 이름과 구조를 정확히 따를 것:\n"
                  + json.dumps(schema, ensure_ascii=False))

    cfg_kwargs = dict(
        system_instruction=system,
        response_mime_type="application/json",   # guarantees valid, escaped JSON
        max_output_tokens=max_tokens,
        temperature=0,
    )
    # Gemini "thinks" by default, which eats the output-token budget and truncates
    # the JSON (Unterminated string). These are deterministic extraction tasks, so
    # minimize thinking. The control differs by family: 2.5 uses thinking_budget=0;
    # Gemini 3 uses thinking_level (lowest broadly-supported level is "low").
    if "2.5" in model:
        cfg_kwargs["thinking_config"] = genai.types.ThinkingConfig(thinking_budget=0)
    elif model.startswith("gemini-3"):
        cfg_kwargs["thinking_config"] = genai.types.ThinkingConfig(thinking_level="low")
    cfg = genai.types.GenerateContentConfig(**cfg_kwargs)
    try:
        resp = client.models.generate_content(model=model, contents=user, config=cfg)
    except gerr.APIError as e:
        code = getattr(e, "code", None)
        if code in RETRYABLE:
            raise _Retryable(f"gemini {model} {code} {getattr(e, 'status', '')}", code)
        if code == 404:
            _DEAD[("gemini", model)] = (float("inf"), "404")
        raise _SwitchLink(f"gemini {model} {code} {getattr(e, 'message', e)}")
    except Exception as e:                        # transport / unexpected
        raise _Retryable(f"gemini {model} transport: {e}")

    text = getattr(resp, "text", None)
    if not text:
        fr = None
        try:
            fr = resp.candidates[0].finish_reason
        except Exception:
            pass
        raise _SwitchLink(f"gemini {model} empty response (finish={fr})")
    return text


def _anthropic(model, system, user, max_tokens, schema):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise _SkipLink("ANTHROPIC_API_KEY missing")
    import anthropic

    key = os.environ["ANTHROPIC_API_KEY"]
    client = _CLIENTS.get(("anthropic", key))
    if client is None:
        client = anthropic.Anthropic(timeout=TIMEOUT_S)   # gemini 와 동일한 요청 타임아웃
        _CLIENTS[("anthropic", key)] = client

    kwargs = dict(
        model=model, max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    if schema is not None:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    try:
        resp = client.messages.create(**kwargs)
    except anthropic.APIStatusError as e:
        code = getattr(e, "status_code", None)
        if code in RETRYABLE:
            raise _Retryable(f"anthropic {model} {code}", code)
        raise _SwitchLink(f"anthropic {model} {code} {getattr(e, 'message', e)}")
    except anthropic.APIConnectionError as e:
        raise _Retryable(f"anthropic {model} transport: {e}")
    except anthropic.APIError as e:
        raise _SwitchLink(f"anthropic {model} {e}")

    if resp.stop_reason == "max_tokens":
        raise _SwitchLink(f"anthropic {model} truncated at max_tokens={max_tokens}")
    return "".join(b.text for b in resp.content if b.type == "text")


_ADAPTERS = {"gemini": _gemini, "anthropic": _anthropic}


# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------
def _chain(raw=None):
    """raw = 호출자가 넘긴 체인 문자열(예: LIVEPM_LLM_CHAIN). 없으면 LLM_CHAIN → DEFAULT_CHAIN."""
    raw = raw or os.environ.get("LLM_CHAIN") or DEFAULT_CHAIN   # empty/unset -> default
    out = []
    for part in raw.split(","):
        provider, _, model = part.strip().partition(":")
        provider, model = provider.strip().lower(), model.strip()
        if provider in _ADAPTERS and model:
            out.append((provider, model))
    if MAX_LINKS > 0:
        out = out[:MAX_LINKS]
    return out or [("gemini", "gemini-3.5-flash")]


def configured(chain=None):
    """True if at least one chain link has its API key available."""
    for provider, _ in _chain(chain):
        if provider == "gemini" and (os.environ.get("GEMINI_API_KEY")
                                     or os.environ.get("GOOGLE_API_KEY")):
            return True
        if provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
            return True
    return False


def _invoke(provider, model, system, user, max_tokens, schema, primary=False):
    """Run one link with transient retries; raise _SwitchLink/_SkipLink to move on.

    primary=True(체인 1순위)이고 마지막 실패가 5xx 면 UNAVAIL_WAITS 만큼 더 기다리며 재시도한다(쿨다운 중이면 생략).
    재시도 끝에 429 면 그 링크를 EXHAUSTED_COOLDOWN_S 동안 건너뛴다."""
    fn = _ADAPTERS[provider]
    link = (provider, model)
    last = None
    waits = [_backoff(a) for a in range(MAX_RETRY - 1)]
    extra = list(UNAVAIL_WAITS) if primary and _UNAVAIL_UNTIL.get(link, 0) <= time.monotonic() else []
    i = 0
    while True:
        try:
            return fn(model, system, user, max_tokens, schema)
        except (_SkipLink, _SwitchLink):
            raise
        except _Retryable as e:
            last = e
        if i < len(waits):
            time.sleep(waits[i])
        elif extra and getattr(last, "code", None) in TRANSIENT_5XX:
            w = extra.pop(0)
            _warn(f"{provider}:{model} {last.code} — 1순위 링크 장애, {w:.0f}s 후 재시도")
            time.sleep(w)
        else:
            break
        i += 1
    code = getattr(last, "code", None)
    if primary and code in TRANSIENT_5XX and UNAVAIL_WAITS:
        _UNAVAIL_UNTIL[link] = time.monotonic() + UNAVAIL_COOLDOWN_S
    if code == 429:
        _DEAD[link] = (time.monotonic() + EXHAUSTED_COOLDOWN_S, "429")
    raise _SwitchLink(f"retries exhausted: {last}")


def generate_json(system, user, *, max_tokens=4096, schema=None, return_model=False, chain=None):
    """Return parsed JSON from the first chain link that succeeds.

    system/user are plain strings. `schema` is a JSON-Schema dict used for strict
    output on providers that support it (Anthropic); on Gemini the schema is
    appended to the system instruction plus response_mime_type=application/json.

    With return_model=True, returns (data, model_id) so callers can record which
    model produced the answer (the chain may have fallen through several links).

    chain = 'provider:model,...' 문자열로 이 호출만 체인을 바꾼다(Live PM 판단 등, 2026-10-18).
    없으면 LLM_CHAIN 환경변수 → DEFAULT_CHAIN.

    Raises LLMError if every configured link fails.
    """
    seen = []
    for idx, (provider, model) in enumerate(_chain(chain)):
        dead = _DEAD.get((provider, model))
        if dead and dead[0] > time.monotonic():
            seen.append(f"{provider}:{model} skipped ({dead[1]} 이후 건너뜀)")
            continue
        try:
            text = _invoke(provider, model, system, user, max_tokens, schema, primary=(idx == 0))
        except _SkipLink as e:
            seen.append(f"{provider}:{model} skipped ({e})")
            continue
        except _SwitchLink as e:
            _warn(f"switching off {provider}:{model} ({e})")
            seen.append(f"{provider}:{model} failed ({e})")
            continue
        try:
            data, junk = _parse_json(text)
        except json.JSONDecodeError as e:
            _warn(f"{provider}:{model} returned unparseable JSON ({e}); switching")
            seen.append(f"{provider}:{model} bad JSON ({e})")
            continue
        if junk:
            _warn(f"{provider}:{model} JSON 뒤 여분 텍스트 {junk}자 무시(관용 파싱)")
        return (data, model) if return_model else data
    raise LLMError("all LLM links failed: " + " | ".join(seen))
