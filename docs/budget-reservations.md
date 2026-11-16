# Shared durable budget reservations (local implementation)

Migration 030 adds `BudgetLedgerLock` and `BudgetReservationRow`. Positive ordinary
estimates get a UUID, project/key binding, amount (rounded up to eight decimals),
original UTC day/month, debited-window flags and state `held`. `Attempt.reservation_id`
passes that identity to booking/refund; equal amounts on one key are never matched
by amount. A zero estimate creates no reservation, but paid/trial calls still
check every key/project/global limit against settled spend and unresolved holds.
The explicit free-tier zero-estimate bypass remains. `known_free_model` exempts
the special paid cap-0 refusal only; it does not bypass an already exceeded cap.
Unknown provider prices are not inferred from this accounting mechanism.

`admit_ordinary` acquires `lock_budget`, reloads the key, checks daily/monthly key
and project limits and the global daily limit, then inserts the hold and updates
key counters in one transaction. Project limits honor the stricter of fresh DB
and caller caps. `ordinary_held` sums all unresolved ordinary holds regardless of
age; the existing Vivix hold rows remain their authoritative source. Both paths
sum settled usage plus both kinds of holds under the same lock. Global admission
uses live usage, not the former 30-second cache. No network operation holds a DB lock.

Lock order is shared budget lock, then Vivix ledger lock (Vivix only), then key.
Vivix monetary transitions also take the shared lock. Its local lifetime limits,
CAS lifecycle and ambiguous-submit retention remain unchanged. `get_runtime`
still returns None, so the public Vivix route remains disabled/503.

`record_usage(reservation_id=...)` takes the shared lock, validates the exact
key/project binding and calls `booking_reservation`. In the SAME transaction it
refunds the estimate, inserts usage, books actual counters, and marks the hold
settled with usage ID and a fingerprint of the booking parameters. Exact retries
return that ID without another charge; changed parameters fail. A failed write
rolls back the refund and leaves the hold. This also covers billed quality
rejections. `_shield_booking` keeps a strong task reference so cancelling its
caller does not interrupt the transaction. Unreserved usage writes also acquire
the lock, though no reservation can bound a call that supplied no estimate.

`release_cost(reservation_id=...)` is an idempotent cancellation refund. Missing
IDs cannot touch holds; settled/released IDs do nothing, and booking a released
ID is rejected. `refund_debit` subtracts only from the original stamped windows.
`uncounted_key_holds` adds holds omitted by a reset without double-counting those
already in current key counters. `budget_day` supplies the UTC admission date
after acquiring the budget and key locks. Booking samples `budget_now` after
the budget lock and binds the same UTC timestamp/date/month to usage and counters.
It does not use PostgreSQL transaction-start `CURRENT_DATE`/`now()` for money
windows, because a lock wait can cross midnight or the start of a month.
Ordinary answered failures still book zero, as before; ordinary cancellation
refunds. This preserves existing invoice-reconciliation policy for answerless
timeouts. Vivix ambiguous submissions instead retain their hold until verified
reconciliation, and never retry POST automatically.

Operational limits and review gates:

- Migration 030 is prepared only. Drain old workers/in-flight requests before
  any future deployment: old reservations have no durable identity and cannot
  be safely synthesized. Apply 028/029/030 before code that reads these tables.
  No rolling mix of old/new accounting implementations is supported.
- No hold expiry or automatic crash refund. Process death and failed booking
  leave durable obligations for explicit reconciliation. There is no new public
  reconciliation API or operator UI in this change. Do not purge held rows or
  settled idempotency records as ordinary job/usage retention.
- The singleton serializes budget mutations across projects. PostgreSQL load,
  contention and concurrency validation remain required before release; local
  SQLite transactions are not evidence of PostgreSQL behavior.
- Estimates are admission limits, not guaranteed upstream charges. Actual
  settlement above the estimate records the debt honestly. Unestimated calls,
  existing zero-cost policy and external invoice reconciliation remain limits.
- Haiku documentation review is unavailable/unmet. This document is a manual
  implementation draft. Publication denial and disabled-provider gates remain.

Validation: `test_budget_reservations` covers both admission orders and concurrent
admission on different keys, daily/monthly/global limits, equal amounts across
projects, repeated finalize/refund and rollover. `test_budget_attempt_atomic`
exercises the post-response booking gap, billed rejection, cancellation of the
shield caller, atomic rollback, transaction overlap, and error/cancel cleanup.
The former three strict XFAIL cases in `test_vivix_budget_overlap` now assert
rejection and PASS. PostgreSQL migration tests isolate schema from create_all,
apply 028→029→030 and repeat migrations while preserving unresolved holds;
they remain NOT RUN locally without an authorized TEST_DATABASE_URL.

`test_budget_zero_clock` covers zero-estimate paid/trial/known-free calls against
exceeded key daily/monthly, project daily/monthly and global caps, with settled
spend, ordinary holds and Vivix holds. It verifies exact-cap acceptance without
inserting a zero hold, the free-tier bypass, and controlled post-lock month
rollover for admission and atomic usage booking.
