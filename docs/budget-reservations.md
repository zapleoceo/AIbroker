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

## CI compatibility correction and rollout preparation (2026-11-17)

CI run 44151895130 on fba9e62 found ten outdated collaborator assertions in
test_reservation_release and test_review_2026_09_07, on both SQLite and PG shard3.
The local correction returns explicit reservation IDs from mocks, distinguishes
zero-cost settlement from cancellation refund, and asserts the ID and exact cost
passed to booking. Success must not release before booking. Runtime code, gates,
limits and error policy are unchanged. These corrected tests have NOT been run
locally, respecting the owner's request; static lint is not execution evidence.

The same original PG shard ran test_vivix_postgres (three tests) successfully,
but the full run failed/cancelled other shards: the PostgreSQL gate is NOT PASS.
This note is not Haiku evidence. Local native/npm Claude executables were not
found; the documented server agent-worker CLI is not proof of a usable Haiku
documentation executor in this session.

### Required maintenance sequence before any merge-triggered deployment

1. Close CI/quality and Haiku documentation gates for the reviewed revision.
   Confirm production SHA/schema and aggregate in-flight/hold state read-only.
2. Establish an approved maintenance window that stops new admissions, then drain
   existing requests and stop all old API/dispatcher/accounting writers. Capture
   which agent workers were running; preserve workers intentionally stopped by
   the owner. Do not equate an eight-second shutdown grace with a completed drain.
3. Confirm a recoverable backup owned by the existing Vera/NAS procedure, with
   exact snapshot/time and restoration evidence. No backup was made by this task.
4. With writers stopped, apply reviewed 028,029,030,031 in order, stopping on any
   SQL error. Review legacy video quarantine from031 and unresolved legacy holds;
   never synthesize missing reservation IDs or infer refunds.
5. Only after that evidence permit the reviewed merge. The existing restricted
   script fetches/reset-to-master, builds, starts base services and refreshes only
   agent workers it observes as running. It does NOT back up, drain or migrate.
   Pre-stopped agent workers therefore need an explicit approved restart list;
   do not blindly start every profile. Verify exact image/SHA, schema, readiness
   and financial invariants without model generation before reopening admissions.
6. On failure keep admissions stopped and preserve ledger/receipt rows. Do not
   roll back to pre-ledger binaries or restore a stale DB over new financial
   events without a reconciled recovery plan. No destructive down migration is
   supplied. Vivix activation remains disabled throughout.

Operational gap: the ordinary autodeploy cannot guarantee steps2-4 itself. They
need a separately approved maintenance execution with evidence before merge, or
a reviewed deploy-orchestration change. This local preparation changes neither
the production script nor CI and does not authorize production actions.
