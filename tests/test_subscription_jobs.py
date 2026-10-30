"""Universal jobs contract for explicit subscription pins."""
from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from PIL import Image

from aibroker.agent.events import FileRef, RunCompleted
from aibroker.agent.runs import append_event, create_run, get_run, record_usage
from aibroker.agent.subscription_job import output_problem, prompt_for_messages
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.main import app
from aibroker.providers.catalog import UnknownModel, listing, resolve_pin
from aibroker.providers.job_contract import (
    SUBSCRIPTION_PROMPT_MAX_BYTES,
    subscription_prompt_problem,
)
from aibroker.providers.subscriptions import subscription_models
from aibroker.routes import proxy
from tests.test_routes_proxy import _make_project


def _ctx(scopes: tuple[str, ...] = ("llm:chat", "llm:agent")) -> SimpleNamespace:
    return SimpleNamespace(project=SimpleNamespace(id=11, allowed_scopes=list(scopes)),
                           has_scope=lambda scope: scope in scopes)


def test_catalog_keeps_route_identity_and_subscription_variable_price():
    rows = {r["id"]: r for r in listing()}
    claude = rows["claude-sub/claude-sonnet-5-5"]
    codex = rows["codex-sub/gpt-6.1-sol"]
    assert claude["source"] == codex["source"] == "broker"
    assert claude["billing_type"] == codex["billing_type"] == "subscription"
    assert claude["price"] == codex["price"] == {
        "kind": "subscription", "input_usd_per_mtok": 0.0,
        "output_usd_per_mtok": 0.0, "subscription_fee_excluded": True,
    }
    assert codex["available"] is False
    assert resolve_pin(claude["id"], "chat:fast", ["groq", "claude-sub"])[0].provider == "claude-sub"
    with pytest.raises(UnknownModel):
        resolve_pin(claude["id"], "chat:fast", ["groq"])
    with pytest.raises(UnknownModel):
        resolve_pin("claude-sonnet-5-5", "chat:fast", ["groq", "claude-sub"])


