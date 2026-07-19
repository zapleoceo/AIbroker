"""services/llm_service — provider-error classification (the DRY classifier)."""
from __future__ import annotations

import pytest

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


def test_classify_cohere_trial_quota_mislabeled_by_litellm():
    """REGRESSION (2026-07-03): LiteLLM 1.89.3 maps cohere's 429 quota
    response to APIConnectionError instead of RateLimitError (confirmed live
    against a real exhausted key: exception class APIConnectionError,
    status_code=500 — both wrong). The message body's 'rate limits' (with a
    space) doesn't match 'ratelimit'/'rate_limit' above, so this fell through
    to generic 'error' — _penalize does nothing for 'error' (no cooldown, no
    mark_dead), so the exhausted key got retried on every pick with zero
    backoff. Must classify as rate_limit regardless of the wrong exception
    class, purely from the message body."""
    real_message = (
        'litellm.APIConnectionError: Cohere_chatException - {"id":"x",'
        '"message":"You are using a Trial key, which is limited to 1000 '
        "API calls / month. You can continue to use the Trial key for free "
        'or upgrade to a Production key with higher rate limits at '
        'https://dashboard.cohere.com..."}'
    )
    assert classify_provider_error(RuntimeError(real_message)) == "rate_limit"


def test_classify_auth():
    assert classify_provider_error(RuntimeError("401 Unauthorized")) == "auth"
    assert classify_provider_error(Exception("403 forbidden")) == "auth"
    assert classify_provider_error(Exception("invalid auth token")) == "auth"


def test_classify_credits_depleted_429_is_auth_not_rate_limit():
    """REGRESSION (2026-07-10): a PAID Gemini key out of money returns HTTP 429
    'Your prepayment credits are depleted' — a billing/auth state, not a
    throttle. The generic 429/rate-limit signs matched it first and cooled it on
    a short cycle (churn) instead of mark_dead. Billing exhaustion is now checked
    BEFORE the rate-limit signs → 'auth' → mark_dead; the monitor revives it once
    topped up."""
    msg = ('litellm.RateLimitError: GeminiException - {"error": {"code": 429, '
           '"message": "Your prepayment credits are depleted. Please go to AI Studio"}}')
    assert classify_provider_error(RuntimeError(msg), provider="gemini") == "auth"
    assert classify_provider_error(RuntimeError("429: insufficient balance")) == "auth"


def test_classify_timeout_is_rate_limit():
    """REGRESSION (2026-07-07): our own call-timeout backstop
    (litellm_adapter.call_llm's asyncio.wait_for) raises a bare TimeoutError
    with no message — no string sign can ever match it, so it fell to generic
    'error' (no cooldown) and an overloaded key got hit again immediately with
    zero backoff. Confirmed live: a zai key was taking 90-180s per call, well
    past our timeout ceiling. A too-slow-right-now provider is transient
    overload, not a dead credential — rate_limit (cooldown), not auth/error."""
    assert classify_provider_error(TimeoutError()) == "rate_limit"
    assert classify_provider_error(TimeoutError(), "zai") == "rate_limit"


def test_is_timeout_classifies_backstop_and_provider_timeouts():
    """is_timeout distinguishes a held-then-timed-out call (our wait_for backstop
    or the provider's own timeout) from a pre-processing reject. Used to steepen
    the cooldown for a hanging key (a 60s-wasted timeout escalates faster than a
    0s-wasted 429); no longer drives billing (answerless timeouts book $0 as of
    2026-07-16)."""
    from aibroker.providers.provider_errors import is_timeout

    class _LiteLLMTimeout(Exception):
        pass
    _LiteLLMTimeout.__name__ = "Timeout"

    assert is_timeout(TimeoutError())
    assert is_timeout(_LiteLLMTimeout())          # litellm.Timeout by class name
    assert not is_timeout(RuntimeError("429 rate limit"))
    assert not is_timeout(Exception("401 unauthorized"))


def test_classify_anthropic_credit_balance_exhausted():
    """REGRESSION (2026-07-05): confirmed live — Anthropic's 'default' key was
    failing ~2743 times/day with this exact message (no '401'/'403'/'auth'
    substring), classified as generic 'error' — no mark_dead, so it kept
    getting picked and kept failing at zero cost to the credential but real
    waste on every request whose chain reached anthropic. This is a billing
    problem, not transient — must be 'auth' so mark_dead stops real traffic;
    the monitor's own probe still checks independently and auto-revives it
    once credits are topped up."""
    real_message = (
        'litellm.BadRequestError: AnthropicException - {"type":"error","error":'
        '{"type":"invalid_request_error","message":"Your credit balance is too '
        'low to access the Anthropic API. Please go to Plans & Billing to '
        'upgrade or purchase credits."},"request_id":"req_011CciYm3rbwdsviiFJD2YLt"}'
    )
    assert classify_provider_error(RuntimeError(real_message)) == "auth"


def test_classify_zai_invalid_api_parameter():
    """REGRESSION (2026-07-07): confirmed live during a real incident (cerebras/
    groq daily quota exhaustion overflowed traffic onto zai) — key 'eatmeat' hit
    this exact message on 3141 of ~3189 attempts in 30 min (98.5%), while every
    other zai key on the same account type/model succeeded normally. Isolated
    to one key, a persistent config problem not a shared outage — was generic
    'error' (no mark_dead), hammered with zero backoff on every pick."""
    real_message = "litellm.BadRequestError: ZaiException - Invalid API parameter, please check the documentation."
    # Provider-scoped: 'auth' for zai, but must NOT penalise another provider's
    # key if a request-construction bug elicits the same string elsewhere.
    assert classify_provider_error(RuntimeError(real_message), "zai") == "auth"
    assert classify_provider_error(RuntimeError(real_message), "deepseek") == "error"
    assert classify_provider_error(RuntimeError(real_message)) == "error"


def test_classify_deepseek_response_format_unavailable():
    """REGRESSION (2026-07-05): confirmed live — DeepSeek's 'This response_format
    type is unavailable now' hit every deepseek key identically (veranda,
    eatmeat, levaromat, demoniwwwe, zapleosoft, itstep — not one bad key, a
    provider-side feature outage), ~2510 wasted attempts/day. No '429'/
    'rate_limit' substring, so it fell to generic 'error' (no cooldown) and
    every triage call re-hit the same guaranteed failure on the next key pick
    with zero backoff. Not literally a rate limit, but rate_limit's cooldown
    (throttle, don't mark_dead — the credential itself is fine) is exactly
    the wanted behavior."""
    real_message = (
        'litellm.BadRequestError: DeepseekException - {"error":{"message":'
        '"This response_format type is unavailable now","type":'
        '"invalid_request_error","param":null,"code":"invalid_request_error"}}'
    )
    # Provider-scoped to deepseek (rate_limit → cooldown, don't mark_dead).
    assert classify_provider_error(RuntimeError(real_message), "deepseek") == "rate_limit"
    assert classify_provider_error(RuntimeError(real_message)) == "error"


