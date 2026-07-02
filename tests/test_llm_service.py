"""services/llm_service — provider-error classification (the DRY classifier)."""
from __future__ import annotations

from aibroker.services.llm_service import classify_provider_error


def test_classify_rate_limit():
    assert classify_provider_error(RuntimeError("429 Too Many Requests")) == "rate_limit"
    assert classify_provider_error(Exception("provider rate_limit exceeded")) == "rate_limit"


def test_classify_camelcase_and_quota_shapes():
    """REGRESSION (2026-06-29): cerebras 'RateLimitError - Tokens per day limit
    exceeded' lower-cases to 'ratelimiterror ... tokens per day' — no
    'rate_limit' underscore, no '429'. The old classifier returned 'error' so
    the key was never cooled → infinite retry storm. All these must be
    rate_limit now."""
    for msg in (
        "litellm.RateLimitError: CerebrasException - Tokens per day limit exceeded",
        "RESOURCE_EXHAUSTED: free_tier quota exceeded",
        "GeminiException: too many requests",
        "Tokens per minute (TPM) limit",
    ):
        assert classify_provider_error(RuntimeError(msg)) == "rate_limit", msg


def test_classify_auth():
    assert classify_provider_error(RuntimeError("401 Unauthorized")) == "auth"
    assert classify_provider_error(Exception("403 forbidden")) == "auth"
    assert classify_provider_error(Exception("invalid auth token")) == "auth"


def test_classify_generic_error():
    assert classify_provider_error(RuntimeError("boom")) == "error"
    assert classify_provider_error(ValueError("connection reset by peer")) == "error"


# ─── size-aware provider filter in run_chat ──────────────────────────────────


async def test_run_chat_skips_groq_for_oversize_prompt(monkeypatch):
    """A 24k-token prompt must never be offered to groq (8k ceiling).
    We record every provider pick_and_reserve is called with and assert groq
    is absent — without needing a real DB (pick returns None → chain walks)."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picked: list[str] = []

    async def fake_pick(provider, scope, **kw):
        # no key → walk to next provider; we only care who's tried
        picked.append(provider)

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for",
                         lambda cap: ["cerebras", "groq", "gemini", "mistral"])

    big = [{"role": "user", "content": "x" * 100_000}]  # ~25k tokens
    project = SimpleNamespace(id=1, name="stepan")
    out = await svc.run_chat(
        project=project, capability="chat:smart", messages=big,
        model=None, max_tokens=512, temperature=0.7,
        response_format=None, workflow="stepan",
    )
    assert out is None                 # no keys available in this mock
    assert "groq" not in picked        # size-skipped
    assert "cerebras" in picked        # big-context providers still tried
    assert "mistral" in picked


async def test_run_chat_keeps_groq_for_small_prompt(monkeypatch):
    """A tiny prompt fits groq's ceiling → groq stays in the chain."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picked: list[str] = []

    async def fake_pick(provider, scope, **kw):
        picked.append(provider)

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["cerebras", "groq"])

    small = [{"role": "user", "content": "привет"}]
    project = SimpleNamespace(id=1, name="vera")
    await svc.run_chat(
        project=project, capability="chat:fast", messages=small,
        model=None, max_tokens=128, temperature=0.7,
        response_format=None, workflow="vera",
    )
    assert "groq" in picked


