"""providers/litellm_adapter — call_llm + embed wrappers (mocked LiteLLM)."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aibroker.config import get_settings
from aibroker.providers.litellm_adapter import (
    DEFAULT_MODEL,
    call_llm,
    embed,
    estimate_llm_cost,
    model_for,
    transcribe,
)

# ─── model_for ────────────────────────────────────────────────────────────


def test_drop_params_enabled():
    """Broker forwards the same kwargs to every provider; LiteLLM must drop the
    ones a provider rejects (cohere UnsupportedParamsError regression). The
    module-level import at the top of this file already applied the flag."""
    import litellm
    assert litellm.drop_params is True


def _marked(msg: dict) -> bool:
    """Has a cache breakpoint. Checks the TYPE only — the ttl is asserted
    separately (test_apply_prompt_cache_uses_the_configured_ttl) so every
    placement test doesn't have to change when the TTL policy does."""
    c = msg["content"]
    return isinstance(c, list) and c[0].get("cache_control", {}).get("type") == "ephemeral"


def test_apply_prompt_cache_marks_system_prefix_end_and_history_end():
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    msgs = [{"role": "system", "content": "big stable prompt"},
            {"role": "user", "content": "hi"}]
    out = apply_prompt_cache("anthropic/claude-haiku-4-5", msgs)
    # breakpoint 1: end of the system prefix (whole static prefix cached)
    sysblk = out[0]["content"]
    assert isinstance(sysblk, list)
    assert sysblk[0]["cache_control"]["type"] == "ephemeral"
    assert sysblk[0]["text"] == "big stable prompt"
    # breakpoint 2 (NEW): the last turn — the rolling history breakpoint, so
    # next turn the whole [system + this turn] prefix is a cache read
    assert _marked(out[1])
    assert out[1]["content"][0]["text"] == "hi"


