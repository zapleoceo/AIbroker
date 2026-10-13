"""API chat gate: structured output is validated like the subscription path
(object for json_object, schema conformance for json_schema), via ONE shared
validator (agent.subscription_job.output_problem)."""
from __future__ import annotations

from types import SimpleNamespace

import aibroker.services.llm_service as svc

SCHEMA_RF = {"type": "json_schema", "json_schema": {"name": "r", "strict": True, "schema": {
    "type": "object", "required": ["answer"],
    "properties": {"answer": {"type": "integer"}}}}}


def _gate(rf, provider="deepseek"):
    return svc._chat_gate(provider=provider, key_id=1, response_format=rf,
                          tools=None, tool_choice=None)


def test_chat_gate_json_object_rejects_non_object():
    gate = _gate({"type": "json_object"})
    for bad in ("[1,2,3]", '"just a string"', "42", "null"):
        rej = gate(bad, {})
        assert rej is not None and rej.error_kind == "InvalidJSON", bad
    assert gate('{"a": 1}', {}) is None
    assert gate("", {}).error_kind == "EmptyBody"
    assert gate("not json{", {}).error_kind == "InvalidJSON"


def test_chat_gate_json_schema_validates_against_schema():
    gate = _gate(SCHEMA_RF)
    assert gate('{"wrong": "shape"}', {}).error_kind == "InvalidJSON"
    assert gate('{"answer": "text"}', {}).error_kind == "InvalidJSON"
    assert gate("[1]", {}).error_kind == "InvalidJSON"


def test_valid_schema_answer_accepted():
    assert _gate(SCHEMA_RF)('{"answer": 7}', {}) is None


def test_fenced_json_tolerance_unchanged():
    # json.loads never tolerated fences on the API path; the shared validator
    # keeps exactly that (same as the subscription path).
    for rf in ({"type": "json_object"}, SCHEMA_RF):
        assert _gate(rf)('```json\n{"answer": 1}\n```', {}).error_kind == "InvalidJSON"


def test_unusable_schema_degrades_to_parse_only():
    bad = {"type": "json_schema", "json_schema": {"schema": {"type": "no-such-type"}}}
    assert _gate(bad)('{"x": 1}', {}) is None
    assert _gate(bad)("nope", {}).error_kind == "InvalidJSON"
    ref = {"type": "json_schema", "json_schema": {"schema": {"$ref": "#/$defs/missing"}}}
    assert _gate(ref)('{"x": 1}', {}) is None


async def _noop():
    return None


async def test_downgraded_provider_schema_violation_walks_to_next(monkeypatch):
    """cerebras is downgraded json_schema->json_object, so it can return valid JSON
    that violates the schema; that is now InvalidJSON and the walk moves on."""
    picks: list[str] = []
    keys = {"cerebras": SimpleNamespace(id=1, label="a", tier="free", provider="cerebras",
                                        token_encrypted="x"),
            "gemini": SimpleNamespace(id=2, label="b", tier="free", provider="gemini",
                                      token_encrypted="x")}

    async def fake_pick(provider, scope, **kw):
        picks.append(provider)
        return keys.get(provider)

    async def fake_caps(**kw):
        return None

    async def fake_call_llm(**kw):
        m = kw["model"]
        body = '{"wrong": "shape"}' if m.startswith("cerebras") else '{"answer": 3}'
        return body, {"model": m, "tokens_in": 10, "tokens_out": 5, "cost_usd": 0.0,
                      "latency_ms": 10, "cache_read_tokens": 0, "cache_write_tokens": 0}

    monkeypatch.setattr(svc, "pick_and_reserve", fake_pick)
    monkeypatch.setattr("aibroker.services.attempt.reserve_cost", fake_caps)
    monkeypatch.setattr("aibroker.services.attempt.release_cost", fake_caps)
    monkeypatch.setattr(svc, "call_llm", fake_call_llm)
    monkeypatch.setattr(svc, "model_for", lambda p, c: f"{p}/model")
    monkeypatch.setattr("aibroker.services.attempt.decrypt", lambda t: "plain")
    monkeypatch.setattr("aibroker.services.attempt.record_usage", lambda **kw: _noop())
    monkeypatch.setattr(svc, "chain_for", lambda cap: ["cerebras", "gemini"])
    monkeypatch.setattr(svc, "deprioritize_for_json", lambda c: c)

    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="vera"), capability="structured",
        messages=[{"role": "user", "content": "hi"}], model=None,
        max_tokens=128, temperature=0.7, response_format=SCHEMA_RF, workflow="w",
    )
    assert picks.count("cerebras") == 1 and "gemini" in picks
    assert out.text == '{"answer": 3}'


