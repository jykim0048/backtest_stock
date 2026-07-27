"""Provider-agnostic LLM JSON helper with an ordered fallback chain.

One model getting its free quota / billing exhausted should not break the batch.
`generate_json()` walks an ordered chain of `provider:model` links and, when a
link fails in a *switchable* way (missing key, quota/billing exhausted, rate
limit that won't clear, auth error, server error), falls through to the next
link automatically. Only if every link fails does it raise `LLMError`.

Configure the chain with the LLM_CHAIN env var (comma-separated, highest
priority first); a link whose API key is missing is skipped silently:

    LLM_CHAIN=gemini:gemini-2.5-flash,gemini:gemini-2.0-flash,anthropic:claude-sonnet-4-6

Default chain (cost first — all Gemini, separate per-model quotas):
    gemini-3.5-flash -> gemini-3.1-flash-lite -> gemini-3-flash-preview
    -> gemini-2.5-flash -> gemini-2.5-flash-lite

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

DEFAULT_CHAIN = ("gemini:gemini-3.5-flash,"
                 "gemini:gemini-3.1-flash-lite,"
                 "gemini:gemini-3-flash-preview,"
                 "gemini:gemini-2.5-flash,"
                 "gemini:gemini-2.5-flash-lite")

MAX_RETRY = 3                                   # transient retries within one link
RETRYABLE = {429, 500, 502, 503, 504, 529}      # HTTP codes worth retrying / then switching

# 워스트케이스 꼬리 제어(브리핑 등 폴백 산출물이 있는 호출자용) — 기본값은 기존과 동일.
#   LLM_TIMEOUT_S : 요청당 HTTP 타임아웃(초). 120s×링크당 재시도 3회×5링크가 슬로우
#                   회차 꼬리를 만들 수 있어, 기계적 폴백이 있는 스텝은 짧게 잡는다.
#   LLM_MAX_LINKS : 폴백 체인 길이 상한(0=무제한). 하위 링크까지 다 도는 최악 경로 차단.
TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "120"))
MAX_LINKS = int(os.environ.get("LLM_MAX_LINKS", "0"))

_CLIENTS = {}                                   # (provider, key) -> SDK client (reused)


class LLMError(Exception):
    """Raised only when every link in the chain fails."""


class _SkipLink(Exception):
    """This link can't run (e.g. missing key) — try the next one immediately."""


class _Retryable(Exception):
    """Transient failure — retry this link, then switch if it persists."""


class _SwitchLink(Exception):
    """This link failed for good (auth/quota/billing) — switch to the next."""


def _warn(msg):
    print(f"[llm] {msg}", file=sys.stderr)


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
            raise _Retryable(f"gemini {model} {code} {getattr(e, 'status', '')}")
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
            raise _Retryable(f"anthropic {model} {code}")
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
def _chain():
    raw = os.environ.get("LLM_CHAIN") or DEFAULT_CHAIN   # empty/unset -> default
    out = []
    for part in raw.split(","):
        provider, _, model = part.strip().partition(":")
        provider, model = provider.strip().lower(), model.strip()
        if provider in _ADAPTERS and model:
            out.append((provider, model))
    if MAX_LINKS > 0:
        out = out[:MAX_LINKS]
    return out or [("gemini", "gemini-3.5-flash")]


def configured():
    """True if at least one chain link has its API key available."""
    for provider, _ in _chain():
        if provider == "gemini" and (os.environ.get("GEMINI_API_KEY")
                                     or os.environ.get("GOOGLE_API_KEY")):
            return True
        if provider == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
            return True
    return False


def _invoke(provider, model, system, user, max_tokens, schema):
    """Run one link with transient retries; raise _SwitchLink/_SkipLink to move on."""
    fn = _ADAPTERS[provider]
    last = None
    for attempt in range(MAX_RETRY):
        try:
            return fn(model, system, user, max_tokens, schema)
        except (_SkipLink, _SwitchLink):
            raise
        except _Retryable as e:
            last = e
            if attempt < MAX_RETRY - 1:
                time.sleep(_backoff(attempt))
    raise _SwitchLink(f"retries exhausted: {last}")


def generate_json(system, user, *, max_tokens=4096, schema=None, return_model=False):
    """Return parsed JSON from the first chain link that succeeds.

    system/user are plain strings. `schema` is a JSON-Schema dict used for strict
    output on providers that support it (Anthropic); on Gemini the schema is
    appended to the system instruction plus response_mime_type=application/json.

    With return_model=True, returns (data, model_id) so callers can record which
    model produced the answer (the chain may have fallen through several links).

    Raises LLMError if every configured link fails.
    """
    seen = []
    for provider, model in _chain():
        try:
            text = _invoke(provider, model, system, user, max_tokens, schema)
        except _SkipLink as e:
            seen.append(f"{provider}:{model} skipped ({e})")
            continue
        except _SwitchLink as e:
            _warn(f"switching off {provider}:{model} ({e})")
            seen.append(f"{provider}:{model} failed ({e})")
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            _warn(f"{provider}:{model} returned unparseable JSON ({e}); switching")
            seen.append(f"{provider}:{model} bad JSON ({e})")
            continue
        return (data, model) if return_model else data
    raise LLMError("all LLM links failed: " + " | ".join(seen))