def test_apply_prompt_cache_uses_the_configured_ttl():
    """Every breakpoint carries the configured cache lifetime. Measured on live
    traffic (2026-07-24): the shared system prefix never went cold on the 5-min
    default (95% of calls within 5 min, timer refreshes on hit), but a lead's
    follow-up turn lands inside 5 min only 73% of the time vs 89% within an
    hour — the extended TTL is bought for the per-dialogue history breakpoint."""
    from aibroker.providers.litellm_adapter import _CACHE_TTL, apply_prompt_cache
    out = apply_prompt_cache("anthropic/x", [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"}])
    for m in out:
        cc = m["content"][0]["cache_control"]
        assert cc["type"] == "ephemeral"
        if _CACHE_TTL is None:
            assert "ttl" not in cc      # plain 5-minute default
        else:
            assert cc["ttl"] == _CACHE_TTL


def test_extended_ttl_write_premium_is_charged_on_cache_writes():
    """REGRESSION GUARD: litellm.cost_per_token has NO ttl parameter — it prices
    every cache write at the 5-minute rate. Writing with a longer TTL costs
    more, so without an explicit premium every write would be UNDER-counted and
    the daily caps would silently allow overspend (the 2026-06-01 / 2026-06-11
    stale-pricing failure mode). Reads are unaffected."""
    import litellm

    from aibroker.providers.litellm_adapter import (
        _CACHE_TTL,
        _CACHE_TTL_RATE_FIELD,
        estimate_llm_cost,
    )
    model = "anthropic/claude-sonnet-5"
    info = litellm.get_model_info(model)
    default_rate = info.get("cache_creation_input_token_cost")
    extended_rate = info.get(_CACHE_TTL_RATE_FIELD)
    if not (default_rate and extended_rate) or _CACHE_TTL is None:
        pytest.skip("model has no separate extended-TTL write rate")

    tokens = 100_000
    charged = estimate_llm_cost(model, tokens, 0, cache_write_tokens=tokens)
    litellm_only, _ = litellm.cost_per_token(
        model=model, prompt_tokens=tokens, completion_tokens=0,
        cache_creation_input_tokens=tokens)
    # we must charge MORE than litellm alone, by exactly the rate difference
    assert charged > float(litellm_only)
    assert charged == pytest.approx(
        float(litellm_only) + tokens * (float(extended_rate) - float(default_rate)))
    # a pure cache READ carries no write premium
    read_only = estimate_llm_cost(model, tokens, 0, cache_read_tokens=tokens)
    read_litellm, _ = litellm.cost_per_token(
        model=model, prompt_tokens=tokens, completion_tokens=0,
        cache_read_input_tokens=tokens)
    assert read_only == pytest.approx(float(read_litellm))


def test_extended_ttl_write_premium_stays_zero_without_a_long_ttl_rate():
    """The premium must apply ONLY where the vendor really charges more for a
    long-TTL write. A model with no such rate (or an unpriced/unknown model)
    gets nothing added — otherwise we'd invent cost that isn't billed and the
    caps would throttle traffic for no reason (the mirror of under-counting)."""
    from aibroker.providers.litellm_adapter import _extended_ttl_write_premium
    # unknown model → get_model_info raises → no premium, no crash
    assert _extended_ttl_write_premium("not-a-real/model-xyz", 10_000) == 0.0
    # a real model with no separate long-TTL write rate (deepseek caches
    # server-side; we never send it cache_control at all)
    assert _extended_ttl_write_premium("deepseek/deepseek-v4-flash", 10_000) == 0.0
    # nothing written → nothing to surcharge
    assert _extended_ttl_write_premium("anthropic/claude-sonnet-5", 0) == 0.0


def test_apply_prompt_cache_noop_for_other_providers():
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
    # cerebras/gemini/deepseek: no explicit cache_control injected
    assert apply_prompt_cache("cerebras/gpt-oss-120b", msgs) == msgs
    assert apply_prompt_cache("deepseek/deepseek-chat", msgs) == msgs


def test_apply_prompt_cache_marks_every_leading_system_message():
    """REGRESSION (shipped 2026-07-23, caught in prod 2026-07-24): a single mark
    on the LAST leading system message looked equivalent — a breakpoint caches
    everything before it — but that is only true while the whole run is stable.
    Stepan's prefix is [78,938-char stable persona][~700-char per-lead DOSSIER],
    so the terminal mark put the VARIABLE dossier inside the cache key and no
    two leads ever shared an entry: 5.5% hit rate, ~29k tokens re-written on
    EVERY call, $0.12/call, draining the $5/day Sonnet cap in hours.

    Marking each system message puts a breakpoint at the stable/variable
    boundary wherever it falls; anthropic matches the LONGEST cached prefix, so
    the extra marks cost nothing and the stable part always hits."""
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    out = apply_prompt_cache("anthropic/x", [
        {"role": "system", "content": "stable persona"},
        {"role": "system", "content": "per-lead dossier"},
        {"role": "user", "content": "hi"}])
    assert _marked(out[0])   # stable part cached on its own — the whole point
    assert _marked(out[1])   # end of the system run
    assert _marked(out[2])   # rolling history end


def test_apply_prompt_cache_respects_the_breakpoint_budget():
    """anthropic allows _MAX_CACHE_MARKS breakpoints. With more leading system
    messages than the budget, one slot stays reserved for the history mark so
    the multi-turn win isn't lost to a long system run."""
    from aibroker.providers.litellm_adapter import (
        _MAX_CACHE_MARKS,
        apply_prompt_cache,
    )
    out = apply_prompt_cache("anthropic/x", [
        {"role": "system", "content": f"s{i}"} for i in range(6)])
    assert sum(_marked(m) for m in out) <= _MAX_CACHE_MARKS
    assert _marked(out[0])     # earliest (most stable) prefix is marked
    assert _marked(out[-1])    # history end still gets its slot


def test_apply_prompt_cache_rolling_history_grows_with_the_dialogue():
    """The history breakpoint tracks the LAST message each turn, so a longer
    multi-turn dialogue caches more of itself — the multi-turn sales win."""
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    out = apply_prompt_cache("anthropic/x", [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"}])
    assert _marked(out[0])                # system prefix end
    assert not _marked(out[1]) and not _marked(out[2])   # mid-history untouched
    assert _marked(out[3])                # newest turn = rolling breakpoint


def test_apply_prompt_cache_history_end_marked_even_after_a_late_system():
    """The leading-system-run breakpoint stops at the first non-system message,
    but the rolling history breakpoint is the LAST message overall — so a
    system message that appears late still gets the history mark (it's the end
    of the conversation), while the mid-dialogue user turn stays untouched."""
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    out = apply_prompt_cache("anthropic/x", [
        {"role": "system", "content": "head"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "late"}])
    assert _marked(out[0])                          # system prefix end (head)
    assert out[1] == {"role": "user", "content": "hi"}   # mid-history untouched
    assert _marked(out[2])                          # last message = history end


def test_apply_prompt_cache_skips_nonstr_and_empty_in_head():
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    listy = {"role": "system", "content": [{"type": "text", "text": "x"}]}
    out = apply_prompt_cache("anthropic/x", [
        {"role": "system", "content": "  "},   # empty → untouched
        listy,                                  # list content → untouched
        {"role": "system", "content": "real"}])
    assert out[0] == {"role": "system", "content": "  "}
    assert out[1] == listy
    assert _marked(out[2])


def test_cache_tokens_reads_anthropic_and_openai_shapes():
    from aibroker.providers.litellm_adapter import _cache_tokens
    assert _cache_tokens({"cache_read_input_tokens": 900,
                          "cache_creation_input_tokens": 100}) == (900, 100)
    # OpenAI-shape nested cached_tokens
    assert _cache_tokens({"prompt_tokens_details": {"cached_tokens": 512}}) == (512, 0)
    assert _cache_tokens({}) == (0, 0)


def test_model_for_known_provider_capability():
    assert model_for("cerebras", "chat:fast") == "cerebras/gpt-oss-120b"
    assert model_for("voyage", "embedding") == "voyage/voyage-4"
    assert model_for("anthropic", "chat:smart") == "anthropic/claude-sonnet-5"


def test_anthropic_sonnet5_on_smart_code_vision_and_edit():
    """2026-07-02: bumped off sonnet-4-6. chat:edit specifically matters — it's
    Stepan/Stepan2 Coach's anthropic fallback (after gemini, deepseek fail)."""
    for cap in ("chat:smart", "chat:code", "vision", "chat:edit"):
        assert model_for("anthropic", cap) == "anthropic/claude-sonnet-5", cap
    # fast tier untouched
    assert model_for("anthropic", "chat:fast") == "anthropic/claude-haiku-4-5"
    assert model_for("anthropic", "structured") == "anthropic/claude-haiku-4-5"


def test_model_for_unknown_provider_returns_none():
    assert model_for("nonexistent", "chat:fast") is None


def test_model_for_unknown_capability_returns_none():
    assert model_for("cerebras", "chat:nothing") is None


def test_default_model_has_voyage_embedding():
    assert "voyage" in DEFAULT_MODEL
    assert "embedding" in DEFAULT_MODEL["voyage"]


def test_deepseek_is_on_v41_flash_everywhere():
    """2026-07-17: deepseek-chat is deprecated 2026-07-24 — every deepseek slot
    moves to v4-flash. Safe ONLY because _DeepseekAdapter disables thinking on
    v4-* (the 07-10 regression was the thinking DEFAULT eating max_tokens, not
    the model; confirmed live: valid JSON at max_tokens=120 with the knob).
    This test + test_deepseek_v4_disables_thinking guard both halves — moving
    the model without the knob resurrects the ~49% InvalidJSON storm.
    2026-09-12: the slot is now `deepseek-flash` (V4.1-Flash; the only flash
    model DeepSeek's /models lists on our keys, v4-flash is a temporary alias)
    and it gained a vision lane. The thinking guard for the new name is
    test_deepseek_flash_v41_is_treated_as_the_same_hybrid_family."""
    for cap, model in DEFAULT_MODEL["deepseek"].items():
        assert model == "deepseek/deepseek-flash", cap
    assert "vision" in DEFAULT_MODEL["deepseek"]
    assert "deepseek-chat" not in str(DEFAULT_MODEL["deepseek"])
    assert "v4-pro" not in str(DEFAULT_MODEL["deepseek"])


def test_deepseek_flash_is_priced_off_peak_base():
    """deepseek-flash is missing from litellm's map on this version; without
    our register_model entry every call would book $0 and blind the caps. The
    registered base must be the OFF-PEAK list price, because peak_pricing
    multiplies by 2 in the weekday peak windows (DeepSeek: off-peak = 50% of
    peak) — a peak-rate base would double-count, as litellm's own v4-flash
    entry did."""
    from datetime import UTC, datetime
    off = estimate_llm_cost("deepseek/deepseek-flash", 1_000_000, 1_000_000,
                            at=datetime(2026, 9, 14, 12, 0, tzinfo=UTC))  # Monday noon
    assert off == pytest.approx(0.15 + 0.60)
    peak = estimate_llm_cost("deepseek/deepseek-flash", 1_000_000, 1_000_000,
                             at=datetime(2026, 9, 14, 2, 0, tzinfo=UTC))   # Monday 02:00
    assert peak == pytest.approx(2 * off)


def test_gemini_rotations_are_measured_sets():
    """2026-09-12: vision got its own rotation (the primary 2.5-flash bucket
    was exhausted on every key while 3 sibling buckets sat idle) and
    3.5-flash joined chat. 3.6-flash is deliberately NOT in vision — one of
    three measured calls took 29s against a 60s cloud vision timeout."""
    from aibroker.providers.litellm_adapter import rotation_for
    assert set(rotation_for("gemini", "vision")) == {
        "gemini/gemini-3.5-flash-lite", "gemini/gemini-3.5-flash",
        "gemini/gemini-3.1-flash-lite"}
    assert "gemini/gemini-3.5-flash" in rotation_for("gemini", "chat:smart")
    assert model_for("gemini", "vision") not in rotation_for("gemini", "vision")


def test_gemini_smart_is_flash_not_starved_pro():
    """REGRESSION (2026-07-10): gemini-2.5-pro's free tier (~50-100 RPD/5 RPM)
    can't serve smart volume — chat:smart must stay on a FLASH-tier model.
    2026-08-16: the specific flash moved 2.5 → 3.7, so assert the tier (the
    actual invariant) rather than one frozen version string."""
    smart = model_for("gemini", "chat:smart")
    assert "flash" in smart and "pro" not in smart
    assert smart == "gemini/gemini-2.5-flash"


def test_gemini_utility_lanes_use_flash_lite_quota_bucket():
    """2026-07-18: Google's free quota is PER MODEL per key — flash-lite's
    1000 RPD bucket (4× flash's 250) was unused while flash burned quota on
    utility calls. A/B on our keys: translate byte-identical, prefilter JSON
    identical. Quality-sensitive lanes (vision/smart/structured) MUST stay on
    flash — the freed quota is theirs.

    2026-08-16: the quality lanes were briefly moved to gemini-3.7-flash and
    moved straight back — at N=10 on a realistic prompt 3.7 answered only 7/10
    (3 ServiceUnavailable) at a 1495ms median against 2.5's 10/10 at 878ms. The
    two-bucket split below is therefore still the shipped arrangement."""
    for cap in ("prefilter", "translate"):
        assert model_for("gemini", cap) == "gemini/gemini-2.5-flash-lite", cap
    for cap in ("chat:smart", "structured", "vision", "chat:edit", "chat:sales"):
        assert model_for("gemini", cap) == "gemini/gemini-2.5-flash", cap


def test_cohere_smart_is_cheap_r7b_not_flagship_command_a():
    """REGRESSION (2026-07-10): command-a-03-2025 (flagship) billed ~$2.4/day
    mostly on failed calls — smart/code fall back to the cheap r7b."""
    assert model_for("cohere", "chat:smart") == "cohere/command-r7b-12-2024"
    assert model_for("cohere", "chat:code") == "cohere/command-r7b-12-2024"


# ─── estimate_llm_cost ────────────────────────────────────────────────────


def test_estimate_cost_returns_float():
    """Real LiteLLM cost lookup for cerebras."""
    cost = estimate_llm_cost("cerebras/gpt-oss-120b", 1000, 100)
    assert isinstance(cost, float)
    assert cost >= 0.0


def test_estimate_cost_unknown_model_returns_zero():
    cost = estimate_llm_cost("nonsense-model-xyz", 100, 50)
    assert cost == 0.0


def test_estimate_cost_voyage_4_is_registered():
    """voyage-4 is absent from LiteLLM's pricing map — before the
    register_model at import, every embed cost estimate warned 'no LiteLLM
    pricing' (log spam) and priced a PAID voyage key at $0, blinding its
    daily cost cap (2026-07-16). $0.06/M input is the voyage-4 list price."""
    assert estimate_llm_cost("voyage/voyage-4", 1_000_000, 0) == pytest.approx(0.06)


# ─── call_llm — mocked LiteLLM ────────────────────────────────────────────


async def test_call_llm_happy_path_object_response():
    """Response shape: SimpleNamespace with .choices and .usage."""
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="hello dima"),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3),
    )
    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                AsyncMock(return_value=fake_resp)):
        text, meta = await call_llm(
            model="cerebras/gpt-oss-120b",
            messages=[{"role": "user", "content": "hi"}],
            api_key="test-key",
        )
    assert text == "hello dima"
    assert meta["tokens_in"] == 12
    assert meta["tokens_out"] == 3
    assert meta["model"] == "cerebras/gpt-oss-120b"
    assert meta["finish_reason"] == "stop"
    assert "latency_ms" in meta