def test_classify_voyage_no_payment_method():
    """REGRESSION (2026-07-07): confirmed live (docker logs, 24h window) —
    Voyage's 'no payment method on file' response hit every voyage key
    (lev/verandapay/eatmeat/itstep/...) dozens of times/day with zero
    backoff, since it fell to generic 'error' (no '429'/'401'/'403'/'auth'
    substring). The account isn't dead or unauthorized, just throttled to
    'reduced rate limits of 3 RPM and 10K TPM' — same bucket as any other
    rate limit: cooldown, don't mark_dead."""
    real_message = (
        'litellm.APIConnectionError: VoyageException - {"detail":"You have '
        "not yet added your payment method in the billing page and will "
        "have reduced rate limits of 3 RPM and 10K TPM. To unlock our "
        "standard rate limits, please add a payment method in the billing "
        'page for the appropriate organization in the user dashboard '
        '(https://dashboard.voyageai.com/)."}'
    )
    # Provider-scoped to voyage.
    assert classify_provider_error(RuntimeError(real_message), "voyage") == "rate_limit"
    assert classify_provider_error(RuntimeError(real_message)) == "error"


def test_classify_mistral_unauthorized_is_monthly_rate_limit():
    """mistral's bare 401 'Unauthorized' on our accounts is monthly Vibe-plan
    exhaustion, not a revoked key — must classify as rate_limit (→ cooled to
    next month, key stays alive), NOT auth (→ mark_dead). Provider-scoped so a
    genuine 401 from another provider still marks that key dead."""
    real = 'litellm.AuthenticationError: MistralException - {"detail":"Unauthorized"}'
    assert classify_provider_error(RuntimeError(real), "mistral") == "rate_limit"
    # Same 'Unauthorized' from another provider is still a dead-key auth verdict.
    assert classify_provider_error(RuntimeError(real), "openai") == "auth"


def test_classify_generic_error():
    assert classify_provider_error(RuntimeError("boom")) == "error"
    assert classify_provider_error(ValueError("connection reset by peer")) == "error"


# ─── model-unavailable (404) detection ───────────────────────────────────────


def test_is_model_unavailable_by_signature():
    """A vanished/unprovisioned MODEL (not a dead key, not a rate limit).
    Confirmed live 2026-07-10: nvidia kimi-k2.6 → 404 'Function not found for
    account' ~30x/hr. Must be detected so run_chat breaks to the next provider
    WITHOUT penalizing the key (its other models still work)."""
    from aibroker.providers.provider_errors import is_model_unavailable

    real = ("litellm.NotFoundError: Nvidia_nimException - Error code: 404 - "
            "{'status': 404, 'detail': \"Function '23d4': Not found for account 'kgN3'\"}")
    assert is_model_unavailable(RuntimeError(real)) is True
    assert is_model_unavailable(RuntimeError("model_not_found")) is True
    assert is_model_unavailable(RuntimeError("The model does not exist")) is True
    # a plain rate limit / generic error is NOT model-unavailable
    assert is_model_unavailable(RuntimeError("429 rate limit")) is False
    assert is_model_unavailable(RuntimeError("boom")) is False


def test_is_model_unavailable_by_exception_type():
    """litellm raises NotFoundError as its own class — detect by type name too,
    independent of the message wording."""
    from aibroker.providers.provider_errors import is_model_unavailable

    class NotFoundError(Exception):
        pass

    assert is_model_unavailable(NotFoundError("anything")) is True


async def test_run_chat_model_unavailable_skips_provider_without_penalty(monkeypatch):
    """A 404 model-gone breaks to the NEXT provider and does NOT cooldown/
    mark_dead the key (the key's other models still work)."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    penalized: list[int] = []
    tried: list[str] = []

    async def fake_pick(provider, scope, **kw):
        return SimpleNamespace(id=1, label="k", tier="free", provider=provider,
                                token_encrypted="x", account_id=None)

    async def fake_call_llm(**kw):
        tried.append(kw["model"])
        if kw["model"].startswith("dead/"):
            raise RuntimeError("404 - Function 'x': Not found for account 'y'")
        return "hi", {"model": kw["model"], "tokens_in": 1, "tokens_out": 1,
                      "cost_usd": 0.0, "latency_ms": 5,
                      "cache_read_tokens": 0, "cache_write_tokens": 0}

    async def fake_penalize(k, e):
        penalized.append(k.id)
        return "error"

    async def fake_caps(**kw):
        return None

    async def fake_record(**kw):
        return 1

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "_penalize", fake_penalize)
    monkeypatch.setattr(svc, "record_usage", fake_record)
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "estimate_llm_cost", lambda *a, **k: 0.0)
    monkeypatch.setattr(svc, "model_for",
                        lambda p, c: "dead/model" if p == "deadprov" else "good/model")
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["deadprov", "goodprov"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=4, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=64, temperature=0.7, response_format=None, workflow="x",
    )
    assert out is not None
    assert out.provider == "goodprov"          # fell through to next provider
    assert penalized == []                      # key was NOT penalized on the 404
    assert tried == ["dead/model", "good/model"]  # broke after ONE dead try, not N keys


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

    fake_key = SimpleNamespace(id=1, label="k", tier="free", provider="cerebras",
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
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
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
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
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


# ─── cap-block honesty (visible CapBlock row + BUDGET_EXHAUSTED abort) ────────


def _cap_block_env(monkeypatch, kind: str, chain: list[str]):
    """Wire run_chat so every paid pick's reserve_cost raises a `kind` cap block.
    Returns (picks, recorded) collected during the walk."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc
    from aibroker.routing import CostGuardError

    picks: list[str] = []
    recorded: list[dict] = []

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        return SimpleNamespace(id=1, label="k", tier="paid", provider=provider,
                                token_encrypted="x", account_id=None)

    async def fake_reserve(**kw):
        raise CostGuardError(kind, 0.5, 0.5, 0.001)

    async def fake_record(**kw):
        recorded.append(kw)
        return 1

    async def fake_audit(**kw):
        return None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_reserve)
    monkeypatch.setattr(svc, "record_usage", fake_record)
    monkeypatch.setattr(svc, "audit", fake_audit)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "estimate_llm_cost", lambda *a, **k: 0.001)
    monkeypatch.setattr(svc, "chain_for", lambda cap: list(chain))
    return svc, picks, recorded


