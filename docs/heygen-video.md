# HeyGen Video through AI Broker

The default video provider remains Google Gemini. HeyGen is an explicit
option, not an automatic fallback or a public signup feature. This page supplements the default Gemini
video chapter in the API reference.

Submit to the existing asynchronous `POST /v1/video/jobs` route with
`provider: "heygen"` and optionally `model: "heygen/heygen-video-1"`.
Send the same `Idempotency-Key` only when retrying the same payload. Accepted
jobs return the standard polling URL. Completed jobs return MP4 including any
audio produced by the provider.

Supported here: 5–15 seconds, 480p or 768p, and 9:16, 16:9 or 1:1. A reference
image uses reference-to-video; without it the job is text-to-video. State
dialogue, room tone and any background effects in the prompt.

HeyGen access is opt-in per Broker project and requires the existing video
permission plus its provider-specific grant. Configure a scoped video key
using the normal provider credential flow. Keep vendor credentials out of
client applications. The change does not fund or enable a vendor account.

The `HeyGenVideoTransport` submits and polls the vendor job and retrieves
its media. `estimate_heygen_cost` reserves the applicable resolution and
reference-mode rate before the paid request.

See [HeyGen Video docs](https://developers.heygen.com/docs/models/heygen-video)
and the [separate API credits guide](https://help.heygen.com/en/articles/10060327-heygen-api-pricing-credits-explained).

Failed or unexpected HeyGen answers (POST and GET of the vendor video) are kept on
the job as `result_meta.provider_responses` by `note_provider_response()`, with the
status and a redacted body cut to 1000 characters. An `unknown` outcome keeps its
budget hold until reconciliation; a job without a provider receipt is closed with
`POST /admin/media/jobs/{job_id}/resolve-no-submission` only after the provider confirms
that no video exists. See the media receipts plan for the full contract
(`resolve_unknown_without_submission`, `MediaNoSubmissionRequest`).