async def test_call_llm_dict_message_response():
    """Some providers return choices[0].message as a dict."""
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(
            message={"content": "from dict"},
            finish_reason="stop",
        )],
        usage={"prompt_tokens": 5, "completion_tokens": 2},
    )
    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                AsyncMock(return_value=fake_resp)):
        text, meta = await call_llm(
            model="x/y", messages=[{"role": "user", "content": "x"}],
            api_key="k",
        )
    assert text == "from dict"
    assert meta["tokens_in"] == 5


async def test_call_llm_empty_choices():
    fake_resp = SimpleNamespace(
        choices=[], usage={"prompt_tokens": 1, "completion_tokens": 0},
    )
    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                AsyncMock(return_value=fake_resp)):
        text, meta = await call_llm(
            model="x/y", messages=[{"role": "user", "content": "hi"}],
            api_key="k",
        )
    assert text == ""
    assert meta["finish_reason"] is None


async def test_call_llm_passes_response_format_kwarg():
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="{}"),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                side_effect=fake_acompletion):
        await call_llm(
            model="x/y", messages=[{"role": "user", "content": "x"}],
            api_key="k",
            response_format={"type": "json_object"},
            max_tokens=512, temperature=0.3,
        )
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["max_tokens"] == 512
    assert captured["temperature"] == 0.3
    assert captured["api_key"] == "k"