async def test_cap_block_books_visible_capblock_row(monkeypatch):
    """A cap-blocked pick writes a usage_log row (status=error, CapBlock, 402,
    $0) so the block is visible in the usage view — not audit_log only (prod
    2026-07-16: ~8800 cap-blocked picks/2h vanished, jobs died invisibly)."""
    from types import SimpleNamespace

    svc, _picks, recorded = _cap_block_env(monkeypatch, "project", ["deepseek"])
    await svc.run_chat(
        project=SimpleNamespace(id=7, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=64, temperature=0.7, response_format=None, workflow="w",
    )
    assert recorded[0]["error_kind"] == "CapBlock"
    assert recorded[0]["http_status"] == 402
    assert recorded[0]["cost_usd"] == 0.0
    assert recorded[0]["status"] == "error"


async def test_project_cap_downgrades_walk_to_free_not_abort(monkeypatch):
    """Non-paid_only: a project cap blocks only PAID keys ($0 free keys are
    exempt in cost_guard), so run_chat must NOT abort — it downgrades the rest
    of the walk to free-only and tries providers past the paid tail (which
    deprioritize_for_json sinks BELOW it on JSON requests). Returns retryable
    None, not BUDGET_EXHAUSTED. Regression 2026-07-17: the abort starved 14 idle
    cerebras keys once Stepan's $0.50 paid cap filled."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc
    from aibroker.routing import CostGuardError

    picks: list[tuple[str, str | None]] = []

    async def fake_pick(provider, *, scope=None, require_tier=None, **kw):
        picks.append((provider, require_tier))
        if provider == "deepseek" and require_tier != "free":
            return SimpleNamespace(id=1, label="k", tier="paid", provider=provider,
                                    token_encrypted="x", account_id=None)
        return None  # free tail momentarily empty → walk ends retryable

    async def fake_reserve(**kw):
        raise CostGuardError("project", 0.5, 0.5, 0.001)

    async def fake_record(**kw):
        return 1

    async def fake_audit(**kw):
        return None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_reserve)
    monkeypatch.setattr(svc, "record_usage", fake_record)
    monkeypatch.setattr(svc, "audit", fake_audit)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "estimate_llm_cost", lambda *a, **k: 0.001)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["deepseek", "cerebras"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=7, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=64, temperature=0.7, response_format=None, workflow="w",
    )
    assert out is None                        # retryable — NOT BUDGET_EXHAUSTED
    assert ("deepseek", None) in picks        # paid tail tried, cap-blocked
    assert ("cerebras", "free") in picks      # walk downgraded + continued


async def test_paid_only_project_cap_returns_budget_exhausted(monkeypatch):
    """paid_only (the job queue's final-retry escalation) has NO free fallback,
    so a project cap there returns BUDGET_EXHAUSTED — the job fails honestly
    instead of retrying budget it can't create."""
    from types import SimpleNamespace

    svc, picks, _rec = _cap_block_env(
        monkeypatch, "project", ["deepseek", "anthropic", "openai"])
    out = await svc.run_chat(
        project=SimpleNamespace(id=7, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=64, temperature=0.7, response_format=None, workflow="w",
        paid_only=True,
    )
    assert out is svc.BUDGET_EXHAUSTED
    assert picks == ["deepseek"]           # aborted — anthropic/openai untried


async def test_per_key_cap_advances_to_next_provider(monkeypatch):
    """A per-key cap is LOCAL — other providers may still have room, so the walk
    continues to the next provider (returns None only after all are tried)."""
    from types import SimpleNamespace

    svc, picks, _rec = _cap_block_env(
        monkeypatch, "api_key", ["deepseek", "anthropic"])
    out = await svc.run_chat(
        project=SimpleNamespace(id=7, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=64, temperature=0.7, response_format=None, workflow="w",
    )
    assert out is None                     # no capacity anywhere, but honest None
    assert picks == ["deepseek", "anthropic"]  # advanced past the per-key block


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
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
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


async def test_run_chat_empty_body_retries_same_provider(monkeypatch):
    """REGRESSION (2026-07-10): a blank/whitespace body is a TRANSIENT provider
    throttle (deepseek json_object intermittently returns an empty string on
    large prompts under load), not a model JSON defect. run_chat must RETRY the
    same provider's next key — which almost always returns valid JSON — instead
    of skipping the whole provider like it does for genuinely malformed JSON."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []
    calls = {"n": 0}
    k1 = SimpleNamespace(id=1, label="a", tier="paid", provider="deepseek", token_encrypted="x")
    k2 = SimpleNamespace(id=2, label="b", tier="paid", provider="deepseek", token_encrypted="x")
    ds = iter([k1, k2])

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        return next(ds, None) if provider == "deepseek" else None

    async def fake_noop(**kw):
        return None

    async def fake_call_llm(**kw):
        calls["n"] += 1
        body = "   \n  " if calls["n"] == 1 else '{"ok": true}'  # empty first, valid on retry
        return body, {"model": kw["model"], "tokens_in": 100, "tokens_out": 50,
                      "cost_usd": 0.0, "latency_ms": 100,
                      "cache_read_tokens": 0, "cache_write_tokens": 0}

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_noop)
    monkeypatch.setattr(svc, "release_cost", fake_noop)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["deepseek"])
    monkeypatch.setattr(svc, "_max_keys", lambda p: 3)
    monkeypatch.setattr(svc, "deprioritize_for_json", lambda c: c)

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan"), capability="chat:smart",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7,
        response_format={"type": "json_object"}, workflow=None,
    )
    assert picks.count("deepseek") == 2   # retried the SAME provider, not skipped
    assert out.text == '{"ok": true}'     # got valid JSON on the retry


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


# ─── wall-clock gate (no double-execution) ───────────────────────────────────


async def test_run_chat_wall_deadline_stops_starting_attempts(monkeypatch):
    """A non-deep walk that outlasts _CHAT_WALL_DEADLINE_S stops starting NEW
    attempts and returns None, so the job finishes before job_queue's 25-min
    stale-reclaim window could re-execute it. The gate is checked between
    attempts only — it never aborts an in-flight call."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []
    clock = {"t": 1000.0}

    def fake_now() -> float:
        return clock["t"]

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        clock["t"] += 10 * 60      # each attempt burns 10 wall-clock minutes
        # no key returned → run_chat walks to the next provider

    monkeypatch.setattr(svc, "_now", fake_now)
    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["a", "b", "c", "d"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=64, temperature=0.7, response_format=None, workflow="w",
    )
    assert out is None
    # deadline = 1000 + 1080 = 2080. pick a@1000→t1600, b@1600→t2200,
    # then 2200 >= 2080 → stop before c/d.
    assert picks == ["a", "b"]


async def test_run_chat_deep_has_no_wall_deadline(monkeypatch):
    """chat:deep is exempt — a legitimately long (~19min) single call must not
    be gated. The deadline branch is skipped even when the clock is far ahead."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []

    def fake_now() -> float:
        return 10 ** 9        # way past any deadline

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)      # no key returned → walk continues

    monkeypatch.setattr(svc, "_now", fake_now)
    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["nvidia"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan"), capability="chat:deep",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=64, temperature=0.7, response_format=None, workflow="w",
    )
    assert out is None
    assert picks == ["nvidia"]   # attempted despite the clock — no gate


# ─── translate exact-match cache ─────────────────────────────────────────────


async def test_run_chat_translate_cache_hit_skips_providers(monkeypatch):
    """A cached translate response returns immediately — no provider is picked."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc
    from aibroker.services import response_cache

    response_cache.clear()
    picked = {"n": 0}

    async def fake_pick(provider, scope, **kw):
        picked["n"] += 1

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["mistral"])

    msgs = [{"role": "user", "content": "Halo"}]
    response_cache.put("translate", msgs, "Hello", model=None,
                        max_tokens=128, temperature=0.7)

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="translate",
        messages=msgs, model=None, max_tokens=128, temperature=0.7,
        response_format=None, workflow="translate",
    )
    response_cache.clear()
    assert out.text == "Hello"
    assert out.provider == "cache"
    assert picked["n"] == 0            # never touched a provider


async def test_run_chat_translate_caches_success(monkeypatch):
    """A successful translate response is stored for the next identical call."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc
    from aibroker.services import response_cache

    response_cache.clear()
    key = SimpleNamespace(id=1, label="m", tier="free", provider="mistral",
                           token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        return key

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        return "Hello", {"model": "mistral/mistral-small-latest", "tokens_in": 10,
                          "tokens_out": 5, "cost_usd": 0.0, "latency_ms": 100,
                          "cache_read_tokens": 0, "cache_write_tokens": 0}

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["mistral"])

    msgs = [{"role": "user", "content": "Terima kasih"}]
    await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="translate",
        messages=msgs, model=None, max_tokens=128, temperature=0.7,
        response_format=None, workflow="translate",
    )
    assert response_cache.get("translate", msgs, model=None,
                               max_tokens=128, temperature=0.7) == "Hello"
    response_cache.clear()


async def test_run_chat_does_not_cache_chat_capability(monkeypatch):
    """chat/* must never be cached — non-deterministic."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc
    from aibroker.services import response_cache

    response_cache.clear()
    key = SimpleNamespace(id=1, label="m", tier="free", provider="mistral",
                           token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        return key

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        return "hi", {"model": "m", "tokens_in": 10, "tokens_out": 5,
                      "cost_usd": 0.0, "latency_ms": 100,
                      "cache_read_tokens": 0, "cache_write_tokens": 0}

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["mistral"])

    msgs = [{"role": "user", "content": "hi"}]
    await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="chat:fast",
        messages=msgs, model=None, max_tokens=128, temperature=0.7,
        response_format=None, workflow="x",
    )
    assert response_cache.get("chat:fast", msgs, model=None,
                               max_tokens=128, temperature=0.7) is None


# ─── size-filter skipped for small prompts (#2) ──────────────────────────────


async def test_run_chat_skips_size_filter_for_small_prompt(monkeypatch):
    """A sub-ceiling prompt fits every provider, so learned_ceilings() (a DB
    round-trip) must be skipped on the high-volume small-prompt path."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    called = {"learned": 0}

    async def fake_learned():
        called["learned"] += 1
        return {}

    async def fake_pick(provider, scope, **kw):
        return None

    monkeypatch.setattr(svc, "learned_ceilings", fake_learned)
    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["mistral"])

    await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="chat:fast",
        messages=[{"role": "user", "content": "short prompt"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="x",
    )
    assert called["learned"] == 0     # skipped: prompt < MIN_LEARNABLE_CEILING


async def test_run_chat_runs_size_filter_for_large_prompt(monkeypatch):
    """A prompt above the floor still consults learned_ceilings()."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    called = {"learned": 0}

    async def fake_learned():
        called["learned"] += 1
        return {}

    async def fake_pick(provider, scope, **kw):
        return None

    monkeypatch.setattr(svc, "learned_ceilings", fake_learned)
    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["mistral"])

    big = [{"role": "user", "content": "x" * 40_000}]   # ~10k tokens > floor
    await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="chat:smart",
        messages=big, model=None, max_tokens=128, temperature=0.7,
        response_format=None, workflow="x",
    )
    assert called["learned"] == 1


# ─── per-request dynamic attempt budget ──────────────────────────────────────


def test_attempt_budget_is_key_sum_bounded_by_backstop():
    """Budget = sum of per-provider key allowances, capped at the runaway
    backstop. A short chain gets its full key sum; a long one is clamped."""
    import aibroker.services.llm_service as svc

    # 3 default providers (5 keys each) → 15, under the backstop.
    assert svc._attempt_budget(["a", "b", "c"]) == 15
    # gemini/cerebras allowance is lower (3) — summed exactly, not defaulted.
    assert svc._attempt_budget(["gemini", "cerebras", "groq"]) == 3 + 3 + 5
    # 20 default providers → 100 key-attempts, clamped to the backstop.
    assert svc._attempt_budget([f"p{i}" for i in range(20)]) == svc._MAX_ATTEMPTS_ABS


def test_call_timeout_uniform_across_capabilities_except_deep():
    """REGRESSION (2026-07-07): per-call timeout raised 45s -> 60s, applied
    uniformly to every key/provider via a single constant — chat:deep is the
    sole capability-specific exception (nemotron legitimately runs minutes as
    an async job)."""
    import aibroker.services.llm_service as svc

    assert svc._CALL_TIMEOUT_S == 60.0
    for cap in ("chat:fast", "chat:smart", "chat:code", "chat:edit", "structured"):
        assert svc._call_timeout(cap) == 60.0
    assert svc._call_timeout("chat:deep") == svc._DEEP_CALL_TIMEOUT_S
    assert svc._call_timeout("chat:deep") != 60.0


async def test_run_chat_stops_at_absolute_backstop(monkeypatch):
    """A pathologically long chain of failing providers stops at the absolute
    backstop, not walking every provider × every key unbounded."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    attempts = {"n": 0}

    async def fake_pick(provider, scope, **kw):
        return SimpleNamespace(id=1, label="k", tier="free", provider=provider,
                                token_encrypted="x")

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        attempts["n"] += 1
        raise RuntimeError("boom generic error")

    async def fake_penalize(k, e):
        return "error"

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "_penalize", fake_penalize)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    # 20-provider chain (default 5 keys each = 100 attempts without the cap).
    monkeypatch.setattr(svc, "chain_for",
                         lambda cap: [f"p{i}" for i in range(20)])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="chat:smart",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="x",
    )
    assert out is None
    assert attempts["n"] == svc._MAX_ATTEMPTS_ABS


async def test_run_chat_reaches_paid_tail_when_free_providers_saturated(monkeypatch):
    """REGRESSION (2026-07-07 incident): with a flat cap of 12 and a 14-provider
    chain, the paid tail was never reached when the free head was saturated, so
    long dialogs 503'd. A saturated provider yields no key (0 attempts), so the
    dynamic budget must let the chain fall through to the last provider and
    succeed there."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    # 13 saturated free providers (no key) + 1 paid tail that succeeds.
    chain = [f"free{i}" for i in range(13)] + ["paid_tail"]
    tried: list[str] = []

    async def fake_pick(provider, scope, **kw):
        if provider == "paid_tail":
            return SimpleNamespace(id=1, label="tail", tier="paid",
                                    provider=provider, token_encrypted="x")
        return None  # saturated — no key available

    async def fake_call_llm(**kw):
        tried.append(kw["model"])
        return "hello", {"model": kw["model"], "tokens_in": 1, "tokens_out": 1,
                         "cost_usd": 0.01, "latency_ms": 5,
                         "cache_read_tokens": 0, "cache_write_tokens": 0}

    async def fake_caps(**kw):
        return None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "estimate_llm_cost", lambda *a, **k: 0.0)

    async def fake_record(**kw):
        return 1

    monkeypatch.setattr(svc, "record_usage", fake_record)
    monkeypatch.setattr(svc, "chain_for", lambda cap: chain)

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="x",
    )
    assert out is not None
    assert out.provider == "paid_tail"
    assert tried == ["paid_tail/model"]


# ─── final-retry paid escalation (paid_only) ─────────────────────────────────


async def test_run_chat_paid_only_threads_require_tier_paid(monkeypatch):
    """paid_only=True must demand a paid-tier key on EVERY pick_and_reserve —
    the job queue's final retry escalates to the paid tail instead of dying on
    a cooling free pool (2026-07-16: 148 jobs/h died 'no provider available'
    during a cerebras storm while the paid deepseek tail was healthy)."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    tiers: list[str | None] = []

    async def fake_pick(provider, scope, **kw):
        # Returns None: no paid key either → chain walks on, honest None.
        tiers.append(kw.get("require_tier"))

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["cerebras", "deepseek"])

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="x",
        paid_only=True,
    )
    assert out is None                    # all paid capped/dead → job errors as before
    assert tiers == ["paid", "paid"]      # every pick demanded the paid tier


async def test_run_chat_default_pick_has_no_tier_requirement(monkeypatch):
    """Without paid_only the walk must stay tier-agnostic (require_tier=None) —
    free keys remain first-class on every non-final attempt."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    tiers: list[str | None] = []

    async def fake_pick(provider, scope, **kw):
        tiers.append(kw.get("require_tier"))

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["cerebras", "deepseek"])

    await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan"), capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None, workflow="x",
    )
    assert tiers == [None, None]


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

    free_key = SimpleNamespace(tier="free", provider="gemini")
    paid_key = SimpleNamespace(tier="paid", provider="anthropic")
    meta = {"cost_usd": 2.80}

    assert _billed_cost(free_key, meta) == 0.0
    assert _billed_cost(paid_key, meta) == 2.80


def test_billed_cost_voyage_free_tier_is_zero_on_voyage4():
    """REGRESSION (2026-07-07): a voyage carve-out here once billed real cost
    unconditionally, because voyage-3 had a ZERO free-token allocation. We
    moved the default embedding model to voyage-4 (200M free tokens/month,
    genuinely $0 under our run-rate), so a voyage free-tier key is now $0 like
    any other free key — the carve-out is gone. (voyage-4 now HAS a price —
    litellm_adapter registers $0.06/M at import, 2026-07-16 — so this tier
    check is exactly what keeps a free voyage key at $0.)"""
    from types import SimpleNamespace

    from aibroker.services.llm_service import _billed_cost

    free_voyage_key = SimpleNamespace(tier="free", provider="voyage")
    paid_voyage_key = SimpleNamespace(tier="paid", provider="voyage")
    meta = {"cost_usd": 0.51}

    assert _billed_cost(free_voyage_key, meta) == 0.0
    assert _billed_cost(paid_voyage_key, meta) == 0.51


# ─── _correct_local_transcript — local ASR proofreading pass ────────────────


def _fake_project():
    from aibroker.db.models import ProjectRow
    return ProjectRow(id=1, name="x", project_key_hash="x", project_key_prefix="x",
                       allowed_scopes=["llm:audio"], is_active=True, notes="")


async def test_correct_local_transcript_returns_corrected_text():
    from unittest.mock import AsyncMock, patch

    from aibroker.services.llm_service import ChatOutcome, _correct_local_transcript

    outcome = ChatOutcome(
        text="Привет, это голосовое сообщение.", provider="gemini", model="gemini-2.5-flash",
        tokens_in=10, tokens_out=8, cost_usd=0.0, latency_ms=200,
        key_label="k", request_id=5,
    )
    with patch("aibroker.services.llm_service.run_chat", AsyncMock(return_value=outcome)):
        result = await _correct_local_transcript(
            project=_fake_project(), text="привет ето галасовое сообщение", workflow="media",
        )
    assert result == "Привет, это голосовое сообщение."


async def test_correct_local_transcript_falls_back_when_no_provider():
    """No chat:fast capacity right now — the raw (uncorrected) transcript is
    still a working answer; don't turn that into a failure."""
    from unittest.mock import AsyncMock, patch

    from aibroker.services.llm_service import _correct_local_transcript

    with patch("aibroker.services.llm_service.run_chat", AsyncMock(return_value=None)):
        result = await _correct_local_transcript(
            project=_fake_project(), text="raw transcript", workflow=None,
        )
    assert result == "raw transcript"


async def test_correct_local_transcript_falls_back_on_budget_exhausted():
    from unittest.mock import AsyncMock, patch

    from aibroker.services.llm_service import BUDGET_EXHAUSTED, _correct_local_transcript

    with patch("aibroker.services.llm_service.run_chat", AsyncMock(return_value=BUDGET_EXHAUSTED)):
        result = await _correct_local_transcript(
            project=_fake_project(), text="raw transcript", workflow=None,
        )
    assert result == "raw transcript"


async def test_correct_local_transcript_falls_back_on_exception():
    """A proofreading failure must never sink an already-working transcript."""
    from unittest.mock import patch

    from aibroker.services.llm_service import _correct_local_transcript

    async def boom(*a, **kw):
        raise RuntimeError("boom")

    with patch("aibroker.services.llm_service.run_chat", side_effect=boom):
        result = await _correct_local_transcript(
            project=_fake_project(), text="raw transcript", workflow=None,
        )
    assert result == "raw transcript"


async def test_correct_local_transcript_skips_empty_text():
    from unittest.mock import AsyncMock, patch

    from aibroker.services.llm_service import _correct_local_transcript

    mock_run_chat = AsyncMock()
    with patch("aibroker.services.llm_service.run_chat", mock_run_chat):
        result = await _correct_local_transcript(project=_fake_project(), text="   ", workflow=None)
    assert result == "   "
    mock_run_chat.assert_not_called()


async def test_correct_local_transcript_keeps_raw_when_correction_truncated():
    """A correction far shorter than the raw was truncated at max_tokens — a
    COMPLETE raw transcript beats a cut-off one, so the tail of a long voice
    note is never lost."""
    from unittest.mock import AsyncMock, patch

    from aibroker.services.llm_service import ChatOutcome, _correct_local_transcript

    raw = "это длинное голосовое сообщение " * 40   # long
    truncated = ChatOutcome(
        text="это длинное", provider="cerebras", model="m", tokens_in=1,
        tokens_out=1, cost_usd=0.0, latency_ms=1, key_label="k", request_id=1)
    with patch("aibroker.services.llm_service.run_chat", AsyncMock(return_value=truncated)):
        result = await _correct_local_transcript(
            project=_fake_project(), text=raw, workflow=None)
    assert result == raw   # kept the full raw, not the truncated correction


async def test_correct_local_transcript_scales_max_tokens_to_length():
    """Fixed 800 truncated long notes — the proofread budget now scales with
    the input (floor 800, capped)."""
    from unittest.mock import AsyncMock, patch

    from aibroker.services.llm_service import (
        _LOCAL_ASR_CORRECTION_MAX_TOKENS,
        _LOCAL_ASR_CORRECTION_TOKEN_CAP,
        ChatOutcome,
        _correct_local_transcript,
    )

    ok = ChatOutcome(text="fine corrected text", provider="c", model="m", tokens_in=1,
                     tokens_out=1, cost_usd=0.0, latency_ms=1, key_label="k", request_id=1)
    short = AsyncMock(return_value=ok)
    with patch("aibroker.services.llm_service.run_chat", short):
        await _correct_local_transcript(project=_fake_project(), text="short one", workflow=None)
    assert short.call_args.kwargs["max_tokens"] == _LOCAL_ASR_CORRECTION_MAX_TOKENS

    longm = AsyncMock(return_value=ok)
    with patch("aibroker.services.llm_service.run_chat", longm):
        await _correct_local_transcript(
            project=_fake_project(), text="word " * 3000, workflow=None)
    assert longm.call_args.kwargs["max_tokens"] > _LOCAL_ASR_CORRECTION_MAX_TOKENS
    assert longm.call_args.kwargs["max_tokens"] <= _LOCAL_ASR_CORRECTION_TOKEN_CAP


async def test_run_transcribe_empty_local_escalates_never_dropped(monkeypatch):
    """The core 'never drop a voice' guarantee: an empty transcript from local
    (small model / VAD clip) must NOT be returned as a silent successful "" —
    it's booked as an error and the walk escalates to a reliable cloud whisper."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        if provider in ("local", "groq") and picks.count(provider) == 1:
            return SimpleNamespace(id=1, label="k", tier="free",
                                    token_encrypted="x", provider=provider)
        return None

    async def fake_transcribe(*, model, audio, filename, api_key):
        if model.startswith("local"):
            return "", {"latency_ms": 10}
        return "привет мир", {"latency_ms": 20}

    recorded: list[dict] = []

    async def fake_record(**kw):
        recorded.append(kw)
        return 7

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "transcribe", fake_transcribe)
    monkeypatch.setattr(svc, "record_usage", fake_record)
    monkeypatch.setattr(svc, "note_affinity_shared", _noop)
    monkeypatch.setattr(svc, "_handle_attempt_failure", _noop)
    monkeypatch.setattr(svc, "decrypt", lambda _x: "plain")

    out = await svc.run_transcribe(project=_fake_project(), audio=b"x",
                                    filename="a.ogg", workflow="media")
    assert out is not None
    assert out.provider == "groq"          # escalated past the empty local
    assert out.text == "привет мир"
    # local's empty booked as an error (visible), groq's result as ok
    assert any(r["provider"] == "local" and r["error_kind"] == "EmptyBody"
               and r["status"] == "error" for r in recorded)
    assert any(r["provider"] == "groq" and r["status"] == "ok" for r in recorded)


async def test_correct_local_transcript_tags_workflow_and_uses_chat_fast():
    """Correction calls are tagged distinctly so the dashboard's per-project
    workflow breakdown doesn't fold them into the caller's own workflow."""
    from unittest.mock import AsyncMock, patch

    from aibroker.services.llm_service import ChatOutcome, _correct_local_transcript

    outcome = ChatOutcome(text="ok", provider="gemini", model="m", tokens_in=1, tokens_out=1,
                           cost_usd=0.0, latency_ms=1, key_label="k", request_id=1)
    mock_run_chat = AsyncMock(return_value=outcome)
    with patch("aibroker.services.llm_service.run_chat", mock_run_chat):
        await _correct_local_transcript(project=_fake_project(), text="raw", workflow="media")
    assert mock_run_chat.call_args.kwargs["workflow"] == "media+asr-correct"
    assert mock_run_chat.call_args.kwargs["capability"] == "chat:fast"

    mock_run_chat.reset_mock()
    with patch("aibroker.services.llm_service.run_chat", mock_run_chat):
        await _correct_local_transcript(project=_fake_project(), text="raw", workflow=None)
    assert mock_run_chat.call_args.kwargs["workflow"] == "asr-correct"


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
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
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
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
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
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
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
    monkeypatch.setattr(svc, "reserve_cost", fake_caps)
    monkeypatch.setattr(svc, "release_cost", fake_caps)
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


# ─── run_embed retries same-provider keys (never crosses provider) ──────────


async def test_run_embed_retries_next_key_on_transient_failure(monkeypatch):
    """A transient APIConnectionError on the first voyage key must fall
    through to a second voyage key — not raise immediately."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    keys = [
        SimpleNamespace(id=1, label="a", tier="free", provider="voyage",
                         token_encrypted="x"),
        SimpleNamespace(id=2, label="b", tier="free", provider="voyage",
                         token_encrypted="x"),
    ]
    picks: list[str] = []
    calls = {"n": 0}

    async def fake_pick(provider, scope, **kw):
        idx = len(picks)
        picks.append(provider)
        return keys[idx] if idx < len(keys) else None

    async def fake_penalize(key, exc):
        return "error"

    async def fake_embed(**kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("APIConnectionError: connection reset")
        return [[0.1, 0.2]], {"model": kw["model"], "tokens_in": 10,
                              "tokens_out": 0, "cost_usd": 0.0001, "latency_ms": 50}

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "_penalize", fake_penalize)
    monkeypatch.setattr(svc, "embed", fake_embed)
    monkeypatch.setattr(svc, "model_for", lambda p, c: "voyage/voyage-3")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())

    out = await svc.run_embed(
        project=SimpleNamespace(id=1, name="vera"), provider="voyage",
        inputs=["hello"], model=None, workflow="reindex",
    )
    assert out.embeddings == [[0.1, 0.2]]
    assert out.key_label == "b"           # succeeded on the SECOND key
    assert picks == ["voyage", "voyage"]  # both attempts stayed on voyage


async def test_run_embed_raises_after_exhausting_all_keys(monkeypatch):
    """Every key of the provider fails → EmbedFailed (502), not a silent
    cross-provider fallback."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    async def fake_pick(provider, scope, **kw):
        return SimpleNamespace(id=1, label="k", tier="free", provider=provider,
                                token_encrypted="x")

    async def fake_penalize(key, exc):
        return "error"

    async def fake_embed(**kw):
        raise RuntimeError("APIConnectionError: connection reset")

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "_penalize", fake_penalize)
    monkeypatch.setattr(svc, "embed", fake_embed)
    monkeypatch.setattr(svc, "model_for", lambda p, c: "voyage/voyage-3")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())

    with pytest.raises(svc.EmbedFailed):
        await svc.run_embed(
            project=SimpleNamespace(id=1, name="vera"), provider="voyage",
            inputs=["hello"], model=None, workflow="reindex",
        )


async def test_run_embed_never_falls_back_to_a_different_provider(monkeypatch):
    """Embeddings from different providers aren't interchangeable (different
    vector spaces/dimensionality) — a failing voyage key must never silently
    resolve via cohere. Only 'voyage' is ever picked."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picked_providers: set[str] = set()

    async def fake_pick(provider, scope, **kw):
        picked_providers.add(provider)
        return SimpleNamespace(id=1, label="k", tier="free", provider=provider,
                                token_encrypted="x")

    async def fake_penalize(key, exc):
        return "error"

    async def fake_embed(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "_penalize", fake_penalize)
    monkeypatch.setattr(svc, "embed", fake_embed)
    monkeypatch.setattr(svc, "model_for", lambda p, c: "voyage/voyage-3")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())

    with pytest.raises(svc.EmbedFailed):
        await svc.run_embed(
            project=SimpleNamespace(id=1, name="vera"), provider="voyage",
            inputs=["hello"], model=None, workflow="reindex",
        )
    assert picked_providers == {"voyage"}


async def test_run_embed_returns_none_when_no_key_available(monkeypatch):
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    async def fake_pick(provider, scope, **kw):
        return None

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)

    out = await svc.run_embed(
        project=SimpleNamespace(id=1, name="vera"), provider="voyage",
        inputs=["hello"], model=None, workflow="reindex",
    )
    assert out is None


async def test_run_embed_succeeds_first_try_records_usage(monkeypatch):
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    recorded = {}
    key = SimpleNamespace(id=5, label="only", tier="free", provider="voyage",
                           token_encrypted="x")

    async def fake_pick(provider, scope, **kw):
        return key

    async def fake_embed(**kw):
        return [[1.0, 2.0]], {"model": kw["model"], "tokens_in": 42,
                              "tokens_out": 0, "cost_usd": 0.0005, "latency_ms": 30}

    def fake_record_usage(**kw):
        recorded.update(kw)
        return _noop()

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "embed", fake_embed)
    monkeypatch.setattr(svc, "model_for", lambda p, c: "voyage/voyage-3")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", fake_record_usage)

    out = await svc.run_embed(
        project=SimpleNamespace(id=1, name="vera"), provider="voyage",
        inputs=["hello", "world"], model=None, workflow="reindex",
    )
    assert out.embeddings == [[1.0, 2.0]]
    assert out.tokens_in == 42
    assert recorded["status"] == "ok"
    assert recorded["tokens_in"] == 42


async def test_record_error_books_429_for_rate_limit(monkeypatch):
    """REGRESSION (2026-07-10): _record_error must book http_status=429 for a
    rate_limit — adaptive_cooldown counts recent `http_status = 429` rows to
    escalate backoff; with NULL the exponential step never fired."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc
    captured: dict = {}

    async def fake_record(**kw):
        captured.update(kw)

    monkeypatch.setattr(svc, "record_usage", fake_record)
    await svc._record_error(
        key=SimpleNamespace(id=1, provider="cerebras"), project=SimpleNamespace(id=2),
        provider="cerebras", model="m", capability="chat:fast", workflow=None,
        exc=RuntimeError("RateLimitError - Tokens per day limit exceeded"),
    )
    assert captured["http_status"] == 429
    assert captured["status"] == "error"


async def test_record_error_books_none_for_generic_error(monkeypatch):
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc
    captured: dict = {}

    async def fake_record(**kw):
        captured.update(kw)

    monkeypatch.setattr(svc, "record_usage", fake_record)
    await svc._record_error(
        key=SimpleNamespace(id=1, provider="x"), project=SimpleNamespace(id=2),
        provider="x", model="m", capability="chat:fast", workflow=None,
        exc=RuntimeError("some unclassifiable failure"),
    )
    assert captured["http_status"] is None


def test_classify_cloudflare_neurons_quota_is_rate_limit_not_auth():
    """REGRESSION (2026-07-12): cloudflare's 'daily free allocation of 10,000
    neurons' arrives wrapped in litellm.APIConnectionError; it fell through the
    sign tables and the key was marked DEAD for a daily quota that resets at
    00:00 UTC. Scoped to cloudflare so 'neurons' on another provider's message
    can't cool an unrelated healthy key."""
    exc = Exception(
        'litellm.APIConnectionError: CloudflareException - {"errors":[{"message":'
        '"AiError: you have used up your daily free allocation of 10,000 neurons, '
        "please upgrade to Cloudflare's Workers Paid plan\"}]}"
    )
    assert classify_provider_error(exc, "cloudflare") == "rate_limit"
    assert classify_provider_error(exc, "groq") == "error"  # scoped, not global


# ─── answerless timeouts must NOT consume the admission budget ───────────────


async def test_timeout_attempt_not_billed_to_admission(monkeypatch):
    """REVERSAL (2026-07-16): a paid timeout used to book the reserved estimate
    so the per-key cost cap saw upstream spend (the 2026-07-12 $122 gemini gap).
    But with a $0.50/day cap, a few ANSWERLESS timeouts booked at the estimate
    burned the whole day's ADMISSION budget on ZERO answers. Now a failed
    attempt always books $0 — the reservation is released and real timeout spend
    is reconciled off the provider invoice, not the admission counter."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    booked: dict = {}
    released: dict = {}

    async def fake_record_usage(**kw):
        booked.update(kw)
        return 1

    async def fake_reserve(**kw):
        return None

    async def fake_release(**kw):
        released.update(kw)

    async def fake_call_llm(**kw):
        raise TimeoutError()

    async def fake_penalize(k, e):
        return "rate_limit"

    monkeypatch.setattr(svc, "record_usage", fake_record_usage)
    monkeypatch.setattr(svc, "reserve_cost", fake_reserve)
    monkeypatch.setattr(svc, "release_cost", fake_release)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "_penalize", fake_penalize)
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "estimate_llm_cost", lambda *a, **k: 0.0123)

    key = SimpleNamespace(id=1, provider="gemini", label="g", tier="paid",
                          token_encrypted="x", account_id=None)
    project = SimpleNamespace(id=2, name="stepan")
    flow, outcome = await svc._run_attempt(
        key=key, project=project, provider="gemini",
        use_model="gemini/gemini-2.5-flash", capability="chat:fast",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=None,
        workflow=None, est_tokens=1000, call_timeout=60.0,
    )
    assert flow is svc._Flow.NEXT_KEY
    assert outcome is None
    assert booked["cost_usd"] == 0.0                       # $0 to the admission cap
    assert released["estimated_cost"] == pytest.approx(0.0123)  # reservation unwound


async def test_run_chat_empty_body_capped_then_next_provider(monkeypatch):
    """A provider that returns empty bodies DETERMINISTICALLY (deepseek
    json_object on a 30k prompt) must burn at most _MAX_EMPTY_RETRIES + 1 keys
    before the chain breaks to the next provider — not every key it has."""
    from types import SimpleNamespace

    import aibroker.services.llm_service as svc

    picks: list[str] = []

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        return SimpleNamespace(id=len(picks), label=f"k{len(picks)}", tier="paid",
                                provider=provider, token_encrypted="x")

    async def fake_noop(**kw):
        return None

    async def fake_call_llm(**kw):
        body = "   " if kw["model"].startswith("deepseek") else '{"ok": true}'
        return body, {"model": kw["model"], "tokens_in": 100, "tokens_out": 0,
                      "cost_usd": 0.0, "latency_ms": 50,
                      "cache_read_tokens": 0, "cache_write_tokens": 0}

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "reserve_cost", fake_noop)
    monkeypatch.setattr(svc, "release_cost", fake_noop)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr(svc, "decrypt", lambda t: "plain")
    monkeypatch.setattr(svc, "record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "estimate_llm_cost", lambda *a, **k: 0.0)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["deepseek", "gemini"])
    monkeypatch.setattr(svc, "deprioritize_for_json", lambda c: c)

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="stepan"), capability="chat:smart",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7,
        response_format={"type": "json_object"}, workflow=None,
    )
    assert picks.count("deepseek") == svc._MAX_EMPTY_RETRIES + 1  # capped, not 5
    assert out.provider == "gemini"
    assert out.text == '{"ok": true}'


# ─── _penalize — single-session penalty path (2026-07-16) ────────────────────


async def _seed_key(provider: str = "gemini"):
    from aibroker.crypto import encrypt
    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow

    async with get_session() as s:
        key = ApiKeyRow(provider=provider, label="pen", tier="free",
                        scopes=["llm:chat"], token_encrypted=encrypt("x"),
                        is_active=True, is_alive=True,
                        daily_limit=0, daily_used=0,
                        daily_cost_used_usd=0.0, monthly_cost_used_usd=0.0,
                        total_cost_usd=0.0, error_count=0, notes="")
        s.add(key)
        await s.flush()
        return key


async def test_penalize_rate_limit_opens_exactly_one_session(monkeypatch):
    """The whole penalty (adaptive 429 COUNT + cooldown UPDATE) lands in ONE
    session — it used to be one per statement, pure pool churn on a path that
    fires on every failed provider attempt."""
    from contextlib import asynccontextmanager

    import aibroker.db.engine as engine_mod
    import aibroker.routing.cooldown as cooldown_mod
    import aibroker.routing.selector as selector_mod
    import aibroker.services.llm_service as svc
    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow

    key = await _seed_key()
    opened = {"n": 0}
    real = engine_mod.get_session

    @asynccontextmanager
    async def counting():
        opened["n"] += 1
        async with real() as s:
            yield s

    monkeypatch.setattr(svc, "get_session", counting)
    monkeypatch.setattr(cooldown_mod, "get_session", counting)
    monkeypatch.setattr(selector_mod, "get_session", counting)

    kind = await svc._penalize(key, RuntimeError("429 Too Many Requests"))

    assert kind == "rate_limit"
    assert opened["n"] == 1
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key.id)
    assert row.cooldown_until is not None
    assert row.error_count == 1
    assert row.last_error == "429 Too Many Requests"


async def test_penalize_timeout_feeds_circuit_breaker():
    """A timeout penalty records the key in the selection-side circuit-breaker,
    so the selector can soft-skip a bulk-timing-out provider and won't re-pin
    cache-affinity to the hung key (2026-07-16 storm)."""
    import aibroker.services.llm_service as svc
    from aibroker.routing import circuit

    circuit.reset()
    key = await _seed_key()
    kind = await svc._penalize(key, TimeoutError())
    assert kind == "rate_limit"
    assert key.id in circuit.recent_timeout_key_ids()
    circuit.reset()


async def test_penalize_falls_back_to_flat_cooldown_when_resolver_fails(monkeypatch):
    """cooldown_until blowing up must not lose the penalty: the session is
    rolled back and the flat 5-min fallback UPDATE still lands in it."""
    from datetime import UTC, datetime

    import aibroker.routing.cooldown as cooldown_mod
    import aibroker.services.llm_service as svc
    from aibroker.db import get_session
    from aibroker.db.models import ApiKeyRow

    key = await _seed_key()

    async def boom(*a, **kw):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(cooldown_mod, "cooldown_until", boom)
    kind = await svc._penalize(key, RuntimeError("429 Too Many Requests"))

    assert kind == "rate_limit"
    async with get_session() as s:
        row = await s.get(ApiKeyRow, key.id)
    assert row.cooldown_until is not None
    parked_s = (row.cooldown_until - datetime.now(UTC).replace(tzinfo=None)).total_seconds()
    assert 200 < parked_s <= 330  # the flat _COOLDOWN (5 min), not the adaptive path
