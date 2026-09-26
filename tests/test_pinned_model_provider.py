"""A caller-pinned `provider/model` must stay inside its own provider.

REGRESSION (2026-09-26, found while answering "how do I call an OpenRouter
router model through the broker?"): pinning a model did not pin the key.
run_chat walked the capability's chain as usual and handed the pinned string to
whichever provider it picked; LiteLLM routes by the model's OWN prefix, so
`openrouter/…` reached OpenRouter carrying a groq/gemini/cohere key. That 401
is classified `auth`, and `_penalize` answers `auth` with `mark_dead` — so one
request could have killed every healthy key sitting ahead of openrouter in the
chain (~20 on chat:fast) before the walk reached the right one.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from aibroker.routing.chains import provider_of_model


def test_qualified_model_resolves_to_its_provider():
    assert provider_of_model("openrouter/typesafe/jev-router") == "openrouter"
    assert provider_of_model("gemini/gemini-2.5-flash") == "gemini"
    assert provider_of_model("local/qwen3vl") == "local"


def test_unqualified_or_unknown_prefix_is_not_a_pin():
    """A bare name keeps its old meaning — "this model on whatever provider the
    chain picks" — and an unknown prefix must not silently empty the chain."""
    for model in (None, "", "gemini-2.5-flash", "not-a-provider/whatever",
                  "openrouter/", "openrouter"):
        assert provider_of_model(model) is None, model


async def _walk(monkeypatch, *, model: str | None, capability: str = "chat:fast",
                chain: list[str] | None = None) -> list[str]:
    import aibroker.services.llm_service as svc

    picked: list[str] = []

    async def fake_pick(provider, scope, **kw):
        picked.append(provider)          # no key → the walk moves on

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for",
                        lambda cap: list(chain or ["groq", "gemini", "openrouter", "zai"]))
    await svc.run_chat(
        project=SimpleNamespace(id=8, name="SIN_HRM"), capability=capability,
        messages=[{"role": "user", "content": "hi"}], model=model,
        max_tokens=64, temperature=0.2, response_format=None, workflow="w",
    )
    return picked


async def test_pinned_openrouter_model_never_touches_another_provider(monkeypatch):
    assert await _walk(monkeypatch, model="openrouter/typesafe/jev-router") == ["openrouter"]


async def test_unpinned_request_still_walks_the_whole_chain(monkeypatch):
    assert await _walk(monkeypatch, model=None) == ["groq", "gemini", "openrouter", "zai"]


async def test_bare_model_name_still_walks_the_whole_chain(monkeypatch):
    """Pinning a bare model (no provider prefix) is unchanged behaviour: it
    overrides the model on every provider the chain tries."""
    assert await _walk(monkeypatch, model="gemini-2.5-flash") == [
        "groq", "gemini", "openrouter", "zai"]


async def test_pinned_provider_outside_the_chain_ends_in_503(monkeypatch):
    """Honest 503 rather than sending the request to a provider that cannot
    serve the pinned model."""
    import aibroker.services.llm_service as svc

    picked = await _walk(monkeypatch, model="anthropic/claude-sonnet-5",
                         chain=["groq", "gemini"])
    assert picked == []
    out = await svc.run_chat(
        project=SimpleNamespace(id=8, name="SIN_HRM"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}],
        model="anthropic/claude-sonnet-5", max_tokens=64, temperature=0.2,
        response_format=None, workflow="w",
    )
    assert out is None


@pytest.mark.parametrize("model", ["gemini/gemini-2.5-flash", None])
async def test_pin_and_the_tools_filter_compose(monkeypatch, model):
    """Both filters apply and neither resurrects what the other excluded: the
    tools filter narrows to TOOL_PROVIDERS, the pin to the owning provider.
    (A native tool call keeps the stricter rule of tool_model_provider, which
    accepts only a TOOL_PROVIDERS-qualified model — openrouter is not one, so
    an openrouter pin plus tools is rejected before any walk; see
    test_openrouter_pin_with_tools_is_refused.)"""
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
        assert picked == ["gemini"]


async def test_openrouter_pin_with_tools_is_refused(monkeypatch):
    """Unchanged pre-existing rule, pinned down here because the new pin filter
    sits next to it: a NATIVE TOOL call may only name a TOOL_PROVIDERS model,
    and tool_model_provider raises for anything else rather than cross-route."""
    import aibroker.services.llm_service as svc

    async def fake_pick(provider, scope, **kw):
        raise AssertionError("must not reach key selection")

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["gemini", "openrouter"])
    with pytest.raises(ValueError, match="qualified by an enabled provider"):
        await svc.run_chat(
            project=SimpleNamespace(id=8, name="SIN_HRM"), capability="chat:fast",
            messages=[{"role": "user", "content": "hi"}],
            model="openrouter/typesafe/jev-router",
            max_tokens=64, temperature=0.2, response_format=None, workflow="w",
            tools=[{"type": "function", "function": {"name": "f", "description": "d",
                                                     "parameters": {"type": "object"}}}],
        )