async def test_call_llm_hands_the_capability_to_the_adapter():
    """The lane reaches the adapter. Nothing keys off it for anthropic today
    (chat:sales lost its exemption after measuring 44% InvalidJSON), but the
    plumbing is what lets a lane ever diverge — and it must not crash or leak
    the argument into the provider payload."""
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                side_effect=fake_acompletion):
        await call_llm(
            model="anthropic/claude-sonnet-5",
            messages=[{"role": "user", "content": "x"}], api_key="k",
            response_format={"type": "json_object"}, capability="chat:sales")
    assert captured["response_format"]["type"] == "json_schema"   # forced JSON
    assert "capability" not in captured        # broker-side only, never sent


async def test_call_llm_unwraps_the_anthropic_tool_envelope():
    """End-to-end through call_llm: LiteLLM's forced-tool envelope is unwrapped
    before the body is returned — so the JSON gate, record_usage and the
    response cache all see the caller's real object, not {"parameters": {…}}.
    Measured live: ~half of chat:sales replies arrived enveloped."""
    async def enveloped(**_kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps({"parameters": {"reply": "halo",
                                                       "move": "ask_budget"}})),
                finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                side_effect=enveloped):
        text, _meta = await call_llm(
            model="anthropic/claude-sonnet-5",
            messages=[{"role": "user", "content": "x"}], api_key="k",
            response_format={"type": "json_object"})
    assert json.loads(text) == {"reply": "halo", "move": "ask_budget"}

    # a NON-anthropic provider returning the same shape is left alone
    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                side_effect=enveloped):
        text, _meta = await call_llm(
            model="deepseek/deepseek-v4-flash",
            messages=[{"role": "user", "content": "x"}], api_key="k",
            response_format={"type": "json_object"})
    assert json.loads(text) == {"parameters": {"reply": "halo",
                                               "move": "ask_budget"}}


