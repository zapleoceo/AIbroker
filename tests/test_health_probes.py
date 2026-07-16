"""Health probes — verdict classification."""
from __future__ import annotations

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
    fake = AsyncMock()
    fake.status_code = status_code
    fake.text = ""
    with patch("aibroker.providers.health_probes.httpx.AsyncClient") as m:
        ctx = m.return_value.__aenter__.return_value
        ctx.request = AsyncMock(return_value=fake)
        verdict, http, _ = await probe("cerebras", "fake-key")
    assert verdict == expected
    assert http == status_code


async def test_probe_dead_with_no_funds_hint():
    fake = AsyncMock()
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
    fake = AsyncMock()
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
    from aibroker.providers.health_probes import _PROBES
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
    from aibroker.providers.health_probes import _PROBES
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
    from aibroker.providers.health_probes import _PROBES
    _, url, headers, _ = _PROBES["gemini"]("SECRET_KEY")
    assert "SECRET_KEY" not in url
    assert "key=" not in url
    assert headers.get("x-goog-api-key") == "SECRET_KEY"
