"""Health probes — verdict classification."""
from __future__ import annotations


def _response(status_code: int = 200, text: str = "", headers: dict | None = None):
    """A plain stand-in for httpx.Response. NOT AsyncMock: that made every
    attribute a coroutine, so `dict(r.headers)` in production choked on it and
    a defensive try/except had to live in health_probes.py purely to tolerate
    the test double (2026-09-07 review). A real Response has plain attributes."""
    return SimpleNamespace(status_code=status_code, text=text, headers=headers or {})


from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.providers.health_probes import probe


@pytest.mark.parametrize("status_code,expected", [
    (200, "alive"),
    (201, "alive"),
    (429, "cooldown"),
    (401, "dead"),
    (403, "dead"),
    (402, "dead"),
])
async def test_probe_status_classification(status_code, expected):
    fake = _response()
    fake.status_code = status_code
    fake.text = ""
    with patch("aibroker.providers.health_probes.httpx.AsyncClient") as m:
        ctx = m.return_value.__aenter__.return_value
        ctx.request = AsyncMock(return_value=fake)
        verdict, http, _ = await probe("cerebras", "fake-key")
    assert verdict == expected
    assert http == status_code


async def test_probe_dead_with_no_funds_hint():
    fake = _response()
    fake.status_code = 403
    fake.text = "Insufficient balance for this request"
    with patch("aibroker.providers.health_probes.httpx.AsyncClient") as m:
        ctx = m.return_value.__aenter__.return_value
        ctx.request = AsyncMock(return_value=fake)
        verdict, _, hint = await probe("deepseek", "fake-key")
    assert verdict == "dead"
    assert "fund" in hint.lower()


async def test_probe_mistral_401_is_monthly_cooldown_not_dead():
    """mistral's bare 401 = monthly Vibe quota, not a revoked key — the probe
    must return 'cooldown' with a 'monthly quota' hint (key stays alive, monitor
    cools it to next month), NOT 'dead'. Other providers' 401 stays dead."""
    fake = _response()
    fake.status_code = 401
    fake.text = '{"detail":"Unauthorized"}'
    with patch("aibroker.providers.health_probes.httpx.AsyncClient") as m:
        ctx = m.return_value.__aenter__.return_value
        ctx.request = AsyncMock(return_value=fake)
        verdict, http, hint = await probe("mistral", "fake-key")
    assert verdict == "cooldown"
    assert http == 401
    assert hint == "monthly quota"


async def test_probe_neterr_on_exception():
    with patch("aibroker.providers.health_probes.httpx.AsyncClient") as m:
        ctx = m.return_value.__aenter__.return_value
        ctx.request = AsyncMock(side_effect=ConnectionError("dns failure"))
        verdict, http, hint = await probe("cerebras", "fake-key")
    assert verdict == "neterr"
    assert http == 0
    assert "ConnectionError" in hint


async def test_probe_unknown_provider_returns_skip_not_alive():
    """REGRESSION (2026-07-16): an unprobeable provider used to read 'alive',
    so the monitor force-revived its dead keys every sweep (cloudflare flapped
    pick→fail→dead→revive forever). Neutral 'skip' = leave state unchanged."""
    verdict, http, hint = await probe("nonexistent-provider", "fake-key")
    assert verdict == "skip"
    assert http == 0
    assert "no probe" in hint


async def test_probe_cloudflare_without_account_id_returns_skip():
    """A cloudflare key with no account_id can't be called at all — the probe
    must skip (leave state), not fabricate a verdict. No HTTP is attempted."""
    verdict, http, hint = await probe("cloudflare", "fake-key", None)
    assert verdict == "skip"
    assert http == 0
    assert "account_id" in hint


def test_cloudflare_probe_uses_account_scoped_api_base():
    """The cloudflare probe URL must embed the key's account_id (Workers AI has
    no account header — the ID rides in the path, same as the adapter's
    api_base) and probe the same gpt-oss-120b the chat lanes use."""
    from aibroker.providers.registry import REGISTRY
    _PROBES = {n: sp.probe.build for n, sp in REGISTRY.items() if sp.probe}
    method, url, headers, body = _PROBES["cloudflare"]("SECRET", "acct-123")
    assert method == "POST"
    assert "api.cloudflare.com" in url
    assert "/accounts/acct-123/" in url
    assert body["model"] == "@cf/openai/gpt-oss-120b"
    assert body["max_tokens"] == 1
    assert headers["Authorization"] == "Bearer SECRET"


def test_probe_models_are_live_not_dead_or_paid():
    """REGRESSION (2026-07-10): probes must target live/free models. voyage-3
    billed real $ (zero free allocation) and nvidia's kimi-k2.6 404s (removed
    from routing), which made a revoked nvidia key read as alive."""
    from aibroker.providers.registry import REGISTRY
    _PROBES = {n: sp.probe.build for n, sp in REGISTRY.items() if sp.probe}
    _, _, _, voyage_body = _PROBES["voyage"]("k")
    assert voyage_body["model"] == "voyage-4"
    _, _, _, nvidia_body = _PROBES["nvidia"]("k")
    assert "kimi" not in nvidia_body["model"]
    assert "nemotron" in nvidia_body["model"]
    # openai + cloudflare both have probes now (dead keys detectable).
    assert "openai" in _PROBES
    assert "cloudflare" in _PROBES


