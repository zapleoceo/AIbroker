"""Per-request key tier (docs/api.md "Key tier").

A caller may pick which tier of provider keys serves its request:

- ``auto`` (default): today's walk: free-tier keys first, paid keys after, the job
  queue's final retry paid-only.
- ``paid``: paid-tier keys only, from the first attempt (latency-sensitive replies:
  Gemini free keys run 20-50 s where the paid key answers in 2-3 s).
- ``free``: keys of tier ``free`` or ``trial`` (both cost nothing); never a paid key, not even
  on the final retry.

Subscription models (claude-sub / codex-sub) run on their own accounts and ignore it.
"""
from __future__ import annotations

from typing import Literal

KeyTier = Literal["auto", "paid", "free"]
KEY_TIERS: tuple[str, ...] = ("auto", "paid", "free")


def normalize(value: object) -> str:
    """A stored request's tier; anything missing or unknown is ``auto``."""
    return value if isinstance(value, str) and value in KEY_TIERS else "auto"


def paid_lane(value: str, lane: str) -> str:
    """Speech, image and video run on paid keys only: "auto" and "paid" are accepted
    (no change), "free" is a validation error naming the lane."""
    if value == "free":
        raise ValueError(f"key_tier 'free' is not available for {lane}: "
                         "only paid keys serve it (use 'auto' or 'paid')")
    return value
