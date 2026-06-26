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
