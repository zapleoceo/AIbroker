"""Native tool-call validation; the gateway never executes client tools."""

from __future__ import annotations

import json
import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import BaseModel, ConfigDict, Field, model_validator

TOOL_PROVIDERS = frozenset({"openai", "anthropic", "gemini", "mistral"})


def tool_model_provider(model: str | None) -> str | None:
    """Native overrides must identify the key owner; never guess or cross-route."""
    if model is None:
        return None
    provider, separator, name = model.partition("/")
    if not separator or provider not in TOOL_PROVIDERS or not name.strip():
        raise ValueError("native tool model must be qualified by an enabled provider")
    return provider


class FunctionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    description: str | None = None
    parameters: dict[str, Any]
    strict: bool | None = None

    @model_validator(mode="after")
    def valid_schema(self) -> FunctionDefinition:
        try:
            Draft202012Validator.check_schema(self.parameters)
        except SchemaError as exc:
            raise ValueError("invalid tool parameter schema") from exc
        if self.parameters.get("type") != "object":
            raise ValueError("tool parameters must be an object schema")

        # Remote references must never cause network I/O during validation.
        def local_refs(value: Any) -> None:
            if isinstance(value, dict):
                for key, child in value.items():
                    if key in {"$ref", "$dynamicRef"} and (
                        not isinstance(child, str) or not child.startswith("#")
                    ):
                        raise ValueError("only local schema references are supported")
                    local_refs(child)
            elif isinstance(value, list):
                for child in value:
                    local_refs(child)

        local_refs(self.parameters)
        return self


class ToolDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: str = Field(pattern="^function$")
    function: FunctionDefinition


def validate_choice(tools: list[dict[str, Any]], choice: Any) -> None:
    names = [tool["function"]["name"] for tool in tools]
    if len(set(names)) != len(names):
        raise ValueError("duplicate tool names")
    if choice is None or choice in ("auto", "none", "required"):
        return
    if isinstance(choice, dict) and choice.get("type") == "function":
        function = choice.get("function")
        if isinstance(function, dict) and function.get("name") in names:
            return
    raise ValueError("tool_choice must select a declared function")


def validate_result(
    text: str, meta: dict[str, Any], tools: list[dict[str, Any]], choice: Any
) -> str | None:
    """Stable failure labels for telemetry, without logging tool arguments."""
    finish = meta.get("finish_reason")
    if meta.get("refusal") or finish == "content_filter":
        return "ToolRefusal"
    if finish not in {"stop", "tool_calls"}:
        return "IncompleteToolResponse"
    calls = meta.get("tool_calls") or []
    if not calls:
        if choice == "required" or isinstance(choice, dict) or finish == "tool_calls":
            return "MissingToolCall"
        return None if text.strip() else "EmptyBody"
    if choice == "none" or not isinstance(calls, list) or len(calls) > 16:
        return "InvalidToolCall"
    if finish != "tool_calls":
        return "IncompleteToolResponse"
    schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in tools}
    seen: set[str] = set()
    for call in calls:
        try:
            if not isinstance(call, dict) or call.get("type") != "function":
                return "InvalidToolCall"
            call_id = call["id"]
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                return "InvalidToolCall"
            seen.add(call_id)
            name = call["function"]["name"]
            if name not in schemas:
                return "UnknownToolCall"
            if isinstance(choice, dict) and name != choice["function"]["name"]:
                return "UnexpectedToolCall"
            arguments = call["function"]["arguments"]
            if not isinstance(arguments, str):
                return "InvalidToolArguments"
            parsed = json.loads(
                arguments,
                parse_constant=_invalid_constant,
                parse_float=_finite_float,
                object_pairs_hook=_unique_object,
            )
            Draft202012Validator(schemas[name]).validate(parsed)
        except Exception:  # fail closed for malformed output or unresolved schema references
            return "InvalidToolArguments"
    return None


def _invalid_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("nonfinite JSON number")
    return number


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result