def test_gemini_probe_key_in_header_not_url():
    """REGRESSION: the gemini key must ride the x-goog-api-key header, never the
    URL query string (a URL key can leak into logged request URLs)."""
    from aibroker.providers.registry import REGISTRY
    _PROBES = {n: sp.probe.build for n, sp in REGISTRY.items() if sp.probe}
    _, url, headers, _ = _PROBES["gemini"]("SECRET_KEY")
    assert "SECRET_KEY" not in url
    assert "key=" not in url
    assert headers.get("x-goog-api-key") == "SECRET_KEY"


@pytest.mark.parametrize("provider", ["gemini", "cohere", "mistral", "openrouter"])
def test_scarce_free_quota_providers_probe_via_unmetered_endpoints(provider):
    """REGRESSION (2026-10-03): the gemini probe was a generateContent call on
    gemini-2.5-flash — one of the 20/day/model free calls — every sweep; cohere
    (1000/month) and mistral (monthly allowance) burned theirs the same way. A
    key-validation GET spends nothing."""
    from aibroker.providers.registry import REGISTRY
    _PROBES = {n: sp.probe.build for n, sp in REGISTRY.items() if sp.probe}
    method, url, _headers, body = _PROBES[provider]("K", None)
    assert method == "GET" and body is None
    assert "generateContent" not in url and "chat/completions" not in url
    assert "/chat" not in url


def test_gemini_probe_is_models_list_endpoint():
    from aibroker.providers.registry import REGISTRY
    _PROBES = {n: sp.probe.build for n, sp in REGISTRY.items() if sp.probe}
    _, url, headers, _ = _PROBES["gemini"]("K")
    assert url.startswith("https://generativelanguage.googleapis.com/v1beta/models")
    assert "gemini-2.5-flash" not in url
    assert headers["x-goog-api-key"] == "K"


async def test_gemini_bad_key_400_is_dead_not_alive():
    """Google answers an invalid key with HTTP 400 API_KEY_INVALID, which used to
    fall through to 'alive/uncertain'."""
    fake = _response(400, '{"error": {"message": "API key not valid. Please pass a valid API key."}}')
    with patch("aibroker.providers.health_probes.httpx.AsyncClient") as m:
        ctx = m.return_value.__aenter__.return_value
        ctx.request = AsyncMock(return_value=fake)
        verdict, http, _ = await probe("gemini", "bad")
    assert (verdict, http) == ("dead", 400)


def _fake(status, text=""):
    fake = _response()
    fake.status_code = status
    fake.text = text
    return fake


async def _probe_capture(provider, status, text="", **kw):
    with patch("aibroker.providers.health_probes.httpx.AsyncClient") as m:
        ctx = m.return_value.__aenter__.return_value
        ctx.request = AsyncMock(return_value=_fake(status, text))
        out = await probe(provider, "fake-key", **kw)
    return out, ctx.request.await_args


async def test_free_gemini_key_keeps_cheap_list_probe():
    (verdict, _, _), call = await _probe_capture("gemini", 200)
    assert verdict == "alive"
    assert call.args[0] == "GET" and "generateContent" not in call.args[1]


async def test_billable_gemini_probe_is_one_token_generation():
    (verdict, _, _), call = await _probe_capture("gemini", 200, billable=True)
    assert verdict == "alive"
    assert call.args[0] == "POST" and "generateContent" in call.args[1]
    assert call.kwargs["json"]["generationConfig"]["maxOutputTokens"] == 1


@pytest.mark.parametrize("status", [400, 402, 429])
async def test_billing_dead_body_is_dead_not_cooldown(status):
    (verdict, _, hint), _ = await _probe_capture(
        "gemini", status, "Your prepayment credits are depleted", billable=True)
    assert (verdict, hint) == ("dead", "no funds")


async def test_provider_without_billing_probe_ignores_billable():
    (_, _, _), call = await _probe_capture("cerebras", 200, billable=True)
    assert call.args[0] == "POST"   # its normal generation probe


def test_monitor_billable_probe_selection():
    from types import SimpleNamespace as K

    from aibroker.monitor import _needs_billable_probe
    assert _needs_billable_probe(K(tier="paid", is_alive=True, last_error=None))
    assert _needs_billable_probe(K(tier="free", is_alive=False,
                                   last_error="Your prepayment credits are depleted"))
    assert not _needs_billable_probe(K(tier="free", is_alive=False, last_error="auth failed"))
    assert not _needs_billable_probe(K(tier="free", is_alive=True, last_error=None))


async def test_probe_all_passes_billable_flag():
    from aibroker.providers.health_probes import probe_all
    with patch("aibroker.providers.health_probes.probe",
               AsyncMock(return_value=("alive", 200, ""))) as p:
        await probe_all([(1, "gemini", "k", None, True), (2, "groq", "k", None)])
    flags = {c.args[0]: c.args[3] for c in p.await_args_list}
    assert flags == {"gemini": True, "groq": False}
