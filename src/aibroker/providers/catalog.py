"""Model catalog — what a caller may pin, and how a pin resolves.

Built from the provider registry (one ModelSpec per model), never hand-listed.

A pin is either a routing id (`gemini/gemini-2.5-flash`) or a bare canonical name
(`gpt-oss-120b`, served by several providers). Resolution is DETERMINISTIC: the
candidate (provider, model) pairs are ordered by `ProviderSpec.rank`, paid
providers last — never shuffled, never rotated. The walk then tries free-tier
keys of every candidate before any paid key (see services/llm_service).
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass

from aibroker.providers.registry import (
    ModelSpec,
    all_models,
    model_spec,
    spec_or_default,
)

# Capabilities that are the same KIND of call: a model wired for one chat lane
# can legitimately be pinned on another (callers pin a specific gemini version).
_CHAT_FAMILY = frozenset({"chat:fast", "chat:smart", "chat:sales", "chat:code",
                          "chat:edit", "chat:deep", "structured", "prefilter",
                          "translate"})


def capability_family(capability: str) -> str:
    return "chat" if capability in _CHAT_FAMILY else capability


def canonical_name(model_id: str) -> str:
    """`groq/openai/gpt-oss-120b` / `cloudflare/@cf/openai/gpt-oss-120b` ->
    `gpt-oss-120b`: last path segment, lower-cased, `:free` dropped."""
    tail = model_id.split("/", 1)[-1]
    return tail.rsplit("/", 1)[-1].lower().removesuffix(":free")


@dataclass(frozen=True, slots=True)
class PinTarget:
    provider: str
    model: str          # routing id to send to that provider


class UnknownModel(ValueError):
    """The pinned model is not in the catalog (or does not serve the capability)."""

    def __init__(self, model: str, suggestions: list[str], reason: str = "") -> None:
        self.model = model
        self.suggestions = suggestions
        text = reason or f"unknown model {model!r}"
        if suggestions:
            text += f"; did you mean: {', '.join(suggestions)}"
        text += " (GET /v1/models lists every pin-able id)"
        super().__init__(text)


def _serves(spec: ModelSpec, capability: str) -> bool:
    family = capability_family(capability)
    return any(capability_family(c) == family for c in spec.capabilities)


def _order(specs: list[ModelSpec]) -> list[ModelSpec]:
    return sorted(specs, key=lambda m: (spec_or_default(m.provider).paid,
                                        spec_or_default(m.provider).rank, m.id))


def pinnable_models() -> list[ModelSpec]:
    """Every model a caller can pin: wired for at least one capability."""
    return _order([m for m in all_models() if m.capabilities])


def _suggest(model: str, pool: list[ModelSpec]) -> list[str]:
    names = {m.id: m.id for m in pool} | {canonical_name(m.id): canonical_name(m.id) for m in pool}
    return difflib.get_close_matches(model.lower(), [n.lower() for n in names], n=3, cutoff=0.5)


def resolve_pin(model: str, capability: str, chain: list[str]) -> list[PinTarget]:
    """Ordered (provider, model) targets for a pinned `model` on `capability`.

    `chain` is the capability's provider chain: a model whose provider is not in
    it cannot serve the lane. Raises UnknownModel (-> HTTP 400) with suggestions
    when nothing matches."""
    name = model.strip()
    exact = model_spec(name)
    if exact is not None:
        matches = [exact]
    else:
        key = name.lower().removesuffix(":free")
        matches = [m for m in all_models()
                   if canonical_name(m.id) == key
                   or m.id.split("/", 1)[-1].lower().removesuffix(":free") == key]
    if not matches:
        raise UnknownModel(model, _suggest(name, pinnable_models()))
    servable = [m for m in matches if _serves(m, capability)]
    if not servable:
        raise UnknownModel(
            model, [], f"model {model!r} does not serve {capability}")
    served = [m for m in _order(servable) if m.provider in chain]
    if not served:
        raise UnknownModel(
            model, [], f"model {model!r} is not served by any provider of the "
                       f"{capability} chain ({', '.join(chain)})")
    return [PinTarget(m.provider, m.id) for m in served]


def price_info(spec: ModelSpec) -> dict[str, object]:
    """Price summary for the listing. litellm-priced models are read lazily from
    litellm's map; a missing entry reports `kind: "unknown"` rather than guessing."""
    info: dict[str, object] = {"kind": spec.pricing}
    if spec.pricing == "litellm":
        try:
            import litellm
            mi = litellm.get_model_info(spec.id)
            inp, out = mi.get("input_cost_per_token"), mi.get("output_cost_per_token")
            info["input_usd_per_mtok"] = round(inp * 1e6, 6) if inp is not None else None
            info["output_usd_per_mtok"] = round(out * 1e6, 6) if out is not None else None
        except Exception:  # noqa: BLE001 — not in litellm's map
            info["kind"] = "unknown"
    elif spec.pricing == "override":
        info["input_usd_per_mtok"] = spec.input_usd_per_mtok
        info["output_usd_per_mtok"] = spec.output_usd_per_mtok
    elif spec.pricing == "per_minute":
        info["usd_per_minute"] = spec.usd_per_minute
    return info


def listing() -> list[dict[str, object]]:
    """The GET /v1/models payload rows (OpenAI-style `data` entries)."""
    rows = []
    for m in pinnable_models():
        also = [o.id for o in pinnable_models()
                if o.id != m.id and canonical_name(o.id) == canonical_name(m.id)]
        rows.append({
            "id": m.id,
            "object": "model",
            "owned_by": m.provider,
            "name": canonical_name(m.id),
            "capabilities": sorted(m.capabilities),
            "also_served_by": also,
            "price": price_info(m),
        })
    return rows

