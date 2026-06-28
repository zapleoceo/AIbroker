"""Per-provider single-request token ceiling + prompt-size estimation.

Some providers can't serve a single request above a hard token ceiling on
our tier — Groq's free TPM (~8k tokens/min) means a 24k-token prompt 413s
or 429s 100% of the time. Sending it anyway is a guaranteed wasted call
that delays the request until the chain falls through to a provider that
can handle it.

Skipping such a provider for an over-ceiling prompt is a PURE efficiency
win: the request lands on exactly the same provider it would have reached
after the wasted failures, so the answer (and its quality) is identical —
it just gets there faster and without burning a failed attempt.

Only set a ceiling where we're confident. None ⇒ no skip (provider handles
large context fine, e.g. cerebras gpt-oss-120b, gemini, mistral-large).
"""
from __future__ import annotations

from typing import Any

# Hard single-request token ceiling per provider on our tier. Conservative:
# only populated where over-ceiling prompts are KNOWN to fail every time.
PROVIDER_MAX_REQUEST_TOKENS: dict[str, int | None] = {
    "groq": 8_000,        # free TPM ≈ 8k → a single bigger request always 413/429
    # everyone else: large context, no per-request size skip
    "cerebras": None,
    "gemini": None,
    "mistral": None,
    "cohere": None,
    "openrouter": None,
    "deepseek": None,
    "anthropic": None,
    "openai": None,
    "voyage": None,
}

# A safety margin so we don't send a prompt that just barely fits and then
# overflows once the model's own output + overhead is added.
_FIT_MARGIN = 0.90


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate from message chars. ~4 chars/token for English;
    Russian is denser (~2-3) so this under-counts a bit — we compare against
    a 90%-discounted ceiling to keep a cushion on the safe side."""
    chars = sum(len(m.get("content") or "") for m in messages)
    return chars // 4


def fits_context(provider: str, est_tokens: int) -> bool:
    """True if `provider` can serve a single request of ~est_tokens.
    Providers with no configured ceiling always fit."""
    cap = PROVIDER_MAX_REQUEST_TOKENS.get(provider)
    if cap is None:
        return True
    return est_tokens <= int(cap * _FIT_MARGIN)
