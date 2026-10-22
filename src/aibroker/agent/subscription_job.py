"""Translate explicit subscription pins into the existing jobs contract."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Any

from jsonschema import Draft202012Validator, ValidationError
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from aibroker.agent.job_files import live_files_inline as live_files  # noqa: F401 - re-export
from aibroker.strict_json import loads_strict

_REF_KEYS = ("$ref", "$dynamicRef", "$recursiveRef")


def _has_ref(value: Any) -> bool:
    if isinstance(value, dict):
        return any(k in value for k in _REF_KEYS) or any(_has_ref(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_ref(v) for v in value)
    return False


def _refs(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [value[k] if isinstance(value[k], str) else "" for k in _REF_KEYS if k in value]
        return own + [r for v in value.values() for r in _refs(v)]
    if isinstance(value, list):
        return [r for v in value for r in _refs(v)]
    return []


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
        schema = _schema_of(response_format)
        if not isinstance(schema, dict):
            return "json_schema.schema must be an object"
        if _has_ref(schema):
            return "unsupported_for_subscription_model: json_schema references"
        return json_schema_problem(response_format)
    return "unsupported_for_subscription_model: response_format type"


# A caller schema over this size is REJECTED (never half-validated): check_schema on a 1.5 MB /
# 50k-property schema blocked the event loop for ~8.7 s on every cache miss, and each
# cache entry would pin the whole schema text in memory. 64 KiB covers real schemas.
MAX_SCHEMA_BYTES = 64 * 1024


def _schema_of(response_format: dict[str, Any]) -> Any:
    wrapper = response_format.get("json_schema")
    return wrapper.get("schema") if isinstance(wrapper, dict) else None


@lru_cache(maxsize=128)
def _checked_validator(schema_json: str) -> tuple[Draft202012Validator | None, str | None]:
    """(validator, None) for a schema (keyed on its canonical JSON) the broker can FULLY
    validate, else (None, why). Unusable = invalid, or a $ref / $dynamicRef / $recursiveRef
    that does not resolve inside the schema itself. The empty Registry means NO retrieval:
    a remote ref is never fetched (SSRF), it is refused. The verdict is cached too."""
    try:
        schema = json.loads(schema_json)
        Draft202012Validator.check_schema(schema)
        registry = Registry()
        resolver = registry.resolver_with_root(Resource.from_contents(schema, DRAFT202012))
        for ref in _refs(schema):
            if not ref.startswith("#"):
                return None, "json_schema has a remote reference"
            resolver.lookup(ref)
        return Draft202012Validator(schema, registry=registry), None
    except Unresolvable:
        return None, "json_schema has an unresolvable reference"
    except Exception:  # noqa: BLE001 - unusable schema: refused, never a crash
        return None, "json_schema is invalid"


def json_schema_problem(response_format: dict[str, Any]) -> str | None:
    """Why the broker cannot FULLY validate this json_schema request (so it is rejected at
    submit and fails closed at output), or None. Parse-only is never presented as validation."""
    schema = _schema_of(response_format)
    if not isinstance(schema, dict):
        return "json_schema.schema must be an object"
    try:
        canonical = json.dumps(schema, sort_keys=True)
    except (TypeError, ValueError):
        return "json_schema.schema is invalid"
    if len(canonical) > MAX_SCHEMA_BYTES:
        return f"json_schema.schema exceeds {MAX_SCHEMA_BYTES} bytes"
    return _checked_validator(canonical)[1]


def output_problem(text: str, response_format: dict[str, Any] | None) -> str | None:
    """The ONE structured-output check, shared by the subscription path and the API
    chat gate (services/llm_service.py): json_object must be a JSON object,
    json_schema must validate. Strict JSON (no NaN / Infinity / duplicate keys). A schema
    the broker cannot fully validate (oversized, invalid, unresolvable or remote ref) and any
    validator failure FAIL CLOSED: parse-only is never reported as schema validation."""
    if not response_format:
        return None
    try:
        value = loads_strict(text)
    except (TypeError, ValueError):
        return "InvalidJSON: subscription model did not return JSON"
    if response_format["type"] == "json_object" and not isinstance(value, dict):
        return "InvalidJSON: expected a JSON object"
    if response_format["type"] == "json_schema":
        if json_schema_problem(response_format):
            return "InvalidJSON: json_schema cannot be validated by the broker"
        validator = _checked_validator(json.dumps(_schema_of(response_format), sort_keys=True))[0]
        try:
            validator.validate(value)
        except ValidationError:
            return "InvalidJSON: response does not match json_schema"
        except Exception:  # noqa: BLE001 - a validator failure must not pass as success
            return "InvalidJSON: json_schema validation failed"
    return None
