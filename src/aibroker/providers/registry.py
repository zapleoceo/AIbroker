"""Provider registry — ONE `ProviderSpec` per provider.

Everything the broker knows about a provider as a *fact* lives here (adapter,
health probe, quotas, cooldown base, JSON reliability, cache behaviour, error
signs, size limits, models and their prices, rotation). Policy — which provider
serves which capability in which order — stays in `routing/chains.py`.

Adding a provider or a model is one entry in `providers/specs.py`; see
docs/how-to-add-provider.md. Consumers use the accessor functions below (views
over the registry) instead of per-concern tables.

This module is a LEAF: it must not import litellm, the DB or `routing`, so it
can be imported from anywhere (including tests) for free.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from aibroker.providers.adapters import ProviderAdapter

JsonReliability = Literal["reliable", "unreliable", "incapable"]
Pricing = Literal["litellm", "override", "per_minute", "free", "local"]
AuthStyle = Literal["bearer", "anthropic", "gemini", "gemini_key"]

DEFAULT_COOLDOWN_S = 300
DEFAULT_MAX_KEYS = 5


@dataclass(frozen=True, slots=True)
class Quota:
    """Effective per-day caps. None on an axis => that axis is uncapped."""
    req_per_day: int | None = None
    tok_per_day: int | None = None       # total in+out
    tok_in_per_day: int | None = None
    tok_out_per_day: int | None = None
    doc: str = ""


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """One routable model of a provider.

    `pricing` says where its price comes from — every model must be priced or
    explicitly marked: "litellm" (litellm's pricing map knows it), "override"
    (our own per-token list price, registered into litellm when `register`),
    "per_minute" (audio), "free" (free by construction / no usable meter) or
    "local" (self-hosted, no bill).
    """
    id: str                                    # routing id, provider prefix included
    provider: str                              # registry provider that serves it
    capabilities: frozenset[str]
    transport: str = "litellm"                 # key into providers.transport.TRANSPORTS
    pricing: Pricing = "litellm"
    input_usd_per_mtok: float | None = None
    output_usd_per_mtok: float | None = None
    cache_read_usd_per_mtok: float | None = None
    usd_per_minute: float | None = None
    litellm_extra: Mapping[str, Any] = field(default_factory=dict)
    register: bool = True                      # push an "override" into litellm's map
    note: str = ""                             # one-line rationale, details in docs/history


@dataclass(frozen=True, slots=True)
class ProbeSpec:
    """The cheapest call that proves a key alive (monitor + key-create flow)."""
    url: str                                   # may contain {account_id}
    body: Mapping[str, Any] | None = None      # None -> a body-less GET (free list endpoint)
    auth: AuthStyle = "bearer"
    needs_account_id: bool = False

    def build(self, key: str, account_id: str | None = None):
        """(method, url, headers, body), or None for an unprobeable key."""
        if self.needs_account_id and not account_id:
            return None
        if self.auth == "anthropic":
            headers = {"x-api-key": key, "anthropic-version": "2023-06-01",
                       "content-type": "application/json"}
        elif self.auth == "gemini":
            headers = {"content-type": "application/json", "x-goog-api-key": key}
        elif self.auth == "gemini_key":
            headers = {"x-goog-api-key": key}      # key in a header, never the URL
        else:
            headers = {"Authorization": f"Bearer {key}", "content-type": "application/json"}
        method = "POST" if self.body is not None else "GET"
        body = dict(self.body) if self.body is not None else None
        return method, self.url.format(account_id=account_id or ""), headers, body


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    name: str
    adapter: ProviderAdapter = field(default_factory=ProviderAdapter)
    probe: ProbeSpec | None = None
    quota: Quota = field(default_factory=Quota)
    cooldown_base_s: int = DEFAULT_COOLDOWN_S
    json_reliability: JsonReliability = "reliable"
    cache_sticky: bool = False                 # per-key prompt cache worth pinning to one key
    explicit_cache: bool = False               # needs explicit cache_control marks (anthropic)
    cache_key_param: str | None = None         # documented stable-cache-key request param
    rate_limit_signs: tuple[str, ...] = ()     # provider-scoped error substrings -> cooldown
    auth_signs: tuple[str, ...] = ()           # ... -> mark dead
    monthly_signs: tuple[str, ...] = ()        # ... -> cooldown to next month
    quota_headers: Literal["openai", "anthropic", ""] = ""
    trust_req_header: bool = True              # cerebras: the req/day header is not a hard cap
    max_request_tokens: int | None = None      # bootstrap size ceiling (learned value overrides)
    max_keys: int = DEFAULT_MAX_KEYS           # keys tried before falling to the next provider
    paid: bool = False                         # billed per token (paid-tier tail)
    rank: int = 1000                           # deterministic order among providers of one model
    empty_is_failure: bool = False             # an empty body is never a real answer (local)
    refine_transcript: bool = False            # proofread this provider's ASR output
    defaults: Mapping[str, str] = field(default_factory=dict)       # capability -> model id
    rotation: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    models: Mapping[str, ModelSpec] = field(default_factory=dict)   # id -> spec

    def model_for(self, capability: str) -> str | None:
        return self.defaults.get(capability)

    def models_for(self, capability: str) -> list[str]:
        """Every model this provider may serve `capability` with, primary first."""
        primary = self.defaults.get(capability)
        if primary is None:
            return []
        return [primary, *(m for m in self.rotation.get(capability, ()) if m != primary)]


def build_provider(
    name: str, *,
    defaults: Mapping[str, str] | None = None,
    rotation: Mapping[str, tuple[str, ...]] | None = None,
    model_meta: Mapping[str, Mapping[str, Any]] | None = None,
    **fields: Any,
) -> ProviderSpec:
    """Assemble a ProviderSpec, deriving `models` from defaults + rotation.

    Each distinct model id gets a ModelSpec whose capabilities are every lane it
    appears in; `model_meta[id]` overrides transport/pricing/etc. A model that
    appears only in `model_meta` (e.g. a decision fallback) is still registered.
    """
    defaults = dict(defaults or {})
    rotation = {c: tuple(m) for c, m in (rotation or {}).items()}
    caps: dict[str, set[str]] = {}
    for cap, model in defaults.items():
        caps.setdefault(model, set()).add(cap)
    for cap, models in rotation.items():
        for model in models:
            caps.setdefault(model, set()).add(cap)
    for model in (model_meta or {}):
        caps.setdefault(model, set())
    models: dict[str, ModelSpec] = {}
    for mid, c in caps.items():
        meta = dict((model_meta or {}).get(mid, {}))
        meta_caps = set(meta.pop("capabilities", ()))
        models[mid] = ModelSpec(id=mid, provider=name, capabilities=frozenset(c | meta_caps), **meta)
    return ProviderSpec(name=name, defaults=defaults, rotation=rotation, models=models, **fields)


REGISTRY: dict[str, ProviderSpec] = {}


def register(spec: ProviderSpec) -> ProviderSpec:
    if spec.name in REGISTRY:
        raise ValueError(f"provider {spec.name!r} registered twice")
    REGISTRY[spec.name] = spec
    return spec


_DEFAULT_SPEC = ProviderSpec(name="")


def get_spec(provider: str) -> ProviderSpec | None:
    return REGISTRY.get(provider)


def spec_or_default(provider: str | None) -> ProviderSpec:
    """The spec for `provider`, or an all-defaults one (unknown providers keep
    the historical neutral behaviour: reliable JSON, 300s cooldown, no quirks)."""
    return REGISTRY.get(provider or "", _DEFAULT_SPEC)


def provider_names() -> list[str]:
    return list(REGISTRY)


def provider_of_model_id(model: str) -> str:
    """`gemini/gemini-2.5-flash` -> `gemini` (the routing prefix)."""
    return model.split("/", 1)[0]


# ─── Views — one per concern that used to be its own table ──────────────────


def paid_providers() -> frozenset[str]:
    return frozenset(n for n, s in REGISTRY.items() if s.paid)


def cache_sticky_providers() -> frozenset[str]:
    return frozenset(n for n, s in REGISTRY.items() if s.cache_sticky)


def providers_with_json(level: JsonReliability) -> frozenset[str]:
    return frozenset(n for n, s in REGISTRY.items() if s.json_reliability == level)


def max_keys_for(provider: str) -> int:
    return spec_or_default(provider).max_keys


def cooldown_base_for(provider: str) -> int:
    return spec_or_default(provider).cooldown_base_s


def quotas() -> dict[str, Quota]:
    return {n: s.quota for n, s in REGISTRY.items()}


def model_for(provider: str, capability: str) -> str | None:
    return spec_or_default(provider).model_for(capability)


def models_for(provider: str, capability: str) -> list[str]:
    return spec_or_default(provider).models_for(capability)


def rotation_for(provider: str, capability: str) -> tuple[str, ...]:
    """The EXTRA models only (no primary) — see ProviderSpec.rotation."""
    return spec_or_default(provider).rotation.get(capability, ())


def model_spec(model_id: str) -> ModelSpec | None:
    """The ModelSpec for a routing id. Looked up across ALL providers, not by
    prefix: a routing prefix need not equal the provider name
    (`nvidia_nim/...` is served by `nvidia`)."""
    spec = REGISTRY.get(provider_of_model_id(model_id))
    if spec and model_id in spec.models:
        return spec.models[model_id]
    for s in REGISTRY.values():
        if model_id in s.models:
            return s.models[model_id]
    return None


def all_models() -> list[ModelSpec]:
    return [m for s in REGISTRY.values() for m in s.models.values()]


def default_models() -> dict[str, dict[str, str]]:
    """provider -> capability -> default model (the old DEFAULT_MODEL shape)."""
    return {n: dict(s.defaults) for n, s in REGISTRY.items()}


# Populate the registry (kept last: specs.py imports the types above).
from aibroker.providers import specs as _specs  # noqa: E402,F401