async def test_run_chat_learns_ceiling_on_too_large_error(monkeypatch):
    """When a provider 413s a prompt, run_chat records the size and jumps to
    the next provider instead of burning all 5 key retries."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []
    recorded: list[tuple[str, int]] = []

    fake_key = SimpleNamespace(id=1, label="k", provider="cerebras",
                               token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        return fake_key if provider == "cerebras" else None

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        raise RuntimeError("Error: maximum context length exceeded")

    async def fake_record_too_large(provider, est):
        recorded.append((provider, est))

    async def fake_penalize(key, exc):
        return "error"

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "check_caps", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "_penalize", fake_penalize)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "record_too_large", fake_record_too_large)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["cerebras", "mistral"])

    big = [{"role": "user", "content": "x" * 60_000}]   # ~15k tokens
    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan"), capability="chat:smart",
        messages=big, model=None, max_tokens=512, temperature=0.7,
        response_format=None, workflow="stepan",
    )
    assert out is None
    # cerebras 413'd → recorded its ceiling once, then moved on (not 5 retries)
    assert recorded and recorded[0][0] == "cerebras"
    assert picks.count("cerebras") == 1     # broke after the too-large error
    assert "mistral" in picks               # advanced to next provider


async def _noop():
    return None


# ─── per-provider retry cap ──────────────────────────────────────────────────


def test_max_keys_per_provider_defaults_and_overrides():
    from aibroker.services.llm_service import _max_keys
    assert _max_keys("gemini") == 3       # chronically rate-limited free tier
    assert _max_keys("cerebras") == 3
    assert _max_keys("mistral") == 5      # default breadth
    assert _max_keys("anthropic") == 5
    assert _max_keys("whatever") == 5


async def test_run_chat_caps_gemini_retries(monkeypatch):
    """gemini keys 429 in lockstep — the retry loop must stop at _max_keys=3,
    not burn 5 picks before moving on."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []
    key = SimpleNamespace(id=1, label="g", tier="free", provider="gemini",
                           token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        return key if provider == "gemini" else None

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        raise RuntimeError("429 rate limit exceeded")

    async def fake_penalize(k, e):
        return "rate_limit"

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "check_caps", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "_penalize", fake_penalize)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["gemini"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="vera",
    )
    assert out is None
    assert picks.count("gemini") == 3     # capped, not 5


# ─── InvalidJSON breaks to next PROVIDER, not next key ───────────────────────


async def test_run_chat_invalid_json_skips_provider_not_key(monkeypatch):
    """Malformed JSON is a model property — retrying sibling keys of the same
    provider re-mangles the same prompt. run_chat must break to the next
    provider after ONE bad-JSON response, not loop the provider's keys."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []
    ka = SimpleNamespace(id=1, label="a", tier="free", provider="cerebras",
                          token_encrypted="x")
    kb = SimpleNamespace(id=2, label="b", tier="free", provider="gemini",
                          token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        return {"cerebras": ka, "gemini": kb}.get(provider)

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        m = kw["model"]
        body = "not-json{{{" if m.startswith("cerebras") else '{"ok": true}'
        return body, {"model": m, "tokens_in": 100, "tokens_out": 50,
                      "cost_usd": 0.0, "latency_ms": 100,
                      "cache_read_tokens": 0, "cache_write_tokens": 0}

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "check_caps", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["cerebras", "gemini"])
    # isolate break-behaviour from the JSON reorder — keep the given order
    monkeypatch.setattr(svc, "deprioritize_for_json", lambda c: c)

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="structured",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7,
        response_format={"type": "json_object"}, workflow="rel_extract",
    )
    assert picks.count("cerebras") == 1   # tried ONCE, not 5×
    assert "gemini" in picks              # advanced to next provider
    assert out.text == '{"ok": true}'


async def test_run_chat_json_request_deprioritizes_unreliable(monkeypatch):
    """A JSON request must reorder the live chain via deprioritize_for_json —
    cerebras (unreliable) tried after gemini even though it's first in the raw
    chain."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)  # no keys → just record the order the chain is walked

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["cerebras", "gemini", "mistral"])

    await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="structured",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7,
        response_format={"type": "json_object"}, workflow="rel_extract",
    )
    # gemini + mistral (reliable) walked before cerebras (unreliable, to back)
    assert picks.index("gemini") < picks.index("cerebras")
    assert picks.index("mistral") < picks.index("cerebras")


# ─── free-tier keys must never bill a real $ cost ───────────────────────────


def test_billed_cost_zeroes_free_tier():
    """REGRESSION (2026-07-01): LiteLLM's cost_per_token prices by MODEL, with
    no concept of 'this key is on a free plan' — a free cerebras/gemini/mistral
    key calling a real-priced model got billed the same nominal cost a paid
    caller would pay, even though the free plan absorbs it at $0 to us. This
    inflated `daily_cost_used_usd` for every free key ($5.26 across 51 keys in
    a few hours) and made the dashboard show 'spend' on tokens that cost
    nothing."""
    from types import SimpleNamespace

    from aibroker.services.llm_service import _billed_cost

    free_key = SimpleNamespace(tier="free")
    paid_key = SimpleNamespace(tier="paid")
    meta = {"cost_usd": 2.80}

    assert _billed_cost(free_key, meta) == 0.0
    assert _billed_cost(paid_key, meta) == 2.80


