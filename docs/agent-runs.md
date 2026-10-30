# Agent runs (owner-only CLI workers)

Run the official **Codex CLI** / **Claude Code CLI** as workers driven by the broker queue.
Status: the universal jobs API, Claude and Codex CLI adapters, and subscription
catalog routes are implemented. The owner-enabled Codex route has completed
live text and generated-PNG requests through the production Broker and
Orchestra. Input vision and client-controlled subscription tools remain
separate rollout gates; see `docs/subscription-tool-loop.md`.

**Served-model evidence (additive).** A run records `model.observed` events
(`model`, `kind`, `source`) for every model the CLI reports, and the job keeps the
deduped list (one entry per kind and model, max 10; a stronger kind replaces the weakest
entry when full, otherwise the new one is dropped, and either way `observations_truncated`
is set, `observations_complete` is `false`, and the top-level `model_served` is `null` when
more than one distinct `response` model is stored) as `model_observations`, kept on failed, cancelled and timed-out runs too.
The poll `evidence` carries the strongest kind seen as `model_served`, `model_served_kind`
and `model_served_source`, while the top-level `model_served` stays strict and is filled
only by `response`-level evidence:
`response` (read off an API response message: Claude `assistant.message.model`) beats
`thread_config` (the model the CLI configured for the thread, Codex App Server
`thread/start`; NOT proof of the backend of each response) beats `session_init`
(Claude `system/init`). When nothing is observed `model_served` is `null`,
`model_served_kind` is `unknown`, and the requested model is never substituted. When
observations of different kinds disagree, all remain in `model_observations`. A
`model.observed` event never releases the Codex auth-refresh gate. Codex `exec`
reports no known model field; the parser only logs event key names to find one, from a fixed
allowlist of structural names (plus candidate model fields); any other key is logged only as the
constant `<key>` with a count, never its own text.
Code map: the `ModelObserved` event (`agent/events.py`), `add_model_observation` and
`served_model_meta` (`agent/runs.py`, accumulate and pick the strongest), and the poll
models `ModelObservation` and `JobEvidence` (`routes/proxy.py`).

`GET /v1/agent/runs/{id}` reports `model` and `requested_model` as the exact
requested CLI pin and `model_served: null` when the actual served model is not
independently attested.
The same distinction applies to subscription results from `GET /v1/jobs/{id}`: its top-level
`model_served` is filled only by `response`-level evidence; `thread_config`, `session_init`
and unknown leave it `null`, with the weaker evidence visible only inside `evidence`.
Codex App Server `thread/start.model` and `turn/start.model` are requested
settings; `turn/completed` does not report a general served-model identity.
`model/rerouted` reports a reroute when one occurs; the App Server tool path
rejects an observed reroute. Its absence cannot prove the original model
served the turn. An observed reroute is recorded as a `model.rerouted` event
with `from_model`, `to_model`, and `source: codex_app_server`, then the tool turn
fails. Neither a successful answer nor an image result changes `model_served`
to the requested ID by inference. Unknown served-model identity does not block
a functional callback or input-vision probe. Report functional success for the
requested route separately from model identity.

The `usage` event and `GET /v1/agent/runs/{id}.usage` carry input, output,
total, cache-read, and reasoning tokens only when the CLI reports them; absent
counts remain `null`. `codex exec --json` uses its final turn usage, while the
App Server callback path uses `thread/tokenUsage/updated.tokenUsage.total` from
the pinned 0.162.1 protocol. A structured worker log line correlates these
non-secret counts with `job_id` and `run_id`, without prompt or credentials.
`cost_usd` remains `null` when no price is confirmed.

Codex App Server stdout is newline-delimited JSON. Its Python subprocess
reader has a **20 MiB per-frame hard limit**. A real `asyncio.StreamReader`
with the former 1 MiB limit reproduces `Separator is not found, and chunk
exceed the limit` on a valid frame. Broker job 593923 reported that error;
its exact frame and method were not retained. This bound allows JSON escaping
of the Broker's
3 MB UTF-8 prompt budget while rejecting oversized, malformed, or unterminated
frames. The error reports the phase and size limit, never frame contents; debug
logs record only an allowlisted method, bounded numeric id, and frame byte count.
Frames rejected before JSON parsing have no trusted method or id to log; their
diagnostic stays at the transport phase and byte limit.
The bound is a transport limit, not a model context or subscription quota.

## Legal / scope note

Owner-only, personal-subscription use. This is **not a product backend**: it must not serve
third parties or be offered as a service. Access needs the `llm:agent` scope
(`AGENT_SCOPE`), granted explicitly by the owner (admin API). It is in `OWNER_ONLY_SCOPES`
(`aibroker.agent.constants`) and is stripped from `SIGNUP_DEFAULT_SCOPES`, so self-signup
can never grant it. Capabilities: `agent:codex`, `agent:claude` (`AGENT_CAPABILITIES`).

**Server-side allowlist (fail-closed).** The scope alone is not enough: the project id must also
be in `AGENT_ALLOWED_PROJECT_IDS` (comma-separated integers; `allowed_project_ids`,
`project_allowed`). Empty or unset means NOBODY: every `/v1/agent/runs` endpoint (create, get,
events, cancel) answers 403 `{"error": "agent_project_not_allowed"}` (`AgentProjectNotAllowed`)
right after the scope check, even with `llm:agent`. The worker re-checks before it starts a
run: a queued run of a project removed from the list fails with `run.failed
project_not_allowed` - no CLI start, no `billing.attested`. Compose passes the variable to both
`api` and `agent-worker`; the operator sets `AGENT_ALLOWED_PROJECT_IDS=11` in `.env` BEFORE
deploying this change, otherwise the owner test project is locked out (by design).

The same checks run on every read of an `agent:*` job, through `GET /v1/jobs/{id}` and
`GET /v1/deep/{id}`. A project that lost `llm:agent`, left the allowlist, or no longer matches
`AGENT_PROJECT_POLICY` gets 403 for previously created results too. `GET /v1/agent/runs/{id}` and `/events` re-check
`AGENT_PROJECT_POLICY` against the run's stored model, account and capability, so a removed policy
closes the run's output (403 `agent_policy_missing`, `model_not_allowed`, `account_not_allowed`).

