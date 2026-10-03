"""Cache affinity: the key pin (project, provider) -> key and the route pin
(project, workflow, capability, pin) -> (provider, model, key) for ALL
capabilities. Pure helpers run DB-free; the walk tests drive run_chat with fakes."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.routing import affinity, shared_state
from aibroker.routing.affinity import AffinityTarget, _affinity_for, _note_affinity


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "")      # shared store off: in-process fallback
    affinity.reset()
    yield
    affinity.reset()


# --- key pin (the original per-(project, provider) mechanism) ------------------


def test_key_pin_round_trip():
    _note_affinity(1, "deepseek", 42)
    assert _affinity_for(1, "deepseek") == 42
    assert _affinity_for(1, "gemini") is None       # other provider - no pin
    assert _affinity_for(2, "deepseek") is None     # other project - no pin
    assert _affinity_for(None, "deepseek") is None  # callers without a project


def test_key_pin_last_success_wins():
    _note_affinity(1, "deepseek", 42)
    _note_affinity(1, "deepseek", 43)
    assert _affinity_for(1, "deepseek") == 43


def test_key_pin_expires_after_ttl(monkeypatch):
    _note_affinity(7, "gemini", 9)
    monkeypatch.setattr(affinity, "ttl_s", lambda: -1.0)
    assert _affinity_for(7, "gemini") is None
    assert (7, "gemini") not in affinity._affinity   # expired entries are dropped


def test_ttl_is_a_setting_defaulting_to_two_hours():
    assert affinity.ttl_s() == 7200.0


async def test_shared_wrappers_fall_back_in_process():
    await affinity.note_affinity_shared(1, "deepseek", 42)
    assert await affinity._affinity_for_shared(1, "deepseek") == 42
    assert await affinity._affinity_for_shared(None, "deepseek") is None
    assert await affinity._affinity_for_shared(2, "deepseek") is None


async def test_shared_key_pin_wins_over_in_process(monkeypatch):
    _note_affinity(1, "deepseek", 42)

    async def fake_get(project_id: int, provider: str) -> int | None:
        return 99 if (project_id, provider) == (1, "deepseek") else None

    monkeypatch.setattr(shared_state, "get_affinity", fake_get)
    assert await affinity._affinity_for_shared(1, "deepseek") == 99
    _note_affinity(2, "deepseek", 7)       # shared miss falls through to the dict
    assert await affinity._affinity_for_shared(2, "deepseek") == 7


# --- route pin ------------------------------------------------------------------


def test_route_key_is_stable_and_scoped_by_every_part():
    base = affinity.route_key(1, "w", "chat:fast", None)
    assert base == affinity.route_key(1, "w", "chat:fast", None)
    assert affinity.route_key(1, None, "chat:fast", None) == affinity.route_key(1, "", "chat:fast", None)
    others = {affinity.route_key(2, "w", "chat:fast", None),
              affinity.route_key(1, "x", "chat:fast", None),
              affinity.route_key(1, "w", "chat:smart", None),
              affinity.route_key(1, "w", "chat:fast", "gpt-oss-120b")}
    assert base not in others and len(others) == 4


async def test_route_pin_round_trip_and_last_success_wins():
    t1 = AffinityTarget("groq", "groq/openai/gpt-oss-120b", 5)
    t2 = AffinityTarget("gemini", "gemini/gemini-2.5-flash", 8)
    assert await affinity.lookup_route(1, "w", "chat:fast", None) is None
    await affinity.note_success(1, "w", "chat:fast", None, "groq", t1.model, 5)
    assert await affinity.lookup_route(1, "w", "chat:fast", None) == t1
    assert await affinity.lookup_route(1, "w", "chat:smart", None) is None   # other lane
    assert await affinity.lookup_route(1, "other", "chat:fast", None) is None
    await affinity.note_success(1, "w", "chat:fast", None, "gemini", t2.model, 8)
    assert await affinity.lookup_route(1, "w", "chat:fast", None) == t2
    # note_success also refreshes the legacy key pin
    assert _affinity_for(1, "gemini") == 8


async def test_route_pin_expires(monkeypatch):
    await affinity.note_success(1, "w", "vision", None, "gemini", "gemini/x", 3)
    monkeypatch.setattr(affinity, "ttl_s", lambda: -1.0)
    assert await affinity.lookup_route(1, "w", "vision", None) is None


async def test_route_pin_is_shared_through_redis_when_available(monkeypatch):
    store: dict[str, object] = {}

    async def get_json(key):
        return store.get(key)

    async def set_json(key, value, ttl_s):
        store[key] = value
        store["ttl"] = ttl_s

    monkeypatch.setattr(shared_state, "get_json", get_json)
    monkeypatch.setattr(shared_state, "set_json", set_json)
    await affinity.note_route(1, "w", "chat:fast", None, AffinityTarget("groq", "m", 5))
    affinity._routes.clear()          # another worker: nothing in-process
    assert await affinity.lookup_route(1, "w", "chat:fast", None) == AffinityTarget("groq", "m", 5)
    assert store["ttl"] == 7200.0


async def test_corrupt_shared_payload_is_a_miss(monkeypatch):
    async def bad(key):
        return {"p": "groq"}              # missing fields

    monkeypatch.setattr(shared_state, "get_json", bad)
    assert await affinity.lookup_route(1, "w", "chat:fast", None) is None


def test_paid_target_never_jumps_a_free_head():
    paid = AffinityTarget("deepseek", "deepseek/deepseek-flash", 9)
    free = AffinityTarget("gemini", "gemini/gemini-2.5-flash", 8)
    assert not affinity.may_promote(paid, "groq", paid_only=False)
    assert affinity.may_promote(paid, "anthropic", paid_only=False)   # paid head: fine
    assert affinity.may_promote(free, "groq", paid_only=False)
    assert affinity.may_promote(paid, "groq", paid_only=True)         # final-retry walk


# --- the walk -------------------------------------------------------------------


def _key(kid: int, provider: str, tier: str = "free"):
    return SimpleNamespace(id=kid, label=f"k{kid}", tier=tier, provider=provider,
                           token_encrypted="x", account_id=None)


def _wire(monkeypatch, *, picker, calls):
    import aibroker.services.llm_service as svc

    async def fake_call(**kw):
        calls.append(kw)
        return "hi", {"model": kw["model"], "tokens_in": 1, "tokens_out": 1,
                      "cost_usd": 0.0, "latency_ms": 5,
                      "cache_read_tokens": 0, "cache_write_tokens": 0}

    monkeypatch.setattr(svc, "pick_and_reserve", picker)
    monkeypatch.setattr(svc, "call_llm", fake_call)
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda t: "plain")
    monkeypatch.setattr("aibroker.services.attempt.reserve_cost", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.release_cost", AsyncMock(return_value=None))
    monkeypatch.setattr("aibroker.services.attempt.record_usage", AsyncMock(return_value=1))
    monkeypatch.setattr("aibroker.services.response_cache.get", lambda *a, **k: None)
    monkeypatch.setattr("aibroker.services.response_cache.put", lambda *a, **k: None)
    return svc


async def _chat(svc, chain, *, workflow="w", model=None, capability="chat:fast"):
    with patch.object(svc, "chain_for", lambda cap: list(chain)):
        return await svc.run_chat(
            project=SimpleNamespace(id=8, name="SIN_HRM"), capability=capability,
            messages=[{"role": "user", "content": "hi"}], model=model,
            max_tokens=64, temperature=0.2, response_format=None, workflow=workflow)


async def test_affine_target_is_tried_first_and_wins(monkeypatch):
    picks: list[tuple[str, int | None]] = []

    async def picker(provider, scope, **kw):
        picks.append((provider, kw.get("only_key_id")))
        return _key(kw.get("only_key_id") or 1, provider)

    calls: list[dict] = []
    svc = _wire(monkeypatch, picker=picker, calls=calls)
    await affinity.note_success(8, "w", "chat:fast", None, "groq", "groq/openai/gpt-oss-120b", 5)
    out = await _chat(svc, ["cerebras", "groq"])
    assert picks == [("groq", 5)]                       # exactly the pinned key, nothing else
    assert out.provider == "groq" and calls[0]["model"] == "groq/openai/gpt-oss-120b"


async def test_affine_miss_walks_normally(monkeypatch):
    picks: list[tuple[str, int | None]] = []

    async def picker(provider, scope, **kw):
        picks.append((provider, kw.get("only_key_id")))
        return _key(1, provider)

    svc = _wire(monkeypatch, picker=picker, calls=[])
    out = await _chat(svc, ["cerebras", "groq"])
    assert picks == [("cerebras", None)] and out.provider == "cerebras"


async def test_unavailable_affine_key_fails_over_and_repins(monkeypatch):
    """The pinned key is cooling/capped (pick returns None) -> the normal walk runs,
    and the request family is re-pinned to whatever succeeded."""
    picks: list[tuple[str, int | None]] = []

    async def picker(provider, scope, **kw):
        picks.append((provider, kw.get("only_key_id")))
        if kw.get("only_key_id"):
            return None
        return _key(2, provider)

    svc = _wire(monkeypatch, picker=picker, calls=[])
    await affinity.note_success(8, "w", "chat:fast", None, "groq", "groq/openai/gpt-oss-120b", 5)
    out = await _chat(svc, ["cerebras", "groq"])
    assert picks[0] == ("groq", 5) and picks[1] == ("cerebras", None)
    assert out.provider == "cerebras"
    target = await affinity.lookup_route(8, "w", "chat:fast", None)
    assert (target.provider, target.key_id) == ("cerebras", 2)


async def test_paid_affine_provider_does_not_jump_a_free_head(monkeypatch):
    picks: list[tuple[str, int | None]] = []

    async def picker(provider, scope, **kw):
        picks.append((provider, kw.get("only_key_id")))
        return _key(1, provider)

    svc = _wire(monkeypatch, picker=picker, calls=[])
    await affinity.note_success(8, "w", "chat:code", None, "deepseek", "deepseek/deepseek-flash", 9)
    await _chat(svc, ["groq", "deepseek"], capability="chat:code")
    assert picks == [("groq", None)]


async def test_affine_provider_outside_the_chain_is_ignored(monkeypatch):
    picks: list[tuple[str, int | None]] = []

    async def picker(provider, scope, **kw):
        picks.append((provider, kw.get("only_key_id")))
        return _key(1, provider)

    svc = _wire(monkeypatch, picker=picker, calls=[])
    await affinity.note_success(8, "w", "chat:fast", None, "cloudflare",
                                "cloudflare/@cf/openai/gpt-oss-120b", 5)
    await _chat(svc, ["cerebras", "groq"])
    assert picks == [("cerebras", None)]


async def test_free_only_affine_leg_refuses_a_paid_key(monkeypatch):
    """gemini is MIXED (free + paid keys): if the pinned key turned out paid while
    the walk heads with a free provider, it is skipped, not spent on."""
    picks: list[tuple[str, int | None]] = []

    async def picker(provider, scope, **kw):
        picks.append((provider, kw.get("only_key_id")))
        return _key(kw.get("only_key_id") or 1, provider,
                    tier="paid" if kw.get("only_key_id") else "free")

    svc = _wire(monkeypatch, picker=picker, calls=[])
    await affinity.note_success(8, "w", "chat:fast", None, "gemini", "gemini/gemini-2.5-flash", 5)
    out = await _chat(svc, ["groq", "gemini"])
    assert picks[0] == ("gemini", 5) and picks[1] == ("groq", None)
    assert out.provider == "groq"


async def test_pinned_model_affinity_is_scoped_to_the_pin(monkeypatch):
    picks: list[tuple[str, int | None]] = []

    async def picker(provider, scope, **kw):
        picks.append((provider, kw.get("only_key_id")))
        return _key(kw.get("only_key_id") or 1, provider)

    svc = _wire(monkeypatch, picker=picker, calls=[])
    await affinity.note_success(8, "w", "chat:fast", "gpt-oss-120b", "groq",
                                "groq/openai/gpt-oss-120b", 5)
    await _chat(svc, ["cerebras", "groq"], model="gpt-oss-120b")
    assert picks == [("groq", 5)]
    picks.clear()
    await _chat(svc, ["cerebras", "groq"])                 # unpinned family: separate pin
    assert picks == [("cerebras", None)]


async def test_stable_cache_key_is_sent_per_request_family(monkeypatch):
    async def picker(provider, scope, **kw):
        return _key(1, provider)

    calls: list[dict] = []
    svc = _wire(monkeypatch, picker=picker, calls=calls)
    await _chat(svc, ["cerebras"])
    affinity.reset()
    await _chat(svc, ["cerebras"])
    affinity.reset()
    await _chat(svc, ["cerebras"], workflow="other")
    a, b, c = (x["cache_key"] for x in calls)
    assert a == b and a != c and a.startswith("aib-")
