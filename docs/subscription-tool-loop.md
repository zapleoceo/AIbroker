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

The App Server numbers its own requests to the Broker (`item/tool/call`) with an
id counter independent of ours, so the adapter treats a message as the reply to
its `initialize` / `thread/start` / `turn/start` request only when the id matches
AND the message has no `method`. Matching on id alone mistook the App Server's
fourth tool call (its id 3) for our `turn/start` reply and failed the run with
"App Server did not assign a turn" (prod, 2026-10-13, exam jobs with 4+ calls).
Incoming messages are logged at DEBUG with method and id only.
A second `turn/start` reply, or a repeated `item/tool/call` (same server request id
or same `callId`), fails the run closed; a repeated `thread/start` reply is ignored.

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
   `finish_reason: tool_calls` while the call waits (`is_waiting`). The model
   process remains paused. The deadline bounds only the wait for a result: once
   it passes with no result, poll returns a plain `status: pending` with
   `poll_after_s` and no `tool_calls` or `finish_reason`. The client authorizes
   and executes the function under its own policy; Broker never executes that client function.
4. An authenticated, same-project `POST /v1/jobs/{id}/tool-results`
   accept exactly one result per pending call ID. It must reject an unknown,
   expired (past its deadline without an accepted result), duplicate-with-different-content,
   or already-terminal call with 409. An accepted (`202`) result is always delivered to
   the worker, even when the worker's poll lands after the deadline: `take_result`
   returns a ready result past the deadline.
   The worker sends the bounded result as `DynamicToolCallResponse` and
   continues the same turn. Repeating the identical result is idempotent.
   After the final model answer, the existing `GET /v1/jobs/{id}` returns
   `status: done` and `finish_reason: stop`.

The pending call and result use `deep_jobs.result_meta` under row locks. The
worker checks its lease fence before publishing or consuming a callback and
never replays a possibly executed client operation after lease loss. On lease
loss, `clear_pending_call` withdraws the call only while it is still waiting:
poll stops advertising it and later result submits get 409. A result already
accepted with 202 is kept. On a crash, the run's terminal write replaces
`result_meta`, which withdraws the call. A hard worker death is bounded by the
300-second deadline; the reaper then marks the run `worker_lost`.
`run_tool_turn` drives the App Server exchange; `publish_call`, `submit_result`,
and `take_result` manage the durable handoff and raise `ClientToolConflict` for
invalid state. `ClientToolCancelled` (a `ClientToolConflict` subclass) marks a
cancel during the tool wait; `is_waiting` decides whether poll advertises the
call; `clear_pending_call` withdraws it. `ClientToolResultIn` validates the
HTTP payload and `jobs_tool_result` enforces project ownership. The catalog uses
`subscription_tool_model_verified` to require an exact verified Codex ID.
Cancellation terminates the waiting App Server process and ends the run as
`run.cancelled` (outcome `cancelled`), not `failed` / `client_tool_protocol`.
Deadlines also terminate the waiting App Server process. A client
should use the Broker call ID as its own idempotency key. One outstanding call,
`AGENT_CLIENT_TOOL_MAX_CALLS` calls per run (64), `AGENT_CLIENT_TOOL_MAX_BYTES` for the arguments and
for each result (1 MiB; a larger result is `413`), `AGENT_CLIENT_TOOL_WAIT_S` for one call to
be answered (900 s), and exact-result idempotent retries while the job runs are enforced.
The sum of waits is bounded by the run's outer cap `AGENT_SUBSCRIPTION_MAX_WALL_S` (14400 s), not by
calls x wait. While a call is pending the executor suspends the idle watchdog (the adapter ends the
wait at the tool deadline; the executor only backstops it with that deadline plus 60 s); a CLI that is
silent with no pending call is ended by `AGENT_SUBSCRIPTION_IDLE_S`; the App Server adapter reports
every valid `item/*` notification of its thread and turn as a payload-free `progress` event, which
resets idle (and refreshes `last_event_at` every 30 s for the reaper) without storing a row. The App Server stdout line limit
is 16 MiB, so a long notification or tool argument line is read whole. Cancel keeps working during a
wait: the worker notices the flag within a second. A result sent after terminal state is rejected.
The current `deep_jobs` pending/done/error status values remain
intact for clients that do not use tools. In a subscription result,
`requested_model` keeps the exact route pin and `model_served: null` means the
CLI has not independently attested which model served it. This does not block
the functional callback canary. The raw client tool result is removed from pending
metadata when consumed and withheld from the persistent tool event.

This is a new authenticated write endpoint and a new path for client-provided
tool output into the worker. Independent review and CI precede enabling one
exact model ID for a controlled canary under the existing owner-only project
policy. For project 12, the narrow canary settings are
`AGENT_SUBSCRIPTION_CODEX_TOOLS_ENABLED=true`,
`AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS=12`, and
`AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_MODELS=["codex-sub/gpt-6.1-sol"]`.
The canary list works only alongside the existing `llm:agent` scope, project
allowlist, and exact model/account policy; it does not advertise
`supports_tools: true` in the catalog. The API and worker both check the same
project/model gate. After one successful callback through Orchestra, clear the
canary settings and put only that exact route in
`AGENT_SUBSCRIPTION_CODEX_TOOLS_VERIFIED_MODELS` to advertise it. A failed
canary leaves the verified list empty. No new
scope, external port, credential, API-paid fallback, or wider Linux sandbox is
part of this design. Claude subscription tools require a separate adapter:
the current Claude headless CLI does not expose this client-response pause;
the Claude Agent SDK has in-process MCP tool handlers, which would need the
same durable Broker callback contract and separate review.

`subscription_tool_model_canary` evaluates the temporary exact project/model
gate. `ModelRerouted` records an explicit App Server reroute notification in
the run event stream before the tool turn fails; no notification is not proof
of the serving model.

## Input image canary

Input vision is separate from PNG generation. The deployed universal jobs
route accepts bounded `image_url` `data:` blocks, writes actual image files,
and passes them through Codex CLI `--image`, but every Codex model remains
unavailable for input vision until its exact ID is in
`AGENT_SUBSCRIPTION_VISION_VERIFIED_MODELS`. That allowlist is empty by default.
The production project 12 already has `llm:vision` and `llm:agent`; no new
scope grant is needed for its canary.

Run the diagnostic image probe even when `model_served` is unknown. A correct
answer to a visually discriminating question proves functional image handling
for the requested route in that run; it does not certify underlying model
identity. Keep `requested_model`, `model_served`, any `model.rerouted` event,
subscription billing attestation, and provider-reported token counts separate
in the result. The diagnostic route does not require the universal vision
allowlist, so the first probe needs no new scope or credential.

For a functional callback canary, Orchestra submits one ordinary
`POST /v1/jobs?capability=chat:fast` using the existing project-12 key,
`model: codex-sub/gpt-6.1-sol`, and one harmless declared function such as
`lookup_probe({id: 7})`. Return a fresh unpredictable marker in the tool
result, and require that exact marker in the final answer. A pass requires a `pending` poll with exactly one
schema-valid `tool_calls` entry, one same-project result accepted at
`POST /v1/jobs/{id}/tool-results`, and a final `done` answer that uses the
supplied result. A cross-project or mismatched call ID must be refused. Record
`requested_model`, `model_served: null` when unknown, reroute evidence if
observed, subscription billing attestation, and reported token usage. This
proves callback function for the requested route; it does not prove the
underlying model's identity. Do not send a paid API fallback.

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