def _rf(schema):
    return {"type": "json_schema", "json_schema": {"schema": schema}}


def test_remote_ref_never_touches_network(monkeypatch):
    import socket

    from aibroker.agent import subscription_job as sj
    sj._checked_validator.cache_clear()
    calls: list = []

    def boom(*a, **k):
        calls.append(a)
        raise AssertionError("network touched")

    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    for ref in ("http://169.254.169.254/latest/meta-data/", "https://example.invalid/s.json"):
        for schema in ({"$ref": ref}, {"type": "object", "properties": {"a": {"$ref": ref}}}):
            assert sj.output_problem('{"a": 1}', _rf(schema)) is None  # parse-only
            assert sj.output_problem("nope", _rf(schema)).startswith("InvalidJSON")
    assert calls == []


def test_local_defs_ref_still_validates():
    from aibroker.agent import subscription_job as sj
    schema = {"$defs": {"n": {"type": "integer"}}, "type": "object",
              "properties": {"a": {"$ref": "#/$defs/n"}}}
    assert sj.output_problem('{"a": 1}', _rf(schema)) is None
    assert sj.output_problem('{"a": "x"}', _rf(schema)).startswith("InvalidJSON")


def test_json_schema_wrapper_without_schema_is_parse_only():
    from aibroker.agent import subscription_job as sj
    for rf in ({"type": "json_schema"}, {"type": "json_schema", "json_schema": {}},
               {"type": "json_schema", "json_schema": {"schema": "x"}}):
        assert sj.output_problem("[1]", rf) is None
        assert sj.output_problem("nope", rf).startswith("InvalidJSON")


def test_checked_schema_is_cached(monkeypatch):
    from jsonschema import Draft202012Validator

    from aibroker.agent import subscription_job as sj
    sj._checked_validator.cache_clear()
    n = {"c": 0}
    real = Draft202012Validator.check_schema

    def spy(schema, *a, **k):
        n["c"] += 1
        return real(schema, *a, **k)

    monkeypatch.setattr(Draft202012Validator, "check_schema", spy)
    rf = _rf({"type": "object", "properties": {"cached": {"type": "integer"}}})
    sj.output_problem('{"cached": 1}', rf)
    sj.output_problem('{"cached": 2}', rf)
    assert n["c"] == 1
    bad = _rf({"type": "no-such-type"})
    sj.output_problem("{}", bad)
    sj.output_problem("{}", bad)
    assert n["c"] == 2  # the unusable verdict is cached too


def test_oversized_schema_is_parse_only_and_not_cached(monkeypatch):
    from jsonschema import Draft202012Validator

    from aibroker.agent import subscription_job as sj
    sj._checked_validator.cache_clear()
    monkeypatch.setattr(Draft202012Validator, "check_schema",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("checked")))
    big = {"type": "object", "required": ["x"], "properties": {
        f"p{i}": {"type": "integer"} for i in range(sj.MAX_SCHEMA_BYTES // 10)}}
    assert sj.output_problem('{"y": 1}', _rf(big)) is None  # would fail "required" if checked
    assert sj.output_problem("nope", _rf(big)).startswith("InvalidJSON")
    assert sj._checked_validator.cache_info().currsize == 0
