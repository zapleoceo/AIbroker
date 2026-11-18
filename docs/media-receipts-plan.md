# Media receipt/reconciliation contract proposal

Initial read-only review was against be18e51029d2bd75d31ccdcfa848cad75c44b462.
The initial plan and narrow-guard review below document the pre-implementation
findings. The final section records the subsequently implemented LOCAL dispatch
slice and supersedes their statements that no guard exists. This is not a released
API or a claim of provider-side exactly-once execution. Vivix remains disabled.

## Confirmed behavior

- `routes/proxy.py:speech_endpoint` is synchronous and has no idempotency key.
  `SpeechResponse.request_id` is usage_log.id. Audio is encoded from the returned
  bytes into the response; speech_service does not persist an artifact or receipt.
- `routes/proxy.py:video_submit` accepts Idempotency-Key, but uses it only for the
  disabled Vivix branch. Gemini calls submit_job without forwarding that key.
  In-flight payload dedup is best-effort, time-limited and not durable replay.
- Known video job IDs can already be polled: `deep_jobs.get_job` filters by both
  job ID and project ID, and completed jobs expose stored video_base64. The gap
  is recovering a lost submit response by key, not absence of all video lookup.
- `deep_jobs.submit_job_ex` already has project-scoped unique-key insertion and
  conflict/replay handling. Reuse that mechanism; do not invent another queue.
- `providers/gemini_video.py:generate_via_gemini` combines POST, interaction GET
  polling and artifact download. The interaction ID is not persisted between
  these stages. A worker crash can lose the ID even if the provider accepted POST.
- `video_service.run_video` and `speech_service.run_speech` rotate paid keys after
  failed attempts. Video also has dispatcher retries; job_queue stale recovery
  requeues non-agent jobs. Broker request dedup alone does not prevent these calls.
- Ordinary Attempt errors book zero/refund and cancellation refunds. A durable
  media submission with an uncertain upstream outcome needs a distinct policy
  retaining its reservation. Do not silently change all existing providers.

## Minimal safe contract (proposed)

Use the existing Broker app, auth and job queue. Add an explicitly opted-in durable
media mode: keyed Gemini video submission and an async speech jobs endpoint.
Keep legacy synchronous speech behavior documented as non-recoverable; do not
advertise its usage request_id as an artifact receipt or implicitly retry it.

1. Authentication determines project_id. The key is unique within that project
   (matching the existing jobs constraint), with media capability included in
   the fingerprint. Record the authenticated actor for audit. Never authorize
   from a caller-supplied owner/project field. ProjectCtx has project+actor, not
   a verified independent end-user identity: users sharing a project credential
   share its scope. SIM must retain its own owner mapping; stronger Broker user
   isolation requires a separate authentication design, not an owner_id string.
2. Require a bounded validated Idempotency-Key for durable mode. Store a versioned
   canonical fingerprint of all result-affecting input: capability, requested
   model, prompt/text, voice/style, duration/ratio/resolution, decoded reference
   content hash and MIME, workflow and applicable defaults. Resolve and persist
   the execution model once for the first request. Replay lookup precedes fresh
   default-model resolution so a default change cannot cause another submission.
3. In one transaction create the job and durable receipt identity. Same project,
   key and fingerprint returns that receipt/status, including terminal/unknown;
   different fingerprint returns 409. Concurrent inserts resolve through the DB
   unique constraint. Durable mode fails closed on missing migration; it must
   not use legacy missing-column fallback to duplicate-tolerant insertion.
4. Proposed lookup: an authenticated read-only receipt-by-key route, plus normal
   job-ID polling and a receipt-scoped artifact read. No generation is triggered
   by lookup. Missing/foreign-project IDs and keys return the same 404. Recheck
   project activity and capability scope for every read, including artifacts.

## Durable execution and recovery

