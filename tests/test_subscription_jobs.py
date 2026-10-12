"""Universal jobs contract for explicit subscription pins."""
from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from PIL import Image

from aibroker.agent.events import FileRef, RunCompleted
from aibroker.agent.runs import append_event, create_run, get_run, record_usage
from aibroker.agent.subscription_job import output_problem
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.providers.catalog import UnknownModel, listing, resolve_pin
from aibroker.providers.subscriptions import subscription_models
from aibroker.routes import proxy


def _ctx(scopes: tuple[str, ...] = ("llm:chat", "llm:agent")) -> SimpleNamespace:
    return SimpleNamespace(project=SimpleNamespace(id=11, allowed_scopes=list(scopes)),
                           has_scope=lambda scope: scope in scopes)


def test_catalog_keeps_route_identity_and_unknown_subscription_price():
    rows = {r["id"]: r for r in listing()}
    claude = rows["claude-sub/claude-sonnet-5-5"]
    codex = rows["codex-sub/gpt-6.1-sol"]
    assert claude["source"] == codex["source"] == "broker"
    assert claude["billing_type"] == codex["billing_type"] == "subscription"
    assert claude["price"] == codex["price"] == {"kind": "subscription"}
    assert codex["available"] is False
    assert resolve_pin(claude["id"], "chat:fast", ["groq", "claude-sub"])[0].provider == "claude-sub"
    with pytest.raises(UnknownModel):
        resolve_pin(claude["id"], "chat:fast", ["groq"])
    with pytest.raises(UnknownModel):
        resolve_pin("claude-sonnet-5-5", "chat:fast", ["groq", "claude-sub"])


def test_compose_default_catalog_sentinel_keeps_defaults_and_malformed_fails_closed(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS", "default")
    assert {m.id for m in subscription_models()} == {
        "claude-sub/claude-sonnet-5-5",
        "codex-sub/gpt-6.1-sol", "codex-sub/gpt-6-astra", "codex-sub/gpt-6-sol",
        "codex-sub/gpt-6-luna", "codex-sub/gpt-5.6-sol", "codex-sub/gpt-5.6-terra",
        "codex-sub/gpt-5.6-luna",
    }
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "broken")
    assert subscription_models() == []


def test_account_verified_catalog_expands_without_an_allow_all_pin(monkeypatch):
    from aibroker.providers.subscriptions import subscription_model_verified
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5","claude-opus-example"],'
                        '"codex-sub":["gpt-6.1-sol","gpt-example"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5","claude-opus-example"],'
                        '"codex-sub":["gpt-example"]}')
    rows = {r["id"]: r for r in listing()}
    assert rows["claude-sub/claude-opus-example"]["available"] is True
    assert rows["codex-sub/gpt-example"]["available"] is False  # route gate remains off
    assert subscription_model_verified("codex-sub/gpt-6.1-sol") is False
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["not-a-candidate"]}')
    assert subscription_models() == []


@pytest.mark.asyncio
async def test_unverified_candidate_cannot_enqueue_even_if_project_policy_allows(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5","claude-opus-example"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5"]}')
    body = proxy.ChatRequest(messages=[proxy.ChatMessage(role="user", content="hi")],
                             model="claude-sub/claude-opus-example")
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(body, Response(), capability="chat:fast", ctx=_ctx())
    assert exc.value.status_code == 503
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_jobs_subscription_pin_reuses_submit_shape_and_exact_cli_model(monkeypatch):
    create = AsyncMock(return_value=(42, True))
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    result = await proxy.jobs_submit(
        proxy.ChatRequest(messages=[proxy.ChatMessage(role="user", content="Draw a cat")],
                          model="claude-sub/claude-sonnet-5-5"),
        Response(), capability="chat:fast", ctx=_ctx(),
    )
    assert result.job_id == 42 and result.poll_url == "/v1/jobs/42"
    kwargs = create.await_args.kwargs
    assert kwargs["capability"] == "agent:claude"
    assert kwargs["model"] == "claude-sonnet-5-5"
    assert kwargs["client_job"]["model"] == "claude-sub/claude-sonnet-5-5"
    assert kwargs["allowed_tools"] == []


@pytest.mark.asyncio
async def test_jobs_subscription_rejects_unsupported_tools_and_images_before_enqueue(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    for messages, tools in (
        ([{"role": "user", "content": [{"type": "text", "text": "hi"}]}], None),
        ([{"role": "user", "content": "hi"}], [{"type": "function", "function": {
            "name": "f", "description": "d", "parameters": {"type": "object"}}}]),
    ):
        body = proxy.ChatRequest.model_validate({"messages": messages,
                                                  "model": "claude-sub/claude-sonnet-5-5",
                                                  **({"tools": tools} if tools else {})})
        with pytest.raises(HTTPException) as exc:
            await proxy.jobs_submit(body, Response(), capability="chat:fast", ctx=_ctx())
        assert exc.value.status_code == 400
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_codex_disabled_and_streaming_rejected_before_enqueue(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    codex = proxy.ChatRequest(messages=[proxy.ChatMessage(role="user", content="hi")],
                              model="codex-sub/gpt-6.1-sol")
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(codex, Response(), capability="chat:fast", ctx=_ctx())
    assert exc.value.status_code == 503
    claude = proxy.ChatRequest(messages=[proxy.ChatMessage(role="user", content="hi")],
                               model="claude-sub/claude-sonnet-5-5", stream=True)
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(claude, Response(), capability="chat:fast", ctx=_ctx())
    assert exc.value.status_code == 400
    create.assert_not_awaited()


def test_subscription_json_is_checked_without_model_fallback():
    fmt = {"type": "json_schema", "json_schema": {"schema": {
        "type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}}}
    assert output_problem('{"ok": true}', fmt) is None
    assert output_problem('{"ok": "yes"}', fmt).startswith("InvalidJSON")
    assert output_problem("```json\n{}\n```", fmt).startswith("InvalidJSON")


@pytest.mark.asyncio
async def test_subscription_run_polls_through_normal_job_with_png_and_null_cost():
    plain = generate_project_key()
    async with get_session() as session:
        session.add(ProjectRow(id=11, name="subscription-test",
                               project_key_hash=hash_project_key(plain),
                               project_key_prefix=plain[:12],
                               allowed_scopes=["llm:chat", "llm:agent"], is_active=True, notes=""))
    run_id, _ = await create_run(
        project=SimpleNamespace(id=11), capability="agent:codex", prompt="draw",
        task_id="jobs", account_id="owner", session_id=None, idempotency_key=None,
        wall_timeout_s=30, idle_timeout_s=30, allowed_tools=[], model="gpt-6.1-sol",
        client_job={"provider": "codex-sub", "model": "codex-sub/gpt-6.1-sol",
                    "response_format": None},
    )
    await record_usage(run_id, {"input_tokens": 12, "output_tokens": 6}, "subscription")
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), "red").save(buf, format="PNG")
    png = buf.getvalue()
    await append_event(run_id, RunCompleted(
        final_text="Image generated", files=[FileRef(path="out/generated.png", size=len(png),
            content_b64=base64.b64encode(png).decode())]))
    row = (await get_run(run_id, 11))[1]
    result = proxy._job_response(row)
    assert result.status == "done"
    assert result.provider == "codex-sub" and result.model == "codex-sub/gpt-6.1-sol"
    assert result.billing_type == "subscription" and result.cost_usd is None
    assert base64.b64decode(result.files[0]["content_b64"]) == png
