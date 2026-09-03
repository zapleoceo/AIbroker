"""Offline native tool contract and adapter regressions."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from aibroker.providers import litellm_adapter as adapter
from aibroker.routes.proxy import ChatRequest, _job_response
from aibroker.services.tool_contract import validate_result

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }
]
CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "search", "arguments": '{"city":"Da Nang"}'},
}


def test_request_roundtrip():
    body = ChatRequest.model_validate(
        {
            "tools": TOOLS,
            "tool_choice": "required",
            "messages": [
                {"role": "user", "content": "scooter"},
                {"role": "assistant", "content": None, "tool_calls": [CALL]},
                {"role": "tool", "tool_call_id": "call_1", "content": "[]"},
            ],
        }
    )
    assert body.messages[1].model_dump(exclude_unset=True)["content"] is None
    assert body.messages[2].tool_call_id == "call_1"
    assert body.tools[0].function.name == "search"


@pytest.mark.parametrize(
    "update",
    [
        {"tool_choice": "unknown"},
        {"tools": TOOLS * 2},
        {"response_format": {"type": "json_object"}},
        {"tool_choice": {"type": "function", "function": {"name": "drop_db"}}},
    ],
)
def test_reject_invalid_requests(update):
    with pytest.raises((ValueError, ValidationError)):
        ChatRequest.model_validate(
            {"messages": [{"role": "user", "content": "x"}], "tools": TOOLS, **update}
        )


@pytest.mark.parametrize("reason", ["length", "content_filter", None, "unknown"])
def test_incomplete_cannot_succeed(reason):
    assert (
        validate_result("", {"finish_reason": reason, "tool_calls": [CALL]}, TOOLS, "required")
        is not None
    )


@pytest.mark.parametrize(
    "arguments", ["{", "{}", '{"city":3}', '{"city":NaN}', '{"city":"a","city":"b"}']
)
def test_arguments_fail_closed(arguments):
    call = deepcopy(CALL)
    call["function"]["arguments"] = arguments
    assert (
        validate_result(
            "", {"finish_reason": "tool_calls", "tool_calls": [call]}, TOOLS, "required"
        )
        == "InvalidToolArguments"
    )


def test_unknown_and_duplicate_calls():
    call = deepcopy(CALL)
    call["function"]["name"] = "drop_db"
    assert (
        validate_result(
            "", {"finish_reason": "tool_calls", "tool_calls": [call]}, TOOLS, "required"
        )
        == "UnknownToolCall"
    )
    assert (
        validate_result(
            "", {"finish_reason": "tool_calls", "tool_calls": [CALL, CALL]}, TOOLS, "required"
        )
        == "InvalidToolCall"
    )


def test_valid_tool_only_and_text_final():
    assert (
        validate_result(
            "", {"finish_reason": "tool_calls", "tool_calls": [CALL]}, TOOLS, "required"
        )
        is None
    )
    assert validate_result("done", {"finish_reason": "stop"}, TOOLS, "auto") is None
    assert (
        validate_result("done", {"finish_reason": "stop"}, TOOLS, "required") == "MissingToolCall"
    )
    assert (
        validate_result("no", {"finish_reason": "stop", "refusal": "blocked"}, TOOLS, "auto")
        == "ToolRefusal"
    )


async def test_adapter_preserves_tool_metadata(monkeypatch):
    completion = AsyncMock(
        return_value=SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message={"content": None, "tool_calls": [CALL]}, finish_reason="tool_calls"
                )
            ],
            usage={},
        )
    )
    monkeypatch.setattr(adapter.litellm, "acompletion", completion)
    text, meta = await adapter.call_llm(
        model="openai/gpt-4o-mini",
        messages=[{"role": "user", "content": "find"}],
        api_key="test",
        tools=TOOLS,
        tool_choice="required",
    )
    assert text == ""
    assert meta["tool_calls"] == [CALL]
    assert meta["finish_reason"] == "tool_calls"
    assert completion.call_args.kwargs["drop_params"] is False
    assert completion.call_args.kwargs["tools"] == TOOLS


def test_poll_preserves_metadata():
    response = _job_response(
        SimpleNamespace(
            id=1,
            status="done",
            result_text="",
            result_meta={"tool_calls": [CALL], "finish_reason": "tool_calls"},
        )
    )
    assert response.tool_calls == [CALL]
    assert response.finish_reason == "tool_calls"


@pytest.mark.parametrize("override", [None, "openai/pinned"])
async def test_service_filters_routes_and_fails_over_invalid_call(monkeypatch, override):
    from aibroker.services import llm_service as svc

    key = SimpleNamespace(id=1, label="test", tier="free", token_encrypted="x", account_id=None)
    monkeypatch.setattr(svc, "chain_for", lambda _: ["cohere", "gemini", "openai"])
    monkeypatch.setattr(svc, "model_for", lambda provider, _: provider + "/model")
    monkeypatch.setattr(svc, "rotation_for", lambda *_: [])
    monkeypatch.setattr(svc, "pick_and_reserve", AsyncMock(return_value=key))
    monkeypatch.setattr(svc, "decrypt", lambda _: "test")
    for name in ("reserve_cost", "release_cost", "note_affinity_shared"):
        monkeypatch.setattr(svc, name, AsyncMock())
    record = AsyncMock(return_value=42)
    monkeypatch.setattr(svc, "record_usage", record)
    meta = {
        "model": "openai/model",
        "tokens_in": 10,
        "tokens_out": 20,
        "cost_usd": 0,
        "latency_ms": 5,
        "tool_calls": [CALL],
    }
    outcomes = [
        ("", {**meta, "finish_reason": "length"}),
        ("", {**meta, "finish_reason": "tool_calls"}),
    ]
    provider = AsyncMock(side_effect=outcomes if override is None else outcomes[1:])
    monkeypatch.setattr(svc, "call_llm", provider)
    out = await svc.run_chat(
        project=SimpleNamespace(id=1, name="test"),
        capability="chat:fast",
        messages=[{"role": "user", "content": "find"}],
        model=override,
        max_tokens=1000,
        temperature=0,
        response_format=None,
        workflow=None,
        tools=TOOLS,
        tool_choice="required",
    )
    assert out.tool_calls == [CALL]
    assert [call.kwargs["model"] for call in provider.call_args_list] == (
        ["gemini/model", "openai/model"] if override is None else ["openai/pinned"]
    )
    if override is None:
        assert record.call_args_list[0].kwargs["error_kind"] == "IncompleteToolResponse"
    else:
        assert svc.pick_and_reserve.call_args.args == ("openai",)
    assert provider.call_args.kwargs["tools"] == TOOLS


@pytest.mark.parametrize(
    "schema", [{"type": "garbage"}, {"type": "object", "$ref": "https://example.org/private"}]
)
def test_unsafe_schema_rejected(schema):
    tools = deepcopy(TOOLS)
    tools[0]["function"]["parameters"] = schema
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": [{"role": "user", "content": "x"}], "tools": tools})


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "tool", "tool_call_id": "unknown", "content": "[]"}],
        [{"role": "assistant", "content": None, "tool_calls": [CALL]}],
        [
            {"role": "assistant", "content": None, "tool_calls": [CALL, CALL]},
            {"role": "tool", "tool_call_id": "call_1", "content": "[]"},
        ],
    ],
)
def test_invalid_history_rejected(messages):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate({"messages": messages, "tools": TOOLS})


def test_stop_with_calls_is_inconsistent():
    assert (
        validate_result("", {"finish_reason": "stop", "tool_calls": [CALL]}, TOOLS, "required")
        == "IncompleteToolResponse"
    )


def test_overflow_float_rejected_even_when_schema_allows_numbers():
    tools = deepcopy(TOOLS)
    tools[0]["function"]["parameters"]["properties"]["city"] = {"type": "number"}
    call = deepcopy(CALL)
    call["function"]["arguments"] = '{"city":1e999}'
    assert (
        validate_result(
            "", {"finish_reason": "tool_calls", "tool_calls": [call]}, tools, "required"
        )
        == "InvalidToolArguments"
    )


@pytest.mark.parametrize("model", ["cohere/command", "gpt-4o-mini", "openai/"])
def test_native_model_override_requires_enabled_provider(model):
    with pytest.raises(ValidationError):
        ChatRequest.model_validate(
            {"model": model, "tools": TOOLS, "messages": [{"role": "user", "content": "find"}]}
        )
    # The old no-tools contract is deliberately unchanged.
    assert (
        ChatRequest.model_validate(
            {"model": model, "messages": [{"role": "user", "content": "find"}]}
        ).model
        == model
    )


async def test_internal_invalid_override_fails_before_key_selection(monkeypatch):
    from aibroker.services import llm_service as svc

    picker = AsyncMock()
    monkeypatch.setattr(svc, "pick_and_reserve", picker)
    with pytest.raises(ValueError, match="qualified"):
        await svc.run_chat(
            project=SimpleNamespace(id=1, name="test"),
            capability="chat:fast",
            messages=[{"role": "user", "content": "find"}],
            model="cohere/command",
            max_tokens=1000,
            temperature=0,
            response_format=None,
            workflow=None,
            tools=TOOLS,
        )
    picker.assert_not_called()


def test_prompt_estimate_counts_call_history_once():
    import json

    from aibroker.providers.context_limits import estimate_prompt_tokens

    call = deepcopy(CALL)
    call["function"]["arguments"] = json.dumps({"city": "x" * 8000})
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [call]},
        {"role": "tool", "content": "data", "tool_call_id": "call_1"},
    ]
    extra = len(json.dumps({"tool_calls": [call]}, ensure_ascii=False, separators=(",", ":")))
    extra += len(json.dumps({"tool_call_id": "call_1"}, separators=(",", ":")))
    assert estimate_prompt_tokens(messages) == (extra + len("data")) // 4
    assert estimate_prompt_tokens(messages) > 2000
    assert estimate_prompt_tokens([{"role": "user", "content": "abcdefgh"}]) == 2
