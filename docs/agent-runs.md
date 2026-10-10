# Agent runs (owner-only CLI workers)

Run the official **Codex CLI** / **Claude Code CLI** as workers driven by the broker queue.
Status: **phase 1, checkpoint C1** - data model, event schema, adapter contract, fake adapter,
API. No worker executes anything yet (C2) and the real CLI adapters do not exist yet (C3).

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
- On **quota exhaustion** the run waits or fails with `error_class=quota_exhausted`.
  **No fallback to API billing, no "extra usage", no rotation to another account.**
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

## Not done yet

C2: worker lane, per-session advisory lock, workspaces, wall/idle timeouts, cancel
execution, file caps. C3: billing-mode guard and the real `CodexCLI` / `ClaudeCLI` adapters
(unverified against the real CLIs until phase 4).
