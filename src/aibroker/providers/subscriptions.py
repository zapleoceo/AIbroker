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


def _configured_ids(field: str) -> dict[str, list[str]]:
    configured = getattr(get_settings(), field)
    raw = Settings.model_fields[field].default if configured == "default" else configured
    data = json.loads(raw)
    if not isinstance(data, dict) or set(data) - PROVIDERS.keys():
        raise ValueError(f"invalid {field} providers")
    for provider, ids in data.items():
        if not isinstance(ids, list) or not all(
            isinstance(mid, str) and _MODEL_ID.fullmatch(mid) for mid in ids
        ) or len(ids) != len(set(ids)):
            raise ValueError(f"invalid {field} {provider} model ids")
    return data


def _catalog() -> tuple[list[ModelSpec], frozenset[str]]:
    """Candidate catalog and verified route ids; malformed configuration fails closed."""
    try:
        data = _configured_ids("AGENT_SUBSCRIPTION_MODELS")
        verified = _configured_ids("AGENT_SUBSCRIPTION_VERIFIED_MODELS")
        vision = _configured_ids("AGENT_SUBSCRIPTION_VISION_VERIFIED_MODELS")
        if any(set(ids) - set(data.get(provider, [])) for provider, ids in verified.items()):
            raise ValueError("verified models must be catalog candidates")
        if set(vision) - {"codex-sub"} or any(
            set(ids) - set(verified.get(provider, [])) for provider, ids in vision.items()
        ):
            raise ValueError("input vision models must be verified Codex models")
        models = []
        for provider, ids in data.items():
            for mid in ids:
                capabilities = CHAT_CAPABILITIES | ({"vision"} if mid in vision.get(provider, [])
                                                    else set())
                models.append(ModelSpec(id=f"{provider}/{mid}", provider=provider,
                                        capabilities=frozenset(capabilities), transport="agent",
                                        pricing="subscription", register=False))
        return models, frozenset(f"{provider}/{mid}" for provider, ids in verified.items()
                                 for mid in ids)
    except ValueError:
        log.error("Subscription model catalog or verification invalid: refusing subscription catalog")
        return [], frozenset()


def subscription_models() -> list[ModelSpec]:
    return _catalog()[0]


def subscription_model_verified(model: str) -> bool:
    return model in _catalog()[1]


def _tool_model_ids(field: str) -> list[str]:
    try:
        value = json.loads(getattr(get_settings(), field))
    except ValueError:
        return []
    if (not isinstance(value, list) or not all(isinstance(mid, str) for mid in value)
            or len(value) != len(set(value))
            or not all(mid.startswith("codex-sub/") and subscription_model_verified(mid)
                       for mid in value)):
        return []
    return value


def subscription_tool_model_verified(model: str) -> bool:
    """Advertise client tools only after a live callback for this exact Codex id."""
    return model in _tool_model_ids("AGENT_SUBSCRIPTION_CODEX_TOOLS_VERIFIED_MODELS")


def subscription_tool_model_canary(model: str, project_id: int) -> bool:
    """Allow the temporary exact-model canary only for owner project 12."""
    return (project_id == 12
            and get_settings().AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS == "12"
            and model in _tool_model_ids("AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_MODELS"))


def subscription_model(model: str | None) -> ModelSpec | None:
    if model is None:
        return None
    return next((m for m in subscription_models() if m.id == model), None)