Persist `prepared -> submitting` with a compare-and-swap worker fence BEFORE any
paid request. Bind receipt to job, provider/account/key, resolved model and durable
reservation ID. Only the worker that wins that transition may call POST. Do not
hold a DB transaction across network IO. Recovered `submitting` means `unknown`,
even if the process may have died before sending: availability is sacrificed
when a second charge cannot be ruled out. No automatic reset to prepared.

For video, split transport submission from polling/download. Save the returned
provider interaction ID before continuing; `submitted` recovery performs GET
only using the original account binding. If the ID is missing after an ambiguous
POST, expose unknown and retain the hold. No key rotation, blind POST resubmit,
or generic stale-job retry for this mode. Provider-side search/idempotency can be
used only after its actual supported contract is verified; none is assumed here.

Speech can lose a successfully generated synchronous response before persistence.
Without a provider receipt/status API, that state remains unknown; usage ID alone
cannot reconstruct bytes. Persist returned audio before exposing ready. Proposed
minimal artifact storage is bounded bytes in the existing database, with MIME,
length and SHA-256; no new object-store service or external URL download endpoint.
Define an explicit artifact size/retention limit before implementing this path.

Ready requires durable artifact plus usage/settlement linkage. Extend transactional
booking to commit receipt finalization, artifact and reservation settlement together,
or retain a durable artifact-pending-booking state recoverable without generation.
Exact finalization retries must not duplicate usage or overwrite an artifact with
different bytes. Cancellation before submitting can refund; cancellation/timeout
after that fence retains unknown/submitted and its hold until verified reconciliation.
Operator reconciliation must require scoped evidence, not an untrusted client claim.

Retain key/fingerprint/status tombstones after artifact expiration and job cleanup;
return explicit artifact_expired without regenerating. Never purge unresolved
receipts or holds through generic job retention. The key-reuse retention horizon
must be part of the client contract. This provides Broker-side replay and
conservative recovery, NOT exactly-once execution at an external provider.

## Implementation slices and release gates

1. Receipt schema/migration, project-scoped key/fingerprint/CAS store and read-only
   lookup tests. No paid-call wiring until all later transitions are present.
2. Gemini video transport split, durable pre-call fence, provider-ID persistence,
   GET-only recovery, and receipt-aware dispatcher retry/stale/shutdown handling.
3. Async speech job shape, bounded durable artifacts and atomic booking linkage;
   then expose durable route mode. Keep Vivix dispatcher/runtime untouched.
4. Offline fault-injection matrix: same/different fingerprint, parallel submits,
   other project/scope, crashes before POST/after accepted POST/before ID commit,
   known-ID GET recovery, speech response loss, artifact/booking DB failure,
   cancellation on both sides of the fence, stale worker finalization, duplicate
   finalization, restart with no in-memory state, retention/expired artifact. Count
   mock POSTs and assert no second call for unknown. Then run isolated PostgreSQL
   unique-key/CAS/concurrency/rollback/migration checks against the exact commit.

This exceeds a safe route-only patch: it crosses queue recovery, provider transport,
media-specific billing and artifact retention. Implementation is intentionally not
started in this review. PostgreSQL runtime remains unavailable; mocks would not
clear that gate. Haiku documentation review and the existing publication denial
remain blockers. No push, PR, deploy, production access or Vivix activation.

## Narrow guard review and executable reproductions

Historical findings at 95aef41, before the implementation described below.

`tests/test_media_resubmit_risk.py` adds fault-injection reproductions against the
real Gemini transport orchestration, video key walk and queue execution. HTTP
`_send`, key selection and billing collaborators are mocked; no external request
or real charge is performed. The tests deliberately remain strict XFAIL only
after fully asserting duplicate accepted mock POSTs, using a dedicated exception.
Unexpected exceptions/assertion failures are FAIL, not swallowed by XFAIL.

| Scenario | Observed unsafe sequence asserted by regression | Boundary |
| --- | --- | --- |
| Provider accepted create, response lost | POST, POST on next paid key | Offline transport + real key walk |
| Known interaction ID, polling unavailable | POST, GET known ID, POST on next paid key | Offline transport + real key walk |
| Artifact returned, durable job finish fails | POST, lost finish, reclaimed execution, POST | SQLite queue execution; reclaim state explicitly simulated |
| Same lost finish with actual stale-reclaim SQL | Same required safety invariant | PostgreSQL-only; NOT RUN without authorized test runtime |

