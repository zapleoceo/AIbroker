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
