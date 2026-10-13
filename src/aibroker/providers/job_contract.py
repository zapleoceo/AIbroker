"""Broker jobs request rules exposed in the model catalog and used at submit."""
from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Any, Protocol

from aibroker.agent.subscription_job import response_format_problem
from aibroker.config import get_settings
from aibroker.providers.registry import ModelSpec

DEFAULT_MAX_TOKENS = 1024
DEFAULT_TEMPERATURE = 0.7
JOB_MAX_TOKENS_MIN = 1
JOB_MAX_TOKENS_MAX = 16384
JOB_TEMPERATURE_MIN = 0.0
JOB_TEMPERATURE_MAX = 2.0
IGNORED_SUBSCRIPTION_PARAMS = ("max_tokens", "temperature")
SUBSCRIPTION_ROLES = ("system", "user", "assistant")
SUBSCRIPTION_RESPONSE_FORMATS = ("json_object", "json_schema")
JOB_CAPABILITIES = frozenset({
    "chat:fast", "chat:smart", "chat:sales", "chat:code", "chat:edit",
    "chat:deep", "structured", "prefilter", "translate", "vision",
})


class _Message(Protocol):
    role: str
    content: str | list[dict[str, Any]] | None
    tool_calls: list[dict[str, Any]] | None
    tool_call_id: str | None


class _JobBody(Protocol):
    messages: Sequence[_Message]
    response_format: dict[str, Any] | None
    tools: list[Any] | None
    tool_choice: Any
    stream: bool | None


def job_request_contract(spec: ModelSpec, tools_supported: bool | None) -> dict[str, Any] | None:
    """Document Broker acceptance, not unverified upstream model limits."""
    if not spec.capabilities & JOB_CAPABILITIES:
        return None
    if spec.pricing == "subscription":
        return {
            "max_tokens": {"default": DEFAULT_MAX_TOKENS,
                           "accepted": {"minimum": JOB_MAX_TOKENS_MIN,
                                        "maximum": JOB_MAX_TOKENS_MAX}, "forwarded": False},
            "temperature": {"default": DEFAULT_TEMPERATURE,
                            "accepted": {"minimum": JOB_TEMPERATURE_MIN,
                                         "maximum": JOB_TEMPERATURE_MAX}, "forwarded": False},
            "message_roles": [*SUBSCRIPTION_ROLES, "tool"] if tools_supported else list(SUBSCRIPTION_ROLES),
            "message_content": (["text", "image_data_url"] if "vision" in spec.capabilities
                                else ["text"]),
            "tools": tools_supported,
            "tool_history": bool(tools_supported),
            "stream": False,
            "response_format_types": list(SUBSCRIPTION_RESPONSE_FORMATS),
            "json_schema_refs": False,
            "max_prompt_chars": subscription_prompt_max_chars(),
        }
    return {
        "max_tokens": {"default": DEFAULT_MAX_TOKENS,
                       "accepted": {"minimum": JOB_MAX_TOKENS_MIN,
                                    "maximum": JOB_MAX_TOKENS_MAX}, "forwarded": True},
        "temperature": {"default": DEFAULT_TEMPERATURE,
                        "accepted": {"minimum": JOB_TEMPERATURE_MIN,
                                     "maximum": JOB_TEMPERATURE_MAX}, "forwarded": True},
        "message_roles": None,
        "message_content": None,
        "tools": tools_supported,
        "tool_history": None,
        "stream": None,
        "response_format_types": None,
        "json_schema_refs": None,
        "max_prompt_chars": None,
    }


def subscription_request_problem(body: _JobBody, capability: str, provider: str) -> str | None:
    """Reject controls that the Claude/Codex CLI route cannot apply (sampling params are only ignored)."""
    if body.tools and capability == "vision":
        return "unsupported_for_subscription_model: tools on vision"
    if body.tools and provider != "codex-sub":
        return "unsupported_for_subscription_model: tools"
    if body.stream:
        return "unsupported_for_subscription_model: stream"
    with_tools = bool(body.tools and provider == "codex-sub")
    if any((m.role not in (*SUBSCRIPTION_ROLES, "tool") if with_tools
            else m.role not in SUBSCRIPTION_ROLES or bool(m.tool_calls or m.tool_call_id))
           or not (isinstance(m.content, str | list) or
                   (with_tools and m.role == "assistant" and m.content is None and m.tool_calls))
           or (isinstance(m.content, list) and provider != "codex-sub")
           for m in body.messages):
        return "unsupported_for_subscription_model: tool history or image input"
    if capability == "vision" and provider != "codex-sub":
        return "unsupported_for_subscription_model: vision"
    if problem := response_format_problem(body.response_format):
        return problem
    return None


def ignored_subscription_params(fields_set: Collection[str]) -> list[str]:
    """Sampling params the caller set that the CLI route cannot apply: they are accepted so one
    request body works on every model, and named in the result so no caller assumes they took effect."""
    return [name for name in IGNORED_SUBSCRIPTION_PARAMS if name in fields_set]


def subscription_prompt_max_chars() -> int:
    return get_settings().AGENT_SUBSCRIPTION_PROMPT_MAX_CHARS


def subscription_prompt_problem(prompt: str) -> str | None:
    limit = subscription_prompt_max_chars()
    return f"subscription prompt exceeds {limit} characters" if len(prompt) > limit else None