The ordinary video tests also retain controls for pre-dispatch budget rejection
(zero transport calls), no eligible key (zero generation), and deterministic
prompt refusal (no key rotation). These controls are not proof of provider-side
non-acceptance for generic HTTP/network failures.

A transport-only guard cannot meet the requested restart guarantee. `_call`
currently turns both POST uncertainty and GET polling failure into RuntimeError;
changing that exception could stop the immediate key walk but not stale/shutdown
reclaim after process death. A queue-only guard before `run_video` is too early:
it cannot distinguish no-key/decrypt/admission failure from actual submission.
In-memory flags and exception text are not durable submission evidence. No such
partial guard was installed, and the three offline risks remain unfixed.

The next dependent implementation unit is **durable media dispatch context**, not
a new service and not artifact storage yet. It must be one coordinated slice:

1. Extend the existing job's durable record with submission phase, immutable
   attempt identity, claim fence, account/key binding, reservation ID and optional
   provider receipt. A schema-backed CAS must persist `prepared -> submitting`
   immediately before the paid POST; a new worker must load this same record.
2. Pass this context from `job_queue._run_video_job` through `run_video` and the
   media-specific Attempt path to Gemini transport. Key selection, validation,
   budget admission and decrypt remain before the submission fence. Failures
   proven to precede it may retain current retry/refund behavior. Once fenced,
   ambiguous outcomes retain their hold and cannot rotate to a new paid key.
3. Split `generate_via_gemini` create and resume phases. Commit known provider ID
   before polling. Existing ID permits GET-only resume with original binding;
   missing ID after submitting becomes unknown and prohibits another POST.
4. Make `_requeue_stale_running`, `_release_claim`/shutdown, `_execute_guarded`
   and `_requeue_or_fail` consult that persisted phase. A stale claim cannot
   overwrite a newer receipt/terminal state. Unknown cannot be converted to a
   fresh pending generation. Lost artifact with a known provider ID may retry
   GET/download only; without recoverable evidence it stays unknown.

These changes touch `db/models.py` + migration, `job_queue.py`, `video_service.py`,
`attempt.py` and `providers/gemini_video.py` together. Receipt lookup/artifact
retention and speech remain subsequent slices. Required regressions for this
unit: all three existing XFAIL become ordinary PASS with at most one POST;
pre-dispatch no-key/decrypt/budget failures remain safely retryable; parallel
claims/worker death before and after fence; known-ID recovery with zero POSTs;
reservation retention on unknown; foreign scope/fence rejection; and PostgreSQL
CAS/rollback/stale-reclaim integration. The current change supplies reproductions
and this dependency boundary only, not the guard or durable receipt capability.

## Implemented local dispatch slice (not released)

`MediaDispatchRow` in migration 031 stores job/project identity, `media_fingerprint`
of canonical job input, phase, queue claim timestamp, key/account binding, hash of
the encrypted credential, reservation ID, resolved model, provider ID and usage ID.
The credential itself is not copied into the receipt. Project scope and the exact
claim are checked by `MediaDispatch.fence`; `MediaUnknown` is the explicit refusal
to repeat an uncertain operation. `validate_input` compares the actual call to
the durable payload, and begin rechecks its fingerprint under lock. A stale/foreign worker cannot submit,
persist a provider receipt or finalize someone else's job. Changed input is refused.

`run_video` now requires a `MediaDispatch` context, supplied by the existing queue.
No-key selection and proven validation/decrypt/admission failures remain before
the dispatch fence. `bind` associates the attempt's reservation in memory only;
it is NOT the guarantee. After request validation and decryption, `begin` locks
and compares the durable job/receipt, checks its held reservation and current key,
then commits prepared -> submitting immediately before the POST. Only one CAS
winner can submit. A loser settles/refunds only its own unused reservation and
does not change the winner's dispatch or rotate to another paid generation.

