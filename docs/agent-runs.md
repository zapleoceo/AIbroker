# Agent runs (owner-only CLI workers)

Run the official **Codex CLI** / **Claude Code CLI** as workers driven by the broker queue.
Status: **phase 1, C1 + C2-min** - data model, event schema, adapter contract, fake adapter,
API, worker lane, locks, workspaces, timeouts. One real adapter (`ClaudeCLI`) and the billing guard are in; Codex, the model catalog and multi-account are later.

## Legal / scope note

Owner-only, personal-subscription use. This is **not a product backend**: it must not serve
third parties or be offered as a service. Access needs the `llm:agent` scope
(`AGENT_SCOPE`), granted explicitly by the owner (admin API). It is in `OWNER_ONLY_SCOPES`
(`aibroker.agent.constants`) and is stripped from `SIGNUP_DEFAULT_SCOPES`, so self-signup
can never grant it. Capabilities: `agent:codex`, `agent:claude` (`AGENT_CAPABILITIES`).

## Readiness and quota rules (hard requirements)

- **The subscription route must not be declared ready from fake tests alone.** Everything
  here is tested against `FakeAgentCLI`; a real CLI on a real subscription login has not
  been run. Readiness requires the phase-4 verification against the real CLIs.
- On **quota exhaustion** the run WAITS (see below), visible as `waiting_quota`.
  **No fallback to API billing/OpenRouter, no "extra usage", no rotation to another account.**
  An account is chosen by the caller (`account_id`) and never swapped by the broker.

## Architecture

A run is a `deep_jobs` row (`capability = agent:*`) - so queue, retention (7 days) and the
client-visible request id `job-<id>` are reused - plus:

| Table | Model | Holds |
|---|---|---|
| `agent_runs` | `AgentRunRow` | `task_id`, `account_id`, `session_id` (set from the first `session.assigned`), `resume_session_id`, `idempotency_key`, `workspace`, `cancel_requested`, `last_event_at`, `last_seq`, `outcome` |
| `agent_run_events` | `AgentRunEventRow` | `(run_id, seq)`, `ts`, `type`, `payload` (JSONB); cascade-deleted with the job |

Migration `infra/sql/migrations/017_agent_runs.sql`, mirrored in `infra/sql/init.sql`.
The LLM dispatcher (`services/job_queue.py`) never claims or stale-requeues `agent:%` rows;
they belong to the dedicated worker lane (C2).

`aibroker.agent.runs`: `create_run` (validate, dedupe), `get_run`, `list_events`,
`append_event` (single writer per run; a terminal event closes the run and moves the queue
row to `done` / `error`), `cancel_run`. Errors: `RunRejected` (400), `RunConflict` (409).

## API (scope `llm:agent`)

| Method | Path | Body / query | Result |
|---|---|---|---|
| `POST` | `/v1/agent/runs` | `AgentRunRequest` | `AgentRunSubmitResponse` (202) |
| `GET` | `/v1/agent/runs/{id}` | - | `AgentRunResponse` |
| `GET` | `/v1/agent/runs/{id}/events?after_seq=` | - | `AgentEventsResponse` (poll; pass `next_after_seq` back until `finished`) |
| `POST` | `/v1/agent/runs/{id}/cancel` | - | `AgentRunResponse` |

Handlers: `agent_run_submit`, `agent_run_get`, `agent_run_events`, `agent_run_cancel`.
`AgentRunRequest`: `capability`, `prompt`, `task_id`, `account_id` (required), optional
`session_id` (resume), `idempotency_key`, `wall_timeout_s` (10-7200, default 1800),
`idle_timeout_s` (5-1800, default 300), `allowed_tools` (<=32 printable entries).
Status: `pending|running|completed|failed|cancelled|timed_out`. Events items are
`AgentEventOut` (`seq`, `ts`, `type`, `payload`).

Rules enforced at submit:
- **Dedupe**: same `idempotency_key` -> same run (`deduplicated: true`); the same key with a
  different payload -> 409. An identical payload still pending/running -> same run.
- **Resume** only with an **exact** `session_id` produced by a previous run of the same
  project, capability and `account_id`; otherwise 400. A session still used by an unfinished
  run -> 409. There is no "last" / "continue" anywhere.
- **Cancel**: a queued run is cancelled at once (`run.cancelled`); a running one gets
  `cancel_requested` for the worker (C2); a finished one is returned unchanged.

## Events (schema_version 1)

Models in `aibroker.agent.events`; `parse_event` validates a raw dict (unknown type, extra
field or other version -> `ValidationError`). `usage` carries tokens/turns as the CLI
reported - never dollars. Text is clipped to `MAX_TEXT_CHARS` on store.

