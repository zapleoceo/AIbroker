"""Chat/embed orchestration.

Routes stay thin (validate → call → shape response). Everything about picking a
key, checking caps, calling the provider, classifying the error, recording usage
and walking to the next provider in the chain lives here.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from aibroker.crypto import decrypt
from aibroker.db.models import ApiKeyRow, ProjectRow
from aibroker.providers import call_llm
from aibroker.providers.context_limits import (
    estimate_prompt_tokens,
    fits_context,
    is_too_large_error,
)
from aibroker.providers.litellm_adapter import embed, model_for, transcribe
from aibroker.providers.observations import learned_ceilings, record_too_large
from aibroker.routing import (
    CostGuardError,
    chain_for,
    check_caps,
    pick_and_reserve,
    scope_for,
)
from aibroker.routing.selector import mark_cooldown, mark_dead, record_usage
from aibroker.telemetry import audit

log = logging.getLogger(__name__)

_COOLDOWN = timedelta(minutes=5)
# Free keys (esp. gemini) rate-limit constantly; try a few keys of a provider
# before falling through to the next provider, like a direct client would.
_MAX_KEYS_PER_PROVIDER = 5


# Substrings (lower-cased) that mean a provider throttled us — covers the
# many shapes: '429', 'rate_limit' (underscore), 'ratelimiterror' (CamelCase
# from litellm/cerebras), Google's 'resource_exhausted', and the quota
# phrasings ('quota', 'tokens per day', 'too many tokens'). Missing any of
# these meant cerebras 'RateLimitError - Tokens per day' fell through to
# 'error' and the key was never cooled → infinite retry storm.
_RATE_LIMIT_SIGNS = (
    "rate_limit",
    "ratelimit",
    "429",
    "resource_exhausted",
    "quota",
    "tokens per day",
    "tokens per minute",
    "too many tokens",
    "too many requests",
)


def classify_provider_error(exc: Exception) -> str:
    """Map a provider exception to one of: 'rate_limit', 'auth', 'error'.

    Single source of truth — both chat and embed paths classify the same way.
    """
    emsg = str(exc).lower()
    if any(sign in emsg for sign in _RATE_LIMIT_SIGNS):
        return "rate_limit"
    if "401" in emsg or "403" in emsg or "auth" in emsg:
        return "auth"
    return "error"


async def _penalize(key: ApiKeyRow, exc: Exception) -> str:
    """Cooldown on rate-limit, mark dead on auth error. Returns the error kind."""
    kind = classify_provider_error(exc)
    if kind == "rate_limit":
        # 2026-06-29: cooldown resolved by the provider's own signal —
        # retry-after hint > daily-quota (until UTC midnight) > adaptive
        # backoff. Stops the retry storm where a daily-exhausted key
        # (cerebras "tokens per day limit exceeded") got a 60 s cooldown,
        # recovered, got hammered, re-failed — looping until midnight.
        try:
            from aibroker.routing.cooldown import cooldown_until
            until = await cooldown_until(key.id, key.provider, str(exc))
        except Exception:
            until = datetime.now(UTC) + _COOLDOWN
        await mark_cooldown(key.id, until)
    elif kind == "auth":
        await mark_dead(key.id)
    return kind


def _wants_json(response_format: dict[str, Any] | None) -> bool:
    return bool(response_format) and response_format.get("type") in (
        "json_object", "json_schema"
    )


def _is_valid_json(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, TypeError):
        return False
    return True


def _billed_cost(key: ApiKeyRow, meta: dict[str, Any]) -> float:
    """What we actually owe the provider for this call.

    `estimate_llm_cost` (LiteLLM's pricing map) prices by MODEL — it has no
    concept of "this specific key is on a free plan". A free-tier key calling
    e.g. gemini-2.5-flash gets the same nominal per-token price a paid caller
    would pay, even though the free plan absorbs it at $0 real cost to us.
    Free-tier keys must always bill $0, whatever the model's list price is.
    """
    return 0.0 if key.tier == "free" else meta["cost_usd"]


@dataclass
class ChatOutcome:
    text: str
    provider: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_ms: int
    key_label: str


async def run_chat(
    *,
    project: ProjectRow,
    capability: str,
    messages: list[dict[str, Any]],
    model: str | None,
    max_tokens: int,
    temperature: float,
    response_format: dict[str, Any] | None,
    workflow: str | None,
) -> ChatOutcome | None:
    """Walk the capability chain; return the first provider that succeeds, else None.

    Within a provider, try up to `_MAX_KEYS_PER_PROVIDER` keys (the selector hands
    out a fresh LRU key each time and `_penalize` cools failed ones) before falling
    through — so one rate-limited free key doesn't sink the whole request.
    """
    scope = scope_for(capability)
    # Size-aware provider filter: drop providers whose single-request token
    # ceiling can't fit this prompt (e.g. groq getting a 24k Coach prompt —
    # a guaranteed 413). The ceiling is self-learned per provider (overrides
    # the code seed). The request still reaches a provider that CAN serve it,
    # so quality is identical; we just skip the wasted failures. If EVERY
    # provider is size-skipped (impossible today — big-context providers have
    # no ceiling), fall back to the full chain so we never starve.
    est_tokens = estimate_prompt_tokens(messages)
    learned = await learned_ceilings()
    full_chain = chain_for(capability)
    sized_chain = [
        p for p in full_chain if fits_context(p, est_tokens, learned.get(p))
    ]
    chain = sized_chain or full_chain
    if len(sized_chain) < len(full_chain):
        log.info("chat:%s prompt ~%d tok — skipping over-ceiling providers: %s",
                 capability, est_tokens,
                 [p for p in full_chain if p not in sized_chain])

    for provider in chain:
        for _ in range(_MAX_KEYS_PER_PROVIDER):
            key = await pick_and_reserve(provider, scope=scope)
            if key is None:
                break  # no (more) available key for this provider → next provider
            try:
                await check_caps(api_key=key, project=project, estimated_cost=0.0)
            except CostGuardError as e:
                await audit(actor=f"project:{project.name}", action="cap_block",
                            target=f"provider={provider}", metadata={"reason": str(e)})
                break  # project/global cap — more keys won't help → next provider
            use_model = model or model_for(provider, capability)
            if not use_model:
                break  # provider can't serve this capability → next provider
            plain = decrypt(key.token_encrypted)
            try:
                text, meta = await call_llm(
                    model=use_model, messages=messages, api_key=plain,
                    max_tokens=max_tokens, temperature=temperature,
                    response_format=response_format,
                )
                meta["cost_usd"] = _billed_cost(key, meta)
            except Exception as e:  # noqa: BLE001 — classify, cool the key, try next
                kind = await _penalize(key, e)
                # Self-learn the size ceiling: if the provider rejected the
                # prompt for being too big, remember it so we skip this
                # provider for prompts ≥ this size next time (no hardcoded cap).
                if is_too_large_error(e):
                    await record_too_large(provider, est_tokens)
                    log.info("learned: %s rejects ~%d tok prompts",
                             provider, est_tokens)
                    break  # bigger keys won't help — go straight to next provider
                await record_usage(
                    api_key_id=key.id, project_id=project.id, lease_id=None,
                    provider=provider, model=use_model, capability=capability,
                    workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
                    latency_ms=None, status="error", error_kind=type(e).__name__,
                    http_status=None,
                )
                log.warning("provider %s key %s failed (%s): %s",
                            provider, key.label, kind, e)
                continue  # try the next key of this provider

            # Deterministic JSON quality gate: an unparseable JSON body (gemini
            # truncated, deepseek rogue) is billed but treated as a failure.
            if _wants_json(response_format) and not _is_valid_json(text):
                await record_usage(
                    api_key_id=key.id, project_id=project.id, lease_id=None,
                    provider=provider, model=use_model, capability=capability,
                    workflow=workflow, tokens_in=meta["tokens_in"],
                    tokens_out=meta["tokens_out"], cost_usd=meta["cost_usd"],
                    latency_ms=meta["latency_ms"], status="error",
                    error_kind="InvalidJSON", http_status=200,
                )
                log.warning("provider %s returned unparseable JSON, trying next", provider)
                continue  # try the next key of this provider

            await record_usage(
                api_key_id=key.id, project_id=project.id, lease_id=None,
                provider=provider, model=use_model, capability=capability,
                workflow=workflow, tokens_in=meta["tokens_in"],
                tokens_out=meta["tokens_out"], cost_usd=meta["cost_usd"],
                latency_ms=meta["latency_ms"], status="ok", error_kind=None,
                http_status=200,
            )
            return ChatOutcome(
                text=text, provider=provider, model=meta["model"],
                tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
                cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
                key_label=key.label,
            )
    return None


@dataclass
class EmbedOutcome:
    embeddings: list[list[float]]
    provider: str
    model: str
    tokens_in: int
    cost_usd: float
    latency_ms: int
    key_label: str


class EmbedFailed(Exception):
    """Provider call failed — route maps this to HTTP 502."""


async def run_embed(
    *,
    project: ProjectRow,
    provider: str,
    inputs: list[str],
    model: str | None,
    workflow: str | None,
) -> EmbedOutcome | None:
    """Embed `inputs` via `provider`. None → no key (503); EmbedFailed → 502."""
    key = await pick_and_reserve(provider, scope=scope_for("embedding"))
    if key is None:
        return None
    use_model = model or model_for(provider, "embedding") or "voyage/voyage-3"
    plain = decrypt(key.token_encrypted)
    try:
        vectors, meta = await embed(model=use_model, texts=inputs, api_key=plain)
        meta["cost_usd"] = _billed_cost(key, meta)
    except Exception as e:
        await _penalize(key, e)
        await record_usage(
            api_key_id=key.id, project_id=project.id, lease_id=None,
            provider=provider, model=use_model, capability="embedding",
            workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
            latency_ms=None, status="error", error_kind=type(e).__name__,
            http_status=None,
        )
        raise EmbedFailed(str(e)) from e
    await record_usage(
        api_key_id=key.id, project_id=project.id, lease_id=None,
        provider=provider, model=use_model, capability="embedding",
        workflow=workflow, tokens_in=meta["tokens_in"], tokens_out=0,
        cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
        status="ok", error_kind=None, http_status=200,
    )
    return EmbedOutcome(
        embeddings=vectors, provider=provider, model=use_model,
        tokens_in=meta["tokens_in"], cost_usd=meta["cost_usd"],
        latency_ms=meta["latency_ms"], key_label=key.label,
    )


@dataclass
class TranscribeOutcome:
    text: str
    provider: str
    model: str
    cost_usd: float
    latency_ms: int
    key_label: str


class TranscribeFailed(Exception):
    """All transcription providers in the chain failed — route maps to 502."""


async def run_transcribe(
    *,
    project: ProjectRow,
    audio: bytes,
    filename: str,
    workflow: str | None,
) -> TranscribeOutcome | None:
    """Audio → text, walking the 'transcription' chain (groq → openai).

    None → no key anywhere (503); TranscribeFailed → every provider errored (502).
    """
    scope = scope_for("transcription")
    last_exc: Exception | None = None
    any_key_seen = False

    for provider in chain_for("transcription"):
        key = await pick_and_reserve(provider, scope=scope)
        if key is None:
            continue
        any_key_seen = True
        use_model = model_for(provider, "transcription")
        if not use_model:
            continue
        plain = decrypt(key.token_encrypted)
        try:
            text, meta = await transcribe(
                model=use_model, audio=audio, filename=filename, api_key=plain,
            )
            meta["cost_usd"] = _billed_cost(key, meta)
        except Exception as e:
            last_exc = e
            await _penalize(key, e)
            await record_usage(
                api_key_id=key.id, project_id=project.id, lease_id=None,
                provider=provider, model=use_model, capability="transcription",
                workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
                latency_ms=None, status="error", error_kind=type(e).__name__,
                http_status=None,
            )
            continue
        await record_usage(
            api_key_id=key.id, project_id=project.id, lease_id=None,
            provider=provider, model=use_model, capability="transcription",
            workflow=workflow, tokens_in=0, tokens_out=0,
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            status="ok", error_kind=None, http_status=200,
        )
        return TranscribeOutcome(
            text=text, provider=provider, model=use_model,
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            key_label=key.label,
        )

    if not any_key_seen:
        return None
    raise TranscribeFailed(str(last_exc) if last_exc else "all providers failed")
