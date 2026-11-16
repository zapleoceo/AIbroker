# Vivix transport foundation (2026-11-16)

Status: disabled provider integration, not an operational Vivix video service.
The registered `vivix` ProviderSpec has an HTTP transport but no routable models,
default capability, automatic key probe or billing probe. It is classified as paid.
There is no free-price fallback. No personal workspace entitlement, balance,
model availability or successful generation has been verified.

`VivixVideoTransport` implements the existing `VideoTransport.generate_video`
entry point as a `VideoRejected` failure before HTTP. Internal helpers separate
submission (a task ID) and one poll (a `VideoResult`). The adapter does not
poll in a loop, retry a POST, download media or synthesize speech separately.
Its default enabled-model set is empty: submission fails before HTTP. The explicit
constructor allowlist is used by mock tests; no application configuration enables it.
Callers must not use it to bypass admission or billing controls.

The starting checkout, 4c7f34d, predates video support. This change is based on
master df4460a, which already has `POST /v1/video/jobs`, a video worker,
`VideoRequest`, and `VideoTransport.generate_video`. Its service selects the first
provider in the chain and uses Gemini token pricing plus retries across keys.
Those assumptions cannot safely price or retry Vivix submissions. Consequently
`GET /v1/models` does not advertise Vivix video models, and pins cannot route them
through chat or video. A provider listing does not imply model support.

## Upstream contracts

Checked against official documentation on 2026-11-16:

- [Models](https://docs.vivix.ai/overview/models): discovery reads `data.models`
  after validating the outer response. Bare IDs and explicit tags are distinct
  permissions. Discovery is scoped to the key used for that request; it never
  mutates the shared model registry or caches another key's permissions.
- [Avatar generation](https://docs.vivix.ai/video-generation/avatar-video-generation):
  A1 submits to `/v1/videos/generations` and polls that resource by `video_id`.
  The adapter checks both outer `code` and business `data.error_code`.
  An empty polling list is pending, `ready` remains nonterminal, and
  `failed`/`timeout` terminate. Success requires a playable URL.
- [World generation](https://docs.vivix.ai/video-generation/world-video-generation):
  W1 uses `/v1/videos/jobs`, with one job and a `results` submission envelope;
  polling returns a single job and `video_url`. W1 stream is a different API and
  is rejected by this adapter, as is A1 stream. W1 variants preserve their exact IDs.

HTTP status is retained for Broker error classification. Response messages and
raw bodies are not included in exceptions. Redirects are not followed. Network
failures become `TimeoutError`; a submission timeout has an unknown acceptance
outcome and must not trigger automatic resubmission. Malformed submission responses
also require reconciliation before retry. No upstream idempotency guarantee is assumed.

## Pricing and activation work still required

The W1 page currently publishes temporary November rates and separate image,
audio and video reference fees. These are not proof of the personal account's
contract or entitlement, and are not hardcoded as a permanent price. Unknown
pricing is not zero. A1 billing has not been established for this integration.

The local implementation now supplies provider-specific request validation,
durable submit/poll lifecycle, conservative reservation, settlement and a dedicated
dispatcher branch. Activation still requires verified workspace permissions,
media inspections, pricing including reference fees and final billing receipts.
Only after PostgreSQL validation and release gates may a trusted runtime be wired,
priced models registered and activation configured.
Neither an ITStep test result nor access to `name:latest` grants access to a
personal key or a bare model name. No ITStep credentials or credits are used.

## Verification and delivery

### Local lifecycle and accounting stage

`VivixService` is internal; the public route is guarded by `get_runtime()`, which
unconditionally returns `None` in shipped code. `prepare` admits an exact
account/model/request only with a nonexpired `VivixQuote`: a positive USD ceiling,
request digest, account identity, provenance, and explicit inclusion of reference
fees. The quote is a trusted internal input, never a public client assertion.
There are no hardcoded rates, default permissions, pricing discovery, or live calls
in this stage. `vivix_inputs` validates A1 single/multi-segment voice modes and
W1 duration, ratios, seed, reference counts, keyframe conflicts and unknown fields.
`vivix_media` requires trusted URL-bound size/dimension/duration evidence for
referenced media. It never downloads or guesses media properties.

`VivixStore` persists reservations in `vivix_jobs`. Admission serializes on
`vivix_ledger_lock` and checks account, project and global lifetime budgets against
both unresolved holds and settled cost. These are conservative isolated budgets,
not replacements for Broker daily/monthly caps. `BrokerVivixStore` additionally
checks real project/key daily/monthly caps, global daily cap, existing usage and
all unresolved holds. Ordinary `reserve_cost` also counts these holds, including
after UTC window resets. Settlement atomically closes the hold, writes `usage_log`
and updates key counters once; a failed write rolls back everything. No delayed
refund subtracts an old reservation from new-window spend. Existing ordinary-lane
project/global concurrency and global-cache limitations remain as documented in
the cost guard; this does not claim globally serializable admission for all lanes.
Migrations 028/029 and matching fresh-install DDL are prepared, not applied to any
running database. Both are required before deploying this code because ordinary
cost guards now read persistent holds, even while Vivix activation is disabled.
The ledger must not be purged by ordinary job retention while holds are unresolved.

`advance` commits `submitting` before POST. Concurrent callers use versioned
updates, so only one can submit. A timeout, malformed response or HTTP error after
this boundary becomes `unknown`, preserving the full hold; cancellation or a crash
can leave `submitting`, which is also never automatically resubmitted. An accepted
task resumes one GET poll per call after restart. Poll errors leave it pollable.
Terminal provider status alone never implies a zero bill or releases the hold.

`recover_submission` accepts trusted operator evidence after worker loss or an
authoritative lookup. A verified task ID resumes polling; without one it marks
the submission unknown and retains the hold. Recovery never sends POST.
`reconcile` requires a trusted final billing receipt, atomically moves the hold
to actual cost, and rejects conflicting duplicate settlements. A real overrun is
recorded honestly and blocks later admissions as applicable. Zero settlement is
accepted only with explicit reconciliation evidence, never inferred from failure.
Neither recovery nor settlement is exposed as a user-accessible route.

`cancel` refunds only a never-submitted prepared job. After submission it records
local cancellation intent while polling/accounting continue; it does not claim
remote cancellation. During an active POST it leaves the version unchanged so the
worker can persist the task ID; the caller must retry cancellation after that
attempt resolves. An expired pre-submit quote releases its unused hold.

The existing `/v1/video/jobs` route now accepts a separate `provider: vivix` shape
with native `inputs`, exact `model` and mandatory `Idempotency-Key`. It enforces the
ordinary project scope and request cap; replay returns the existing job without
spending the cap again. Clients cannot provide quotes, account IDs or credentials.
With no trusted runtime it returns 503 before enqueue or provider calls. Existing
Gemini bodies remain supported. Vivix jobs in the ordinary dispatcher bypass the
Gemini walk and generic retries; poll ticks do not increment generation retries.
The persisted Broker key/account binding is checked before decryption and cannot
rotate to another key. Pending cancellation stays queued until the Vivix worker
can refund an unsubmitted request or finish accounting for a submitted one.

A successful settled Vivix poll returns `video_url` and `billing_status: settled`;
it does not download the video or encode it as base64. Unknown acceptance reports
reconciliation required and retains the hold. A terminal job without a receipt
stays pending with unknown cost, never reported as free. Credential values and
raw provider error bodies are not persisted. The ledger contains private request
inputs/result URLs and must retain access restrictions and unresolved holds.

Remaining gates: an authorized implementation of `VivixRuntime` backed by verified
permission/media/quote/receipt sources, PostgreSQL migration/concurrency tests,
Haiku docs review and authorized release. No runtime, activation flag, new key,
price table or discovery call is supplied. `tests/test_vivix_postgres.py` explicitly
skips without a guarded PostgreSQL `TEST_DATABASE_URL`; the local SQLite mock
route/dispatcher/accounting suite does not substitute for those checks.

This section was updated locally by Codex; it is **not** Haiku model evidence and
does not satisfy the required Haiku documentation gate.

### Internal validation and bridge symbols

`StrictInput` forbids extra fields and coercion. `Reference` and `media_url` describe
HTTP(S) material references; `WorldJob` / `WorldInputs` validate W1 requests.
`TTSConfig`, `AvatarSegment` and `AvatarInputs` cover A1 voice/segment forms.
`validate_inputs` selects the appropriate schema; `VivixJobRequest` is the separate
public request shape. `MediaEvidence` contains trusted inspection facts and
`validate_media` checks them against exact requested URLs and material limits.
`Authorization` binds a Broker key/account to permissions, quote and media evidence;
`Receipt` supplies final verified cost. `VivixRuntime` is the trusted provider of
those facts; `get_runtime` currently returns no implementation. `VivixDisabled`
is the explicit refusal, and `vivix_jobs.submit` / `execute` bridge standard jobs.
`BrokerVivixStore` uses `held`, `spent` and `check` for existing Broker budgets.
`VivixLedgerLock` serializes admissions and `VivixJobRow` persists job/hold state;
`VivixLimits` supplies the additional lifetime ceilings. `request_digest` binds a
quote to exact JSON inputs and `usd` rejects invalid monetary values. Transport
parsing uses `model_ids` for exact permissions, `video_result` for task state and
`VivixError` for sanitized upstream failures.

`tests/test_vivix.py` intercepts all HTTP using `httpx.MockTransport` and covers
registry/pin exclusion, default-disabled submission, exact tags, both API shapes,
HTTP and envelope errors, terminal states, missing URLs and no POST retries.
Run it with registry, pin resolution, provider error and pricing regression tests.
Live key checks and paid generation are outside this delivery.

CLAUDE.md requires a Haiku documentation subagent before PR/deploy. This document
is an implementation draft, not a claim that the Haiku requirement has passed.
Run `scripts/docs_gate.sh` before pushing; exact-commit CI remains authoritative.
