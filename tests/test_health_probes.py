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


async def test_probe_neterr_on_exception():
    with patch("aibroker.providers.health_probes.httpx.AsyncClient") as m:
        ctx = m.return_value.__aenter__.return_value
        ctx.request = AsyncMock(side_effect=ConnectionError("dns failure"))
        verdict, http, hint = await probe("cerebras", "fake-key")
    assert verdict == "neterr"
    assert http == 0
    assert "ConnectionError" in hint


async def test_probe_unknown_provider_returns_alive():
    """If we never configured a probe for a provider, skip with 'alive'."""
    verdict, http, hint = await probe("nonexistent-provider", "fake-key")
    assert verdict == "alive"
    assert "no probe" in hint
