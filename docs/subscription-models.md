# Subscription models in the universal jobs API

`GET /v1/models` (the Broker catalog endpoint; there is no
`/v1/models/catalog` route) includes explicit route ids such as
`claude-sub/claude-sonnet-5-5` and `codex-sub/gpt-6.1-sol`. Each row retains
its distinct `id`, `owned_by`, `capabilities`, `source: "broker"`,
`billing_type: "subscription"`, and `price` with `kind: "subscription"`,
`input_usd_per_mtok: 0`, `output_usd_per_mtok: 0`, and
`subscription_fee_excluded: true`. These zeroes describe variable token cost;
they do not claim the subscription itself is free. Each subscription row
has `available: true` only after its exact id is in the owner-confirmed
`AGENT_SUBSCRIPTION_VERIFIED_MODELS` list; a Codex row additionally needs its
separately approved worker route enabled. A client merging catalogs should key by `(source, id)`, not the
bare `name`: an API model and subscription model can have different billing.

Submit using the existing `POST /v1/jobs?capability=chat:fast` and the ordinary
`ChatRequest`, setting `model` to the exact catalog id. The response keeps
`job_id`, `poll_url`, and `poll_after_s`; poll the existing `GET /v1/jobs/{id}`.
The job goes to the dedicated CLI worker and is never inserted into a normal
provider fallback chain. `provider` on success is `claude-sub` or `codex-sub`;
`model` and `requested_model` are the requested route id; `model_served` is
`null` until the CLI supplies
independent served-model evidence, and
`cost_usd` remains null. A successful subscription attestation is reflected in
`billing_type: "subscription"`. Quota waits stay pending on the same account;
there is no API-billed fallback. A Claude Code weekly or session cap (for example "You've hit
your weekly limit - resets Oct 15, 8pm (UTC)") is such a wait: the job stays pending until the
stated reset, or ends `failed` with `error_message` `quota_exhausted: <limit wording> (reset ... is past the job
deadline)` if the request deadline (`deadline_s`, default 6 h) falls first.

For `claude-sub`, the account is chosen once at submit: the first account listed for the project in
`AGENT_PROJECT_POLICY` without an active quota pause, otherwise the one whose pause ends first. The
poll response reports it as `account_id`. A started run never changes account; see
[agent-runs.md](agent-runs.md#multiple-claude-subscription-accounts).

Codex subscription requests are no longer serialized across the broker. They run concurrently up to
the worker's concurrency (`AGENT_CONCURRENCY_CODEX`) and share the subscription's usage limits, so a
`quota_exhausted` result waits on the same account rather than falling back. Because the processes
share one `auth.json`, start-ups are briefly serialized (a short `__codex_auth__` lease) whenever the
file's `last_refresh` is older than 4 days or unreadable, so a rotating refresh token is never
refreshed twice at once; see `agent-runs.md`.

Every catalog row now has a `job_request` field. Subscription rows report
`max_tokens.accepted.const: 1024` and `temperature.accepted.const: 0.7`, both
with `forwarded: false`: omit these fields, or send exactly those defaults.
For example, `max_tokens: 16` fits the generic `ChatRequest` range but is
rejected by the subscription route before enqueue. The same contract marks
tools and tool history unsupported by default, and streaming unsupported. Codex input images are accepted
only for separately verified vision model IDs. JSON schemas cannot contain
references. These values come from
the same constants and validator used by `POST /v1/jobs`.

Access requires the capability's existing scope plus `llm:agent`, the server
project allowlist, and the exact CLI model and an account in
`AGENT_PROJECT_POLICY` (`owner` for Codex; for Claude, any listed account, chosen at submit). The worker rechecks these, and `GET /v1/jobs/{id}` and
`GET /v1/deep/{id}` recheck them on every read, as do the agent run reads
(`/v1/agent/runs/{id}` and `/events`). The configured list of
subscription candidates is `AGENT_SUBSCRIPTION_MODELS` (JSON object keyed by
`claude-sub`/`codex-sub`). `AGENT_SUBSCRIPTION_VERIFIED_MODELS` uses the same
shape and must be a subset. Malformed or inconsistent input exposes no
subscription models. `AGENT_SUBSCRIPTION_VISION_VERIFIED_MODELS` is an
independent subset of verified Codex IDs; its default is empty. Only entries
in that list gain the `vision` capability and image input in the catalog.
The direct diagnostic agent route can be used to verify an input image before
enabling its exact model ID. Compose uses the `default` sentinel when no override is
provided. The default verified list contains only the completed Claude Sonnet
smoke model; other candidates remain unavailable until confirmed by a live run. The default Claude
candidates are `claude-sonnet-5-5`, `claude-opus-5-5` and `claude-haiku-5-5`; the last two are listed
with `available: false` until a live check. Fable is not listed.
The default Codex candidate list contains the seven picker-visible exact IDs
returned by the owner's Codex App Server `model/list` on 2026-10-12:
`gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`,
`gpt-5.6-terra`, and `gpt-5.6-luna`. The response marked all seven
`hidden: false`. Hidden internal and legacy entries are not Broker candidates.
Account-aware discovery supports adding a candidate; it does not prove a run
will complete. Add each exact ID to the verified list only after a successful
live subscription run. Claude Code has no documented read-only model-list CLI,
so the owner must confirm further Claude models in the interactive account. These
lists do not grant project access. A project may enumerate exact CLI ids or,
with explicit owner approval, use `"models":["*"]` to admit only configured
candidates for the requested Claude or Codex provider. The account allowlist
still applies. The universal jobs route also requires the global verified-model
check; direct diagnostic agent runs have their own CLI model guards. Never infer subscription
availability from API-model catalogs. The default Codex
route is disabled by `AGENT_SUBSCRIPTION_CODEX_ENABLED=false`. Enabling it
requires separate approval of its sandbox profile and a live CLI canary.

The implementation exposes configured candidates through `subscription_models()`, exact
pin lookup in `subscription_model()`, and the availability check in
`subscription_model_verified()`. For a subscription job,
`prompt_for_messages()` preserves text turns and roles;
`subscription_messages()` converts verified Codex image blocks into bounded
temporary attachments without placing base64 bytes in the CLI prompt;
`response_format_problem()` rejects unsupported formats before enqueue and
`output_problem()` validates the final JSON. `live_files()` hides expired
file bytes when the client polls the job.

Subscription jobs accept text role turns and `response_format` `json_object`
or `json_schema`; the final JSON is validated by the same check as the API path
(`json_object` must parse to an object, `json_schema` must match the schema;
schemas that cannot be evaluated are rejected at intake), and invalid output ends in an
`InvalidJSON` error. Tool declarations, tool history, nondefault temperature, max_tokens,
and `stream: true` are rejected before enqueue by default. The Codex App Server
client-tool path can be enabled separately with
`AGENT_SUBSCRIPTION_CODEX_TOOLS_ENABLED` and advertised per exact ID with
`AGENT_SUBSCRIPTION_CODEX_TOOLS_VERIFIED_MODELS`; see
[subscription tool loop](subscription-tool-loop.md). Claude still rejects them.
Claude subscription jobs still reject image input and `vision`. A Codex model
with separately verified vision accepts up to five embedded, single-frame PNG,
JPEG, WebP, or GIF images, each at most 5 MiB,
10 MiB total, and 25 million pixels per image. Only `data:<type>;base64,...`
URLs are accepted; remote URLs are not fetched. The server fully decodes each
image, stores it as a temporary run attachment, passes the actual file through
Codex CLI `--image`, and removes the workspace after the run. An image requires
the existing `llm:vision` scope even when sent on a chat or other non-vision
capability; this change does not grant that scope to any project. A model-level
live input-vision canary is required before adding an ID to the vision list;
the authenticated Orchestra request is a further integration check.

`GET /v1/jobs/{id}` can return `files` for
worker-exported artifacts. Generated images must be decodable PNG bytes in
`content_b64` with a `.png` path and, for Codex export, sha256 and dimensions;
text or SVG is not an image result. File content expires under the worker's
normal artifact TTL. A direct Codex subscription worker run (`551063`)
generated and exported a decodable 1254x1254 PNG, 1,689,768 bytes, SHA-256
`9e8545d12421b78c41ca6a06b58d7f4d223eaeb053b2d5b32ee7b90e3ca56521`.
The universal HTTP route was also exercised by Orchestra: job `551214` produced
a decodable 1254x1254 PNG with SHA-256
`d19fae578ae4d4c0a03c3e5466ff197b4b3bde4c5a0447e44d1ab1e810e685a`.
This does not verify input vision or client tool-call conformance.

For the Orchestra adapter, submit the ordinary text request (no `tools`,
`response_format`, `stream`, nondefault `temperature`, or nondefault
`max_tokens`):

```json
{"messages":[{"role":"user","content":"$imagegen Generate exactly one real PNG image of a red apple on a white background. Use the built-in GPT image generation tool; do not draw it with code or SVG."}],"model":"codex-sub/gpt-6.1-sol"}
```

Use `POST /v1/jobs?capability=chat:fast`, then poll the returned `poll_url`.
On success, `status` is `done`, `provider` is `codex-sub`, `model` is the
catalog id, `model_served` is `null`, `billing_type` is
`subscription`, and `cost_usd` is null. A generated image is in `files` as
`path` ending in `.png`, `content_b64`, `size`, `sha256`, `width`, `height`,
and `expires_at`. Decode `content_b64` and check the PNG signature, SHA-256,
and dimensions; `text` alone is not an image result. After file expiry,
`content_b64` becomes null and `expired` becomes true. A run may complete
without an image, so the adapter must check `files` explicitly.

`/v1/agent/runs` remains available for diagnostics and features specific to
agent sessions. Clients of the universal jobs contract do not need it.