`generate_via_gemini` persists the returned provider ID through `receipt` before
polling or downloading. `load` after a changed worker claim converts submitting
without a committed receipt to unknown. With a known ID, `media_resume_key`
requires the original key/account/credential fingerprint and uses only GET; it
does not reserve again or pick a replacement key/model. Exceptions/cancellation
after dispatch retain unsettled reservations (`started`/`uncertain`) and do not
book the ordinary zero-cost error/refund policy. This is deliberately conservative,
including post-fence HTTP refusals where payment/nonacceptance is not proven.

`record_usage` invokes `finalize` in the same transaction as actual cost, usage ID
and reservation settlement. Failure rolls back all of those writes. `booked`
reuses the stored usage ID on GET recovery and verifies the returned cost/binding;
it does not book a second charge. Artifact bytes still use the existing job finish
path: if that later write is lost, known-ID recovery GETs the artifact again. If
no recoverable provider ID exists, the job becomes unknown, even when a usage row
already proves its billing. No new artifact storage or retention period is added.

`media_quarantine` blocks unknown jobs in generic retry and shutdown paths. Stale
recovery first quarantines such jobs; subsequent execution also loads the durable
phase before any paid call. Generic requeue now additionally requires running
status, so a late retry cannot revive an already quarantined terminal job.
Cancellation before dispatch remains safe to cancel/refund; after dispatch it
cannot authorize another POST or erase an unsettled hold. Known-ID polling may
still hit normal retry limits, leaving its receipt/hold for explicit reconciliation.
Completed artifacts retain existing job-retention behavior. Unresolved media jobs
are excluded from purge; submission tombstones have no cascading job foreign key.

### Mandatory rollout/dependency gate

- Migrations 028/029/030/031 must exist before these code paths run. Missing 031
  fails closed before video POST; there is no fallback to unguarded generation.
- Stop and drain ALL old workers before applying 031 or starting new workers.
  Mixed old/new workers are unsupported. The migration records unknown and
  quarantines pre-existing pending/running non-Vivix video jobs because their
  historical POST state cannot be proved. It does not alter budgets/credentials,
  nor infer a refund for old jobs. This behavior requires explicit rollout review.
- Migration/init SQL are prepared only and have NOT been applied to production.
  PostgreSQL migration/CAS/restart/stale-reclaim tests are prepared but NOT RUN
  here. SQLite/mock results are not PostgreSQL integration evidence.
- Public key-based receipt lookup, Gemini submit idempotency wiring, async speech,
  durable speech artifacts and operator reconciliation UI are NOT implemented by
  this slice. Guarantees apply to retries/recovery of the same durable video job;
  submitting a distinct job is not provider-side deduplication. Low-level transport
  unit calls without a job context do not claim these queue-level guarantees.
- No provider-side exactly-once claim. A crash after the fence but before the send
  is also unknown, sacrificing availability to avoid a possible second charge.
  Provider receipt expiry/unavailable account can prevent artifact recovery.
- No push/PR/deploy, provider calls or activation occurred. Publication denial,
  unavailable Haiku documentation review and PostgreSQL validation remain gates.

The former three XFAIL tests now assert one POST with ordinary PASS expectations.
Coverage includes GET-only retries, lost job finish, atomic booking rollback,
concurrent CAS/loser refund, pre-dispatch no-key/decrypt/validation, cancellation
and shutdown quarantine, foreign/stale claims and changed credential/project/input.
`test_media_dispatch_postgres` adds a genuine prior-schema migration test in a
transactional isolated schema and a real PostgreSQL stale-reclaim/hold test.

### Local review corrections: cancellation and authorization

Pending submitted Gemini jobs retain `cancel_requested` and remain eligible for
GET-only reconciliation. Transient GET failures likewise requeue despite cancel,
subject to the existing retry limit; cancellation never permits another POST.
Unknown jobs still quarantine, and prepared jobs keep ordinary cancellation.

