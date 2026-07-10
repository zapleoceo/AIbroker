"""providers/litellm_adapter — call_llm + embed wrappers (mocked LiteLLM)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

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


def test_apply_prompt_cache_marks_anthropic_system():
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    msgs = [{"role": "system", "content": "big stable prompt"},
            {"role": "user", "content": "hi"}]
    out = apply_prompt_cache("anthropic/claude-haiku-4-5", msgs)
    sysblk = out[0]["content"]
    assert isinstance(sysblk, list)
    assert sysblk[0]["cache_control"] == {"type": "ephemeral"}
    assert sysblk[0]["text"] == "big stable prompt"
    assert out[1] == {"role": "user", "content": "hi"}   # user untouched


def test_apply_prompt_cache_noop_for_other_providers():
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    msgs = [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
    # cerebras/gemini/deepseek: no explicit cache_control injected
    assert apply_prompt_cache("cerebras/gpt-oss-120b", msgs) == msgs
    assert apply_prompt_cache("deepseek/deepseek-chat", msgs) == msgs


def test_apply_prompt_cache_only_first_system_and_skips_empty():
    from aibroker.providers.litellm_adapter import apply_prompt_cache
    # no system → unchanged; only the FIRST system marked
    assert apply_prompt_cache("anthropic/x", [{"role": "user", "content": "a"}]) == [
        {"role": "user", "content": "a"}]
    out = apply_prompt_cache("anthropic/x", [
        {"role": "system", "content": "one"},
        {"role": "system", "content": "two"}])
    assert isinstance(out[0]["content"], list)          # first marked
    assert out[1]["content"] == "two"                   # second left as-is


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


def test_deepseek_uses_live_v4_flash_not_retired_chat_or_coder():
    """REGRESSION (2026-07-10): deepseek-chat/deepseek-coder are retired from the
    DeepSeek API (GET /models returns only v4-flash + v4-pro; chat deprecates
    2026-07-24). Every deepseek slot must be a live model, and v4-flash is the
    cheaper direct successor to chat ($0.14/$0.28 vs $0.28/$0.42)."""
    for cap, model in DEFAULT_MODEL["deepseek"].items():
        assert model == "deepseek/deepseek-v4-flash", cap
        assert "deepseek-chat" not in model and "deepseek-coder" not in model


def test_gemini_smart_is_flash_not_starved_pro():
    """REGRESSION (2026-07-10): gemini-2.5-pro's free tier (~50-100 RPD/5 RPM)
    can't serve smart volume — moved chat:smart to 2.5-flash (~250 RPD/10 RPM)."""
    assert model_for("gemini", "chat:smart") == "gemini/gemini-2.5-flash"


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


async def test_call_llm_no_thinking_disable_for_non_gemini_or_non_json():
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
        # non-gemini + JSON → no reasoning_effort
        await call_llm(model="cerebras/gpt-oss-120b",
                       messages=[{"role": "user", "content": "x"}], api_key="k",
                       response_format={"type": "json_object"})
        assert "reasoning_effort" not in captured
        captured.clear()
        # gemini + no JSON → no reasoning_effort
        await call_llm(model="gemini/gemini-2.5-flash",
                       messages=[{"role": "user", "content": "x"}], api_key="k")
    assert "reasoning_effort" not in captured


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
