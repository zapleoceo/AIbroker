# Subscription models in the universal jobs API

`GET /v1/models` (the Broker catalog endpoint; there is no
`/v1/models/catalog` route) includes explicit route ids such as
`claude-sub/claude-sonnet-5-5` and `codex-sub/gpt-6.1-sol`. Each row retains
its distinct `id`, `owned_by`, `capabilities`, `source: "broker"`,
`billing_type: "subscription"`, and `price: {"kind": "subscription"}`. The
subscription price has no invented per-token amount. Each subscription row
has `available: true` only after its exact id is in the owner-confirmed
`AGENT_SUBSCRIPTION_VERIFIED_MODELS` list; a Codex row additionally needs its
separately approved worker route enabled. A client merging catalogs should key by `(source, id)`, not the
bare `name`: an API model and subscription model can have different billing.

Submit using the existing `POST /v1/jobs?capability=chat:fast` and the ordinary
`ChatRequest`, setting `model` to the exact catalog id. The response keeps
`job_id`, `poll_url`, and `poll_after_s`; poll the existing `GET /v1/jobs/{id}`.
The job goes to the dedicated CLI worker and is never inserted into a normal
provider fallback chain. `provider` on success is `claude-sub` or `codex-sub`;
`model` is the route id, `model_served` is the exact CLI model passed, and
`cost_usd` remains null. A successful subscription attestation is reflected in
`billing_type: "subscription"`. Quota waits stay pending on the same account;
there is no API-billed fallback.

Access requires the capability's existing scope plus `llm:agent`, the server
project allowlist, and the exact CLI model and `owner` account in
`AGENT_PROJECT_POLICY`. The worker rechecks these. The configured list of
subscription candidates is `AGENT_SUBSCRIPTION_MODELS` (JSON object keyed by
`claude-sub`/`codex-sub`). `AGENT_SUBSCRIPTION_VERIFIED_MODELS` uses the same
shape and must be a subset. Malformed or inconsistent input exposes no
subscription models. Compose uses the `default` sentinel when no override is
provided. The default verified list contains only the completed Claude Sonnet
smoke model; other candidates remain unavailable until confirmed by a live run.
The default Codex candidate list contains the seven picker-visible exact IDs
returned by the owner's Codex App Server `model/list` on 2026-10-12:
`gpt-6.1-sol`, `gpt-6-astra`, `gpt-6-sol`, `gpt-6-luna`, `gpt-5.6-sol`,
`gpt-5.6-terra`, and `gpt-5.6-luna`. The response marked all seven
`hidden: false`. Hidden internal and legacy entries are not Broker candidates.
Account-aware discovery supports adding a candidate; it does not prove a run
will complete. Add each exact ID to the verified list only after a successful
live subscription run. Claude Code has no documented read-only model-list CLI,
so the owner must confirm further Claude models in the interactive account. These
lists do not grant project access: enumerate each desired exact CLI id in
`AGENT_PROJECT_POLICY` for each approved project. Never use a wildcard or
infer subscription availability from API-model catalogs. The default Codex
route is disabled by `AGENT_SUBSCRIPTION_CODEX_ENABLED=false`. Enabling it
requires separate approval of its sandbox profile and a live CLI canary.

The implementation exposes configured candidates through `subscription_models()`, exact
pin lookup in `subscription_model()`, and the availability check in
`subscription_model_verified()`. For a subscription job,
`prompt_for_messages()` preserves text turns and roles;
`response_format_problem()` rejects unsupported formats before enqueue and
`output_problem()` validates the final JSON. `live_files()` hides expired
file bytes when the client polls the job.

Subscription jobs accept text role turns and `response_format` `json_object`
or `json_schema`; the final JSON is validated, with invalid output ending in
an error. Tool definitions, tool history, image input, vision, nondefault
temperature, max_tokens, and `stream: true` are rejected before enqueue because the CLI path
does not implement those controls. `GET /v1/jobs/{id}` can return `files` for
worker-exported artifacts. Generated images must be decodable PNG bytes in
`content_b64` with a `.png` path and, for Codex export, sha256 and dimensions;
text or SVG is not an image result. File content expires under the worker's
normal artifact TTL. No result is asserted to contain a PNG until a real
headless Codex run proves it.

`/v1/agent/runs` remains available for diagnostics and features specific to
agent sessions. Clients of the universal jobs contract do not need it.
