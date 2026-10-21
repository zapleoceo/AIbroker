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


def subscription_image_output_verified(model: str) -> bool:
    """Image generation (exported PNG) verified live for this exact, verified Codex model. A
    listing-only flag: an invalid setting turns it off here and never refuses the catalog."""
    if model not in _catalog()[1]:
        return False
    try:
        ids = _configured_ids("AGENT_SUBSCRIPTION_IMAGE_OUTPUT_VERIFIED_MODELS")
    except ValueError:
        return False
    provider, _, mid = model.partition("/")
    return provider == "codex-sub" and mid in ids.get("codex-sub", [])


def subscription_models() -> list[ModelSpec]:
    return _catalog()[0]


def subscription_model_verified(model: str) -> bool:
    return model in _catalog()[1]


_TOOL_SETTINGS = {"codex-sub": "CODEX", "claude-sub": "CLAUDE"}



def _tool_model_ids(field: str, provider: str) -> list[str]:
    try:
        value = json.loads(getattr(get_settings(), field))
    except ValueError:
        return []
    if (not isinstance(value, list) or not all(isinstance(mid, str) for mid in value)
            or len(value) != len(set(value))
            or not all(mid.startswith(f"{provider}/") and subscription_model_verified(mid)
                       for mid in value)):
        return []
    return value


def subscription_tools_enabled(provider: str) -> bool:
    """The per-provider client-tools switch (Codex App Server / Claude stream-json)."""
    key = _TOOL_SETTINGS.get(provider)
    return bool(key and getattr(get_settings(), f"AGENT_SUBSCRIPTION_{key}_TOOLS_ENABLED"))


def subscription_tool_model_verified(model: str) -> bool:
    """Advertise client tools only after a live callback for this exact subscription id."""
    provider = model.partition("/")[0]
    key = _TOOL_SETTINGS.get(provider)
    return bool(key) and model in _tool_model_ids(
        f"AGENT_SUBSCRIPTION_{key}_TOOLS_VERIFIED_MODELS", provider)


def canary_project_ids() -> frozenset[int]:
    """Projects allowed the exact-model tool canaries; duplicates or junk mean none."""
    raw = [p.strip() for p in get_settings().AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS.split(",")
           if p.strip()]
    if not all(p.isdigit() for p in raw) or len(raw) != len(set(raw)):
        return frozenset()
    return frozenset(int(p) for p in raw)


def subscription_tool_model_canary(model: str, project_id: int) -> bool:
    """The temporary exact-model canary, for the canary projects only."""
    provider = model.partition("/")[0]
    key = _TOOL_SETTINGS.get(provider)
    return (bool(key) and project_id in canary_project_ids()
            and model in _tool_model_ids(f"AGENT_SUBSCRIPTION_{key}_TOOLS_CANARY_MODELS",
                                         provider))


def subscription_tool_model_attested(model: str) -> bool:
    """A live client-tool round-trip on this exact id is on record (setting, any provider)."""
    field = "AGENT_SUBSCRIPTION_TOOLS_ATTESTED_MODELS"
    configured = getattr(get_settings(), field)
    try:
        value = json.loads(Settings.model_fields[field].default if configured == "default"
                           else configured)
    except ValueError:
        return False
    return isinstance(value, list) and model in value and model.partition("/")[0] in _TOOL_SETTINGS


def subscription_tools_accepted(model: str, project_id: int | None) -> bool:
    """THE gate for tools on a subscription route (submit, worker, catalog job_request.tools):
    the provider switch is on and the id is verified, or a canary for this project."""
    return (subscription_tools_enabled(model.partition("/")[0])
            and (subscription_tool_model_verified(model)
                 or (project_id is not None and subscription_tool_model_canary(model, project_id))))


def subscription_tools_advertised(model: str, project_id: int | None) -> bool:
    """catalog supports_tools: accepted AND proven - verified, or an attested canary."""
    return subscription_tools_accepted(model, project_id) and (
        subscription_tool_model_verified(model) or subscription_tool_model_attested(model))


def subscription_model(model: str | None) -> ModelSpec | None:
    if model is None:
        return None
    return next((m for m in subscription_models() if m.id == model), None)
