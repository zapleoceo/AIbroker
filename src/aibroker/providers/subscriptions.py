"""Explicit subscription model pins; never members of fallback chains."""
from __future__ import annotations

import json
import logging
import re

from aibroker.config import Settings, get_settings
from aibroker.providers.registry import ModelSpec

log = logging.getLogger(__name__)
PROVIDERS = {"claude-sub": "agent:claude", "codex-sub": "agent:codex"}
CHAT_CAPABILITIES = frozenset({"chat:fast", "chat:smart", "chat:sales", "chat:code",
                               "chat:edit", "chat:deep", "structured", "prefilter", "translate"})
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def subscription_models() -> list[ModelSpec]:
    """Malformed configuration fails closed instead of exposing an unverified model."""
    try:
        configured = get_settings().AGENT_SUBSCRIPTION_MODELS
        raw = (Settings.model_fields["AGENT_SUBSCRIPTION_MODELS"].default
               if configured == "default" else configured)
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) - PROVIDERS.keys():
            raise ValueError("expected claude-sub/codex-sub lists")
        models = []
        for provider, ids in data.items():
            if not isinstance(ids, list) or not all(
                isinstance(mid, str) and _MODEL_ID.fullmatch(mid) for mid in ids
            ):
                raise ValueError(f"invalid {provider} model ids")
            models.extend(ModelSpec(id=f"{provider}/{mid}", provider=provider,
                                    capabilities=CHAT_CAPABILITIES, transport="agent",
                                    pricing="subscription", register=False) for mid in ids)
        return models
    except ValueError:
        log.error("AGENT_SUBSCRIPTION_MODELS invalid: refusing subscription catalog")
        return []


def subscription_model(model: str | None) -> ModelSpec | None:
    if model is None:
        return None
    return next((m for m in subscription_models() if m.id == model), None)