async def test_call_llm_forwards_json_schema_verbatim():
    """Native structured output (#1): a full json_schema response_format must
    reach the provider UNCHANGED — that's what grammar-constrains generation to
    valid JSON at the source (root-cause fix vs the post-hoc regex gate). The
    broker can't invent a schema, so its whole job here is faithful pass-through."""
    captured = {}
    schema = {
        "type": "json_schema",
        "json_schema": {
            "name": "extraction", "strict": True,
            "schema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"], "additionalProperties": False,
            },
        },
    }

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"name":"x"}'),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                side_effect=fake_acompletion):
        await call_llm(
            model="gemini/gemini-2.5-flash",
            messages=[{"role": "user", "content": "x"}], api_key="k",
            response_format=schema, max_tokens=256, temperature=0.0,
        )
    assert captured["response_format"] == schema        # byte-for-byte, incl. strict
    assert captured["response_format"]["json_schema"]["strict"] is True


async def test_call_llm_disables_gemini_thinking_for_json():
    """gemini + JSON → reasoning_effort=disable so thinking doesn't truncate."""
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                side_effect=fake_acompletion):
        await call_llm(
            model="gemini/gemini-2.5-flash",
            messages=[{"role": "user", "content": "x"}], api_key="k",
            response_format={"type": "json_object"},
        )
    assert captured.get("reasoning_effort") == "disable"


