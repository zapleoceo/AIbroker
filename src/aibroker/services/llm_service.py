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
from aibroker.providers.litellm_adapter import embed, model_for
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


def classify_provider_error(exc: Exception) -> str:
    """Map a provider exception to one of: 'rate_limit', 'auth', 'error'.

    Single source of truth — both chat and embed paths classify the same way.
    """
    emsg = str(exc).lower()
    if "rate_limit" in emsg or "429" in emsg:
        return "rate_limit"
    if "401" in emsg or "403" in emsg or "auth" in emsg:
        return "auth"
    return "error"


async def _penalize(key: ApiKeyRow, exc: Exception) -> str:
    """Cooldown on rate-limit, mark dead on auth error. Returns the error kind."""
    kind = classify_provider_error(exc)
    if kind == "rate_limit":
        await mark_cooldown(key.id, datetime.now(UTC) + _COOLDOWN)
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
    """Walk the capability chain; return the first provider that succeeds, else None."""
    scope = scope_for(capability)
    for provider in chain_for(capability):
        key = await pick_and_reserve(provider, scope=scope)
        if key is None:
            continue
        try:
            await check_caps(api_key=key, project=project, estimated_cost=0.0)
        except CostGuardError as e:
            await audit(actor=f"project:{project.name}", action="cap_block",
                        target=f"provider={provider}", metadata={"reason": str(e)})
            continue
        use_model = model or model_for(provider, capability)
        if not use_model:
            continue
        plain = decrypt(key.token_encrypted)
        try:
            text, meta = await call_llm(
                model=use_model, messages=messages, api_key=plain,
                max_tokens=max_tokens, temperature=temperature,
                response_format=response_format,
            )
        except Exception as e:
            kind = await _penalize(key, e)
            await record_usage(
                api_key_id=key.id, project_id=project.id, lease_id=None,
                provider=provider, model=use_model, capability=capability,
                workflow=workflow, tokens_in=0, tokens_out=0, cost_usd=0.0,
                latency_ms=None, status="error", error_kind=type(e).__name__,
                http_status=None,
            )
            log.warning("provider %s failed (%s): %s", provider, kind, e)
            continue

        # Deterministic quality gate for JSON requests: if the provider returned
        # unparseable JSON (e.g. gemini truncated, or deepseek went rogue), treat
        # it as a failure and walk to the next provider. We still bill the tokens.
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
            continue

        await record_usage(
            api_key_id=key.id, project_id=project.id, lease_id=None,
            provider=provider, model=use_model, capability=capability,
            workflow=workflow, tokens_in=meta["tokens_in"], tokens_out=meta["tokens_out"],
            cost_usd=meta["cost_usd"], latency_ms=meta["latency_ms"],
            status="ok", error_kind=None, http_status=200,
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
