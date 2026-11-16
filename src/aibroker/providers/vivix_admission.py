"""Fail-closed admission using a request-specific, externally verified USD ceiling."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from aibroker.providers.gemini_video import VideoRejected
from aibroker.providers.vivix import _world
from aibroker.providers.vivix_inputs import validate_inputs


def request_digest(model: str, inputs: dict[str, Any]) -> str:
    try:
        raw = json.dumps([model, inputs], sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode()
    except (TypeError, ValueError) as exc:
        raise VideoRejected("Vivix inputs must be finite JSON") from exc
    return hashlib.sha256(raw).hexdigest()


def usd(value: str) -> Decimal:
    if not isinstance(value, str):
        raise VideoRejected("USD amount must be a decimal string")
    try:
        amount = Decimal(value)
    except (ValueError, ArithmeticError, TypeError) as exc:
        raise VideoRejected("Invalid USD amount") from exc
    if not amount.is_finite() or amount < 0:
        raise VideoRejected("Invalid USD amount")
    return amount


@dataclass(frozen=True)
class VivixQuote:
    """Trusted internal quote; never deserialize this from the public video request."""

    account_id: str
    request_hash: str
    ceiling_usd: str
    evidence: str
    expires_at: float
    includes_reference_fees: bool = False


def admit(*, model: str, inputs: dict[str, Any], account_id: str,
          allowed_models: frozenset[str], quote: VivixQuote | None,
          now: float) -> str:
    world = _world(model)
    if not account_id or model not in allowed_models:
        raise VideoRejected("Vivix account/model access is not verified")
    if "model" in inputs:
        raise VideoRejected("Vivix model must be passed separately")
    if world:
        jobs = inputs.get("jobs")
        if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
            raise VideoRejected("Vivix W1 requires exactly one job")
    digest = request_digest(model, inputs)
    validate_inputs(model, inputs)
    if (quote is None or quote.account_id != account_id or quote.request_hash != digest
            or not quote.evidence.strip() or not quote.includes_reference_fees
            or not math.isfinite(quote.expires_at) or not now < quote.expires_at):
        raise VideoRejected("Vivix complete, current pricing evidence is required")
    ceiling = usd(quote.ceiling_usd)
    if ceiling <= 0:
        raise VideoRejected("Vivix unknown/zero pricing cannot admit paid generation")
    return str(ceiling)
