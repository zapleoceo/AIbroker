"""Exact model pinning: catalog resolution + the deterministic pinned walk.

History: until 2026-09-26 a pin did not pin the key, so `openrouter/...` reached
OpenRouter carrying a groq/gemini key - the 401 was classified `auth` and
`mark_dead` killed healthy keys. The first fix kept the walk inside the owning
provider; the catalog (providers/catalog.py) makes it exact: a pinned model
resolves to the providers that really serve it, free-tier keys first, then a
fixed provider rank - never shuffled, never rotated. Unknown model -> error with
suggestions (HTTP 400 at the route).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aibroker.providers.catalog import (
    UnknownModel,
    canonical_name,
    listing,
    pinnable_models,
    resolve_pin,
)

_CHAT_CHAIN = ["cerebras", "groq", "gemini", "sambanova", "zai", "cloudflare"]


# --- catalog resolution -------------------------------------------------------


def test_routing_id_resolves_to_its_own_provider():
    assert [(t.provider, t.model) for t in
            resolve_pin("gemini/gemini-2.5-flash", "chat:fast", _CHAT_CHAIN)] == [
        ("gemini", "gemini/gemini-2.5-flash")]


def test_bare_name_resolves_to_every_provider_serving_it_in_rank_order():
    """gpt-oss-120b is served by cerebras, groq and cloudflare - one fixed order
    (ProviderSpec.rank), the same on every call."""
    first = resolve_pin("gpt-oss-120b", "chat:fast", _CHAT_CHAIN)
    assert [t.provider for t in first] == ["cerebras", "groq", "cloudflare"]
    assert first == resolve_pin("gpt-oss-120b", "chat:fast", list(reversed(_CHAT_CHAIN)))


def test_paid_providers_sort_after_free_ones():
    targets = resolve_pin("claude-sonnet-5", "chat:smart", ["anthropic", "gemini"])
    assert [t.provider for t in targets] == ["anthropic"]
    both = resolve_pin("gemma-4-31b-it", "vision", ["openrouter", "sambanova"])
    assert [t.provider for t in both] == ["sambanova", "openrouter"]


def test_pin_may_move_between_chat_lanes_but_not_kinds():
    """A model wired for prefilter can be pinned on chat:fast (callers pin a
    specific gemini version) - but an embedding model cannot serve chat."""
    assert resolve_pin("gemini/gemini-2.5-flash-lite", "chat:fast", _CHAT_CHAIN)
    with pytest.raises(UnknownModel, match="does not serve"):
        resolve_pin("voyage/voyage-4", "chat:fast", _CHAT_CHAIN)


def test_model_whose_provider_is_outside_the_chain_is_refused():
    with pytest.raises(UnknownModel, match="not served by any provider"):
        resolve_pin("anthropic/claude-sonnet-5", "chat:fast", ["groq", "gemini"])


def test_unknown_model_carries_suggestions():
    with pytest.raises(UnknownModel) as e:
        resolve_pin("gemini/gemini-2.5-flsh", "chat:fast", _CHAT_CHAIN)
    assert "gemini/gemini-2.5-flash" in e.value.suggestions
    assert "GET /v1/models" in str(e.value)


def test_canonical_name_strips_provider_and_free_suffix():
    assert canonical_name("groq/openai/gpt-oss-120b") == "gpt-oss-120b"
    assert canonical_name("cloudflare/@cf/openai/gpt-oss-120b") == "gpt-oss-120b"
    assert canonical_name("openrouter/google/gemma-4-31b-it:free") == "gemma-4-31b-it"


def test_listing_covers_every_pinnable_model_once():
    rows = listing()
    assert [r["id"] for r in rows] == [m.id for m in pinnable_models()]
    assert len({r["id"] for r in rows}) == len(rows)
    gpt = next(r for r in rows if r["id"] == "cerebras/gpt-oss-120b")
    assert gpt["owned_by"] == "cerebras"
    assert "groq/openai/gpt-oss-120b" in gpt["also_served_by"]
    assert gpt["capabilities"] and gpt["price"]["kind"]


# --- the pinned walk ----------------------------------------------------------


async def _walk(monkeypatch, *, model: str | None, capability: str = "chat:fast",
                chain: list[str] | None = None, paid_only: bool = False):
    import aibroker.services.llm_service as svc

    picked: list[tuple[str, str | None]] = []

    async def fake_pick(provider, scope, **kw):
        picked.append((provider, kw.get("require_tier")))   # no key -> the walk moves on

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: list(chain or _CHAT_CHAIN))
    out = await svc.run_chat(
        project=SimpleNamespace(id=8, name="SIN_HRM"), capability=capability,
        messages=[{"role": "user", "content": "hi"}], model=model,
        max_tokens=64, temperature=0.2, response_format=None, workflow="w",
        paid_only=paid_only,
    )
    assert out is None
    return picked


async def test_pinned_model_never_touches_another_provider(monkeypatch):
    picked = await _walk(monkeypatch, model="openrouter/google/gemma-4-31b-it:free",
                         chain=["groq", "gemini", "openrouter", "zai"])
    assert {p for p, _ in picked} == {"openrouter"}


async def test_pinned_walk_is_free_keys_first_then_paid_in_rank_order(monkeypatch):
    picked = await _walk(monkeypatch, model="gpt-oss-120b",
                         chain=["cloudflare", "groq", "cerebras"])
    assert picked == [("cerebras", "free"), ("groq", "free"), ("cloudflare", "free"),
                      ("cerebras", "paid"), ("groq", "paid"), ("cloudflare", "paid")]


async def test_pinned_walk_is_identical_on_every_call(monkeypatch):
    runs = [await _walk(monkeypatch, model="gpt-oss-120b") for _ in range(5)]
    assert all(r == runs[0] for r in runs)


async def test_final_retry_pinned_walk_is_paid_only(monkeypatch):
    picked = await _walk(monkeypatch, model="gemini/gemini-2.5-flash", paid_only=True)
    assert picked == [("gemini", "paid")]


async def test_unpinned_request_still_walks_the_whole_chain(monkeypatch):
    picked = await _walk(monkeypatch, model=None)
    assert [p for p, _ in picked] == _CHAT_CHAIN
    assert all(tier is None for _, tier in picked)


async def test_unknown_pin_raises_instead_of_walking(monkeypatch):
    with pytest.raises(UnknownModel):
        await _walk(monkeypatch, model="not-a-model")


@pytest.mark.parametrize("model", ["gemini/gemini-2.5-flash", None])
async def test_pin_and_the_tools_filter_compose(monkeypatch, model):
    """Both filters apply and neither resurrects what the other excluded: the
    tools filter narrows to TOOL_PROVIDERS, the pin to the owning provider."""
    import aibroker.services.llm_service as svc
    from aibroker.services.tool_contract import TOOL_PROVIDERS

    picked: list[str] = []

    async def fake_pick(provider, scope, **kw):
        picked.append(provider)

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["groq", "gemini", "openrouter"])
    await svc.run_chat(
        project=SimpleNamespace(id=8, name="SIN_HRM"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=model,
        max_tokens=64, temperature=0.2, response_format=None, workflow="w",
        tools=[{"type": "function", "function": {"name": "f", "description": "d",
                                                 "parameters": {"type": "object"}}}],
    )
    assert set(picked) <= TOOL_PROVIDERS
    assert "groq" not in picked and "openrouter" not in picked
    if model:
        assert set(picked) == {"gemini"}


async def test_openrouter_pin_with_tools_is_refused(monkeypatch):
    """A NATIVE TOOL call may only name a TOOL_PROVIDERS model; tool_model_provider
    raises for anything else rather than cross-route."""
    import aibroker.services.llm_service as svc

    async def fake_pick(provider, scope, **kw):
        raise AssertionError("must not reach key selection")

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["gemini", "openrouter"])
    with pytest.raises(ValueError, match="qualified by an enabled provider"):
        await svc.run_chat(
            project=SimpleNamespace(id=8, name="SIN_HRM"), capability="chat:fast",
            messages=[{"role": "user", "content": "hi"}],
            model="openrouter/google/gemma-4-31b-it:free",
            max_tokens=64, temperature=0.2, response_format=None, workflow="w",
            tools=[{"type": "function", "function": {"name": "f", "description": "d",
                                                     "parameters": {"type": "object"}}}],
        )
