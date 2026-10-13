"""Universal jobs contract for explicit subscription pins."""
from __future__ import annotations

import base64
import io
from functools import partial
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException, Response
from fastapi.testclient import TestClient
from PIL import Image

from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.events import FileRef, RunCompleted
from aibroker.agent.real_cli import build_claude_argv
from aibroker.agent.runs import append_event, create_run, get_run, record_usage
from aibroker.agent.subscription_job import output_problem
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import ProjectRow
from aibroker.main import app
from aibroker.providers.catalog import UnknownModel, listing, resolve_pin
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
            "default": 1024, "accepted": {"minimum": 1, "maximum": 16384}, "forwarded": False}
        assert request["temperature"] == {
            "default": 0.7, "accepted": {"minimum": 0.0, "maximum": 2.0}, "forwarded": False}
        assert request["max_prompt_chars"] == 1_000_000
        body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
        post = partial(client.post, "/v1/jobs?capability=chat:fast", headers=headers)
        omitted = post(json=body)
        explicit = post(json={**body, "max_tokens": 1024, "temperature": 0.7})
        high = post(json={**body, "max_tokens": 4000, "temperature": 1.5})
        assert omitted.status_code == explicit.status_code == high.status_code == 202
        # out of the shared pydantic range: 422 as on every other /v1/jobs model, never forwarded
        assert post(json={**body, "max_tokens": 16385}).status_code == 422
        assert post(json={**body, "max_tokens": -1}).status_code == 422
        assert post(json={**body, "temperature": 2.5}).status_code == 422
    assert create.await_count == 6
    ignored = [c.kwargs["client_job"]["ignored_params"] for c in create.await_args_list]
    assert ignored == [[], ["max_tokens", "temperature"], ["max_tokens", "temperature"]] * 2
    for call in create.await_args_list:      # no cap or temperature reaches the run or the CLI
        assert {"max_tokens", "temperature"}.isdisjoint(call.kwargs)
        assert call.kwargs["wall_timeout_s"] == 14400 and call.kwargs["idle_timeout_s"] == 300


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


async def _project12() -> None:
    plain = generate_project_key()
    async with get_session() as session:
        session.add(ProjectRow(id=12, name="limits-test", project_key_hash=hash_project_key(plain),
                               project_key_prefix=plain[:12], allowed_scopes=["llm:agent"],
                               is_active=True, notes=""))


def test_sampling_params_have_no_path_to_the_cli():
    assert {"max_tokens", "temperature"}.isdisjoint(AgentRunSpec.__dataclass_fields__)
    spec = AgentRunSpec(run_id=1, capability="agent:claude", prompt="p", workspace=".",
                        account_id="owner", model="claude-sonnet-5-5")
    assert not any("token" in arg or "temperature" in arg for arg in build_claude_argv(spec))


@pytest.mark.asyncio
async def test_poll_response_and_result_meta_name_the_ignored_params():
    await _project12()
    run_id, _ = await create_run(
        project=SimpleNamespace(id=12), capability="agent:claude", prompt="hi", task_id="jobs",
        account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], model="claude-sonnet-5-5",
        client_job={"provider": "claude-sub", "model": "claude-sub/claude-sonnet-5-5",
                    "response_format": None, "ignored_params": ["max_tokens"]})
    assert proxy._job_response((await get_run(run_id, 12))[1]).ignored_params == ["max_tokens"]
    await append_event(run_id, RunCompleted(final_text="ok"))
    job = (await get_run(run_id, 12))[1]
    assert job.result_meta["ignored_params"] == ["max_tokens"]
    assert proxy._job_response(job).ignored_params == ["max_tokens"]


def test_prompt_limit_is_a_setting_with_an_explicit_400(monkeypatch):
    from aibroker.config import Settings
    from aibroker.providers.job_contract import subscription_prompt_problem
    assert Settings.model_fields["AGENT_SUBSCRIPTION_PROMPT_MAX_CHARS"].default == 1_000_000
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_PROMPT_MAX_CHARS", 1000)
    assert subscription_prompt_problem("x" * 1000) is None
    assert subscription_prompt_problem("x" * 1001) == "subscription prompt exceeds 1000 characters"


def test_file_caps_default_to_2_mib_each_and_8_mib_total():
    from aibroker.agent.executor import ExecConfig
    from aibroker.config import Settings
    fields = Settings.model_fields
    assert fields["AGENT_FILE_MAX_BYTES"].default == 2 * 1024 * 1024
    assert fields["AGENT_FILES_TOTAL_BYTES"].default == 8 * 1024 * 1024
    assert fields["AGENT_FILES_MAX_COUNT"].default == 20
    cfg = ExecConfig(workspace_root="x")
    assert (cfg.file_max_bytes, cfg.files_total_bytes) == (2 * 1024 * 1024, 8 * 1024 * 1024)


@pytest.mark.asyncio
async def test_skipped_images_are_listed_in_the_poll_response():
    await _project12()
    run_id, _ = await create_run(
        project=SimpleNamespace(id=12), capability="agent:codex", prompt="draw", task_id="jobs",
        account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], model="gpt-6.1-sol",
        client_job={"provider": "codex-sub", "model": "codex-sub/gpt-6.1-sol",
                    "response_format": None})
    await append_event(run_id, RunCompleted(final_text="drew", files=[
        FileRef(path="out/big.png", size=9_000_000, skipped="too_large"),
        FileRef(path="out/extra.png", size=10, skipped="cap_reached")]))
    files = proxy._job_response((await get_run(run_id, 12))[1]).files
    assert [(f["path"], f["skipped"], f["content_b64"]) for f in files] == [
        ("out/big.png", "too_large", None), ("out/extra.png", "cap_reached", None)]