async def test_call_llm_thinking_disable_is_gemini_only():
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"),
                                     finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                side_effect=fake_acompletion):
        # non-gemini (cerebras) → never gets reasoning_effort, even on JSON
        await call_llm(model="cerebras/gpt-oss-120b",
                       messages=[{"role": "user", "content": "x"}], api_key="k",
                       response_format={"type": "json_object"})
        assert "reasoning_effort" not in captured
        captured.clear()
        # gemini + no JSON → NOW disabled unconditionally (2026-07-10)
        await call_llm(model="gemini/gemini-2.5-flash",
                       messages=[{"role": "user", "content": "x"}], api_key="k")
    assert captured["reasoning_effort"] == "disable"


async def test_call_llm_extra_kwargs_passed_through():
    captured = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(
                message=SimpleNamespace(content="ok"),
                finish_reason="stop",
            )],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                side_effect=fake_acompletion):
        await call_llm(
            model="x/y", messages=[{"role": "user", "content": "x"}],
            api_key="k",
            extra={"top_p": 0.9, "seed": 42},
        )
    assert captured["top_p"] == 0.9
    assert captured["seed"] == 42


async def test_call_llm_missing_usage_safe():
    """If usage is absent, tokens default to 0."""
    fake_resp = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="x"),
            finish_reason="stop",
        )],
        usage=None,
    )
    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                AsyncMock(return_value=fake_resp)):
        _, meta = await call_llm(
            model="x/y", messages=[{"role": "user", "content": "x"}],
            api_key="k",
        )
    assert meta["tokens_in"] == 0
    assert meta["tokens_out"] == 0


async def test_call_llm_timeout_is_enforced_independently_of_litellm():
    """REGRESSION (2026-07-07): confirmed live that LiteLLM's own `timeout`
    kwarg does NOT reliably cut off a hung/slow call — a zai key was observed
    completing normally at 90-180s wall time on a timeout=60 request (no
    TimeoutError raised by LiteLLM at all). call_llm must enforce the ceiling
    itself via asyncio.wait_for as a hard backstop, independent of whatever
    LiteLLM/the provider plugin does internally with the timeout kwarg."""
    import asyncio

    async def _never_returns(**kw):
        await asyncio.sleep(10)
        raise AssertionError("should have been cancelled by wait_for before this")

    with patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                _never_returns), pytest.raises(TimeoutError):
        await call_llm(
            model="zai/glm-4.5-flash", messages=[{"role": "user", "content": "x"}],
            api_key="k", timeout=0.05,
        )


# ─── embed — mocked LiteLLM ───────────────────────────────────────────────


async def test_embed_happy_path_dict_data():
    fake_resp = SimpleNamespace(
        data=[{"embedding": [0.1, 0.2, 0.3]}, {"embedding": [0.4, 0.5]}],
        usage={"prompt_tokens": 5},
    )
    with patch("aibroker.providers.litellm_adapter.litellm.aembedding",
                AsyncMock(return_value=fake_resp)):
        vectors, meta = await embed(
            model="voyage/voyage-3",
            texts=["hello", "world"],
            api_key="k",
        )
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5]]
    assert meta["tokens_in"] == 5
    assert meta["tokens_out"] == 0
    assert meta["model"] == "voyage/voyage-3"


