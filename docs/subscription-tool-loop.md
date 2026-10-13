# Subscription client tools: implementation boundary

Status: implemented behind `AGENT_SUBSCRIPTION_CODEX_TOOLS_ENABLED=false` and
an empty `AGENT_SUBSCRIPTION_CODEX_TOOLS_VERIFIED_MODELS` list, and, for Claude,
behind `AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED=false` and an empty
`AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS` list. The default catalog
reports `supports_tools: false` for every subscription model. With the Claude
gate off, `POST /v1/jobs` with `tools` on a `claude-sub/*` pin answers `503`
("Claude subscription client tools are not enabled for this project and model").
Native API-provider tools retain their existing contract. Claude is described in
[Claude client tools (MCP bridge)](#claude-client-tools-mcp-bridge).

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
part of this design. Claude subscription tools use the MCP bridge below, not
the App Server.

`subscription_tool_model_canary` evaluates the temporary exact project/model
gate. `ModelRerouted` records an explicit App Server reroute notification in
the run event stream before the tool turn fails; no notification is not proof
of the serving model.

## Claude client tools (MCP bridge)

A `claude-sub/*` job with `tools` follows the same contract as Codex: `pending`
with `tool_calls` and `finish_reason: "tool_calls"`, the caller posts
`/v1/jobs/{id}/tool-results`, the same run continues, then `done`. The Claude
headless CLI cannot pause for a client response by itself, so the worker gives
the CLI one MCP server named `broker`:

- `claude_tools.write_bridge_files` writes `<workspace>/.aibroker-mcp/tools.json`
  (name, description and `inputSchema` of exactly the job's tools), `mcp.json`
  (command and args only, no `env`, no secrets) and reserves `b.sock`. The CLI
  gets `--mcp-config <mcp.json>` next to the existing `--strict-mcp-config`.
- The command is `python -I -m aibroker.agent.mcp_bridge --socket ... --tools ...`
  (`-I`: the run workspace, the CLI's cwd, is not put on `sys.path`). The shim is
  stdlib only and speaks newline-delimited JSON-RPC over stdio: `handle_message`
  answers `initialize`, `ping`, `tools/list`, `tools/call`; `serve` is the loop;
  `socket_forward` relays one call per connection to the worker; `load_tools`
  reads the tools file; `main` is the entry point.
- In the worker, `claude_tools.ToolRelay` listens on the socket (`serve`) and
  handles each call (`call`): it checks the arguments against the declared schema
  (`validate_result`, the Codex check), then `publish_call` (same fences, 16-call and
  64 KiB limits, 300 s deadline), then polls `take_result`. The result becomes
  the MCP result: text content, `isError: true` when `success` is false.
- `run_claude_tool_turn` drives the CLI (`exec_argv`) and rewrites its events.
  `tool_choice: "none"` runs a plain text turn without the bridge.
- Built-in tools are off: `--tools ""`, `--allowedTools=mcp__broker__*` as the
  only allow rule, and `--disallowedTools` names every built-in tool of the
  official tools reference (`BUILTIN_DENY`) besides the scoped credential and
  `/proc` denies. `allowed_tools` and attachments are refused for these runs.

Differences from Codex that clients can observe:

- **Call ids.** The MCP request carries no tool-use id we rely on, so the relay
  matches each call to the CLI's own `tool_use` event by tool name and
  canonical arguments (oldest first) and publishes the call under that
  `toolu_*` id; the `tool.call` event (name rewritten to the bare tool name,
  without `mcp__broker__`) and the poll `tool_calls[].id` are then the same id.
  If no event matches within `ANNOUNCE_WAIT_S` (5 s) the id is `call_N`. Unverified against
  the real CLI.
- **Schema-invalid arguments** do not fail the run at once (Codex does): the model
  gets an MCP error result and nothing is published, so it may correct itself.
  Each rejected call counts: the third one (`MAX_INVALID_CALLS`), or any call that
  makes valid plus rejected calls exceed 16, ends the run as
  `client_tool_protocol`.
- **Faults.** Expired deadline, cancel and publish refusal end the run like
  Codex (`client_tool_protocol` / cancelled). Any other exception in the relay
  (`StaleLease`, database errors) is logged, withdraws the still-waiting call
  and fails the run as `adapter_error`.
- **Required tool.** `tool_choice` `required` or a named function fails the
  run with `client_tool_protocol` when the CLI completes without calling one.

Gate: `subscription_tools_available(model, project_id)` is the one check used by
the API (`503`), the executor at run time (`route_disabled`), and, together with
`subscription_tool_model_verified`, the catalog (`supports_tools`). For
`claude-sub/*` it needs `AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED` and the exact id
in `AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS` (a JSON list such as
`["claude-sub/claude-sonnet-5-5"]`; ids must also be verified catalog models);
there is no canary list for Claude.

Canary: enable the gate for one exact model, submit one `lookup_probe` job as
for Codex, and check that the poll shows one `tool_calls` entry, that its id equals the
`tool.call` event id, that the result is accepted with `202`, that the final
answer contains the marker, and that the init message lists no built-in tool.
CLI flags used: `--mcp-config`, `--strict-mcp-config`, `--tools`,
`--allowedTools`, `--disallowedTools` are documented at
code.claude.com/docs/en/cli-reference and `/permissions`; the `--mcp-config <path>` form
(space, not `=`) follows the documented example. The MCP wire details (protocol version
echo, stripped shim environment) are unverified on the real CLI.

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
