# Media receipt/reconciliation contract proposal

Read-only code review against be18e51029d2bd75d31ccdcfa848cad75c44b462.
This is a proposed local implementation plan, not an implemented API or a claim
of provider-side exactly-once execution. No provider calls or integration tests
were run for this review. Existing Vivix work remains unchanged and disabled.

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