def test_subscription_prompt_uses_serialized_utf8_budget():
    # This is larger than the former 200k-character cap, including the JSON
    # transcript wrapper that the CLI actually receives.
    prompt = prompt_for_messages([{"role": "user", "content": "x" * 950_613}], None)
    assert len(prompt) > 950_613
    assert subscription_prompt_problem(prompt) is None
    assert subscription_prompt_problem("a" * SUBSCRIPTION_PROMPT_MAX_BYTES) is None
    assert subscription_prompt_problem("a" * (SUBSCRIPTION_PROMPT_MAX_BYTES + 1))
    assert subscription_prompt_problem("é" * (SUBSCRIPTION_PROMPT_MAX_BYTES // 2)) is None
    assert subscription_prompt_problem("é" * (SUBSCRIPTION_PROMPT_MAX_BYTES // 2 + 1))


async def test_large_subscription_job_reaches_queue_without_paid_fallback(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"codex-sub":["gpt-6.1-sol"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    create = AsyncMock(return_value=(42, True))
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    plain, _ = await _make_project(["llm:chat", "llm:agent"])
    headers = {"X-Project-Key": plain}
    client = TestClient(app)
    catalog = client.get("/v1/models", headers=headers)
    route = next(row for row in catalog.json()["data"] if row["id"] == "codex-sub/gpt-6.1-sol")
    assert route["job_request"]["max_prompt_chars"] is None
    assert route["job_request"]["max_prompt_bytes"] == SUBSCRIPTION_PROMPT_MAX_BYTES
    body = {"model": "codex-sub/gpt-6.1-sol",
            "messages": [{"role": "user", "content": "x" * 950_613}]}
    accepted = client.post("/v1/jobs?capability=chat:smart", headers=headers, json=body)
    assert accepted.status_code == 202
    assert create.await_count == 1
    assert create.await_args.kwargs["model"] == "gpt-6.1-sol"
    assert len(create.await_args.kwargs["prompt"]) > 950_613
    too_large = {**body, "messages": [{"role": "user", "content": "x" * SUBSCRIPTION_PROMPT_MAX_BYTES}]}
    rejected = client.post("/v1/jobs?capability=chat:smart", headers=headers, json=too_large)
    assert rejected.status_code == 400
    assert "UTF-8 bytes" in rejected.json()["detail"]
    assert create.await_count == 1


async def test_http_catalog_contract_matches_subscription_job_validation(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5"],"codex-sub":["gpt-6.1-sol"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    create = AsyncMock(return_value=(42, True))
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    plain, _ = await _make_project(["llm:chat", "llm:agent"])
    headers = {"X-Project-Key": plain}
    client = TestClient(app)
    catalog = client.get("/v1/models", headers=headers)
    assert catalog.status_code == 200
    rows = {row["id"]: row for row in catalog.json()["data"]}
    for model in ("claude-sub/claude-sonnet-5-5", "codex-sub/gpt-6.1-sol"):
        request = rows[model]["job_request"]
        assert request["max_tokens"] == {
            "default": 1024, "accepted": {"const": 1024}, "forwarded": False}
        assert request["temperature"] == {
            "default": 0.7, "accepted": {"const": 0.7}, "forwarded": False}
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        omitted = client.post("/v1/jobs?capability=chat:fast", headers=headers, json=body)
        explicit = client.post("/v1/jobs?capability=chat:fast", headers=headers,
                               json={**body, "max_tokens": 1024, "temperature": 0.7})
        rejected = client.post("/v1/jobs?capability=chat:fast", headers=headers,
                               json={**body, "max_tokens": 16})
        assert omitted.status_code == explicit.status_code == 202
        assert rejected.status_code == 400
        assert rejected.json()["detail"] == (
            "unsupported_for_subscription_model: temperature or max_tokens")
    assert create.await_count == 4


def test_compose_default_catalog_sentinel_keeps_defaults_and_malformed_fails_closed(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS", "default")
    assert {m.id for m in subscription_models()} == {
        "claude-sub/claude-sonnet-5-5", "claude-sub/claude-opus-5-5", "claude-sub/claude-haiku-5-5",
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


VISION_SCOPES = ("llm:chat", "llm:agent", "llm:vision")


def _image_block(fmt: str = "PNG", media: str = "image/png", raw: bytes | None = None) -> dict:
    if raw is None:
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), "red").save(buf, format=fmt)
        raw = buf.getvalue()
    url = f"data:{media};base64,{base64.b64encode(raw).decode()}"
    return {"type": "image_url", "image_url": {"url": url}}


def _claude_vision(monkeypatch, vision: str) -> AsyncMock:
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS", "default")
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5","claude-opus-5-5"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VISION_VERIFIED_MODELS", vision)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    create = AsyncMock(return_value=(42, True))
    monkeypatch.setattr(proxy, "create_run", create)
    return create


def _image_request(block: dict, model: str = "claude-sub/claude-opus-5-5") -> proxy.ChatRequest:
    return proxy.ChatRequest.model_validate({"model": model, "messages": [
        {"role": "user", "content": [{"type": "text", "text": "Design around this hero"}, block]}]})


@pytest.mark.asyncio
async def test_claude_image_reaches_the_cli_as_a_real_attachment(monkeypatch):
    create = _claude_vision(monkeypatch, '{"claude-sub":["claude-opus-5-5"]}')
    result = await proxy.jobs_submit(_image_request(_image_block()), Response(),
                                     capability="chat:smart", ctx=_ctx(VISION_SCOPES))
    assert result.job_id == 42
    kwargs = create.await_args.kwargs
    assert kwargs["capability"] == "agent:claude" and kwargs["model"] == "claude-opus-5-5"
    [att] = kwargs["attachments"]
    assert att["name"] == "image_1.png" and att["media_type"] == "image/png" and att["data_b64"]
    assert "[attached image: inputs/image_1.png]" in kwargs["prompt"]
    assert att["data_b64"] not in kwargs["prompt"]                 # bytes never go into the prompt
    assert "vision" in next(m for m in subscription_models()
                            if m.id == "claude-sub/claude-opus-5-5").capabilities


@pytest.mark.asyncio
async def test_claude_image_is_validated_before_enqueue(monkeypatch):
    create = _claude_vision(monkeypatch, '{"claude-sub":["claude-opus-5-5"]}')
    for block in (_image_block(media="image/bmp"),                        # type not allowed
                  _image_block(fmt="JPEG"),                               # bytes do not match png
                  _image_block(raw=b"\x89PNG\r\n\x1a\n" + b"x" * 64),     # undecodable
                  _image_block(raw=b"\x89PNG" + b"0" * (25 * 1024 * 1024))):  # over MAX_BYTES
        with pytest.raises(HTTPException) as exc:
            await proxy.jobs_submit(_image_request(block), Response(),
                                    capability="chat:smart", ctx=_ctx(VISION_SCOPES))
        assert exc.value.status_code == 400
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_claude_image_needs_the_exact_model_in_the_vision_list(monkeypatch):
    create = _claude_vision(monkeypatch, '{"claude-sub":["claude-opus-5-5"]}')
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(_image_request(_image_block(), "claude-sub/claude-sonnet-5-5"),
                                Response(), capability="chat:smart", ctx=_ctx(VISION_SCOPES))
    assert exc.value.status_code == 503 and "vision" in exc.value.detail
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_claude_image_needs_llm_vision_and_a_user_turn(monkeypatch):
    create = _claude_vision(monkeypatch, '{"claude-sub":["claude-opus-5-5"]}')
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(_image_request(_image_block()), Response(),
                                capability="chat:smart", ctx=_ctx())         # no llm:vision
    assert exc.value.status_code == 403
    assistant_image = proxy.ChatRequest.model_validate({
        "model": "claude-sub/claude-opus-5-5",
        "messages": [{"role": "user", "content": "hi"},
                     {"role": "assistant", "content": [_image_block()]},
                     {"role": "user", "content": "go on"}]})
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(assistant_image, Response(),
                                capability="chat:smart", ctx=_ctx(VISION_SCOPES))
    assert exc.value.status_code == 400
    create.assert_not_awaited()


def test_claude_vision_ids_must_be_verified_or_the_catalog_closes(monkeypatch):
    _claude_vision(monkeypatch, '{"claude-sub":["claude-fable-5-1"]}')   # candidate, not verified
    assert subscription_models() == []


@pytest.mark.asyncio
async def test_jobs_subscription_rejects_unsupported_tools_and_images_before_enqueue(monkeypatch):
    create = AsyncMock()
    monkeypatch.setattr(proxy, "create_run", create)
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    # image input needs a vision-verified model (503 for Sonnet here); Claude tools are refused
    # while the switch is off (503)
    for messages, tools, status in (
        ([{"role": "user", "content": [{"type": "text", "text": "hi"}, _image_block()]}], None, 503),
        ([{"role": "user", "content": "hi"}], [{"type": "function", "function": {
            "name": "f", "description": "d", "parameters": {"type": "object"}}}], 503),
    ):
        body = proxy.ChatRequest.model_validate({"messages": messages,
                                                  "model": "claude-sub/claude-sonnet-5-5",
                                                  **({"tools": tools} if tools else {})})
        with pytest.raises(HTTPException) as exc:
            await proxy.jobs_submit(body, Response(), capability="chat:fast",
                                    ctx=_ctx(VISION_SCOPES))
        assert exc.value.status_code == status
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


def test_output_modalities_declare_image_only_for_a_verified_image_codex_model(monkeypatch):
    from aibroker.providers.subscriptions import subscription_image_output_verified
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5"],"codex-sub":["gpt-6.1-sol","gpt-6-luna"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5"],"codex-sub":["gpt-6.1-sol","gpt-6-luna"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_IMAGE_OUTPUT_VERIFIED_MODELS",
                        '{"codex-sub":["gpt-6.1-sol"]}')
    rows = {r["id"]: r for r in listing()}
    sol = rows["codex-sub/gpt-6.1-sol"]
    assert sol["output_modalities"] == ["text", "image"] and "chat:fast" in sol["capabilities"]
    assert rows["codex-sub/gpt-6-luna"]["output_modalities"] == ["text"]
    assert rows["claude-sub/claude-sonnet-5-5"]["output_modalities"] == ["text"]
    assert subscription_image_output_verified("codex-sub/gpt-6.1-sol") is True
    assert subscription_image_output_verified("codex-sub/gpt-6-luna") is False
    # API chat models report text output
    assert any(r["output_modalities"] == ["text"] for r in rows.values()
               if r["billing_type"] == "api")


def test_image_output_setting_fails_closed(monkeypatch):
    from aibroker.providers.subscriptions import (
        subscription_image_output_verified,
        subscription_model_verified,
    )
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_MODELS",
                        '{"codex-sub":["gpt-6.1-sol","gpt-6-luna"]}')
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"codex-sub":["gpt-6.1-sol"]}')
    # a listing-only flag: a bad value turns the flag off but never refuses the catalog
    for bad in ('{"codex-sub":["gpt-6-luna"]}', '{"claude-sub":["gpt-6.1-sol"]}', "broken"):
        monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_IMAGE_OUTPUT_VERIFIED_MODELS", bad)
        assert subscription_model_verified("codex-sub/gpt-6.1-sol"), bad
        assert subscription_image_output_verified("codex-sub/gpt-6.1-sol") is False
        assert subscription_image_output_verified("codex-sub/gpt-6-luna") is False
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_IMAGE_OUTPUT_VERIFIED_MODELS", "default")
    assert subscription_image_output_verified("codex-sub/gpt-6.1-sol") is False   # default empty
    assert {r["id"]: r for r in listing()}["codex-sub/gpt-6.1-sol"]["output_modalities"] == ["text"]


async def test_claude_tools_switch_and_verified_list_gate_catalog_and_submit(monkeypatch):
    from aibroker.providers.subscriptions import (
        subscription_tool_model_canary,
        subscription_tool_model_verified,
    )
    settings = get_settings()
    claude = "claude-sub/claude-sonnet-5-5"
    monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS",
                        f'["{claude}"]')
    assert {r["id"]: r for r in listing()}[claude]["supports_tools"] is True
    assert subscription_tool_model_verified(claude)
    # a Codex or unverified id in the Claude list empties it (fails closed)
    monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS",
                        f'["{claude}","codex-sub/gpt-6.1-sol"]')
    assert not subscription_tool_model_verified(claude)
    monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS",
                        '["claude-sub/claude-opus-5-5"]')
    assert not subscription_tool_model_verified("claude-sub/claude-opus-5-5")
    # the canary: owner project 12 only, through the shared project switch
    monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_CANARY_MODELS", f'["{claude}"]')
    monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS", "12")
    assert subscription_tool_model_canary(claude, 12)
    assert not subscription_tool_model_canary(claude, 11)
    # submit: tools reach the job when switched on and verified, 503 when switched off
    monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS", f'["{claude}"]')
    monkeypatch.setattr(proxy, "project_allowed", lambda _id: True)
    monkeypatch.setattr(proxy, "policy_violation", lambda *_: None)
    monkeypatch.setattr(proxy, "admit_request", AsyncMock())
    create = AsyncMock(return_value=(42, True))
    monkeypatch.setattr(proxy, "create_run", create)
    tool = {"type": "function", "function": {"name": "final_result", "description": "d",
                                             "parameters": {"type": "object"}}}
    body = proxy.ChatRequest.model_validate({
        "model": claude, "tools": [tool], "tool_choice": "required",
        "messages": [{"role": "user", "content": "hi"},
                     {"role": "assistant", "content": None, "tool_calls": [{
                         "id": "c1", "type": "function",
                         "function": {"name": "final_result", "arguments": "{}"}}]},
                     {"role": "tool", "tool_call_id": "c1", "content": "ok"}]})
    await proxy.jobs_submit(body, Response(), capability="chat:fast", ctx=_ctx())
    client_job = create.await_args.kwargs["client_job"]
    assert client_job["provider"] == "claude-sub" and client_job["tools"][0]["function"][
        "name"] == "final_result"
    assert create.await_args.kwargs["capability"] == "agent:claude"
    monkeypatch.setattr(settings, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED", False)
    with pytest.raises(HTTPException) as exc:
        await proxy.jobs_submit(body, Response(), capability="chat:fast", ctx=_ctx())
    assert exc.value.status_code == 503
    assert {r["id"]: r for r in listing()}[claude]["supports_tools"] is False


def test_canary_projects_are_a_list_and_attestation_comes_from_settings(monkeypatch):
    from aibroker.providers.subscriptions import (
        canary_project_ids,
        subscription_tool_model_attested,
        subscription_tools_accepted,
        subscription_tools_advertised,
    )
    s = get_settings()
    models = ["claude-sub/claude-sonnet-5-5", "claude-sub/claude-opus-5-5"]
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_VERIFIED_MODELS",
                        '{"claude-sub":["claude-sonnet-5-5","claude-opus-5-5"],"codex-sub":[]}')
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED", True)
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS", "[]")
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_CANARY_MODELS", json.dumps(models))
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS", "12, 34")
    assert canary_project_ids() == {12, 34}
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_TOOLS_ATTESTED_MODELS", json.dumps(models[:1]))
    for pid in (12, 34):
        assert subscription_tools_accepted(models[1], pid)          # canary: tools accepted
        assert subscription_tools_advertised(models[0], pid)        # attested: advertised
        assert not subscription_tools_advertised(models[1], pid)    # no live proof: not advertised
    assert not subscription_tools_accepted(models[0], 99)           # not a canary project
    assert not subscription_tools_accepted(models[0], None)
    assert not {r["id"]: r for r in listing(None)}[models[0]]["supports_tools"]
    rows = {r["id"]: r for r in listing(34)}
    assert rows[models[1]]["job_request"]["tools"] is True and rows[models[1]]["supports_tools"] is False
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_TOOLS_ATTESTED_MODELS", json.dumps(models))
    rows = {r["id"]: r for r in listing(34)}
    assert rows[models[1]]["supports_tools"] is rows[models[1]]["job_request"]["tools"] is True
    for bad in ("12,12", "12,x", "abc", "12,²", "٣", "-1", "1" * 12):
        monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS", bad)
        assert canary_project_ids() == frozenset()
        assert not subscription_tools_accepted(models[0], 12)
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_TOOLS_ATTESTED_MODELS", "default")   # compose sentinel
    assert subscription_tool_model_attested(models[0]) and not subscription_tool_model_attested(models[1])
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_TOOLS_ATTESTED_MODELS", "not json")
    assert not subscription_tool_model_attested(models[0])
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_TOOLS_ATTESTED_MODELS", '["other-sub/x"]')
    assert not subscription_tool_model_attested("other-sub/x")
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED", False)
    monkeypatch.setattr(s, "AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS", "12")
    assert not subscription_tools_accepted(models[0], 12)           # the provider switch wins
