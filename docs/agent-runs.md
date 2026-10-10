# Agent runs (owner-only CLI workers)

Run the official **Codex CLI** / **Claude Code CLI** as workers driven by the broker queue.
Status: **phase 1, C1 + C2-min** - data model, event schema, adapter contract, fake adapter,
API, worker lane, session leases, workspaces, timeouts. One real adapter (`ClaudeCLI`) and the billing guard are in; Codex, the model catalog and multi-account are later.

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
| `agent_session_leases` | `AgentSessionLeaseRow` | PK `(account_id, session_id)`; `run_id` (current holder), `lease_until`, `heartbeat_at`, `fence` (fencing token, +1 per acquisition; expired in place on release, never deleted) |

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
   worker_lost`. It is never re-run.
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
5. Start the lane (after step 6 for the first start): `docker compose --profile agent up -d agent-worker`; grant a project the
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

### Rollback

Routine rollback is: stop the worker (`docker compose --profile agent stop agent-worker`)
and/or revoke the exact test grant (remove `llm:agent` from the test project). Deleting the auth
volume or logging out is NOT a routine rollback: the account's authentication is handled
separately by the owner. Stopping the worker leaves queued runs `pending` and in-flight runs
`running`; `fail_stale_agent_runs` closes the latter as `worker_lost` once the worker is back.

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

## Not done yet

Codex adapter, model catalog from the CLI (`ClaudeCLI.list_models` returns `None`),
multi-account config dirs. Phase 2+: `$imagegen` headless support is unproven; the Claude
critic must receive real image bytes.