async def test_embed_happy_path_object_data():
    fake_resp = SimpleNamespace(
        data=[SimpleNamespace(embedding=[0.1, 0.2])],
        usage=SimpleNamespace(prompt_tokens=3),
    )
    with patch("aibroker.providers.litellm_adapter.litellm.aembedding",
                AsyncMock(return_value=fake_resp)):
        vectors, _ = await embed(
            model="voyage/voyage-3", texts=["x"], api_key="k",
        )
    assert vectors == [[0.1, 0.2]]


async def test_embed_falls_back_to_vector_key():
    """Older LiteLLM versions used `vector` instead of `embedding`."""
    fake_resp = SimpleNamespace(
        data=[{"vector": [0.7, 0.8]}],
        usage={"total_tokens": 2},
    )
    with patch("aibroker.providers.litellm_adapter.litellm.aembedding",
                AsyncMock(return_value=fake_resp)):
        vectors, meta = await embed(
            model="voyage/voyage-3", texts=["x"], api_key="k",
        )
    assert vectors == [[0.7, 0.8]]
    assert meta["tokens_in"] == 2  # from total_tokens fallback


async def test_embed_empty_data_returns_empty_vectors():
    fake_resp = SimpleNamespace(data=[], usage={"prompt_tokens": 0})
    with patch("aibroker.providers.litellm_adapter.litellm.aembedding",
                AsyncMock(return_value=fake_resp)):
        vectors, _ = await embed(
            model="voyage/voyage-3", texts=[], api_key="k",
        )
    assert vectors == []


# ─── transcribe — mocked LiteLLM ──────────────────────────────────────────


def test_model_for_transcription():
    assert model_for("groq", "transcription") == "groq/whisper-large-v3-turbo"
    assert model_for("openai", "transcription") == "openai/whisper-1"


async def test_transcribe_object_response():
    fake_resp = SimpleNamespace(text="  привет мир  ")
    with patch("aibroker.providers.litellm_adapter.litellm.atranscription",
                AsyncMock(return_value=fake_resp)):
        text, meta = await transcribe(
            model="groq/whisper-large-v3-turbo",
            audio=b"oggbytes", filename="v.ogg", api_key="k",
        )
    assert text == "привет мир"   # stripped
    assert meta["model"] == "groq/whisper-large-v3-turbo"
    assert "latency_ms" in meta


async def test_transcribe_dict_response():
    with patch("aibroker.providers.litellm_adapter.litellm.atranscription",
                AsyncMock(return_value={"text": "hello"})):
        text, _ = await transcribe(
            model="groq/whisper-large-v3-turbo",
            audio=b"x", filename="a.mp3", api_key="k",
        )
    assert text == "hello"


async def test_transcribe_passes_filename_as_buffer_name():
    """The format is inferred from the file's .name — verify we set it."""
    captured = {}

    async def fake_atranscription(*, model, file, api_key):
        captured["name"] = getattr(file, "name", None)
        captured["model"] = model
        return SimpleNamespace(text="ok")

    with patch("aibroker.providers.litellm_adapter.litellm.atranscription",
                side_effect=fake_atranscription):
        await transcribe(model="groq/whisper-large-v3-turbo",
                         audio=b"data", filename="voice.ogg", api_key="k")
    assert captured["name"] == "voice.ogg"


# ─── transcribe — local ASR (self-hosted faster-whisper, vera3's asr-local) ─


def test_model_for_local_transcription():
    assert model_for("local", "transcription") == "local/whisper"


async def test_transcribe_local_asr_success(monkeypatch):
    monkeypatch.setattr(get_settings(), "ASR_LOCAL_URL", "http://asr-local:8000")
    fake_resp = SimpleNamespace(
        status_code=200,
        json=lambda: {"text": "  привет  ", "duration_s": 2.1, "language": "ru"},
    )
    with patch("aibroker.providers.litellm_adapter._post_local_asr",
                AsyncMock(return_value=fake_resp)):
        text, meta = await transcribe(
            model="local/whisper", audio=b"oggbytes", filename="v.ogg", api_key="unused",
        )
    assert text == "привет"   # stripped
    assert meta["cost_usd"] == 0.0
    assert meta["model"] == "local/whisper"


