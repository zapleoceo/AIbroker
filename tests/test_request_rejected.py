"""A provider 400 on the request itself (schema / payload) is terminal, not rotated."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.providers.provider_errors import is_request_rejected, rejection_message
from aibroker.services import job_queue

_GEMINI_400 = ('litellm.BadRequestError: GeminiException BadRequestError - {\n  "error": {\n'
               '    "code": 400,\n    "message": "The specified schema produces a constraint '
               'that has too many states for serving.",\n    "status": "INVALID_ARGUMENT"\n  }\n}')


class _Exc(Exception):
    def __init__(self, text: str, status: int | None) -> None:
        super().__init__(text)
        self.status_code = status


def test_schema_400_is_a_request_rejection_with_the_provider_text():
    exc = _Exc(_GEMINI_400, 400)
    assert is_request_rejected(exc, "gemini")
    assert rejection_message(exc) == ("The specified schema produces a constraint that has "
                                      "too many states for serving.")


@pytest.mark.parametrize(("text", "status"), [
    ('{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}',
     400),                                                        # a bad KEY, not the request
    ("Error code: 429 - quota exceeded", 429),                    # throttle
    ("Internal error", 500),                                      # transient
    ("model_not_found: no such model", 404),                      # gone model
])
def test_other_failures_are_not_request_rejections(text, status):
    assert not is_request_rejected(_Exc(text, status), "gemini")


async def test_run_chat_stops_rotating_keys_and_returns_the_rejection(monkeypatch):
    import aibroker.services.llm_service as svc
    from aibroker.services.attempt import AttemptResult, Flow

    picks = []

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        return SimpleNamespace(id=len(picks), label="k", tier="paid", provider=provider,
                               token_encrypted="x", account_id=None)

    async def rejecting_attempt(a):
        return AttemptResult(Flow.REQUEST_REJECTED, error=_Exc(_GEMINI_400, 400))

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr(svc, "run_attempt", rejecting_attempt)
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["gemini"])
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/m")
    out = await svc.run_chat(
        project=SimpleNamespace(id=7, name="p"), capability="structured",
        messages=[{"role": "user", "content": "hi"}], model=None, max_tokens=64,
        temperature=0.7, response_format=None, workflow="w")
    assert isinstance(out, svc.RequestRejected)
    assert out.provider == "gemini" and "too many states" in out.message
    assert picks == ["gemini"]                    # one key, no rotation over siblings


async def test_dispatcher_fails_a_rejected_job_at_once():
    import aibroker.services.llm_service as svc

    pid = 990801
    async with get_session() as s:
        s.add(ProjectRow(id=pid, name="rejected-fixture", project_key_hash="h",
                         project_key_prefix="pk_x", allowed_scopes=["llm:chat"]))
    row = DeepJobRow(id=1, project_id=pid, capability="structured", status="running",
                     retry_count=0, request={"messages": [{"role": "user", "content": "x"}],
                                             "model": None, "max_tokens": 64,
                                             "temperature": 0.7, "response_format": None,
                                             "workflow": "t"})

    async def fake_run_chat(**kw):
        return svc.RequestRejected("gemini", "too many states")

    with patch.object(job_queue, "run_chat", fake_run_chat), \
         patch.object(job_queue, "_finish", AsyncMock()) as finish, \
         patch.object(job_queue, "_requeue_or_fail", AsyncMock()) as requeue:
        await job_queue._execute(row)
    requeue.assert_not_awaited()
    assert finish.await_args.kwargs["status"] == "error"
    assert finish.await_args.kwargs["error_message"] == (
        "provider gemini rejected the request (HTTP 400): too many states")