**Per-project policy (fail-closed, narrowing only).** `AGENT_PROJECT_POLICY` is JSON in the environment,
e.g. `{"11": {"models": ["claude-sonnet-5-5"], "accounts": ["owner"]}}` (`ProjectPolicy`, `policy_for`,
`policy_violation` in `aibroker.agent.policy`). On `POST /v1/agent/runs`, after the scope and project
allowlist checks, a request is refused with 403 `{"error": ...}` (`AgentPolicyViolation`) when:
the project has no entry or an empty `models` / `accounts` list - `agent_policy_missing`; the request's
`model` is absent or not EXACTLY one of the project's models (no prefix match, no implicit default,
`latest` only if listed) - `model_not_allowed`; the `account_id` is not listed - `account_not_allowed`.
For Claude, the `accounts` order is also the preference order of `/v1/jobs` account selection
(see [Multiple Claude subscription accounts](#multiple-claude-subscription-accounts)).
For a project explicitly configured with `"models": ["*"]`, the wildcard admits only
model ids listed as candidates for the requested Claude or Codex subscription
provider. Exact model entries retain their previous policy behavior. The
wildcard does not mark a model verified, grant tools or vision, change the
project's account allowlist, or override the ordinary jobs route's availability
check. Keep this policy entry limited to the project the owner approved.
A malformed setting refuses every project (`agent_policy_missing`) and logs one clear error. The worker
re-checks the same policy before it starts a queued run (`run.failed` with the same code, no CLI start,
no `billing.attested`). The CLI model catalog stays an additional check when it is readable; when it is
not, the already-enforced allowlist decides - the id is never passed through unchecked. The project's
daily cap stays plain metadata (no run quota). Compose passes the variable to `api` and `agent-worker`.

**Internal worker RPC.** The API and each subscription worker share only that
worker's named Unix-socket volume. The socket directory is uid 10001 mode
`0700`; `worker.sock` is mode `0600`. `GET /internal/v1/provider` reports
configured candidate ids and the text-only jobs contract;
`GET /internal/v1/health` reports queue readiness; and
`POST /internal/v1/wake` accepts only a numeric job id and returns `202` even
when the job is unknown or belongs elsewhere. The worker checks the database
and project policy when claiming it. No prompt or credential crosses RPC.
The ordinary `/v1/jobs` and `/v1/agent/runs` clients are unchanged. If RPC is
unavailable, the worker still polls the database. The RPC volumes contain no
CLI auth files, have no host port, and do not change worker networks or the
Codex seccomp/AppArmor profile.
`wake_worker`, `start_worker_rpc`, and `stop_worker_rpc` implement this optional
path in `aibroker.agent.rpc`.

## Readiness and quota rules (hard requirements)

- **Do not declare an individual model or modality ready from fake tests alone.**
  Production has completed real subscription CLI text runs for Sonnet 5.5,
  GPT 6.1 Sol, and GPT 6 Luna, and exported a real PNG from a GPT 6.1 Sol
  Codex run. Each additional model or modality still needs its own verification.
- On **quota exhaustion** the run WAITS (see below), visible as `waiting_quota`.
  **No fallback to API billing/OpenRouter, no "extra usage", no rotation to another account.**
  On `/v1/agent/runs` the caller chooses the account (`account_id`). On `/v1/jobs` (`claude-sub`) the
  broker chooses it once at submit; it is never swapped afterwards (see below).

## Architecture

A run is a `deep_jobs` row (`capability = agent:*`) - so queue, retention (7 days) and the
client-visible request id `job-<id>` are reused - plus:

| Table | Model | Holds |
|---|---|---|
| `agent_runs` | `AgentRunRow` | `task_id`, `account_id`, `session_id` (set from the first `session.assigned`), `resume_session_id`, `idempotency_key`, `workspace`, `cancel_requested`, `last_event_at`, `last_seq`, `outcome` |
| `agent_run_events` | `AgentRunEventRow` | `(run_id, seq)`, `ts`, `type`, `payload` (JSONB); cascade-deleted with the job |
| `deep_job_files` | `DeepJobFileRow` | `(job_id, idx)`, `sha256`, `mime`, `size`, `expires_at`, `data` (raw bytes, STORAGE EXTERNAL); cascade-deleted with the job; written only when `AGENT_FILES_IN_TABLE` is on (migration 025) |
| `agent_session_leases` | `AgentSessionLeaseRow` | PK `(account_id, session_id)`; `run_id` (current holder), `lease_until`, `heartbeat_at`, `fence` (fencing token, +1 per acquisition; expired in place on release, never deleted) |

Expired inline file bytes can be cleared by hand with `python -m aibroker.scripts.clear_expired_files` (dry-run by
default; see `docs/deploy-ops.md`, "Clearing expired agent file bytes"). It sets `content_b64: null, expired: true` on
files the API already reports as expired, so poll responses do not change; it is not scheduled and does not touch
`deep_job_files`.

Migration `infra/sql/migrations/017_agent_runs.sql`, mirrored in `infra/sql/init.sql`.
Migration `infra/sql/migrations/018_agent_session_leases.sql` adds `agent_session_leases`.
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
  `cancel_requested` for the worker (C2); a finished one is returned unchanged. Jobs submitted
  through `POST /v1/jobs` (subscription pins) are cancelled with `POST /v1/jobs/{id}/cancel` /
  `POST /v1/jobs/cancel` (by `trace_id` / `labels`), which call this same `cancel_run` and need
  `llm:agent`; see [api.md](api.md#cancel-migration-020).

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
| `billing.attested` | `BillingAttested` | auth_method, api_provider, paid_continuation_verified_off, fallback (`none`), project_daily_cap_usd |

Exactly one terminal event ends a run (`TERMINAL_TYPES`).

## Adapter contract

`AgentCLI` (`aibroker.agent.adapter`): `supports_resume`, `billing_mode()` (positive
"subscription", else "api"/"unknown" which will be refused in C3), `start(AgentRunSpec)` ->
async iterator of events, `cancel()`. One instance serves one run. `AgentCLI` is a structural
Protocol: nothing inherits it, and its stub bodies raise `NotImplementedError` (never a silent default). `FakeAgentCLI`
(`aibroker.agent.fake`) is a scripted fake: emits events, hangs until cancelled, raises,
writes files into the workspace.

## Worker lane (C2)

`aibroker.agent.worker` is a lane of its own, started from the app lifespan only when
`AGENT_WORKER_ENABLED=true` (off by default - enable it on the one process that has the CLIs).

- `claim_agent_runs(capability, limit, *, interactive_only=False)` claims `agent:*` rows of ONE capability
  (`FOR UPDATE SKIP LOCKED`) in `ORDER BY priority, created_at` order: interactive (`priority` 0) first, then
  batch (1), oldest first within each. `interactive_only=True` skips batch rows; `concurrency_for` gives the slots
  (`AGENT_CONCURRENCY_CODEX` / `AGENT_CONCURRENCY_CLAUDE`, default 1). Compose passes
  `AGENT_CONCURRENCY_CODEX: ${AGENT_CONCURRENCY_CODEX:-1}` to `agent-worker-codex`; the operator
  sets `AGENT_CONCURRENCY_CODEX=7` in `.env` for the production plan. **Auth-refresh guard:**
  concurrent Codex processes share `CODEX_HOME/auth.json`, and ChatGPT refresh tokens rotate, so two
  simultaneous refreshes could invalidate the login. Before starting the CLI the executor reads ONLY
  the non-secret `last_refresh` timestamp (`codex_cli.read_last_refresh`; `refresh_maybe_due` decides;
  assumed ISO-8601, possibly with nanoseconds and `Z`;
  field name and format are UNVERIFIED here - no real file is kept in the repo - so an absent,
  oversized (> 1 MiB), malformed or unparseable value counts as "due" and fails safe; never a
  token). If it is missing, unreadable or older than
  `CODEX_REFRESH_DUE_AFTER_S` (4 days; UNVERIFIED conservative value - Codex's own interval is
  believed to be ~8 days but could not be checked offline), the run takes the start-up lease
  `("__codex_auth__", "refresh")` BEFORE `run.started` / `billing.attested` are written (so a
  requeue repeats no event) and holds it until the first event proving a model call completed
  (`message.delta`, `tool.call` or `usage`; NOT `session.assigned`, which precedes the first model
  request - the ordering is UNVERIFIED), or until the run ends and its CLI is fully stopped. There is
  no time-based early release: a silent start-up is not proof that the refresh finished, so it stays
  under the lease and is bounded only by the run's idle timeout (300 s by default) or, for a
  client-tool run where idle equals wall, the wall timeout (1800 s). A CLI that ends its own
  start-up (an error before any model event) is stopped before the lease is released. While one
  run holds the lease, each waiting refresh-due run occupies one of the worker's
  `AGENT_CONCURRENCY_CODEX` slots, so with 2 slots one silent holder plus one waiter blocks the
  Codex worker until the holder ends.
  Other refresh-due runs retry every `codex_auth_poll_s` for up to `codex_auth_wait_s` (90 s), then
  are requeued; after `session_busy_max` requeues the run fails with `error_class=codex_auth_busy`.
  A run cancelled while it waits ends `cancelled` at once (no requeue, never `codex_auth_busy`).
  A fresh `last_refresh` takes no lease, so runs stay fully parallel. A 401-triggered
  refresh cannot be predicted from the file and is not covered; a two-run live canary remains a
  smoke test, not the safeguard. Slots are never shared
  with the LLM dispatcher, whose claim and stale-requeue SQL exclude `agent:%`.
- **Refresh proof** (`codex_refresh_proof.PROOF`, a `RefreshProof`: `record`, `covers`, `clear`; single Codex worker only - it is process memory). When a gated run releases the lease on its first model event and the login's `last_refresh` (`CodexCLI.last_refresh`, read once per decision; `refresh_due` decides) did not change during the call, the timestamp is recorded. For `AGENT_CODEX_REFRESH_PROOF_S` (default 1800 s, 0 disables) new runs with the same timestamp skip the gate, but never once it is older than `AGENT_CODEX_REFRESH_PROOF_MAX_AGE_S` (6.5 days): near Codex's own ~8-day interval (UNVERIFIED) every start-up is gated again. Reason: for days between 4 and 8 days every start-up was serialised (prod 2026-10-21: last_refresh 142 h, one run in five waited ~96 s, a 120 s client timed out).
  RESIDUAL RISK: runs that skip the gate hold no lease, so a refresh they trigger - in particular a 401-triggered one, which no timestamp predicts - is not serialised with other runs. The time-based refresh stays under the lease while max_age + window is below Codex's interval.
- `fail_stale_agent_runs`: a `running` run past `started_at + wall_timeout + 300 s`
  becomes `run.failed worker_lost`. It is **never re-run** (a CLI run has side effects).
  The scan selects only `id`, `started_at` and `request->wall_timeout_s`, never whole rows.
- Reserved interactive slots: `claim_for_lane(capability, free, concurrency)` first claims up to
  `free - reserve` runs of any priority, then the remaining slots interactive-only, where
  `reserve = effective_interactive_reserve(AGENT_INTERACTIVE_RESERVE, concurrency)` (= `min(reserve,
  concurrency - 1)`, so concurrency 1 reserves 0). Priority is strict with no aging: under a steady interactive
  load batch runs can wait. A batch backlog therefore never takes the last reserved
  slot(s); an interactive run waits only for a slot to free, not behind the whole backlog. A running run
  is never preempted. `priority` comes from the submit (`interactive|batch`, default batch, or
  `labels.workflow == "orchestra:mascot-chat"` via `resolve_priority`; constants `PRIORITY_INTERACTIVE`,
  `PRIORITY_BATCH`, `INTERACTIVE_WORKFLOW_LABEL`; wire name from `priority_name`).
- Loop cadence: the claim runs every tick (1 s, or at once on a wake-up); every finished run
  task sets the wake event, so its slot is refilled immediately. The housekeeping sweeps
  (`fail_stale_agent_runs`, `expire_overdue_waiting`, `fail_unclaimed_runs`) run on startup and
  then at most every `HOUSEKEEPING_EVERY_S` (30 s). `expire_overdue_waiting` also reads only
  `request->deadline_s`.
- `append_event` reads the queue row only for events that touch it: `message.delta`, `tool.*`
  and the like load no job at all; `usage` loads it with `request` deferred; `model.observed`
  and terminal events (which read `client_job` / attachments) load the full row. The fence
  (`check_fence`) and `RunConflict` checks are unchanged.
- `drain_agent_once` is the deterministic one-pass used by tests; `agent_worker_loop` is the
  forever loop.

`aibroker.agent.executor.execute_run` drives a claimed run (`ExecConfig` from
`exec_config_from_settings`) to exactly one terminal event:

1. cancel flag already set -> `run.cancelled`; no adapter registered
   (`aibroker.agent.registry`: `register_adapter`, `make_adapter`, `clear_adapters` for tests) -> `adapter_unavailable`.
2. **Session lease** (resume only): `session_lock(account_id, session_id, run_id)` takes a lease
   ROW in `agent_session_leases` (`AgentSessionLeaseRow`, migration 018, PK
   `(account_id, session_id)`) - no Postgres advisory lock, so the worker needs only the
   pgbouncer connection. `acquire_lease` is one atomic `INSERT ... ON CONFLICT DO UPDATE ... WHERE
   lease_until < now`: it succeeds only for a free or EXPIRED session. On Postgres every
   comparison and stored timestamp uses the DATABASE clock (`now() AT TIME ZONE 'utc'`, TTL via
   `make_interval`), so workers with skewed app clocks agree; the app clock (`_now`) is used only
   by the SQLite branch of the unit tests. While the run is alive a heartbeat task (`renew_lease`,
   same DB clock) extends `lease_until` every `AGENT_SESSION_LEASE_HEARTBEAT_S`
   (30 s); the TTL `AGENT_SESSION_LEASE_TTL_S` (120 s, keep >= 3 heartbeats) lets a crashed
   worker's lease expire by itself. `release_lease` runs when the run ends (any outcome).
   **Lost lease**: the heartbeat sets `Lease.lost` when a renewal is refused (another holder took
   the session) or when no renewal succeeds before a local monotonic deadline (the DB is
   unreachable for longer than the TTL; the deadline counts from the start of the last successful
   renewal request, so it is never later than the DB's `lease_until`; DB errors and hangs are both
   covered). The stream loop then stops the CLI at once (cancel, like a timeout) and the run
   ends with **no terminal write**; the stale-run sweep later closes it as `run.failed
   worker_lost`. It is never re-run. Before ending, `clear_pending_call` withdraws a still-waiting
   client tool call, so poll stops advertising it and later result submits get 409; an already
   accepted (202) result is kept.
   Busy -> the run goes back to `pending` with a delay (`requeue_run`), failing
   `session_busy` after `session_busy_max` tries. Two runs never use one session at once.
   Every acquisition returns a `Lease` with a FENCE token that grows by one on each takeover
   (`release_lease` expires the row in place, so the counter survives). The run's writes -
   events, status/result (`append_event`), usage (`record_usage`) and quota parking
   (`park_for_quota`) and the workspace record (`run.workspace`, written by step 3) - run
   `check_fence` inside their own transaction, which locks the lease
   row and raises `StaleLease` if another run now holds the session. A zombie whose lease was
   taken over therefore cannot write; `_held_drive` stops it (no terminal event) and the adapter
   is cancelled. Nothing relies on session-level database state, so it works with transaction
   pooling.
3. **Workspace**: `create_workspace` makes `AGENT_WORKSPACE_ROOT/run-<id>` with mode 0700;
   it is removed afterwards, also on failure (`cleanup_workspace`).
4. `run.started`, then the adapter stream. **Wall** (`wall_timeout_s`) and **idle** (no event
   for `idle_timeout_s`) timeouts give `run.timed_out`; the cancel flag
   (`cancel_requested`, polled every `AGENT_CANCEL_POLL_S`) gives `run.cancelled`. Both call
   `adapter.cancel()` (the adapter contract owns SIGTERM then SIGKILL), bounded by
   `AGENT_CANCEL_GRACE_S`. A cancel during a client tool wait (`ClientToolCancelled`) ends
   as `run.cancelled`, not `run.failed`.
5. Adapter crash -> `adapter_error`; stream ending without a terminal event ->
   `no_terminal_event`. Failures are never retried or rerouted. The one exception is
   quota exhaustion, which waits (below).
6. **Files**: paths from `file.changed` and `run.completed.files` go through `collect_files` (each path is checked by `workspace.safe_file`, which `images.collect_out_images` reuses):
   regular, non-symlink files inside the workspace only; per-file cap
   (`AGENT_FILE_MAX_BYTES`), total cap, count cap; over-cap/unsafe entries are listed with
   `skipped` and no content. Content is served until `expires_at` (`AGENT_FILES_TTL_S`),
   then the API returns `expired: true` and no content.
   Storage: with `AGENT_FILES_IN_TABLE` (default off) the bytes of png/jpeg/webp/gif files go
   to `deep_job_files` (`job_files.store_files`, same transaction as the result; `result_meta.files[i]` keeps
   the metadata, `content_b64: null` and `content_ref: i`); other files, non-canonical base64 and every
   job written with the flag off keep `content_b64` inline. Readers (`job_files.live_files` for both
   `GET /v1/agent/runs/{id}` and `GET /v1/jobs/{id}`, `live_files_inline` for the synchronous inline-only
   view) return the same JSON in both formats: `content_b64` is encoded on read, `content_ref` is never
   exposed. Rollout/rollback: [deploy-ops.md](deploy-ops.md#agent-file-bytes-in-a-table-migration-025-2026-10-22).
   Absolute paths are normalised first: inside the workspace they become relative, outside it
   they are dropped (debug log, no `skipped` entry); `../` and symlinks stay rejected.
   After the Codex image export, `collect_out_images` (`images.py`, run for every adapter) lists
   the PNG/JPEG/WebP files the run itself wrote under `out/` (a shell or script, not the
   built-in generation): regular non-symlink files, 1 byte to `AGENT_IMAGE_MAX_BYTES`, correct
   magic bytes, a full Pillow decode of the format the extension names, at most 50 Mpx and
   `AGENT_IMAGES_MAX_COUNT` images. A sha256 already exported this run, or a path
   `collect_files` already returned, is not listed again. A rejected file gets `skipped`
   (`unsafe_path`, `empty`, `too_large`, `not_an_image`, `total_budget`). The walk of `out/` is
   bounded (5000 entries scanned, depth 8, 200 image files); each file is opened `O_NOFOLLOW`
   `O_NONBLOCK` and refused unless the descriptor is the same regular, single-link file that
   was checked (a hardlink to an outside file or a swapped-in FIFO is `unsafe_path`). Paths are
   compared normalised (`./out/a.png` == `out/a.png`) and bytes by sha256, also against `files`
   that carry content. `AGENT_IMAGES_TOTAL_BYTES` (24 MiB) is one budget for the generated export
   and this collector together. `no_shell` on the request
   (`client_job.no_shell`, or the `page=compare` label) sets `AgentRunSpec.no_shell`.

## Models, usage, billing separation

- **`model_unavailable` from the CLI.** When a subscription CLI says the requested model is not
  usable on the account, `classify_failure` returns `model_unavailable` (checked after quota, so a
  limit message always wins). Wordings, from the Claude Code errors page
  (https://code.claude.com/docs/en/errors.md, VERIFIED): "There's an issue with the selected
  model", "Model ... not found", "Model ... is not a recognized model id", "Claude Opus is not
  available with the Claude Pro plan". Codex: "The 'X' model is not supported when using Codex with
  a ChatGPT account" (UNVERIFIED, no fixture). The executor rewrites the terminal message so
  `/v1/jobs` shows `model_unavailable: <model> is not available on this subscription account
  (<CLI text>)`. It is terminal: not retried, not parked as a quota wait, and
  `agent/accounts.py` does not pause the account for it (a quota signal or
  `subscription_disabled` does).
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
an exact session id (both as `--opt=value`), the caller's tools as ONE `--allowedTools=A,B`; no
bypass / skip-permission flag, no "continue"/"last"; the prompt goes on stdin. See "Hardening". `billing_mode()` probes
`claude auth status` and `parse_claude_auth_status` accepts only a first-party claude.ai
login. `cancel()` sends SIGTERM, waits `kill_grace_s`, then SIGKILL. The output is parsed by
`ClaudeParser` (system/init, assistant, user tool_result, result -> events; an init
`apiKeySource` other than none is refused at runtime); `classify_failure` maps limit wording
to `quota_exhausted`. **ClaudeCLI and ClaudeParser are UNVERIFIED against the real CLI
(phase 4)**; tests use doc-derived sample lines and a python stand-in process. The CLI uses
the worker's own subscription login; the broker never reads or moves credentials.

## Quota exhaustion = wait

When the adapter reports `run.failed quota_exhausted` (`classify_failure` on the CLI's limit
wording: `hit your (weekly|session|5-hour|daily|opus|fable|sonnet) limit`, `weekly|5-hour limit
reached`, plus the older usage/quota/rate-limit words; `context limit`, "session limit of 10 MB" etc.
stay `cli_error`; only the first 2000 chars are scanned; quota wording is checked first), `_wait_for_quota` does not fail the run: `park_for_quota` stores the non-terminal
`run.waiting_quota` event (`RunWaitingQuota`: `not_before`, `attempt`, `reset_reported`, `reason` =
the CLI's wording, max 300 chars), puts
the job back to `pending` with `run_after = not_before`, and the API shows status
`waiting_quota` with `not_before` and `quota_waits`. `not_before` is the reported reset time
(+5 s) or, without one, exponential backoff 60 s x 2^n capped at 30 min
(`quota_backoff_cap_s`). `parse_reset_at` reads `|<epoch>`, "in N minutes/hours" (wording
UNVERIFIED) and `resets [Mon D,|Mon] H[:MM]am|pm [(TZ)]` (seen in prod 2026-10-12: "resets Oct 15, 8pm
(UTC)"). The zone is UTC/GMT or an IANA name. With no date the reset is today if still ahead, else
tomorrow; `Mon 12:00am` (weekday, no date) is the next such weekday; no zone means UTC; a past date means next year. An unknown zone or invalid date gives no reset time, so the
exponential backoff applies. The retry runs on the SAME account and subscription route (the
billing guard re-checks every attempt) and resumes this run's own session; usage adds up
across attempts. Never an API/OpenRouter fallback, never another account.

The wait is bounded by the request's overall `deadline_s` (default 6 h, counted from creation):
a `not_before` past it, an `execute_run` after it, or `expire_overdue_waiting` (worker tick)
for a run still parked ends `run.failed` with `error_class=quota_exhausted` and a message that keeps the CLI's
wording and the reset time, e.g. `You've hit your weekly limit ... (reset 2026-10-15T20:00:00+00:00 is past
the job deadline)`; `/v1/jobs` shows it as `error_message: quota_exhausted: ...`. Never retried on another
account. Cancel works while waiting. So a reset later than the deadline (e.g. a weekly cap) ends the job
failed with that reason, not after days.

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

## Worker container and owner handoff

The worker runs in its own container, `agent-worker` (`infra/agent-worker/Dockerfile`, compose
profile `agent`, so a normal `docker compose up -d` does NOT start it). It is built on the
broker image plus Node 22 and the official CLI pinned to an exact version
(`@anthropic-ai/claude-code@2.1.296`, must be >= 2.1.277). Entry point:
`python -m aibroker.agent.run_worker` (`run_worker`: only the agent lane, no HTTP, no LLM
dispatcher). No docker.sock, no privileged, no host network, no home mounts, `cap_drop: ALL`,
`no-new-privileges`; CLI login state is the named volume `agent_claude_auth`
(`/home/app/claude-state`). Resource limits (cpus 1, mem 1g, pids 256) are declared in compose
and marked **needs owner OK**. Smoke (no login): `claude --version` -> `2.1.296 (Claude Code)`;
`claude auth status` -> `loggedIn: false`.

### Owner checklist (all on the server, in the compose project directory)

1. Apply migration 018 (adds `agent_session_leases`):
   `psql "$DATABASE_URL" -f infra/sql/migrations/018_agent_session_leases.sql`.
   Build: `docker compose build api && docker compose --profile agent build agent-worker`
2. **Check the rendered compose first:** `docker compose --profile agent config | grep -i anthropic`
   must print NOTHING (no Anthropic variable leaks into the worker service).
   **Billing gate.** In the Claude account settings (claude.ai, Settings > Usage), confirm
   usage credits / **extra usage** are OFF. Absence of API env vars does NOT prove there is no
   paid continuation: with extra usage enabled, spend can continue after the wrap-up
   (needs CLI >= 2.1.277, gradual rollout, not guaranteed -
   https://support.claude.com/en/articles/17040437-claude-code-wrap-up-allowance).
   We never change account settings; the owner confirms.
3. Log in (owner only, interactive, once):
   `docker compose --profile agent run --rm agent-worker claude` then type `/login` and follow
   the official browser/device flow. Check: `docker compose --profile agent run --rm agent-worker
   claude auth status` shows `"loggedIn": true` and `"authMethod": "claude.ai"`.
   The login is written to the named volume `agent_claude_auth`, so it persists across
   `run --rm` containers and restarts; to log out, remove that volume.
4. Only after step 2, set `AGENT_PAID_CONTINUATION_VERIFIED_OFF=true` in the compose `.env`.
   Until then `autonomous` / `exam` runs are refused (`billing_mode_refused`); a single manual
   run needs `run_mode: "manual"` plus `accept_paid_continuation_risk: true` in the request.
5. Set `AGENT_ALLOWED_PROJECT_IDS` (e.g. `11`) in the compose `.env` (empty = nobody may use agent runs).
   Start the lane (after step 6 for the first start): `docker compose --profile agent up -d agent-worker`; grant a project the
   `llm:agent` scope through the admin API.

6. **Enabling the worker only, with the owner's confirmation** - attach pgbouncer (the ONLY
   database endpoint the worker uses) to the worker network without restarting it (compose
   project `aibroker`, so the network is `aibroker_agent_db`; verify with
   `docker network ls | grep agent_db`):
   `docker network connect --alias pgbouncer aibroker_agent_db aibroker-pgbouncer`
   (undo: `docker network disconnect aibroker_agent_db aibroker-pgbouncer`. The attachment does
   not survive a recreate of that container; repeat it then.) Postgres itself is never attached.

### First supervised run (required evidence)

Before any unattended use, the owner watches ONE run in an ISOLATED test setup and the result
must show every probe below refused (the permission denial in the event log), with the allowed
cases still working. **No probe ever touches a real secret, the real auth volume, the broker
environment or another run's data**: every target is a synthetic canary created by the test
harness. The deny syntax is unverified until this is done.

Setup (all synthetic): a throwaway `CLAUDE_CONFIG_DIR` on a SEPARATE test volume (never
`agent_claude_auth`) holding a FAKE `.credentials.json` and `settings.json` canary
(`{"canary": "not-a-credential"}`); a canary file `canary.txt` placed by the harness OUTSIDE
the run workspace; a second, empty workspace `run-canary/` standing in for "another run".
The run's real login is a separate step and is not part of this probe.

- allowed: read and write `inputs/x` (scoped), read an attached image;
- refused: read the outside canary file, the FAKE `.credentials.json` in the throwaway config
  dir (by absolute and by `../` path), `/proc/self/environ` and `/dev/fd/../environ` of the
  probe run (the harness process holds only canary variables), a file in the `run-canary/`
  stand-in via `../`, write to `.claude/settings.json` in the workspace, write outside the
  workspace;
- afterwards: `settings_problem` passes for a clean config dir and refuses the canary
  `settings.json`, and the canary files are unchanged.

### Verified on the real CLI (2026-10-11)

Runs 547957, 547962, 547964 and 547965 ran against the real pinned CLI. Finding that changes how
callers must request tools: **creating a file needs BOTH `Write(scope)` (so the `Write` tool is
present in `--tools`) and `Edit(scope)`** with the same relative scope - the CLI applies its Edit
rules to writes, so `Write(scope)` alone is refused. Request `["Write(out/**)", "Edit(out/**)"]`
for a task that creates files under `out/`.

Canary refusal results (real CLI 2.1.296, subscription login, model `claude-sonnet-5-5`, 2026-10-11).
The canary was a fake file `/tmp/canary-outside.txt` created in the worker container and removed
afterwards; no real secrets were involved.

- **Run 547964** - tools `Edit(out/**)` + `Write(out/**)`; no Read tool present.
  - write `.claude/settings.json`: denied;
  - write `/tmp/escape.txt`: denied (file absent, checked in the container);
  - write `out/ok.md`: succeeded (2 bytes).
- **Run 547965** - tools `Read(inputs/**)`, `Glob(inputs/**)`, `Grep(inputs/**)`, `Edit(out/**)`.
  - read `/tmp/canary-outside.txt`: denied;
  - list the workspace parent: denied;
  - writes: impossible (`Write` not in `--tools`).
- The canary text appeared 0 times in the events and the result of both runs.
- Both runs completed with `billing_mode=subscription`, `cost_usd=null`.

The per-probe outcomes of the other runs in this round are in their event logs
(`GET /v1/agent/runs/{id}/events`).

### Billing attestation (per run)

After the billing guard passes - and before the CLI starts - the executor stores a typed
`billing.attested` event (`BillingAttested`, schema_version 1) and `GET /v1/agent/runs/{id}`
(also the cancel response) derives these exact fields from the run's latest such event:

| field | source |
|---|---|
| `auth_method` | the `claude auth status` probe of THIS run (e.g. `claude.ai`) |
| `api_provider` | the same probe (e.g. `firstParty`) |
| `paid_continuation_verified_off` | `AGENT_PAID_CONTINUATION_VERIFIED_OFF` at run start |
| `fallback` | `none`, written by the code path that refuses API fallback (the only value the event accepts) |
| `project_daily_cap_usd` | the project's `daily_cost_cap_usd` at run start (null = no cap) |

All five are null for a run that never got that far. A refused run (probe says anything but a
subscription login, no probe result - `AgentCLI.attestation()` / `AuthAttestation` -, API
environment variables, unverified paid continuation) gets `run.failed billing_mode_refused` and
NO attestation event: never a success with "unknown". The event carries only these fields (no
email, no token): `ClaudeCLI` reads only `authMethod` / `apiProvider` from the probe JSON
(`parse_claude_auth_details`), and the route reads the stored event with `get_attestation`.
`billing_mode`, `usage_source` and `cost_usd` are unchanged. No migration. A quota-wait retry runs the probe and writes a fresh `billing.attested` event on every attempt
(tested in `test_a_quota_retry_attests_again_and_resumes_the_session`); the GET shows the latest.

### Multiple Claude subscription accounts

- **Setting `AGENT_ACCOUNT_ID`** (`config.py`): a worker claims only the runs whose `agent_runs.account_id`
  equals it (`claim_agent_runs`, an IN-subquery; `SKIP LOCKED` unchanged). Empty = claims the runs of
  every account (dev / tests only): a worker that claims `agent:claude` **refuses to start** with it
  empty (`worker_image_problem`: "AGENT_ACCOUNT_ID is not set"), so no Claude login serves another
  account's runs. Both compose workers set it.
- **Unclaimed runs:** a never-started run (no events, no quota wait) still `pending` after
  `AGENT_PENDING_MAX_S` (default 1800 s, 0 = off) is failed by the worker loop (`fail_unclaimed_runs`) with
  `run.failed` `error_class: account_unavailable`, "no worker claimed this run for account X", but only
  when no worker is alive for that account and capability: it has no `running` run and no run that started, finished or logged an event
  within the window, so a run queued behind a long run is never reaped. The failure is atomic with a
  still-`pending` check (the queue row is locked `FOR UPDATE SKIP LOCKED`, as in the claim). Quota-parked
  runs and runs waiting for a client tool are never touched.
- **Separate billing confirmation:** `agent-worker-claude2` reads `AGENT_CLAUDE2_PAID_CONTINUATION_VERIFIED_OFF`
  from the compose `.env` (default false); the container still sees the standard name
  `AGENT_PAID_CONTINUATION_VERIFIED_OFF`. The owner's confirmation for the first account never covers
  the second one.
- **Containers:** `agent-worker` = `owner`, `agent-worker-claude2` = `claude-2` (a neutral technical id).
  Each has its own named auth volume: `agent_claude_auth` and `agent_claude2_auth`. **One login per
  worker.** Two containers on one login would refresh the OAuth token concurrently, which is a real
  failure (prod job 547951).
- `agent-worker-claude2` has no RPC socket volume and no RPC wake: it finds new runs by its 1 s database poll.
  The image still creates the empty RPC directory, but the container root is read-only, so the socket cannot
  be bound. `start_worker_rpc` recognises this expected case (only when the socket unlink/`bind` fails with
  errno `EROFS`), returns `None` and logs one WARNING line `worker RPC not mounted: capability=... socket=...
  mode=polling only (RPC dir read-only / not mounted)` instead of an ERROR with a traceback. Instant wake
  stays off; other errors (any other errno, wrong owner/mode) still propagate to the ERROR path in
  `run_worker`.
- **Account selection** (`aibroker.agent.accounts`: `pick_account`, `quota_resets`; `ProjectPolicy.ordered_accounts()`):
  `/v1/jobs` `claude-sub` takes the candidates = the accounts in `AGENT_PROJECT_POLICY` for the project, in
  listed order, and among those without an active quota pause picks the one with the fewest
  unfinished runs (`unfinished_runs`: pending, queued or running; ties keep the listed order, 2026-10-17).
  A pause is the latest
  `run.waiting_quota` / `run.failed quota_exhausted` signal whose stored reset time is still in the future
  (every such signal counts; lookback 8 days, so a weekly limit that resets days ahead still pauses the
  account; a deadline-expired wait keeps its reset in `reset_at`). If every
  candidate is paused, the one whose reset comes first is picked, and the job waits honestly there.
- **Account-down pause** (`ACCOUNT_DOWN_CODES`, `_reset_of`; 2026-10-21, `auth_refresh_failed` added 2026-10-28): a
  `run.failed` whose `error_code` is `subscription_disabled` ("organization has disabled Claude subscription access") or
  `auth_refresh_failed` ("Failed to refresh OAuth token") means the login cannot serve at all, which is not a usage
  limit. It pauses that account for `AGENT_ACCOUNT_DISABLED_PAUSE_S` (default 3600 s, `0` = off) from the failure time,
  through the same `quota_resets` scan. Selection then sends new runs to the other login even when that one is busier;
  the first run after the pause probes the account again. A re-login does not lift the pause early: the account stays
  skipped until the hour runs out (lower the setting, or wait). A one-off refresh race on a healthy login also pauses
  it for the hour; if every candidate is paused, the one whose pause ends first is still picked. Reason: prod
  2026-10-19..20, `owner` answered every run with that error (198 runs) while `claude-2` sat idle, because the
  tie-break kept picking `owner`; 2026-10-27..28 the same login failed with the refresh error (job 588602).
- **Excluded accounts** (`excluded_accounts`, `AGENT_CLAUDE_EXCLUDED_ACCOUNTS`, api, comma-separated, default empty;
  2026-10-28): never candidates for a NEW `/v1/jobs` Claude run, whatever their load or pause. If the exclusion leaves a
  project no candidate, the submit is refused with 503 (it never falls back to `owner`). Runs already created keep
  their account; `/v1/agent/runs`, where the caller names the account, is not affected. Filling it is an owner decision.
- A started run never changes account; a quota wait stays on the same account; there is no fallback to
  paid API. `codex-sub` stays on `owner`.
- **Catalog:** `claude-sub/claude-opus-5-5` and `claude-sub/claude-haiku-5-5` are listed but
  `available: false` until a live check. Fable is not listed.

**Operator login for the second account** (the owner does it himself; agents never log in):

1. `ssh hetzner-root`, then `cd /var/www/aibroker`.
2. `docker compose --profile agent run --rm agent-worker-claude2 claude`, then `/login`. Choose the
   claude.ai subscription login, open the shown link in the browser while signed in to your second
   ITSTEP Claude account (Team seat), paste the code back, then `/exit`.
3. Verify: `docker compose --profile agent run --rm agent-worker-claude2 claude auth status` must show
   `loggedIn: true` and `authMethod: claude.ai`, and the organization shown must be the ITSTEP Team
   organization (check the organization shown; the docs do not name the field).
4. Extra usage on a Team seat is controlled by the org admin: ask the Team admin to confirm extra usage is
   OFF (or capped at 0) for this seat. Only then set `AGENT_CLAUDE2_PAID_CONTINUATION_VERIFIED_OFF=true`
   in the compose `.env`. Until then `autonomous` / `exam` runs on `claude-2` are refused.
5. `docker compose --profile agent up -d agent-worker-claude2` (first start is manual; deploy does not start it).
6. Add `"claude-2"` to the project's `accounts` in `AGENT_PROJECT_POLICY`, for example
   `"accounts": ["owner", "claude-2"]`.

### Rollback

Routine rollback is: stop the worker (`docker compose --profile agent stop agent-worker`)
and/or revoke the exact test grant (remove `llm:agent` from the test project). Deleting the auth
volume or logging out is NOT a routine rollback: the account's authentication is handled
separately by the owner. Stopping the worker leaves queued runs `pending` and in-flight runs
`running`; `fail_stale_agent_runs` closes the latter as `worker_lost` once the worker is back.
A stopped worker stays stopped across deploys: `infra/aibroker-deploy.sh` builds it but
recreates only the workers that were running (see
[deploy-ops.md](deploy-ops.md#agent-workers-in-every-deploy-since-2026-10-13)).
The workers use `restart: unless-stopped`: a worker that was running comes back by itself after a
host reboot or a Docker daemon restart, while a worker stopped with `docker compose stop` (the rollback
above) stays stopped. Before 2026-10-22 they had `restart: "no"` and had to be started by hand after
every reboot.

### Billing gate: paid continuation

`paid_continuation_refusal` (in `aibroker.agent.billing`) runs inside `check_billing_mode`
after the env and subscription-mode checks. `AGENT_PAID_CONTINUATION_VERIFIED_OFF` defaults to
false (`ExecConfig.paid_continuation_verified_off`). While false: `run_mode` `autonomous` (the
default) and `exam` are refused; `manual` is allowed only with
`accept_paid_continuation_risk`. Request fields: `run_mode`, `accept_paid_continuation_risk`.

## Hardening (independent review, B1-B4 / S1-S7)

What is guaranteed, and what is not.

- **Tools: allow-list only.** `aibroker.agent.tools.tool_problem` accepts exactly
  `Read|Glob|Grep|Edit|Write`, always with one MANDATORY relative scope `Tool(<scope>)` (a bare
  `Read` is refused; no spaces,
  commas or parentheses; no absolute path, `~`, drive letter or `..`). Anything else is
  refused at submit (422) and again in `build_claude_argv` (`validate_tools`), so a value like
  `--dangerously-skip-permissions`, `--permission-mode=...`, `--settings=...` can never reach the
  argv. **Bash (and every other tool) is not on the list in phase 1**; `llm:agent:bash` does not
  exist. The argv has no positional argument (nothing follows `--`; the prompt is on stdin) and
  every value is joined (`--allowedTools=`, `--model=`, `--resume=`). Besides the scoped
  permissions the argv passes `--tools=<names>` (`tool_names`: the built-in tool set is cut to the requested
  names; none requested = `--tools ""`) and `--strict-mcp-config` without any `--mcp-config`
  (no MCP servers); both flags exist in the pinned 2.1.296 (`claude --help`). Runs with
  attachments get exactly one extra permission, the scoped `Read(inputs/**)`.
- **Environment: allow-list built from scratch.** `cli_env` creates the CLI's environment from
  `PATH, HOME, CLAUDE_CONFIG_DIR, DISABLE_AUTOUPDATER, LANG, TMPDIR` only (never a copy of the
  worker's). The `claude auth status` probe gets the same. The compose service has no real
  `ADMIN_KEY` / `INTERNAL_SECRET` / `TOKEN_SECRET` (inert placeholders). The billing guard still
  refuses a run if `ANTHROPIC_*`, `CLAUDE_CODE_USE_*` or the listed Bedrock / Vertex / Foundry /
  OAuth-token variables exist in the worker environment (`FORBIDDEN_ENV`, `FORBIDDEN_PREFIXES`).
- **Auth/config unreachable by task tools.** File tools are scoped to relative workspace paths; in
  addition `--disallowedTools=` denies Read/Glob/Grep/Edit/Write on `//proc/**`, `//dev/**`, `//sys/**`,
  `**/.credentials.json`, `**/.claude.json` and `CLAUDE_CONFIG_DIR/**`, and
  `--setting-sources=user` loads no project/local settings. `settings_problem` checks
  `settings.json` / `settings.local.json` in the config dir and the workspace before every run
  against an ALLOW-list (`ALLOWED_SETTINGS_KEYS`: `$schema`, `theme`, `verbose`, scalar values
  only); hooks, permissions, statusLine, mcpServers, enableAllProjectMcpServers,
  otelHeadersHelper, awsAuthRefresh, awsCredentialExport, forceLoginMethod, apiKeyHelper, env,
  defaultMode and every unknown key refuse the run, as does invalid JSON (`settings_refused`).
  `<config_dir>/.claude.json` must not define `mcpServers`. The CLI keeps its login in
  `.credentials.json` / `.claude.json`, not settings.json; the first supervised run confirms a
  logged-in config dir still passes. The official CLI itself reads and refreshes its credential in the config
  dir (writable for it, on the named volume); the tests prove no tool string a caller can supply
  names that dir, a credential file, `/proc` or any mount (deny list: `disallowed_tools`). NOT guaranteed: that the CLI's
  `//path` deny syntax behaves as documented (unverified until phase 4), nor protection against
  a bug inside the CLI itself.
- **uid separation: not feasible here.** The CLI runs as the same uid (`app`) as the Python
  worker: switching uid needs CAP_SETUID, which the container drops (`cap_drop: ALL`,
  `no-new-privileges`), and there is no sudo. So `/proc/<worker>/environ` (DB URL with password,
  placeholders) is protected by `run_worker` calling `prctl(PR_SET_DUMPABLE, 0)` at startup
  (`set_non_dumpable`: a same-uid child cannot read the worker's `/proc/<pid>/environ`), by the
  tool allow-list and by the `//proc/**` deny above.
  **Accepted risk**; revisit with a second container for the CLI if the threat model needs it.
- **Process group.** The CLI is started with `start_new_session=True`; cancel and the end of
  every run signal the whole group (SIGTERM, grace, SIGKILL), and compose has `init: true`.
- **Network.** `agent-worker` is alone on the `agent_db` network; pgbouncer is
  attached to it live by the owner (checklist step 6: `docker network connect`, no restart), so
  it is NOT recreated by a deploy, and postgres, api, redis, vision and monitor are never on it.
  The worker uses only the pgbouncer `DATABASE_URL` (transaction pooling): polling claims, no
  `LISTEN`, no direct Postgres connection (`DIRECT_DATABASE_URL` is not set for it).
  Internet egress stays open because the CLI must reach Anthropic (**accepted risk**).
- **Filesystem.** `read_only: true` with tmpfs `/tmp` and the workspace tmpfs; `HOME` is the auth
  volume.
- **Image check.** `worker_image_problem` makes `agent_worker_loop` refuse to start unless
  `CLAUDE_CONFIG_DIR` is set and the marker `/etc/aibroker-agent-worker` (written by the
  Dockerfile, `WORKER_MARKER`) exists.

## Codex adapter (`agent:codex`)

`aibroker.agent.codex_cli.CodexCLI` runs the official `codex exec --json` (pinned `@openai/codex@0.162.1`,
installed with its optional native-binary dependency) next to `ClaudeCLI`; both build on
`SubprocessCLI` in `real_cli.py` (own process group, group kill on cancel, minimal environment).
Argv (`build_codex_argv`, prompt on stdin via `-`, cwd = the run workspace):
`codex exec [resume <thread id>] [--image=<file>...] --json [--model=<id>] -c sandbox_mode="workspace-write"
-c approval_policy="never" -c forced_login_method="chatgpt" -c cli_auth_credentials_store="file"
--skip-git-repo-check --ignore-user-config --ignore-rules -`. Verified against the 0.162.1 help text:
there is **no `-a` flag on `codex exec`** (so the approval policy is a constant `-c` override) and
`resume` has no `--sandbox`/`-C` (so the sandbox is a `-c` override for fresh and resumed runs alike).
Never `--dangerously-bypass-approvals-and-sandbox`, `--yolo`, `--full-auto`, a wider sandbox or a
`network_access` override (network stays off); `--ignore-user-config` means `CODEX_HOME/config.toml` is
not read at all (auth still uses `CODEX_HOME`), so the forced login method travels as `-c`.
Resume uses ONLY the exact thread id from `thread.started` and it must be a UUID (the CLI would also
resolve a thread *name*); never `--last`. The sandbox, resume-with-overrides and sandbox-inside-a-
locked-down-container behaviour are unverified until the first supervised run (if the sandbox cannot
start, the run fails; nothing bypasses it).

- **No per-tool allow-list.** `allowed_tools` must be empty for `agent:codex` (422 at submit, `ValueError`
  in the argv builder): Codex is confined by the workspace-write sandbox and the workspace as cwd.
  Image attachments become `--image=<file>` flags plus a path listing in the prompt (`build_codex_prompt`; headless `$imagegen`
  is unverified).
- **Environment** (`codex_env`): `PATH, HOME, CODEX_HOME, LANG, TMPDIR` only. The billing guard refuses a
  run if `OPENAI_*`, `CODEX_API_KEY` or `CODEX_ACCESS_TOKEN` exist in the worker environment
  (`CODEX_HOME` is legitimate).
- **Billing probe.** `CodexCLI.billing_mode` reports a subscription only if BOTH `codex login -c ... status`
  states a ChatGPT login (`parse_codex_login_status`; wording unverified, anything else is "unknown")
  AND `$CODEX_HOME/auth.json` says `auth_mode == "chatgpt"`. The auth file is read for that ONE key
  (`read_auth_mode`); tokens are never read into a variable, returned or logged. Otherwise the run is
  refused (`billing_mode_refused`). Attestation: `auth_method="chatgpt"`,
  `api_provider="openai-chatgpt-subscription"`, plus the same five `billing.attested` fields.
- **Events** (`CodexParser`, doc-derived, UNVERIFIED against a real run): `thread.started` ->
  `session.assigned`; `agent_message` -> `message.delta`/final text; `command_execution`,
  `mcp_tool_call` -> `tool.call`/`tool.result`; `file_change` -> `file.changed`; `turn.completed.usage`
  -> `usage` (incl. `reasoning_tokens`); `turn.failed`/`error` -> `run.failed`, with usage-limit wording
  mapped to `quota_exhausted` (format undocumented), anything else `cli_error`. A quota failure waits
  and retries on the same thread, exactly like Claude.
- **Auth state** lives in the named volume `agent_codex_auth` (`CODEX_HOME=/home/app/codex-state`; Codex
  refreshes `auth.json`, so it stays writable). Rollback is stopping the worker, not deleting the volume.

Owner login handoff for Codex (once, interactive, after the Claude steps): enable **device code
authorization** in the ChatGPT security settings first, then
`docker compose --profile agent run --rm agent-worker-codex codex login --device-auth -c cli_auth_credentials_store="file" -c forced_login_method="chatgpt"`
(the `-c` overrides force the file store; plain `codex login --device-auth` is the same command), follow
the device-code flow, then check with
`docker compose --profile agent run --rm agent-worker-codex codex login -c cli_auth_credentials_store="file" status`
(expect a ChatGPT login; `Not logged in` before the login). Whether paid "extra usage" can continue a
ChatGPT-plan run requires a separate owner check. Codex uses
`AGENT_CODEX_PAID_CONTINUATION_VERIFIED_OFF` (default false), never Claude's
`AGENT_PAID_CONTINUATION_VERIFIED_OFF`.

**Codex catalog health (`agent/auth_health.py`).** `GET /v1/models` marks every `codex-sub/*` row
`available: false` while the owner's Codex login is known to be broken. `codex_auth_available()`
(async, returns `bool`) decides this from terminal run metadata only:

- it selects owner runs (`account_id` `owner`, capability `agent:codex`, `outcome` `completed` or
  `failed`) and joins exactly the terminal event of each run: the event whose `seq` equals the run's
  `last_seq`, with type `run.completed` or `run.failed`;
- it orders by `last_event_at` desc, then `run_id` desc, and reads at most 128 rows, newest first;
- the first `run.completed` makes it available (`true`);
- the first `run.failed` with `error_class` `cli_error` and a message containing
  `workspace routing discovery unauthorized (401)` (case-insensitive) makes it unavailable (`false`);
- any other failure is skipped; with no match in the window it returns `true`.

The answer is cached per process for 30 seconds, so a new run can take up to 30 s to show in
`GET /v1/models`.

**Fail-closed on DB errors.** If the lookup raises `SQLAlchemyError` or `OSError`, it logs a
warning and returns `false` (cached for the same 30 s). Only the `codex-sub` rows are affected;
the rest of `GET /v1/models` is unchanged. Other exceptions are not caught and propagate.

Only `GET /v1/models` uses the result (`list_models` in `routes/proxy.py`). The check runs only
when `AGENT_SUBSCRIPTION_CODEX_ENABLED` is on; otherwise it is not called and `false` is passed.
The result goes to `catalog.listing(project_id, codex_auth_available=...)` (`providers/catalog.py`),
where a `codex-sub` row is `available` only when the model is verified and both the owner flag and
this check allow it.

**`POST /v1/jobs` is not blocked** by this check, so the owner can verify recovery after re-login
with a direct Broker job. Its `run.completed` flips the listing back to `available: true` (after
the cache TTL). Only a `cli_error` 401 failure flips it to `false`; other failures do not.

**Recovery:** the owner re-logs in with the Codex login handoff above (device-code `codex login`,
then `codex login ... status` must show a ChatGPT login), then sends one direct job. The query has
no project filter, so a recovery run from any project counts. Only a subsequent successful direct
Broker run re-enables the catalog; if it fails, investigate the new error.

Public names: `codex_auth_available` (function, `agent/auth_health.py`) and the keyword parameter
`codex_auth_available` of `catalog.listing`. Module constants `_AUTH_401` and `_CACHE_SECONDS` are
private.

## Codex: container split, read isolation, redaction (independent review fixes)

**One container per CLI.** Per-run containers would need `docker.sock`, which is forbidden. Instead
compose has two identically hardened services (profile `agent`; non-root, `read_only`, `cap_drop: ALL`,
`no-new-privileges`, tmpfs workspaces, network `agent_db` only, 1 CPU / 1 GB / 256 pids, `init`):
`agent-worker` (`AGENT_WORKER_CAPABILITIES=agent:claude`, mounts ONLY `agent_claude_auth`) and
`agent-worker-codex` (`AGENT_WORKER_CAPABILITIES=agent:codex`, mounts ONLY `agent_codex_auth`, `HOME`
is a tmpfs path, `CLAUDE_CONFIG_DIR` blanked). A worker claims only the capabilities it is configured
for (`worker_capabilities`), registers only those adapters (`register_real_adapters`), checks only its
own home variable (`worker_image_problem`) and refuses to start with an empty list. The Codex CLI also
gets a per-run EMPTY `HOME` inside the run workspace (`.agent-home`), never the Claude state dir and
never `CODEX_HOME`; only `CODEX_HOME` leads to the login. Static tests pin the volumes and `HOME`.

**Codex 0.162.1 uses permission profiles for read isolation.** A supervised
fake-data canary confirmed the denials below with the seven-rule AppArmor
profile; real authenticated runs have not been tested. Sources: the official page `learn.chatgpt.com/codex/permissions` (the
`agent-approvals-security` page documents bwrap + seccomp and profiles but no read rules; `/docs/config`
returned 404) and the pinned binary (`codex sandbox --help`, config strings, `-c` parse probes, all
offline in a throwaway container):
- Profiles: `default_permissions = "<name>"` and `[permissions.<name>]` (`extends = ":workspace"` ...),
  filesystem rules `[permissions.<name>.filesystem]` mapping a path to `read`, `write` or `deny`
  (`deny` blocks reads AND writes; the most specific path wins; globs only for `deny`; path forms
  `:root`, `:minimal`, `:workspace_roots`, absolute, `~/`). `codex sandbox -P <profile> <cmd>` exists.
  A separate `deny_read` exists only at the managed `requirements.toml` level.
- What we apply (`permission_overrides`, passed as `-c` inline tables because `-c` does not unquote
  dotted keys, and a `/proc/*/environ` glob makes Codex scan `/proc` and fail): profile `broker`
  extending `:workspace` that DENIES `CODEX_HOME` (the ChatGPT `auth.json`) and `/proc` (other
  processes' environ) to every command the model runs, next to `sandbox_mode="workspace-write"`. The
  CLI itself still reads its own auth: the sandbox wraps only the model's commands.
- NOT available: a separate uid for the auth files (same uid as the worker; the CLI reads auth
  outside the sandbox). Redaction is only a second net.
- Verified on the image: the profile syntax is accepted (an unknown access value is rejected, an
  undefined profile is rejected). On 2026-10-12 a network-disabled canary using
  fake auth reported `auth:DENIED`, `parent:DENIED`, and `claude:ABSENT`. This
  verifies those isolation checks in that container; it does not prove a real
  model run or prevent all possible reads.

**Sandbox start check (fail closed).** Before EVERY Codex run `CodexCLI.preflight_async` runs
`codex sandbox -P broker -c ... true` (a documented command in 0.162.1: it executes `true` under the
Linux sandbox WITH the read-deny profile). A non-zero exit, a bwrap/landlock/seccomp/namespace message,
a timeout or a missing binary ends the run with `run.failed error_class=sandbox_unavailable` before any
process starts; the same classification applies to such wording in events (`sandbox_failure`,
`CodexParser`) and stderr. It is final: no retry, no requeue, never without the sandbox. The
fake-data canary started with unprivileged user namespaces under the dedicated
seccomp and AppArmor profiles, but
the worker remains stopped and the real preflight remains unverified.

**Redaction.** Everything an agent run persists passes `redact` / `redact_obj` / `redact_bytes`
(`aibroker.agent.redact`) first: event payloads, `final_text`, the job error text and returned file
content (UTF-8 text files; binary untouched). Masked with `[REDACTED]`: JWT-like strings
(`eyJ..<dot>..<dot>..`), `refresh_token` / `access_token` / `id_token` values (JSON or `key=value`),
`sk-...` keys and `sess-...` tokens. Pattern based and best effort: defence in depth, not the control.

A structured-output job (`response_format` json_object / json_schema) is redacted structurally by
`redact_final_text`: when the final text parses as JSON only the string values (and the values of
token-named keys) are masked and the document is re-serialized, so a masked span can never eat a quote
or brace. The `key=value` token mask only consumes a quote it opened itself. The run is then validated
(`output_problem`) twice: on the model's answer and on the redacted body that will actually be returned;
if redaction made a valid answer invalid (a `const` / `enum` value that held a secret) the run ends
`run.failed` `invalid_json`, never `done`. A re-serialized answer keeps key order and unicode but
not the model's whitespace or number spelling (`1.50` -> `1.5`); an answer with nothing to mask is
returned byte for byte.

**Codex acceptance run (canaries only).** `scripts/agent_codex_acceptance.sh` is for a SEPARATE test
stack with no real login. The harness places a throwaway `CODEX_HOME` with a FAKE `auth.json`
(`auth_mode: "chatgpt"`, `CANARY-...` tokens), a FAKE claude-state canary file and a fake parent process
holding a canary environment variable. The script only builds the request JSON and checks the
response: the task runs `cat <canary> >/dev/null` for each item and prints `<label>:READABLE` or
`<label>:DENIED` - no content is requested, stored or printed, the script prints only per-item verdicts
(never a body, header or the project key) and refuses any path that does not contain `canary` or points
at a real state directory or pid 1. Pass = every item DENIED and no `CANARY-` value anywhere in the
stored data; exit 2 means the run did not complete (e.g. `sandbox_unavailable`: fail closed, nothing was
read). It never touches the real `auth.json`, the real claude-state or any real `/proc/*/environ`, not
even expecting a refusal.

## Codex sandbox profiles (seccomp + AppArmor)

Under Docker's default seccomp profile the Codex sandbox (bubblewrap, which
needs user and mount namespaces) failed with "Operation not permitted". With
the dedicated seccomp profile it reached Docker's AppArmor `deny mount,` and
reported `Failed to make / slave`. The seven-rule `aibroker-codex` addition below
passed one isolated fake-data canary on 2026-10-12. The tested profile was
loaded in enforce mode on the host; this change brings the deployed file into
line with it. The Codex worker and subscription route remain off.

**Seccomp: `infra/agent-worker/codex-seccomp.json`** = Docker's default profile
(`moby-seccomp-default.json`, verbatim copy of moby/profiles main `seccomp/default.json`, last commit
`6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef`, 2026-09-17, "seccomp: Disable AF_AX25 socket domain") PLUS two
appended `SCMP_ACT_ALLOW` entries, and nothing else changed (a test pins this):
1. `unshare` - only when `(flags & 0x81FDFFFF) == 0`, i.e. the flags are a subset of the namespace
   flags (NEWNS, NEWCGROUP, NEWUTS, NEWIPC, NEWUSER, NEWPID, NEWNET, moby's own mask 0x7E020000).
   `CLONE_NEWTIME` and every non-namespace flag stay refused (32-bit mask, so register garbage in the
   upper half is ignored).
2. `clone`, `setns`, `mount`, `umount2`, `pivot_root` - unconditional. `clone` cannot be narrowed: the
   default rule already allows every non-namespace `clone`, so adding the namespace flags makes it
   unconditional. Without CAP_SYS_ADMIN (`cap_drop: ALL`) these act only inside a user namespace the
   process created itself.
**Widened:** user/mount/pid/net namespace creation and in-namespace mount/pivot_root for the Codex
container only. **Not widened:** `clone3` (kept ENOSYS so libc falls back to `clone`; its flags live in a
struct seccomp cannot inspect - a deliberate deviation from the first request), `bpf`, `ptrace`, `keyctl`,
module loading, `perf_event_open`, ...

**AppArmor: `infra/agent-worker/apparmor-codex`** (profile `aibroker-codex`) = docker-default rendered from
moby/profiles main `apparmor/template.go` @ `a21872828a8e5745d79e2fc1a07ee6dad8aedffa` (2026-09-29;
rendered baseline `apparmor-docker-default.rendered`) with exactly ONE block replacing `deny mount,`:
`mount` rules making `/` a slave/private mount and building the new root (tmpfs, proc, bind/rbind and
read-only remounts) and `pivot_root`. Everything else - the `/proc` write denies, `/sys` denies, sysrq,
kcore, network family denies, signals, ptrace - is unchanged (a test compares the two files). The rule
set includes seven additions relative to the original baseline: silent
`rslave` on `/`, a `devpts` mount on `/newroot/dev/pts/`, one exact read-only
remount on `/newroot/`, three exact-flag read-only remount rules on
`/newroot/**`, and silent `rprivate` on `/oldroot/`. The three subtree rules
also match other descendants with the same flags; they are wider in path than
the ten child paths in the earlier audit, so do not describe them as the
strict minimum path scope. The reviewed file's SHA-256 is
`63389ed14e3e9cb0e7c3a610f7625474dc34ee836537099b8c6f98fe94931052`.
The fake-data canary exited 0 with `claude:ABSENT`, `auth:DENIED`, and
`parent:DENIED`; no new `aibroker-codex` AppArmor denial appeared in the
following audit window. This does not verify real login, model billing, text
completion, or decodable PNG output. The profile file retains historical
pre-canary comments so its tested bytes remain unchanged.

The standard application deploy updates the repository file but does not
install AppArmor profiles. On this host `apparmor.service` is enabled, but the
named file was absent from `/etc/apparmor.d` before this delivery. After CI
and deploy succeed, keep the Codex worker stopped and install only the
reviewed file at `/etc/apparmor.d/aibroker-codex` (mode 0644). Check that its
SHA-256 equals the value above, syntax-check it with
`apparmor_parser -Q /etc/apparmor.d/aibroker-codex`, then load it with
`apparmor_parser -r -W /etc/apparmor.d/aibroker-codex` and verify
`aibroker-codex (enforce)`. The enabled AppArmor service will load that file
again after reboot; merely leaving it in the application checkout will not.
If a later run fails, stop the Codex worker, restore
`/var/backups/aibroker-codex-apparmor-530ff54.profile` (SHA-256
`a6c4303751234861502f9f85f6517ede09576afbfc296046ef53efc23ef9acf4`)
to `/etc/apparmor.d/aibroker-codex`, reload it with
`apparmor_parser -r -W /etc/apparmor.d/aibroker-codex`, and verify enforce
before further testing. Keep seccomp, dropped capabilities,
and the AppArmor selector in place; do not use `unconfined` or `privileged`.

**Compose.** Only `agent-worker-codex` has
`security_opt: [no-new-privileges:true, seccomp=./infra/agent-worker/codex-seccomp.json, apparmor=aibroker-codex]`
(whether compose resolves the `seccomp=` path relative to the project directory is unverified on the
installed compose version); `cap_drop: ALL`, `read_only`, no privileged, no docker.sock, network
`agent_db` only are kept, and `agent-worker` (Claude) is unchanged. ROLLBACK:
disable the Codex route, stop its worker, and reload the known prior AppArmor
profile in enforce mode. Leave the worker stopped while diagnosing the issue.
Removing `seccomp=` and `apparmor=` later would make Codex's sandbox preflight
fail closed as `sandbox_unavailable`; it is not a way to run the worker.

## Codex generated images (worker-side export)

After a fresh Codex thread, the WORKER process copies PNGs only from
`$CODEX_HOME/generated_images/<thread UUID>` into the run's `out/` (`export_generated_pngs`, called through
`export_artifacts`). `CODEX_HOME` is never given to the model (the sandbox profile denies it and the
prompt never names it). The thread UUID must come from this CLI process's single `thread.started` event;
duplicate or conflicting thread identities disable PNG export.
`snapshot_generated_paths()` records pre-existing names before the run, so a
previous image cannot be claimed as this run's output.
The pinned Codex 0.162.1 image extension writes `generated_images/<thread UUID>/<call ID>.png`.
Resumed threads export no PNG because a stale CLI can still write into their shared thread directory
after a lease takeover. An already populated directory for a claimed new thread also exports nothing.
Accepted files: regular files only (no symlinked file, directory or source directory; depth <= 3;
no traversal out of the directory), with a new path since this run's pre-start snapshot.
Codex runs execute in parallel up to `AGENT_CONCURRENCY_CODEX` per worker; only a resume of the same
CLI session is exclusive (per-session lease on account_id + session id). Generated images stay
isolated because export reads only the run's own thread UUID directory (`images.py`), against a
per-thread baseline. Each image is at most
`AGENT_IMAGE_MAX_BYTES` (8 MiB since 2026-10-16; Codex PNGs run 1.7-2.1 MB, the old 2 MiB dropped some; passed to agent-worker-codex in compose) each - a larger image is listed in `files` with `skipped: "too_large"` and its `size`, no content and `AGENT_IMAGES_MAX_COUNT` (4), fully decodable by Pillow as PNG,
de-duplicated by content. Each is returned by the API as a normal run file under `out/` with `sha256`,
`width`, `height` and the content, subject to the usual TTL. `run.completed` events contain only file
metadata; PNG bytes remain in `deep_jobs.result_meta` until the normal 7-day job purge, even though
the API hides them after the 1-hour file TTL. A failing export never fails the run.
**Readiness criterion:** a real decodable PNG (dimensions + sha256 in the run result) that the critic
receives as an actual image - not a description. Direct worker run `551063`
met the PNG generation and export criterion. Delivery through the universal
HTTP route and Orchestra remains to be checked by its client.

## Not done yet

Orchestra's authenticated end-to-end image request and the Claude critic's
receipt of decodable PNG bytes remain. Account-aware CLI catalog discovery
remains later work. Multi-account is done as one worker container per login (see
[Multiple Claude subscription accounts](#multiple-claude-subscription-accounts)).

### Abandoned Claude OAuth refresh lock (2026-10-17)

Claude Code serialises OAuth refresh with an mkdir lock `.oauth_refresh.lock` in its config dir. A CLI killed mid-refresh leaves it behind, and every later run fails with "Failed to refresh OAuth token: another Claude Code process is refreshing it". `ClaudeCLI.preflight` calls `clear_stale_oauth_lock`: it removes only an EMPTY lock directory older than `OAUTH_LOCK_STALE_S` (120 s) and never touches credentials. The `claude auth status` probe is now killed on its 20 s timeout instead of being left holding the lock.
