"""Per-run billing attestation (`billing.attested` event -> GET /v1/agent/runs/{id} fields)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent import real_cli
from aibroker.agent.adapter import AuthAttestation
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import list_events
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.main import app

FIELDS = ("auth_method", "api_provider", "paid_continuation_verified_off", "fallback",
          "project_daily_cap_usd")
_PID = [9700]


@pytest.fixture(autouse=True)
def _adapters():
    clear_adapters()
    yield
    clear_adapters()


async def _project(cap=None):
    _PID[0] += 1
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=_PID[0], name=f"att_{_PID[0]}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes="",
            daily_cost_cap_usd=cap))
    return plain


async def _submit(key, **extra):
    body = {"capability": "agent:claude", "prompt": "p", "task_id": f"t{_PID[0]}",
            "account_id": "a", "run_mode": "exam"}
    body.update(extra)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/agent/runs", json=body, headers={"X-Project-Key": key})
    rid = r.json()["run_id"]
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    return rid


async def _get(key, rid):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return (await c.get(f"/v1/agent/runs/{rid}", headers={"X-Project-Key": key})).json()


def _cfg(tmp_path, **kw):
    base = {"workspace_root": str(tmp_path), "env": {}, "paid_continuation_verified_off": True}
    base.update(kw)
    return ExecConfig(**base)


# --- schema ------------------------------------------------------------------------------------------


def test_event_schema_is_versioned_typed_and_fallback_is_only_none():
    e = ev.parse_event({"type": "billing.attested", "auth_method": "claude.ai",
                        "api_provider": "firstParty", "paid_continuation_verified_off": True,
                        "fallback": "none", "project_daily_cap_usd": None})
    assert e.schema_version == 1 and e.type == "billing.attested"
    for bad in ({"fallback": "api"}, {"email": "x@y.z"}, {"schema_version": 2}):
        data = {"type": "billing.attested", "auth_method": "a", "api_provider": "b",
                "paid_continuation_verified_off": True, "fallback": "none", **bad}
        with pytest.raises(ValidationError):
            ev.parse_event(data)
    assert "billing.attested" not in ev.TERMINAL_TYPES


# --- a successful run exposes the exact fields ---------------------------------------------------------------


async def test_successful_run_reports_the_attested_fields(tmp_path):
    key = await _project(cap=12.5)
    rid = await _submit(key)
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.MessageDelta(text="hi"),
                                                           ev.RunCompleted(final_text="ok")]))
    assert await execute_run(rid, _cfg(tmp_path)) == "completed"
    got = await _get(key, rid)
    assert {k: got[k] for k in FIELDS} == {
        "auth_method": "claude.ai", "api_provider": "firstParty",
        "paid_continuation_verified_off": True, "fallback": "none", "project_daily_cap_usd": 12.5}
    assert got["billing_mode"] == "subscription"                 # existing fields stay as they were
    assert "usage_source" in got and got["cost_usd"] is None


async def test_attestation_is_written_after_the_guard_and_before_the_cli_starts(tmp_path):
    key = await _project()
    rid = await _submit(key)
    order = []

    class Spy(FakeAgentCLI):
        async def start(self, run):
            order.append([e.type for e in await list_events(rid)])
            async for e in super().start(run):
                yield e

    register_adapter("agent:claude", lambda: Spy([ev.RunCompleted()]))
    await execute_run(rid, _cfg(tmp_path))
    assert order == [["run.started", "billing.attested"]]       # already stored when the CLI starts
    assert (await _get(key, rid))["project_daily_cap_usd"] is None   # NULL cap -> null


async def test_event_payload_holds_only_the_non_secret_fields(tmp_path):
    key = await _project(cap=1)
    rid = await _submit(key)
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.RunCompleted()]))
    await execute_run(rid, _cfg(tmp_path))
    (att,) = (e for e in await list_events(rid) if e.type == "billing.attested")
    assert set(att.payload) == {"schema_version", "type", "account_id", *FIELDS}


async def test_cancel_response_carries_the_fields_too(tmp_path):
    key = await _project()
    rid = await _submit(key)
    register_adapter("agent:claude", lambda: FakeAgentCLI([ev.RunCompleted()]))
    await execute_run(rid, _cfg(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = (await c.post(f"/v1/agent/runs/{rid}/cancel", headers={"X-Project-Key": key})).json()
    assert r["auth_method"] == "claude.ai" and r["fallback"] == "none"


# --- refusals: no attestation, never a success with "unknown" ------------------------------------------------


@pytest.mark.parametrize("how", ["unknown_mode", "api_mode", "no_attestation", "api_env",
                                 "paid_continuation"])
async def test_refused_runs_start_nothing_and_have_no_attestation(tmp_path, how):
    key = await _project()
    rid = await _submit(key, **({"run_mode": "autonomous"} if how == "paid_continuation" else {}))
    fake = FakeAgentCLI([ev.RunCompleted()], mode="unknown" if how == "unknown_mode" else (
        "api" if how == "api_mode" else "subscription"))
    if how == "no_attestation":
        fake.auth = None
    register_adapter("agent:claude", lambda: fake)
    cfg = _cfg(tmp_path, env={"ANTHROPIC_API_KEY": "x"} if how == "api_env" else {},
               paid_continuation_verified_off=how != "paid_continuation")
    assert await execute_run(rid, cfg) == "failed" and fake.started is None
    types = [e.type for e in await list_events(rid)]
    assert "billing.attested" not in types and types[-1] == "run.failed"
    assert (await list_events(rid))[-1].payload["error_class"] == "billing_mode_refused"
    got = await _get(key, rid)
    assert got["status"] == "failed" and all(got[k] is None for k in FIELDS)


# --- values come from the probe and the settings ----------------------------------------------------------------


async def test_values_follow_the_probe_the_setting_and_the_project_cap(tmp_path):
    seen = []
    for method, provider, off, cap in (("claude.ai", "firstParty", True, 3.0),
                                       ("claude.ai", "vertex-proxy", False, None)):
        key = await _project(cap=cap)
        rid = await _submit(key, run_mode="manual", accept_paid_continuation_risk=True)
        fake = FakeAgentCLI([ev.RunCompleted()])
        fake.auth = AuthAttestation(method, provider)           # "change the fake probe"
        register_adapter("agent:claude", lambda f=fake: f)
        await execute_run(rid, _cfg(tmp_path, paid_continuation_verified_off=off))
        got = await _get(key, rid)
        seen.append((got["auth_method"], got["api_provider"],
                     got["paid_continuation_verified_off"], got["project_daily_cap_usd"]))
    assert seen == [("claude.ai", "firstParty", True, 3.0), ("claude.ai", "vertex-proxy", False, None)]


# --- the real adapter's probe -----------------------------------------------------------------------------------------


async def test_claude_cli_attestation_comes_from_the_probe_and_carries_no_other_field():
    out = json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty",
                      "email": "owner@example.com", "token": "sk-secret"})

    async def probe(argv, env):
        return out

    cli = real_cli.ClaudeCLI(probe=probe)
    assert cli.attestation() is None                              # nothing probed yet
    assert await cli.billing_mode() == "subscription"
    att = cli.attestation()
    assert (att.auth_method, att.api_provider) == ("claude.ai", "firstParty")
    assert not hasattr(att, "email") and "owner" not in repr(att) and "sk-secret" not in repr(att)


@pytest.mark.parametrize("probe_out", [
    json.dumps({"loggedIn": False, "authMethod": "none"}),
    json.dumps({"loggedIn": True, "authMethod": "api_key", "apiProvider": "firstParty"}),
    json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "bedrock"}),
    "garbage"])
async def test_claude_cli_has_no_attestation_unless_a_subscription_login_is_seen(probe_out):
    async def probe(argv, env):
        return probe_out

    cli = real_cli.ClaudeCLI(probe=probe)
    assert await cli.billing_mode() != "subscription" and cli.attestation() is None

    async def boom(argv, env):
        raise OSError("no binary")

    broken = real_cli.ClaudeCLI(probe=boom)
    assert await broken.billing_mode() == "unknown" and broken.attestation() is None


def test_docs_describe_the_attestation_and_the_write_edit_rule():
    docs = (Path(__file__).resolve().parent.parent / "docs" / "agent-runs.md").read_text(
        encoding="utf-8")
    for needle in ("billing.attested", "auth_method", "api_provider", "project_daily_cap_usd",
                   "Write(scope)", "Edit(scope)", "547957"):
        assert needle in docs, needle


# --- quota wait -> retry re-attests ---------------------------------------------------------------------------


async def test_a_quota_retry_attests_again_and_resumes_the_session(tmp_path):
    from datetime import timedelta

    from aibroker.agent.runs import _now

    key = await _project(cap=5.0)
    rid = await _submit(key)
    seen_at_start: list[list[str]] = []
    probes: list[int] = []

    class Counting(FakeAgentCLI):
        async def billing_mode(self):
            probes.append(1)
            return await super().billing_mode()

        async def start(self, run):
            seen_at_start.append([e.type for e in await list_events(rid)])
            async for e in super().start(run):
                yield e

    first = Counting([ev.SessionAssigned(session_id="S1"),
                      ev.RunFailed(error_class="quota_exhausted", message="usage limit reached")])
    first.auth = AuthAttestation("claude.ai", "firstParty")
    register_adapter("agent:claude", lambda: first)
    assert await execute_run(rid, _cfg(tmp_path)) == "waiting_quota"
    waiting = await _get(key, rid)
    assert waiting["status"] == "waiting_quota" and waiting["api_provider"] == "firstParty"

    async with get_session() as s:                                # the wait is over; worker re-claims
        job = await s.get(DeepJobRow, rid)
        job.run_after = _now() - timedelta(seconds=1)
        job.status = "running"
    second = Counting([ev.RunCompleted(final_text="done")])
    second.auth = AuthAttestation("claude.ai", "firstParty-second-probe")   # the probe changed
    register_adapter("agent:claude", lambda: second)
    assert await execute_run(rid, _cfg(tmp_path)) == "completed"

    types = [e.type for e in await list_events(rid)]
    assert types == ["run.started", "billing.attested", "session.assigned", "run.waiting_quota",
                     "run.started", "billing.attested", "run.completed"]
    assert types.count("billing.attested") == 2                   # one per attempt
    assert seen_at_start == [["run.started", "billing.attested"],
                             types[:5] + ["billing.attested"]]    # each stored BEFORE its CLI start
    assert len(probes) == 2                                       # the probe ran on both attempts
    assert second.started.session_id == "S1"                      # resumed, not lost
    restart = [e for e in await list_events(rid) if e.type == "run.started"][1]
    assert restart.payload["resumed_session_id"] == "S1"
    final = await _get(key, rid)
    assert final["status"] == "completed" and final["session_id"] == "S1"
    assert final["api_provider"] == "firstParty-second-probe"     # GET shows the 2nd attempt
    assert final["fallback"] == "none" and final["project_daily_cap_usd"] == 5.0