Local receipt persistence, uncertainty marking, `booked` and `finalize` use the
identity/claim fence without demanding fresh HTTP authorization. Thus revoking a
project after a completed call cannot roll back its incurred-cost settlement.
Financial bindings and stale/foreign claim protection remain mandatory.
`authorize_http` rechecks project activity/scope before every subsequent GET,
including polling and download; a returned receipt is retained even when that
next call is prohibited. This does not revoke an HTTP call already in flight.
Offline regressions cover both cancellation paths, scope/inactive revocation
during booking, stale/foreign finalization, and revocation between POST and GET.
PostgreSQL and Haiku gates remain unverified; these corrections are local only.

### Explicit admin reconciliation after automatic limits

Known receipts at the GET retry cap or worker-death cap now retain job status
`error`, exact reason `reconciliation_required`, original claim, receipt and
financial binding, with no completed timestamp or automatic scheduling. Existing
poll/list data exposes the waiting job; this uses existing columns, no migration.
Admin-only GET on the same reconciliation URL, with `project_id`, returns its
status, reason and `expected_claim` without exposing credentials or provider data.
Unknown submissions without provider ID remain ineligible.

`POST /admin/media/jobs/{job_id}/reconcile` uses the existing `require_admin` /
`X-Admin-Key` authorization only. Body: `project_id` and `expected_claim` (the
persisted naive UTC `started_at`). A CAS against waiting state and exact claim
grants one bounded existing video execution, strictly GET-only, retaining the
automatic retry counter. Foreign project, stale/duplicate claim, changed input,
revoked project/key access and mismatched reservation are rejected with 409;
nonadmins receive 401. No new permission, credential or project-owner endpoint.

The persisted `reconciliation_manual_running` marker means failures, request
interruption, shutdown and stale-worker recovery return to manual waiting rather
than automatic polling. Successful local accounting settles once. An admin call
may perform multiple GETs within the existing video timeout; it never creates a
new provider operation. A lost admin HTTP response must be followed by job lookup,
not blind reuse: the consumed claim conflicts. New explicit attempts require the
new waiting claim returned by POST or obtained through admin GET. Accepted attempts
emit the existing `media.reconcile` audit.

`media_wait`, `claim_media_resume`, and `_reconcile_stale_media` implement these
durable transitions. SQL stale recovery excludes capped/manual known receipts
from generic terminalization, including rows crossing the stale cutoff during a
scan. Holds survive unknown costs and revoked access; this is not a refund tool.
Offline tests cover cap+cancel, failed/manual restart, concurrent/stale/foreign
claims, nonadmin and revoked provider access, no POST and single settlement.
Real PostgreSQL stale-sweep/concurrency validation remains NOT RUN here. Existing
031 migration/rollout, PostgreSQL, Haiku and publication gates remain mandatory.

### Omni response contract check (2026-11-17)

Primary source: https://aistudio.google.com/docs/omni#rest-response-schema .
The documentation distinguishes SDK `output_video` from raw REST video content
in `steps[].content[]`. Broker sends raw REST and already parses the latter.
`test_documented_omni_raw_rest_response` uses synthetic bytes and the documented
envelope for both create and known-receipt GET-only recovery. No parser change is
justified by this example; neither a live incompatibility nor live success is
claimed. Limits, URL handling, error handling and dispatch code are unchanged.

### Admin handler names and lint compatibility (2026-11-18)

`MediaResumeRequest` carries `project_id` and `expected_claim` for the existing
admin-only reconciliation POST. `media_reconciliation_state` implements its GET
lookup; `reconcile_media` claims and executes the bounded GET-only recovery.
These names describe the existing contract above, not new access or activation.

The pinned deployment Ruff 0.7.4 flags shared fixture imports shadowed by pytest
arguments. Tests now register those same fixture objects via module assignments;
fixture names, dependency injection and assertions remain unchanged. Union-form
`isinstance` checks are equivalent on the required Python 3.12 runtime. No lint
rule or deployment threshold is disabled.
