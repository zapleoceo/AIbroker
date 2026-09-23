"""Decision models — typed choices instead of generated text (TypeSafe Jev).

A decision model does not take chat messages and does not write prose: it
takes a `state` (the text to judge) and a map of named, typed `questions`
(`choice` / `score` / `noul`) and returns one typed answer per question with
calibrated probabilities. OpenRouter serves them on a dedicated endpoint —
`/api/alpha/decisions`; `/chat/completions` refuses them outright with 400
"is a decisions model and cannot be used with the chat/completions endpoint".
That is why this is its own adapter and not a LiteLLM model string.

Question shapes (TypeSafe HTTP API, docs.typesafe.ai/api):
    noul    {"type": "noul",   "instructions": ..., "criteria": {"true": ..., "false": ...}}
    choice  {"type": "choice", "instructions": ..., "criteria": {"<key>": "<description>", ...}}  ≤255 options
    score   {"type": "score",  "instructions": ..., "criteria": ["level 0", "level 1", ...]}   2–10 levels
"""
from __future__ import annotations

import time
from typing import Any

import httpx

OPENROUTER_DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

# Measured 2026-09-23: 120 real Vera triage events, median 0.36s, p90 0.46s,
# max 1.49s. 20s is a hang guard, not a latency budget.
_TIMEOUT_S = 20.0

# jev-1.13 on OpenRouter: $0.042/M input, $0 output (model page, 2026-09-18).
# Only used to size the cost RESERVATION before the call — the booked cost is
# the `usage.cost` OpenRouter returns. LiteLLM's pricing map has no entry for
# this model, so estimate_llm_cost would reserve $0 and let the project cap be
# bypassed; this keeps the reservation honest.
JEV_INPUT_USD_PER_TOKEN = 0.042 / 1_000_000

_QUESTION_TYPES = frozenset({"choice", "score", "noul"})


class DecisionRequestInvalid(ValueError):
    """The caller's questions can't be sent — maps to 422, never retried."""


class DecisionHTTPError(RuntimeError):
    """Non-2xx from the decisions endpoint, WITH the provider's body text.

    classify_provider_error works on the message string. A bare
    httpx.HTTPStatusError reads "Client error '402 Payment Required' for url
    …" — the provider's own reason ("Insufficient credits", "rate limit") is
    not in it, so an empty account would classify as a generic error and be
    retried forever instead of cooling as out-of-money."""


def validate_questions(questions: dict[str, Any]) -> None:
    """Reject malformed questions before they cost a key a failure mark.

    A 400 from the provider would cool the key as if it were broken; the
    mistake is the caller's, so it is caught here and never reaches a key."""
    if not questions:
        raise DecisionRequestInvalid("questions is empty")
    for name, q in questions.items():
        if not isinstance(q, dict) or q.get("type") not in _QUESTION_TYPES:
            raise DecisionRequestInvalid(
                f"question {name!r}: type must be one of {sorted(_QUESTION_TYPES)}")
        criteria = q.get("criteria")
        if q["type"] == "score":
            if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
                raise DecisionRequestInvalid(f"question {name!r}: score needs 2–10 levels")
        elif q["type"] == "choice":
            if not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255:
                raise DecisionRequestInvalid(f"question {name!r}: choice needs 1–255 options")
        elif not isinstance(criteria, dict) or set(criteria) != {"true", "false"}:
            raise DecisionRequestInvalid(
                f"question {name!r}: noul criteria must be exactly 'true' and 'false'")


def estimate_tokens(state: str, questions: dict[str, Any]) -> int:
    """~4 chars/token over everything the model reads — reservation sizing only."""
    return (len(state) + len(str(questions))) // 4 + 1


async def decide(
    *, model: str, state: str, questions: dict[str, Any], api_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """One decisions call → (answers, meta).

    Raises DecisionHTTPError on a non-2xx answer, httpx transport errors on a
    dead connection or timeout."""
    # OpenRouter ids carry no `openrouter/` prefix here — that prefix is
    # LiteLLM's routing syntax, which this endpoint does not go through.
    model_id = model.removeprefix("openrouter/")
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            OPENROUTER_DECISIONS_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model_id, "state": state, "questions": questions},
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    if resp.status_code >= 400:
        # 402 is "out of money" by HTTP definition, whatever wording the body
        # uses — and OpenRouter's wording for an empty prepaid balance is not
        # something we have observed (a $0 free-tier key was NOT refused, so
        # there was no 402 to capture). Tagging it with an existing billing
        # sign makes classify_provider_error cool the key as depleted instead
        # of retrying it as a transient error, without guessing their text.
        tag = "credits are depleted — " if resp.status_code == 402 else ""
        raise DecisionHTTPError(
            f"{tag}openrouter decisions HTTP {resp.status_code}: {resp.text[:400]}")
    body = resp.json()
    usage = body.get("usage") or {}
    return body.get("answers") or {}, {
        "tokens_in": int(usage.get("input_tokens") or 0),
        "tokens_out": int(usage.get("output_tokens") or 0),
        "cost_usd": float(usage.get("cost") or 0.0),
        "latency_ms": latency_ms,
        "model_served": body.get("model"),
    }
