"""Type/status-first provider-error classification (2026-10-03 review).

Bare substrings "401"/"403"/"auth"/"429"/"quota" anywhere in a message used to
misfire and kill (or throttle) healthy keys. These pin the replacement: exception
type and status code first, then anchored word-boundary regexes.
"""
from __future__ import annotations

import pytest

from aibroker.providers.provider_errors import classify_provider_error


class _Typed(Exception):
    def __init__(self, msg: str, status_code: int | None = None):
        super().__init__(msg)
        self.status_code = status_code


def _named(name: str, msg: str = "boom", status_code: int | None = None) -> Exception:
    return type(name, (_Typed,), {})(msg, status_code)


@pytest.mark.parametrize("msg", [
    "request id req_4291abc failed upstream",        # 429 inside an id
    "response was 4030 bytes long",                   # 403 inside a number
    "the author of this prompt is unknown",           # 'auth' inside a word
    "OAuth redirect mismatch in tool schema",         # 'auth' inside 'oauth'
    "model gpt-4012 exploded",                        # 401 inside a model name
])
def test_substrings_inside_other_tokens_do_not_misfire(msg):
    assert classify_provider_error(RuntimeError(msg)) == "error"


def test_400_that_merely_mentions_quota_is_not_a_throttle():
    exc = _named("BadRequestError", "invalid parameter: quota must be an integer", 400)
    assert classify_provider_error(exc) == "error"


def test_exception_type_wins_over_message():
    assert classify_provider_error(_named("RateLimitError", "slow down")) == "rate_limit"
    assert classify_provider_error(_named("AuthenticationError", "nope")) == "auth"
    assert classify_provider_error(_named("PermissionDeniedError", "nope")) == "auth"


def test_status_code_attribute_wins_over_message():
    assert classify_provider_error(_Typed("upstream said no", 429)) == "rate_limit"
    assert classify_provider_error(_Typed("upstream said no", 401)) == "auth"
    assert classify_provider_error(_Typed("upstream said no", 403)) == "auth"


def test_status_in_message_body_is_recognised_when_anchored():
    assert classify_provider_error(RuntimeError('{"error": {"code": 429}}')) == "rate_limit"
    assert classify_provider_error(RuntimeError("Error code: 401 - bad")) == "auth"
    assert classify_provider_error(RuntimeError("status_code=403")) == "auth"


def test_gemini_asr_runtime_error_path_still_works():
    """The raw RuntimeError("gemini-asr <status>: ...") from the gemini ASR
    adapter carries no status attribute — the keyword-anchored regex reads it."""
    assert classify_provider_error(
        RuntimeError('gemini-asr 429: {"status":"RESOURCE_EXHAUSTED"}'), "gemini") == "rate_limit"
    assert classify_provider_error(RuntimeError("gemini-asr 403: PERMISSION_DENIED"), "gemini") == "auth"
    assert classify_provider_error(RuntimeError("gemini-asr unreachable: boom"), "gemini") == "error"


def test_gemini_400_api_key_not_valid_is_auth():
    exc = _named("BadRequestError", "API key not valid. Please pass a valid API key.", 400)
    assert classify_provider_error(exc, "gemini") == "auth"


def test_miscoded_5xx_cohere_style_still_falls_back_to_phrases():
    exc = _Typed("You are using a Trial key, limited to 1000 API calls / month", 500)
    assert classify_provider_error(exc, "cohere") == "rate_limit"


def test_mistral_typed_401_is_still_the_monthly_rate_limit():
    exc = _named("AuthenticationError", '{"detail":"Unauthorized"}', 401)
    assert classify_provider_error(exc, "mistral") == "rate_limit"
    assert classify_provider_error(exc, "openai") == "auth"


def test_billing_429_stays_auth_even_when_typed_rate_limit():
    exc = _named("RateLimitError", "Your prepayment credits are depleted", 429)
    assert classify_provider_error(exc, "gemini") == "auth"