async def test_run_chat_records_zero_cost_for_free_tier_key(monkeypatch):
    """End-to-end through run_chat: a free-tier key's real LiteLLM-priced cost
    must be zeroed before it reaches record_usage / the returned outcome."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    recorded_costs: list[float] = []
    fake_key = SimpleNamespace(id=1, label="k", tier="free", provider="gemini",
                                token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        return fake_key

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        return "hello", {"model": "gemini/gemini-2.5-flash", "tokens_in": 100,
                          "tokens_out": 50, "cost_usd": 2.80, "latency_ms": 200}

    def fake_record_usage(**kw):
        recorded_costs.append(kw["cost_usd"])
        return _noop()

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "check_caps", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", fake_record_usage)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["gemini"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="vera",
    )
    assert out.cost_usd == 0.0          # outcome reflects the billed (zeroed) cost
    assert recorded_costs == [0.0]      # record_usage got the zeroed cost too


async def test_run_chat_keeps_real_cost_for_paid_tier_key(monkeypatch):
    """A paid-tier key's real cost must pass through unchanged."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    recorded_costs: list[float] = []
    fake_key = SimpleNamespace(id=2, label="k", tier="paid", provider="deepseek",
                                token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        return fake_key

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        return "hello", {"model": "deepseek/deepseek-chat", "tokens_in": 100,
                          "tokens_out": 50, "cost_usd": 0.70, "latency_ms": 200}

    def fake_record_usage(**kw):
        recorded_costs.append(kw["cost_usd"])
        return _noop()

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "check_caps", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", fake_record_usage)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["deepseek"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="vera",
    )
    assert out.cost_usd == 0.70
    assert recorded_costs == [0.70]


async def test_run_chat_passes_cache_tokens_to_record_usage_and_outcome(monkeypatch):
    """cache_read_tokens/cache_write_tokens from call_llm's meta must reach
    both record_usage (usage_log persistence) and the returned ChatOutcome
    (API response) — this was the exact gap that left them computed-and-discarded."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    recorded: dict = {}
    fake_key = SimpleNamespace(id=3, label="k", tier="paid", provider="anthropic",
                                token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        return fake_key

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        return "hello", {
            "model": "anthropic/claude-sonnet-5", "tokens_in": 10_000,
            "tokens_out": 500, "cost_usd": 0.013, "latency_ms": 300,
            "cache_read_tokens": 9_000, "cache_write_tokens": 0,
        }

    def fake_record_usage(**kw):
        recorded["cache_read_tokens"] = kw["cache_read_tokens"]
        recorded["cache_write_tokens"] = kw["cache_write_tokens"]
        return _noop()

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "check_caps", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", fake_record_usage)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["anthropic"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan2"), capability="chat:smart",
        messages=[{"role": "system", "content": "kb"}, {"role": "user", "content": "hi"}],
        model=None, max_tokens=128, temperature=0.7, response_format=None,
        workflow="coach",
    )
    assert out.cache_read_tokens == 9_000
    assert out.cache_write_tokens == 0
    assert recorded == {"cache_read_tokens": 9_000, "cache_write_tokens": 0}


async def test_run_chat_defaults_cache_tokens_when_meta_omits_them(monkeypatch):
    """Providers that never populate cache_read_tokens/cache_write_tokens in
    meta (everything except anthropic) must not crash record_usage/Outcome —
    .get() default, not a required key."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    fake_key = SimpleNamespace(id=4, label="k", tier="free", provider="cerebras",
                                token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        return fake_key

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        return "hello", {"model": "cerebras/gpt-oss-120b", "tokens_in": 100,
                          "tokens_out": 50, "cost_usd": 0.0, "latency_ms": 100}

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "check_caps", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["cerebras"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="vera",
    )
    assert out.cache_read_tokens == 0
    assert out.cache_write_tokens == 0
