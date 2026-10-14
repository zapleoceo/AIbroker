"""Strict JSON parsing shared by the tool-call gate and the structured-output gate."""
from __future__ import annotations

import json
import math
from typing import Any


def loads_strict(text: str) -> Any:
    """`json.loads` that rejects NaN / Infinity, non-finite numbers and duplicate keys."""
    return json.loads(
        text,
        parse_constant=_invalid_constant,
        parse_float=_finite_float,
        object_pairs_hook=_unique_object,
    )


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