async def test_transcribe_local_asr_requests_language_auto(monkeypatch):
    """asr-local's own default is 'ru' (vera3's use case) — the broker must
    override it so non-Russian callers (Stepan2's mostly-Bahasa leads) aren't
    force-decoded through Russian."""
    monkeypatch.setattr(get_settings(), "ASR_LOCAL_URL", "http://asr-local:8000")
    captured = {}

    async def fake_post(url, audio, timeout):
        captured["url"] = url
        return SimpleNamespace(status_code=200, json=lambda: {"text": "ok"})

    with patch("aibroker.providers.litellm_adapter._post_local_asr", side_effect=fake_post):
        await transcribe(model="local/whisper", audio=b"x", filename="a.ogg", api_key="k")
    assert "language=auto" in captured["url"]


async def test_transcribe_local_asr_no_url_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "ASR_LOCAL_URL", "")
    with pytest.raises(RuntimeError, match="ASR_LOCAL_URL"):
        await transcribe(model="local/whisper", audio=b"x", filename="a.ogg", api_key="k")


async def test_transcribe_local_asr_connection_error_becomes_timeout(monkeypatch):
    """A downed/unreachable asr-local must classify as a retryable TimeoutError
    (cools the key) — classify_provider_error gives a generic error NO
    cooldown at all, which would hammer a dead endpoint every call."""
    monkeypatch.setattr(get_settings(), "ASR_LOCAL_URL", "http://asr-local:8000")
    with patch("aibroker.providers.litellm_adapter._post_local_asr",
                AsyncMock(side_effect=httpx.ConnectError("refused"))), \
         pytest.raises(TimeoutError):
        await transcribe(model="local/whisper", audio=b"x", filename="a.ogg", api_key="k")


async def test_transcribe_local_asr_5xx_becomes_timeout(monkeypatch):
    monkeypatch.setattr(get_settings(), "ASR_LOCAL_URL", "http://asr-local:8000")
    fake_resp = SimpleNamespace(status_code=500, text="model still loading")
    with patch("aibroker.providers.litellm_adapter._post_local_asr",
                AsyncMock(return_value=fake_resp)), \
         pytest.raises(TimeoutError):
        await transcribe(model="local/whisper", audio=b"x", filename="a.ogg", api_key="k")


async def test_transcribe_local_asr_4xx_stays_plain_error(monkeypatch):
    """A bad request (e.g. audio too large) is a per-request problem, not a
    'the service is down, back off' one — must not cool the key like a 5xx."""
    monkeypatch.setattr(get_settings(), "ASR_LOCAL_URL", "http://asr-local:8000")
    fake_resp = SimpleNamespace(status_code=413, text="audio > 25MB")
    with patch("aibroker.providers.litellm_adapter._post_local_asr",
                AsyncMock(return_value=fake_resp)), \
         pytest.raises(RuntimeError) as exc_info:
        await transcribe(model="local/whisper", audio=b"x", filename="a.ogg", api_key="k")
    assert not isinstance(exc_info.value, TimeoutError)


def test_models_for_returns_primary_first_then_rotation():
    """The primary from DEFAULT_MODEL must stay first so a provider with no
    rotation configured behaves exactly as it did with plain model_for."""
    from aibroker.providers.litellm_adapter import model_for, models_for

    got = models_for("gemini", "chat:sales")
    assert got[0] == model_for("gemini", "chat:sales")
    assert len(got) > 1                       # gemini has extra quota buckets
    assert len(got) == len(set(got))          # no duplicates to waste attempts on


def test_models_for_is_a_noop_without_rotation():
    from aibroker.providers.litellm_adapter import model_for, models_for

    assert models_for("mistral", "chat:smart") == [model_for("mistral", "chat:smart")]
    assert models_for("nosuch", "chat:smart") == []


def test_gemini_rotation_never_uses_a_latest_alias():
    """REGRESSION GUARD: '-latest' aliases resolve to whatever generation Google
    points them at. Measured 2026-08-26, gemini-flash-lite-latest failed 5/5 with
    a 400 — it resolves to a 3.7-class model that rejects
    reasoning_effort='disable', and the alias also slips past _GeminiAdapter's
    version-prefix check, so the adapter cannot pick the right value for it."""
    from aibroker.providers.litellm_adapter import MODEL_ROTATION

    for cap, models in MODEL_ROTATION["gemini"].items():
        for m in models:
            assert not m.endswith("-latest"), (cap, m)
