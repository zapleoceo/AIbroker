"""Redaction must never break or violate a structured (json_object / json_schema) answer."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aibroker.agent.events import RunCompleted
from aibroker.agent.redact import REDACTED, redact, redact_final_text
from aibroker.agent.runs import append_event, create_run, get_run
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import get_session
from aibroker.db.models import ProjectRow

OBJ = {"type": "json_object"}


def _schema(schema: dict) -> dict:
    return {"type": "json_schema", "json_schema": {"name": "x", "schema": schema}}


@pytest.mark.parametrize("text", [
    "access_token=xyzsecret1", "access_token: xyzsecret1", 'access_token: "xyzsecret1"',
    "access_token='xyzsecret1'", 'access_token="xyzsecret1"', "id_token = xyzsecret1, next",
    'access_token="xyzsecret1',
])
def test_token_kv_forms_stay_masked(text):
    out = redact(text)
    assert "xyzsecret1" not in out and REDACTED in out


def test_token_kv_never_eats_a_quote_it_did_not_open():
    assert redact('{"e":"access_token=abc"}') == '{"e":"access_token=[REDACTED]"}'
    assert redact("'refresh_token=abc'") == "'refresh_token=[REDACTED]'"


def test_structural_redaction_keeps_json_valid_and_masks_keys_values_and_nesting():
    out = redact_final_text(
        '{"a": "access_token=abc", "n": [{"b": "sk-abcdefghijk1"}], "access_token": "zzz"}', OBJ)
    assert json.loads(out) == {"a": "access_token=[REDACTED]", "n": [{"b": REDACTED}],
                               "access_token": REDACTED}


def test_untouched_json_and_plain_text_are_not_rewritten():
    assert redact_final_text('{"a":   1}', OBJ) == '{"a":   1}'
    assert redact_final_text("access_token=abc", None) == "access_token=[REDACTED]"
    assert redact_final_text("not json sk-abcdefghijk1", OBJ) == f"not json {REDACTED}"


async def _finish(final_text: str, response_format: dict | None, pid: int = 11):
    plain = generate_project_key()
    async with get_session() as session:
        session.add(ProjectRow(id=pid, name="redact-structured",
                               project_key_hash=hash_project_key(plain),
                               project_key_prefix=plain[:12],
                               allowed_scopes=["llm:chat", "llm:agent"], is_active=True, notes=""))
    run_id, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:codex", prompt="p", task_id="t",
        account_id="owner", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[], model="gpt-6.1-sol",
        client_job={"provider": "codex-sub", "model": "codex-sub/gpt-6.1-sol",
                    "response_format": response_format})
    await append_event(run_id, RunCompleted(final_text=final_text))
    return (await get_run(run_id, pid))[1]


async def test_json_object_with_token_kv_inside_a_string_stays_valid_json_and_done():
    row = await _finish('{"explanation":"access_token=abc"}', OBJ)
    assert row.status == "done"
    assert json.loads(row.result_text) == {"explanation": "access_token=[REDACTED]"}


async def test_redaction_touching_an_unconstrained_string_is_done_with_valid_json():
    fmt = _schema({"type": "object", "required": ["note"],
                   "properties": {"note": {"type": "string"}}})
    row = await _finish(json.dumps({"note": "key sk-abcdefghijk1 here"}), fmt)
    assert row.status == "done"
    assert json.loads(row.result_text) == {"note": f"key {REDACTED} here"}


@pytest.mark.parametrize("constraint", [{"const": "sk-abcdefghijk1"},
                                        {"enum": ["sk-abcdefghijk1", "other"]}])
async def test_redaction_violating_const_or_enum_ends_invalid_json_not_done(constraint):
    fmt = _schema({"type": "object", "required": ["v"],
                   "properties": {"v": {"type": "string", **constraint}}})
    row = await _finish(json.dumps({"v": "sk-abcdefghijk1"}), fmt)
    assert row.status == "error"
    assert row.error_message.startswith("invalid_json:")
    assert "sk-abcdefghijk1" not in (row.result_text or "")


def test_duplicate_keys_never_hide_a_secret_in_structural_redaction():
    """json.loads collapses duplicate keys, so `cleaned == value` returned the raw text with the
    secret; strict parsing falls back to the text redaction instead."""
    out = redact_final_text('{"a":"sk-AAAAAAAAAAAA","a":"x"}', {"type": "json_object"})
    assert "sk-AAAAAAAAAAAA" not in out and REDACTED in out


def test_an_unclosed_quote_does_not_mask_across_lines():
    out = redact('access_token="abc\nlots of text "end')
    assert out.startswith("access_token=" + REDACTED) and "lots of text" in out
