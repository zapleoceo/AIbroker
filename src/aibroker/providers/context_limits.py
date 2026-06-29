"""Self-calibrating per-provider single-request size ceiling.

A provider can't serve a single request above a hard token ceiling on our
tier (e.g. Groq's free TPM ≈8k → a 24k-token prompt 413s/429s every time).
Sending it is a guaranteed wasted call that just delays the request until
the chain falls through to a provider that can handle it.

Design (no hardcoded sole-source):
  - SEED_MAX_REQUEST_TOKENS is a bootstrap guess used ONLY until the provider
    teaches us its real ceiling.
  - When a provider rejects a request as "too large" (413 / context length /
    request-too-large), `is_too_large_error` flags it and the orchestrator
    records the prompt's token estimate into provider_observations. From then
    on the LEARNED value (min of all observed rejections) overrides the seed.

So the effective ceiling = min(learned, seed). Skipping an over-ceiling
provider is a pure efficiency win — the request still reaches a provider
that CAN serve it, so the answer (and its quality) is identical.
"""
from __future__ import annotations

from typing import Any

# Bootstrap seeds — used only until a real rejection is observed per provider.
# None ⇒ no known ceiling (large-context providers: cerebras, gemini, mistral…).
SEED_MAX_REQUEST_TOKENS: dict[str, int | None] = {
    "groq": 8_000,        # free TPM ≈ 8k → a single bigger request always 413/429
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

# Safety margin so a prompt that just barely fits doesn't overflow once the
# model's own output + overhead is added.
_FIT_MARGIN = 0.90

# Substrings that mean "this prompt is physically too big for this provider"
# (as opposed to a transient rate-limit). Lower-cased match against the error.
_TOO_LARGE_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "too large",
    "too many tokens",
    "request too large",
    "reduce the length",
    "413",
    "string too long",
)


def _content_chars(content: Any) -> int:
    """Char count of a message's content — handles plain str and
    OpenAI-style multimodal block lists. Image blocks count as a flat
    ~1000-char proxy (vision tokens aren't text-linear, but this keeps
    the cap math from under-counting a request to near-zero)."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                total += len(block.get("text") or "")
            else:
                total += 1000   # image/audio block proxy
        return total
    return 0


def estimate_prompt_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate from message chars. ~4 chars/token (English);
    Russian is denser so this under-counts — compensated by the 90% margin."""
    chars = sum(_content_chars(m.get("content")) for m in messages)
    return chars // 4


def is_too_large_error(exc: Exception) -> bool:
    """True if the provider rejected the request for being too big (not a
    transient rate-limit). Drives the self-learning of the size ceiling."""
    msg = str(exc).lower()
    # A bare 429 is rate-limit, not size — exclude it unless paired with a
    # size marker. 413 is unambiguously payload-too-large.
    return any(m in msg for m in _TOO_LARGE_MARKERS)


def effective_ceiling(provider: str, learned: int | None) -> int | None:
    """min(learned, seed) — whichever is the tighter known ceiling.
    None ⇒ no ceiling known from either source (provider handles large
    context fine)."""
    seed = SEED_MAX_REQUEST_TOKENS.get(provider)
    candidates = [c for c in (learned, seed) if c is not None]
    return min(candidates) if candidates else None


def fits_context(provider: str, est_tokens: int, learned: int | None = None) -> bool:
    """True if `provider` can serve a single request of ~est_tokens, given the
    effective ceiling (learned overrides seed). Uncapped providers always fit."""
    cap = effective_ceiling(provider, learned)
    if cap is None:
        return True
    return est_tokens <= int(cap * _FIT_MARGIN)