| type | model | fields |
|---|---|---|
| `run.started` | `RunStarted` | capability, account_id, resumed_session_id |
| `session.assigned` | `SessionAssigned` | session_id (first one wins) |
| `message.delta` | `MessageDelta` | text |
| `tool.call` / `tool.result` | `ToolCall` / `ToolResult` | call_id, name, input / ok, output |
| `file.changed` | `FileChanged` | path, action (created/modified/deleted) |
| `usage` | `Usage` | input_tokens, output_tokens, cache_read_tokens, turns |
| `run.completed` | `RunCompleted` | final_text, files (`FileRef`) |
| `run.failed` | `RunFailed` | error_class, message |
| `run.cancelled` | `RunCancelled` | - |
| `run.timed_out` | `RunTimedOut` | limit (wall/idle) |

Exactly one terminal event ends a run (`TERMINAL_TYPES`).

## Adapter contract

`AgentCLI` (`aibroker.agent.adapter`): `supports_resume`, `billing_mode()` (positive
"subscription", else "api"/"unknown" which will be refused in C3), `start(AgentRunSpec)` ->
async iterator of events, `cancel()`. One instance serves one run. `FakeAgentCLI`
(`aibroker.agent.fake`) is a scripted fake: emits events, hangs until cancelled, raises,
writes files into the workspace.

## Worker lane (C2)

`aibroker.agent.worker` is a lane of its own, started from the app lifespan only when
`AGENT_WORKER_ENABLED=true` (off by default - enable it on the one process that has the CLIs).

- `claim_agent_runs(capability, limit)` claims `agent:*` rows of ONE capability
  (`FOR UPDATE SKIP LOCKED`); `concurrency_for` gives the slots
  (`AGENT_CONCURRENCY_CODEX` / `AGENT_CONCURRENCY_CLAUDE`, default 1). Slots are never shared
  with the LLM dispatcher, whose claim and stale-requeue SQL exclude `agent:%`.
- `fail_stale_agent_runs`: a `running` run past `started_at + wall_timeout + 300 s`
  becomes `run.failed worker_lost`. It is **never re-run** (a CLI run has side effects).
- `drain_agent_once` is the deterministic one-pass used by tests; `agent_worker_loop` is the
  forever loop.

`aibroker.agent.executor.execute_run` drives a claimed run (`ExecConfig` from
`exec_config_from_settings`) to exactly one terminal event:

1. cancel flag already set -> `run.cancelled`; no adapter registered
   (`aibroker.agent.registry`: `register_adapter`, `make_adapter`, `clear_adapters` for tests) -> `adapter_unavailable`.
2. **Session lock** (resume only): `session_lock(account_id, session_id)` (key from `lock_name`) is a Postgres
   advisory lock on a dedicated *direct* connection (not PgBouncer); in-process elsewhere.
   Busy -> the run goes back to `pending` with a delay (`requeue_run`), failing
   `session_busy` after `session_busy_max` tries. Two runs never use one session at once.
3. **Workspace**: `create_workspace` makes `AGENT_WORKSPACE_ROOT/run-<id>` with mode 0700;
   it is removed afterwards, also on failure (`cleanup_workspace`).
4. `run.started`, then the adapter stream. **Wall** (`wall_timeout_s`) and **idle** (no event
   for `idle_timeout_s`) timeouts give `run.timed_out`; the cancel flag
   (`cancel_requested`, polled every `AGENT_CANCEL_POLL_S`) gives `run.cancelled`. Both call
   `adapter.cancel()` (the adapter contract owns SIGTERM then SIGKILL), bounded by
   `AGENT_CANCEL_GRACE_S`.
5. Adapter crash -> `adapter_error`; stream ending without a terminal event ->
   `no_terminal_event`. Failures are never retried or rerouted. The one exception is
   quota exhaustion, which waits (below).
6. **Files**: paths from `file.changed` and `run.completed.files` go through `collect_files`:
   regular, non-symlink files inside the workspace only; per-file cap
   (`AGENT_FILE_MAX_BYTES`), total cap, count cap; over-cap/unsafe entries are listed with
   `skipped` and no content. Content is served until `expires_at` (`AGENT_FILES_TTL_S`),
   then the API returns `expired: true` and no content.

## Models, usage, billing separation

- **Model catalog comes from the CLI**, never from this repo: `AgentCLI.list_models()` returns
  `ModelInfo` entries (Codex: app-server `model/list` + `provider/capabilities`; Claude: its
  own list) or `None` when unavailable. Phase 1 defines the interface and the fake only.
- **No silent substitution.** `model` (request field) is passed to the adapter exactly. If the
  CLI catalog is available and lacks the id, the run fails `model_unavailable` before start;
  if the catalog is unavailable the exact id is still passed through. `latest` is used only
  when the caller wrote `latest`.
