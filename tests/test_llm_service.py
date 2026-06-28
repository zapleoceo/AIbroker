"""services/llm_service — provider-error classification (the DRY classifier)."""
from __future__ import annotations

from aibroker.services.llm_service import classify_provider_error


def test_classify_rate_limit():
    assert classify_provider_error(RuntimeError("429 Too Many Requests")) == "rate_limit"
    assert classify_provider_error(Exception("provider rate_limit exceeded")) == "rate_limit"


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
