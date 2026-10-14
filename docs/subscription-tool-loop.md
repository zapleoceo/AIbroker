# Subscription client tools: implementation boundary

Status: implemented behind `AGENT_SUBSCRIPTION_CODEX_TOOLS_ENABLED=false` and
an empty `AGENT_SUBSCRIPTION_CODEX_TOOLS_VERIFIED_MODELS` list. The default
catalog reports `supports_tools: false`. Claude subscription jobs have the same
loop behind `AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED=false` and an empty
`AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS` (see
[Claude transport](#claude-transport)). Native API-provider tools retain their
existing contract.

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
   the call ID, name, arguments, and a deadline (`AGENT_TOOL_WAIT_S`: 90 s in production,
   code default 300 s) before exposing it. `GET
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
tool-call deadline (`AGENT_TOOL_WAIT_S`); the reaper then marks the run `worker_lost`.
A call whose arguments break the declared schema fails the run with
`tool call is outside the declared schema [<validate_result label>]: <path>: <rule> <limit> (got len|type ...)`
(also logged at WARNING with run and call ids):
`schema_violation` names up to three violated rules (JSON path, rule, limit, observed size or
type; only the first 3 errors are computed) and never an argument value or a model-chosen key
(path segments outside the schema's declared `properties` show as `*`); the arguments themselves
are not stored.

Model retry (like pydantic_ai `ModelRetry`) keeps one bad call from costing a whole re-run.
`publish_call` raises `ClientToolInvalidArguments` (a `ClientToolConflict` subclass carrying
`label` and `detail`) only for the argument-schema case (label `InvalidToolArguments`); every
other conflict still fails closed, including `UnknownToolCall`, `UnexpectedToolCall` (another
tool than `tool_choice` forces) and `InvalidToolCall`, as well as not running, cancelled,
duplicate and size limit. The first `AGENT_TOOL_ARG_RETRIES` (default 2, range
0..5) such calls per run are answered to the App Server as a tool result with `success: false`
and the text `Tool arguments rejected: <detail>. Call <tool> again with arguments that satisfy
its schema.`; nothing is published to the client, no `tool.call` event is emitted, the call is
not counted in `client_tool_history`, and the run timeline shows a `tool.result` with
`ok: false` and `[arguments rejected]` (never a value). The next violation fails the run with
the labelled error above. Second case: when the turn completes with `tool_choice` `required`
(or a specific tool) and no client tool was called, the worker sends up to
`AGENT_TOOL_REQUIRED_REMINDERS` (default 1, range 0..3) follow-up `turn/start` requests on the
same thread (`You must now call the <tool> tool with your final answer; do not reply with plain
text.`) and tracks the new turn id for all identity checks; a replayed response to an earlier
turn request still fails closed as `duplicate turn response`. Past the budget the run fails
with `required client tool was not called`.
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
16 calls per run, 64 KiB arguments/results, and exact-result idempotent retries
while the job runs are enforced. A result sent after terminal state is rejected.
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
part of this design. Claude uses the same durable callback contract over its
own transport, below.

`subscription_tool_model_canary` evaluates the temporary exact project/model
gate. `ModelRerouted` records an explicit App Server reroute notification in
the run event stream before the tool turn fails; no notification is not proof
of the serving model.

## Claude transport

Headless `claude -p` has no client-response pause of its own, so a Claude
client-tool run (`ClaudeCLI.start` -> `claude_tool_turn.run_tool_turn`) uses the
CLI's stream-json control channel, the one the Claude Agent SDK uses for its
in-process MCP servers (wire format taken from `claude-agent-sdk`, MIT):

- `build_tool_argv` adds `--input-format stream-json` and
  `--mcp-config={"mcpServers":{"broker":{"type":"sdk","name":"broker"}}}` to the
  usual flags (`--strict-mcp-config`, `--setting-sources=user`,
  `--disallowedTools=`). An `sdk` server is not a process, file or socket: the CLI
  sends its MCP traffic to the worker as `control_request` / `mcp_message` lines
  and waits for our `control_response`. Built-in tools stay off (`--tools ""`;
  `Read(inputs/**)` only for attachments) and the allow-list names only
  `mcp__broker__<tool>`. Tool names must match `[A-Za-z0-9_-]{1,51}` (the API's
  64-character limit minus the prefix).
- The worker writes a control `initialize`, then the prompt as a stream-json user
  message, and keeps stdin open. It answers MCP `initialize`, `tools/list` (the
  declared tools, `inputSchema` = the caller's `parameters`), `ping` and
  notifications; any other method gets JSON-RPC `-32601`.
- `tools/call` goes through the same `publish_call` / `take_result` as Codex, with
  a Broker call id `call_claude_<run>_<n>`. The caller's result returns as MCP
  `content` text with `isError` = not `success`. A schema-breaking call is
  answered with `isError: true` and the rule (never a value), within
  `AGENT_TOOL_ARG_RETRIES`; any other conflict fails the run closed.
- Any other control request (a permission prompt, another MCP server, a cancel
  of a request we have not answered) fails the run closed (`client_tool_protocol`);
  a cancel of an already answered request is ignored. A line that is not valid JSON
  is skipped, as on the plain path. The `apiKeySource` billing check
  of the stream parser still applies.
- `tool_choice` `required` (or a named function) with no call by the end of a
  turn sends up to `AGENT_TOOL_REQUIRED_REMINDERS` reminder user messages on the
  same session; usage is summed over the turns (per-turn `result.usage` is
  assumed - verified on the first live reminder run) and is reported on failed
  and cancelled runs too.
- `MCP_TOOL_TIMEOUT` is set for the CLI to `AGENT_TOOL_WAIT_S` + 60 s, so the CLI
  does not abandon a call the caller may still answer.

Gates mirror Codex: `AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED`, the exact-id lists
`AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS` and
`AGENT_SUBSCRIPTION_CLAUDE_TOOLS_CANARY_MODELS` (only `claude-sub/` ids that are
themselves account-verified; anything else empties the list), and the shared
project canary `AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS` (owner project
12). `subscription_tools_enabled(provider)` is the per-provider switch used by the
catalog, the submit route and the worker. A model is listed in the verified list
only after a live round-trip (call -> caller result -> final answer) on that
exact id.

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