- **Subscription usage is kept apart from API billing**: it is stored on the run
  (`usage_*` columns, `usage_source` = `cli_reported`, `billing_mode`), never in `usage_log`,
  and `cost_usd` stays NULL unless a cost is confirmed (nothing sets it in phase 1).

## Billing guard and the real adapter (C2-min)

`aibroker.agent.billing`: `check_billing_mode(capability, adapter, env)` runs before EVERY
adapter start (after `run.started`, inside `execute_run`). `FORBIDDEN_ENV` lists the
API-billing variables per capability (`forbidden_env_present` reports names, never values):
claude: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_USE_BEDROCK`,
`CLAUDE_CODE_USE_VERTEX`; codex: `OPENAI_API_KEY`, `CODEX_API_KEY`. Any of them in the worker
environment (`ExecConfig.env`, default `os.environ`), or an adapter whose `billing_mode()` is
anything but a positive `subscription` (unknown included, also when the probe raises), ends
the run with `run.failed error_class=billing_mode_refused`. The CLI is never started and there
is no fallback to API. The seen mode is stored in `agent_runs.billing_mode`.

`aibroker.agent.real_cli.ClaudeCLI` (registered by `register_real_adapters` only when
`AGENT_WORKER_ENABLED`) runs `claude -p --output-format stream-json --verbose` in the run
workspace via `build_claude_argv`: `--model <id>` exactly as given, `--resume <id>` only with
an exact session id, `--allowedTools` listing the caller's tools; no bypass / skip-permission
flag, no "continue"/"last"; the prompt goes on stdin. `billing_mode()` probes
`claude auth status` and `parse_claude_auth_status` accepts only a first-party claude.ai
login. `cancel()` sends SIGTERM, waits `kill_grace_s`, then SIGKILL. The output is parsed by
`ClaudeParser` (system/init, assistant, user tool_result, result -> events; an init
`apiKeySource` other than none is refused at runtime); `classify_failure` maps limit wording
to `quota_exhausted`. **ClaudeCLI and ClaudeParser are UNVERIFIED against the real CLI
(phase 4)**; tests use doc-derived sample lines and a python stand-in process. The CLI uses
the worker's own subscription login; the broker never reads or moves credentials.

## Quota exhaustion = wait

When the adapter reports `run.failed quota_exhausted` (`classify_failure` on the CLI's limit
wording; `parse_reset_at` reads a stated reset time - `|<epoch>` or "in N minutes/hours", wording
UNVERIFIED), `_wait_for_quota` does not fail the run: `park_for_quota` stores the non-terminal
`run.waiting_quota` event (`RunWaitingQuota`: `not_before`, `attempt`, `reset_reported`), puts
the job back to `pending` with `run_after = not_before`, and the API shows status
`waiting_quota` with `not_before` and `quota_waits`. `not_before` is the reported reset time
(+5 s) or, without one, exponential backoff 60 s x 2^n capped at 30 min
(`quota_backoff_cap_s`). The retry runs on the SAME account and subscription route (the
billing guard re-checks every attempt) and resumes this run's own session; usage adds up
across attempts. Never an API/OpenRouter fallback, never another account.

The wait is bounded by the request's overall `deadline_s` (default 6 h, counted from creation):
a `not_before` past it, an `execute_run` after it, or `expire_overdue_waiting` (worker tick)
for a run still parked ends with `run.timed_out limit=deadline`. Cancel works while waiting.

## Input attachments

`attachments` in `AgentRunRequest` (`AttachmentIn`: `name`, `media_type`, `data_b64`) carry real
images: png / jpeg / webp / gif only, at most `MAX_FILES` files, `MAX_BYTES` each,
`MAX_TOTAL_BYTES` together, name must match `NAME_PATTERN` and its extension the type, and the
bytes must match the type's magic number (`validate_attachments`, `decode_attachment`; 422 at
submit). The executor writes them with `write_attachments` to `<workspace>/inputs/` (0600) and
`AgentRunSpec.attachments` lists the paths. `ClaudeCLI` (`build_claude_prompt`) names those
paths in the prompt and `build_claude_argv` adds the `Read` tool to `--allowedTools` when the
caller did not (the only implicit permission). The bytes are never replaced by a text
description, are not returned as outputs, and are dropped from the stored request
(`strip_attachment_data`) when the run ends.

## Not done yet

Codex adapter, model catalog from the CLI (`ClaudeCLI.list_models` returns `None`),
multi-account config dirs. Phase 2+: `$imagegen` headless support is unproven; the Claude
critic must receive real image bytes.
