"""In-process proof that the Codex login works at its current `last_refresh`, so the start-up
auth gate can stay open between proofs.

The gate (executor._codex_auth_gate) serialises run start-ups once `last_refresh` is older than
CODEX_REFRESH_DUE_AFTER_S, because two refreshes at once can rotate the refresh token away. Codex's
own interval is longer (~8 days), so for days the gate made EVERY run start alone: in prod on
2026-10-21 one run in five waited ~96 s for it and a 120 s client timed out. A gated run that
completes a model call while `last_refresh` stays the same proves the token is still accepted
without a refresh. Until `window_s` after that proof - and only while the timestamp is unchanged -
new runs skip the gate. The first run after the window takes the gate again and renews the proof.
One Codex worker process shares one CODEX_HOME, so process memory is the right scope.
"""
from __future__ import annotations

import time
from datetime import datetime


class RefreshProof:
    """The last proven-good `last_refresh` and the monotonic time it was proven."""

    def __init__(self) -> None:
        self._last_refresh: datetime | None = None
        self._proven_at: float | None = None

    def record(self, before: datetime | None, after: datetime | None,
               now: float | None = None) -> bool:
        """A gated run made a model call. Proof only when the timestamp was readable and did not
        change during the call (a change means Codex refreshed: nothing is proven). True if kept."""
        if before is None or after != before:
            return False
        self._last_refresh, self._proven_at = before, time.monotonic() if now is None else now
        return True

    def covers(self, last_refresh: datetime | None, window_s: float,
               now: float | None = None) -> bool:
        """True while the same timestamp was proven within the last `window_s` seconds."""
        if (window_s <= 0 or last_refresh is None or self._proven_at is None
                or last_refresh != self._last_refresh):
            return False
        return (time.monotonic() if now is None else now) - self._proven_at <= window_s

    def clear(self) -> None:
        self._last_refresh = self._proven_at = None


PROOF = RefreshProof()
