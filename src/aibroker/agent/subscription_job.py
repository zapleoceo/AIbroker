"""Translate explicit subscription pins into the existing jobs contract."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from jsonschema import Draft202012Validator, ValidationError


def _has_ref(value: Any) -> bool:
    if isinstance(value, dict):
        return "$ref" in value or any(_has_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_ref(v) for v in value)
    return False


def prompt_for_messages(messages: list[dict[str, Any]], response_format: dict[str, Any] | None) -> str:
    """Keep role and tool history; input images use separate attachments."""
    turns = [{k: m[k] for k in ("role", "content", "tool_calls", "tool_call_id") if k in m}
             for m in messages]
    prompt = "Continue this conversation. Respect the roles in the JSON transcript:\n"
    prompt += json.dumps(turns, ensure_ascii=False)
    if response_format:
        prompt += "\nReturn only valid JSON matching the requested response format: "
        prompt += json.dumps(response_format, ensure_ascii=False)
    return prompt


def response_format_problem(response_format: dict[str, Any] | None) -> str | None:
    if response_format is None:
        return None
    kind = response_format.get("type")
    if kind == "json_object":
        return None
    if kind == "json_schema":
        wrapper = response_format.get("json_schema")
        schema = wrapper.get("schema") if isinstance(wrapper, dict) else None
        if not isinstance(schema, dict):
            return "json_schema.schema must be an object"
        if _has_ref(schema):
            return "unsupported_for_subscription_model: json_schema references"
        try:
            Draft202012Validator.check_schema(schema)
        except Exception:
            return "json_schema.schema is invalid"
        return None
    return "unsupported_for_subscription_model: response_format type"


def output_problem(text: str, response_format: dict[str, Any] | None) -> str | None:
    if not response_format:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return "InvalidJSON: subscription model did not return JSON"
    if response_format["type"] == "json_object" and not isinstance(value, dict):
        return "InvalidJSON: expected a JSON object"
    if response_format["type"] == "json_schema":
        try:
            Draft202012Validator(response_format["json_schema"]["schema"]).validate(value)
        except ValidationError:
            return "InvalidJSON: response does not match json_schema"
    return None


def live_files(files: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if files is None:
        return None
    now = datetime.now(UTC).isoformat()
    return [{**f, "content_b64": None, "expired": True}
            if f.get("expires_at") and f["expires_at"] < now else f for f in files]
