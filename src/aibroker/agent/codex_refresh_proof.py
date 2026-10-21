"""In-process proof that the Codex login works at its current `last_refresh`, so the start-up
auth gate can stay open between proofs. Single Codex worker only: the proof is process memory, so a
second process on the same CODEX_HOME would not see it (compose runs exactly one).

The gate (executor._codex_auth_gate) serialises run start-ups once `last_refresh` is older than
CODEX_REFRESH_DUE_AFTER_S, because two refreshes at once can rotate the refresh token away. Codex's
own interval is longer (~8 days), so for days the gate made EVERY run start alone: in prod on
2026-10-21 one run in five waited ~96 s for it and a 120 s client timed out. A gated run that
completes a model call while `last_refresh` stays the same shows the login was accepted without a
refresh at that moment. Until `window_s` after that proof - and only while the timestamp is
unchanged AND younger than `max_age_s` - new runs skip the gate. The first run after the window
takes the gate again and renews the proof.

RESIDUAL RISK. A proof says nothing about the next refresh. Runs that skip the gate hold no lease,
so a refresh they trigger is not serialised with other runs - in particular a 401-triggered
refresh, which no timestamp predicts. `max_age_s` (6.5 days by default) keeps proofs away from
Codex's own interval (~8 days, UNVERIFIED like CODEX_REFRESH_DUE_AFTER_S): above it every
start-up is gated again, so the time-based refresh still happens under the lease as long as
max_age_s + window_s stays below that interval.
"""
from __future__ import annotations

import math
import time
from datetime import UTC, datetime


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

    def covers(self, last_refresh: datetime | None, window_s: float, *,
               max_age_s: float = math.inf, now: float | None = None,
               now_utc: datetime | None = None) -> bool:
        """True while the same timestamp was proven within the last `window_s` seconds and the
        login's refresh is younger than `max_age_s`."""
        if (window_s <= 0 or last_refresh is None or self._proven_at is None
                or last_refresh != self._last_refresh):
            return False
        if ((now_utc or datetime.now(UTC)) - last_refresh).total_seconds() >= max_age_s:
            return False
        return (time.monotonic() if now is None else now) - self._proven_at <= window_s

    def clear(self) -> None:
        self._last_refresh = self._proven_at = None


PROOF = RefreshProof()
