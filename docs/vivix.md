# Vivix transport foundation (2026-11-16)

Status: disabled provider foundation, not an operational Broker video API.
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

Before enabling any model, extend the existing video request schema and worker
with provider-specific inputs and resumable Vivix submission/poll lifecycle,
exact workspace permission checks, conservative cost reservation including
reference fees, settlement/reconciliation on terminal and ambiguous outcomes,
cancellation semantics, and selection beyond the first chain provider.
Only then register priced video models and expose an activation configuration.
Neither an ITStep test result nor access to `name:latest` grants access to a
personal key or a bare model name. No ITStep credentials or credits are used.

## Verification and delivery

`tests/test_vivix.py` intercepts all HTTP using `httpx.MockTransport` and covers
registry/pin exclusion, default-disabled submission, exact tags, both API shapes,
HTTP and envelope errors, terminal states, missing URLs and no POST retries.
Run it with registry, pin resolution, provider error and pricing regression tests.
Live key checks and paid generation are outside this delivery.

CLAUDE.md requires a Haiku documentation subagent before PR/deploy. This document
is an implementation draft, not a claim that the Haiku requirement has passed.
Run `scripts/docs_gate.sh` before pushing; exact-commit CI remains authoritative.
