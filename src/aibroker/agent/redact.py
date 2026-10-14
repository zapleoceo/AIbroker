"""Secret redaction for everything an agent run persists (events, final text, error text, returned
file content): a CLI that was talked into `cat auth.json` must not leave a token in our database or
API responses. Masks JWT-like strings, token fields (JSON or key=value), `sk-` API keys and `sess-`
session tokens with `[REDACTED]`. Pattern based and best effort - the sandbox / tool allow-list are the
real controls; this is the second net.
"""
from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[REDACTED]"

_JWT = re.compile(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_TOKEN_JSON = re.compile(
    r'("(?:refresh_token|access_token|id_token)"\s*:\s*)"[^"]*"', re.IGNORECASE)
_TOKEN_KV = re.compile(
    r"\b((?:refresh_token|access_token|id_token)\s*[=:]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[\"']?[^\s\"',}]+)", re.IGNORECASE)
_SK = re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]{8,}")
_SESS = re.compile(r"\bsess-[A-Za-z0-9_-]{8,}")


def redact(text: str) -> str:
    """`text` with every secret-looking span replaced by [REDACTED]."""
    text = _TOKEN_JSON.sub(lambda m: f'{m.group(1)}"{REDACTED}"', text)
    text = _TOKEN_KV.sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _JWT.sub(REDACTED, text)
    text = _SK.sub(REDACTED, text)
    return _SESS.sub(REDACTED, text)


def redact_obj(value: Any) -> Any:
    """Recursively redact every string inside dicts / lists (keys that name a token also lose
    their value)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {(redact(k) if isinstance(k, str) else k): (REDACTED if isinstance(k, str) and k.lower() in _TOKEN_KEYS
                    and isinstance(v, str) else redact_obj(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value


_TOKEN_KEYS = frozenset({"refresh_token", "access_token", "id_token"})


def redact_final_text(text: str, response_format: dict[str, Any] | None = None) -> str:
    """`redact(text)`, except for a structured-output answer (json_object / json_schema) that
    parses as JSON: then only the string values are redacted and the document is re-serialized,
    so a masked span can never eat a quote or brace and break the client's JSON. Untouched
    text is returned byte for byte."""
    if response_format and response_format.get("type") in ("json_object", "json_schema"):
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return redact(text)
        cleaned = redact_obj(value)
        return text if cleaned == value else json.dumps(cleaned, ensure_ascii=False)
    return redact(text)


def redact_bytes(data: bytes) -> bytes:
    """Redact file content that is UTF-8 text; binary content (images, ...) is returned unchanged."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    cleaned = redact(text)
    return data if cleaned == text else cleaned.encode("utf-8")
