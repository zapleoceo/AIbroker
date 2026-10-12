# Subscription client tools: implementation boundary

Status: implemented behind `AGENT_SUBSCRIPTION_CODEX_TOOLS_ENABLED=false` and
an empty `AGENT_SUBSCRIPTION_CODEX_TOOLS_VERIFIED_MODELS` list. The default
catalog reports `supports_tools: false`. Claude subscription jobs still reject
tools. Native API-provider tools retain their existing contract.

## Verified transport capability

The production Codex worker has `codex-cli 0.162.1`. Its generated experimental
App Server schema includes `ThreadStartParams.dynamicTools`,
`DynamicToolCallParams` (`threadId`, `turnId`, `callId`, `tool`, `arguments`), and
`DynamicToolCallResponse` (`contentItems`, `success`). The official App Server
protocol sends `item/tool/call` to its client and waits for that client's
response. The current Broker adapter uses `codex exec --json`, whose tool events
describe operations already performed inside the CLI. Mapping those events to
public `tool_calls` would falsely imply that Orchestra authorized execution.
The pinned schema proves protocol availability, not live model conformance.

## Universal jobs exchange

1. An authorized client submits the existing `POST /v1/jobs?capability=...`
   request with a subscription model, declared function schemas, and
   `tool_choice`. The server validates the same tool definitions as native
   providers. The job remains pinned to its exact subscription account/model.
2. The worker starts App Server over stdio inside its existing isolated Codex
   container, with the existing workspace and permission profile. Its
   `initialize` request opts in with `capabilities.experimentalApi: true`,
   which the protocol requires for `thread/start.dynamicTools`. It advertises
   *only* the functions declared by this job as dynamic tools. It does not
   expose a host port, store a new credential, or call Orchestra directly.
3. On `item/tool/call`, the worker checks the thread and turn IDs, then persists
   the call ID, name, arguments, and a 300-second deadline before exposing it. `GET
   /v1/jobs/{id}` reports `status: pending`, `tool_calls`, and a distinct
   `finish_reason: tool_calls` while the call waits. The model process remains
   paused. The client authorizes and executes the function under its own
   policy; Broker never executes that client function.
4. An authenticated, same-project `POST /v1/jobs/{id}/tool-results`
   accept exactly one result per pending call ID. It must reject an unknown,
   expired, duplicate-with-different-content, or already-terminal call.
   The worker sends the bounded result as `DynamicToolCallResponse` and
   continues the same turn. Repeating the identical result is idempotent.
   After the final model answer, the existing `GET /v1/jobs/{id}` returns
   `status: done` and `finish_reason: stop`.

The pending call and result use `deep_jobs.result_meta` under row locks. The
worker checks its lease fence before publishing or consuming a callback and
never replays a possibly executed client operation after lease loss.
Cancellation and deadlines terminate the waiting App Server process. A client
should use the Broker call ID as its own idempotency key. One outstanding call,
16 calls per run, 64 KiB arguments/results, and exact-result idempotent retries
are enforced. The current `deep_jobs` pending/done/error status values remain
intact for clients that do not use tools. A completed subscription result has
`model_served: null`: the CLI model pin is not independent evidence of the
model actually served. The raw client tool result is removed from pending
metadata when consumed and withheld from the persistent tool event.

This is a new authenticated write endpoint and a new path for client-provided
tool output into the worker. Independent review and CI precede enabling one
exact model ID for a controlled canary under the existing owner-only project
policy; that first enablement is the live callback canary window. Leave the
feature disabled if the canary fails, and do not add other model IDs without
their own canary. No new
scope, external port, credential, API-paid fallback, or wider Linux sandbox is
part of this design. Claude subscription tools require a separate adapter:
the current Claude headless CLI does not expose this client-response pause;
the Claude Agent SDK has in-process MCP tool handlers, which would need the
same durable Broker callback contract and separate review.

## Input image canary

Input vision is separate from PNG generation. The deployed universal jobs
route accepts bounded `image_url` `data:` blocks, writes actual image files,
and passes them through Codex CLI `--image`, but every Codex model remains
unavailable for input vision until its exact ID is in
`AGENT_SUBSCRIPTION_VISION_VERIFIED_MODELS`. That allowlist is empty by default.
The production project 12 already has `llm:vision` and `llm:agent`; no new
scope grant is needed for its canary.

Using the existing Orchestra project key inside Orchestra, first submit one
diagnostic `POST /v1/agent/runs` for `agent:codex`, `model: gpt-6.1-sol`,
`account_id: owner`, `allowed_tools: []`, and one small, valid PNG attachment
(`name`, `media_type: image/png`, `data_b64`). Ask a visually discriminating
question and check the final answer and subscription billing attestation.
This diagnostic route uses the same real `--image` transport without changing
the public catalog. After a successful canary and owner review, add only
`gpt-6.1-sol` to the input-vision verified setting, then submit the matching
`POST /v1/jobs?capability=vision` request with a `data:image/png;base64,...`
`image_url` block and poll `GET /v1/jobs/{id}` through Orchestra. Do not infer
input vision from the successful output-PNG job.
