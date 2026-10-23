# Deploy & ops

## Auto-deploy

Push to `master` → `.github/workflows/deploy.yml` runs **five jobs** (`docs`,
`test`, `integration`, `quality`, `deploy`):

1. **`docs` job** — any file under `src/`, `infra/`, `services/`, or
   `monitor/` changed must be matched by a `docs/` change. Opt-out per
   commit: literal `docs-not-needed`.
2. **`test` job** — `pytest` on in-memory SQLite, must pass. Repo-wide
   coverage gate **85%** on the `aibroker` package (raised from 70% on
   2026-09-07; stair-step, never drops).
3. **`integration` job** — the full suite again against a real `postgres:16`
   service container (`aibroker_test` database, health-checked with
   `pg_isready`), so the Postgres-only paths that SQLite skips — selector,
   reserved lane, `SKIP LOCKED` job queue, cost guard — run for real.
4. **`quality` job** — strict static analysis on the diff:
   - **Ruff** with `E,F,W,I,B,UP,SIM,C4,RET` (simplify, comprehensions,
     unreachable-after-return). E501/E402 ignored (no formatter; tests
     set env before import). Diff-only: legacy code is grandfathered,
     strict rules apply to whatever this push touched.
   - **Vulture** `--min-confidence 80` on changed files — surfaces dead
     funcs/classes ruff's F401/F841 doesn't see.
   - **Diff-cover** — every changed line ≥75% covered by tests in this
     push (separate from the 85% repo gate). Catches "new function
     without a test".
   - **Docs name-sync** — extract every public def/class from the diff
     (skip `_private`, `test_*`). **Added** symbols must appear in
     `docs/*.md`; **removed** symbols must NOT remain there. Opt-out:
     `docs-not-needed`.
5. **`deploy` job** — `needs: [docs, test, integration, quality]`. SSH to
   `aib.zapleo.com`; key on the server is wired to
   `command="/usr/local/bin/aibroker-deploy"` in `authorized_keys`.
   The wrapper (`infra/aibroker-deploy.sh`) does `git fetch` +
   `reset --hard origin/master` → `docker compose build` → `--profile agent build`
   of the three agent workers → `up -d --remove-orphans` → recreate of the workers that
   were running → drift gate (exit 12) → health gate, up to 180s (exit 11). See "Deploy entrypoint" below.

### What this guarantees

Anything that reaches production has: passing tests, ≥75% coverage on
the actual diff, no dead code in the touched files, no syntax/import
issues, every public name documented, no orphan refs to removed code.
If any gate fails, deploy is blocked.

If the deploy fails, a Telegram alert goes to `OWNER_TELEGRAM_ID` from the bot
behind the `TELEGRAM_BOT_TOKEN_VERA` GitHub secret (the Vera bot, not
`@aibzapleo_bot`). `ci.yml` (unit + Postgres integration) and `docs-check.yml` (the docs gate)
run on every push (including feature branches) so PRs see the verdict early.

## Restricted SSH key

Generated once on a dev box:
```
ssh-keygen -t ed25519 -f aibroker_gh_deploy -N "" -C "github-actions-aibroker-deploy"
```

Public part appended to `/root/.ssh/authorized_keys` on the server:
```
command="/usr/local/bin/aibroker-deploy",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA…
```

If this key leaks, the worst an attacker can do is re-run our deploy
script. No shell, no scp, no port-forward.

## Job retention (2026-07-26)

`deep_jobs` had no retention and had grown to **1.6 GB / 103k rows** — every row
stores the FULL request payload (Stepan's system prompt alone is ~79k chars, and
async transcription parks base64 audio there too), so the nightly dump was
~886 MB of finished work nobody would ever read again. Only 51 rows were
actually live (`pending`).

`job_queue.purge_finished_jobs()` deletes terminal jobs past
`JOB_RETENTION_DAYS` (default **7**), batched at `JOB_RETENTION_BATCH`
(default 5000) so a big first sweep can't hold a long lock — it simply takes
several passes. The dispatcher calls it every `JOB_RETENTION_EVERY_TICKS`
(default 300 ≈ 25 min at the idle poll), Postgres only. Both workers running it
is harmless: the DELETE is idempotent.

Only `done`/`error`/`cancelled` rows with a `completed_at` are eligible — a `pending` or
`running` job is untouchable regardless of age, so nothing can be deleted out
from under the dispatcher or while a caller might still poll it. Verified
against real Postgres before shipping (old done/error deleted; fresh done,
pending and running all kept).

No client change is needed: nothing but the queue itself reads `deep_jobs`, and
callers poll their result within seconds.

Tune per environment with `JOB_RETENTION_DAYS` / `JOB_RETENTION_BATCH` /
`JOB_RETENTION_EVERY_TICKS`.

## Graceful shutdown returns in-flight jobs (2026-10-13)

A deploy recreates `aibroker-api`. Before this, a job it was executing stayed
`running` after the SIGTERM with no executor until the 25-minute stale reclaim
(2026-10-13: vision jobs 552804/552805 sat 22 min after a restart).

On a graceful SIGTERM the dispatcher waits up to `JOB_SHUTDOWN_GRACE_S` (default
**5 s**, clamped to a **max of 8 s**, see the budget below) for the jobs it
has in flight. Jobs still running after that are cancelled, and every job this
process claimed and has not finished is returned to the queue by
`release_claimed_jobs()`: `running` → `pending`, `started_at` cleared,
`run_after = now`, `retry_count` unchanged (an operator restart is not a failed
attempt). The release is guarded by the claimed `started_at`, so a job that
finished, or was re-claimed elsewhere, is left alone. Released jobs are picked up
within seconds; each one logs an INFO line `shutdown: job N returned to pending`.

Shutdown budget (SIGKILL arrives `stop_grace_period` after SIGTERM, so the whole
sequence must fit in it): uvicorn first drains open HTTP requests, capped by
`--timeout-graceful-shutdown 10` (Dockerfile) = **10 s**; then the lifespan gives
the dispatcher **15 s** (`wait_for` in `main.py`: in-flight grace <= 8 s + cancel +
release); 10 + 15 = **25 s** < `stop_grace_period: 30s` (`aibroker-api` in
docker-compose.yml). Without those two settings a long HTTP request would eat the
default 10 s and the container would be killed before the release ran. The release
runs shielded, so the `wait_for` timeout cannot abort it. `aibroker-monitor` runs no
dispatcher and keeps the default.
The 8 s clamp on `JOB_SHUTDOWN_GRACE_S` exists so the grace always ends inside that
15 s dispatcher wait, leaving time for cancel and release.

The 25-minute stale reclaim now covers only hard crashes (SIGKILL, OOM, host loss).

Residual risk: a paid provider call that completed but whose finish had not
committed within the grace is re-run, so double billing is possible in that
narrow window. Agent jobs are untouched; they are executed by agent workers, not
by this dispatcher.

## Schema migrations

Applied via `psql` directly against the running container — there is no
Alembic. Every file in `infra/sql/migrations/` is idempotent
(`IF NOT EXISTS`), so re-running is safe:

```
ssh hetzner-root
docker exec -i aibroker-postgres psql -U aibroker aibroker \
  < /var/www/aibroker/infra/sql/migrations/010_deep_jobs_payload_hash.sql
```

Apply a migration BEFORE merging the code that depends on it (the deploy
pipeline ships code only). Migration 016 (`016_projects_self_signup.sql`) adds
`projects.total_request_cap`, `total_requests_used`, `self_signup` and `signup_ip` for self-signup -
the ORM selects them on every authenticated request, so it MUST be applied before the code lands. `infra/sql/init.sql` mirrors every migration for
fresh-DB bootstrap (enforced by `tests/test_init_sql_mirrors_migrations.py`;
it now includes migration 011, `usage_log.model_served`). Migration 010 (2026-07-16) adds `deep_jobs.payload_hash`
plus the `ix_deep_jobs_dedup` index for in-flight job dedup — the code
degrades to plain inserts (with a logged warning) if it lands first, but
dedup stays off until the migration is applied.

**Rollout order for migration 020 (important).** On `master` the dispatcher claims with
`UPDATE … RETURNING *` and builds `DeepJobRow(**row)` after the commit, so once the new columns exist
that code raises `TypeError` on the unknown keys: claimed jobs sit in `running`, are dropped, and only the
25-minute stale reclaim recovers them (burning a retry). Therefore:

1. Deploy PR #76 first (it contains #71: explicit column lists for the claim and the key pick, plus the
   shutdown requeue). It works with and without the new columns.
2. Apply migration 020 (below) and run the post-check. The migration file's own header repeats this
   order and the `psql -v ON_ERROR_STOP=1` invocation.
3. Deploy the branch with the 020 code (`feat/jobs-trace-idempotency-cancel`). It keeps an explicit
   `_JOB_COLUMNS` claim (the same idea as #71; expect a trivial conflict in `services/job_queue.py` when
   rebasing on #76 — keep one explicit list that includes `trace_id, labels, idempotency_key,
   cancel_requested`).

Never apply 020 while plain `master` code is still serving, and never deploy this branch before 020: the
ORM selects every `deep_jobs` column on each poll and claim.

Migration 020 (2026-10-13, job trace / idempotency / cancel) adds four columns to `deep_jobs` —
`trace_id varchar(128)`, `labels jsonb`, `idempotency_key varchar(128)` (all nullable) and
`cancel_requested boolean NOT NULL DEFAULT false` (the ORM model carries the same server default, so
`create_all` / init.sql and raw `INSERT`s that omit it agree with the migration) — plus two partial
indexes: the UNIQUE
`ux_deep_jobs_idempotency (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL` and
`ix_deep_jobs_trace (project_id, trace_id) WHERE trace_id IS NOT NULL`. Purely additive; the new status
value `cancelled` needs no DDL (`deep_jobs.status` is a plain `varchar(20)`, no CHECK or enum).
Apply it after #76 is live and before the 020 code (order above).

```
docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
  < /var/www/aibroker/infra/sql/migrations/020_deep_jobs_trace_idempotency.sql
```

Post-check (both rows must show `indisvalid = t`):

```
docker exec -i aibroker-postgres psql -U aibroker aibroker -c \
  "SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE indexrelid IN ('ux_deep_jobs_idempotency'::regclass, 'ix_deep_jobs_trace'::regclass);"
```

- Run it exactly like that (stdin / `-f`, autocommit, `-v ON_ERROR_STOP=1`), **not** with `psql -1` or inside a
  transaction: the two `CREATE INDEX CONCURRENTLY` statements cannot run in one. The file does `RESET lock_timeout`
  before the builds, because the 5 s `lock_timeout` would otherwise abort a `CONCURRENTLY` build and leave an
  INVALID index that `IF NOT EXISTS` skips. `deep_jobs` is moderately large and written
  on every submit/claim/finish, so a plain index build would block those writes; both indexes are partial
  over all-NULL history, so the builds are tiny. If a concurrent build is interrupted it leaves an INVALID
  index that `IF NOT EXISTS` would skip — `DROP INDEX CONCURRENTLY IF EXISTS <name>` and re-run.
- `ADD COLUMN … NOT NULL DEFAULT false` is metadata-only on PostgreSQL 11+ (the stack runs `pgvector/pgvector:pg16`):
  no table rewrite, no long lock; `SET lock_timeout = '5s'` makes the `ALTER`s fail fast instead of queueing
  behind a long transaction. `infra/sql/init.sql` mirrors the columns and indexes.
- **Why the order matters.** With plain `master` code still running, the claim `UPDATE … RETURNING *` returns the
  four new columns (through the transaction-pooling pgbouncer cached prepared statements also fail with
  "cached statement plan is invalid" until the connection is recycled) and `DeepJobRow(**row)` raises on the
  unknown keys after the commit. #76/#71 and this branch name the claimed columns explicitly, so they survive any
  later `ALTER`. After step 3 recreate `api` (the deploy does) so no worker keeps a stale prepared statement.
- **Retention.** `cancelled` rows are pruned with `done`/`error` after `JOB_RETENTION_DAYS`; an
  `Idempotency-Key` therefore lives exactly as long as its job row (see
  [api.md](api.md#idempotency-key-migration-020)).

**Migration 024 (job-list indexes).** Adds `ix_deep_jobs_project_id (project_id, id)` and the GIN index
`ix_deep_jobs_labels_gin (labels jsonb_path_ops)` for `GET /v1/jobs?label.k=v`. Additive, no code ordering
dependency (the endpoint works without them, but scans `labels` row by row per project). Apply like 020:
`psql -v ON_ERROR_STOP=1 -f` (autocommit; both are `CREATE INDEX CONCURRENTLY`, so no write lock on
`deep_jobs`, but each build scans the whole table and the GIN index adds write cost to every insert and
`labels` update). No `lock_timeout` is set because there is no ALTER. An interrupted build leaves an INVALID
index that `IF NOT EXISTS` skips: `DROP INDEX CONCURRENTLY IF EXISTS <name>` and re-run; post-check
`SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE indexrelid IN ('ix_deep_jobs_project_id'::regclass, 'ix_deep_jobs_labels_gin'::regclass);`
must show `t` for both.

Migration 019 (2026-10-13, monthly caps) adds `api_keys.monthly_reset_at DATE` (nullable) and
backfills it once. **Apply it BEFORE deploying this code.** Key selection (`pick_and_reserve`)
references the column on every request, so code that lands first breaks key selection
until the column exists. Operator command:

```
docker exec -i aibroker-postgres psql -U aibroker aibroker \
  < /var/www/aibroker/infra/sql/migrations/019_api_keys_monthly_reset.sql
```

Existing `monthly_cost_used_usd` values were never reset (lifetime sums). The migration
sets `lock_timeout = '5s'` and then backfills, once and idempotently (guard
`monthly_reset_at IS NULL`), every key that has a `monthly_cost_cap_usd`: its counter becomes
the real spend of the current UTC month from `usage_log` (`created_at` is naive UTC), so a
capped key keeps its month-to-date spend. Keys without a monthly cap keep a NULL stamp and
read as 0. `infra/sql/init.sql` mirrors the DDL of 019 only (the backfill is data, not schema).

Migration 018 (agent session leases) adds `agent_session_leases`. Apply it BEFORE deploying
code that takes session leases (the agent worker lane); the agent worker itself connects only
through pgbouncer, so it needs no direct Postgres access for this. The owner checklist is in
[agent-runs.md](agent-runs.md#owner-checklist-all-on-the-server-in-the-compose-project-directory).

Migration 015 (2026-10-03, core refactor) adds `usage_log.request_id VARCHAR(64)` and a
partial index `ix_usage_request_id` — the request trace behind the `X-Request-Id`
header. `record_usage` degrades gracefully (drops only the column the live schema lacks,
warns once) if the code lands first, but apply it before deploying so the trail is
recorded from the first request. The index line should be run separately with
`CREATE INDEX CONCURRENTLY` on the live table. Also new, no action needed: setting
`AFFINITY_TTL_S` (default 7200) and Redis keys `aib:raff:*` (route affinity; fail-open).

### Compare page: migration 021 and project provisioning (2026-10-14)

Migration 021 (`021_compare_page.sql`) adds `compare_requests` and `compare_results` for the
public `/ask2many` page. It is additive: no existing table is touched, and only the `/ask2many`
routes read the new tables. Apply it BEFORE deploying the code that reads them:

```
docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
  < /var/www/aibroker/infra/sql/migrations/021_compare_page.sql
```

`infra/sql/init.sql` mirrors the DDL. After the deploy, provision the dedicated project once,
inside the API container, the same way as `bootstrap`:

```
docker exec -it aibroker-api python -m aibroker.scripts.provision_compare
```

It prints `project 'model-compare': created` (or `updated`, with the caps at 0). The script is
idempotent: it creates the project on the first run and then re-asserts the scopes (`llm:chat`, `llm:edit`,
`llm:deep`, `llm:agent`) and the two money caps, without rotating anything. Until it has run, `POST /ask2many/api/requests`
returns `503`.

Subscription models on the page need server env that the script does not set (it is not in the repo).
Before they can run: add the `model-compare` project's numeric id to `AGENT_ALLOWED_PROJECT_IDS`; add an entry for
that id to `AGENT_PROJECT_POLICY` allowing the `claude-sub` and `codex-sub` models and accounts it should use; and configure the
subscription catalog, the verified models and `AGENT_SUBSCRIPTION_CODEX_ENABLED` as for any subscription client.
`codex-sub` is enabled by the owner's decision of 15.10: its agent runs in the separate container
`aibroker-agent-worker-codex`, and the shell of `codex exec` stays (sandbox `workspace-write`, approval never,
`allowed_tools=[]` does not switch it off); the owner accepted that. Subscription jobs cost $0, so the money caps (0)
do not limit them; only the project request cap does (owner decision). The formats are in [subscription-models.md](subscription-models.md) and [auth.md](auth.md#scopes). Until then a
subscription model shows `отказ: проект не допущен к подписочным моделям (agent_project_not_allowed)` or
`отказ: политика проекта (<code>)` as its result; other models are unaffected.

The project's daily and monthly caps are 0. The cost guard then refuses paid models and free
models run. To raise the limit, change the caps on the `model-compare` project in the dashboard
(Projects) or in SQL. Do not raise it by re-running the script: each run sets both caps back to
0. The project key is generated, hashed and discarded by the script. Nobody can read it
afterwards, and it is never printed or sent to a browser.

### Compare page: migration 022 (usage columns, 2026-10-15)

Migration 022 (`022_compare_usage.sql`) adds nullable columns `tokens_in`, `tokens_out`, `cost_usd`,
`finish_reason` and `billing_type` to `compare_results`. It is additive and does not rewrite the table. Apply it
BEFORE deploying the code that writes them: the ORM model names these columns, so the code fails against a
database without them (see the pgbouncer rule below):

```
docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
  < /var/www/aibroker/infra/sql/migrations/022_compare_usage.sql
```

`infra/sql/init.sql` mirrors the DDL. Existing rows stay NULL (shown as «неизвестно»); nothing is backfilled.

### Job priority: migration 023 (2026-10-21)

Migration 023 (`023_deep_jobs_priority.sql`) adds `deep_jobs.priority SMALLINT NOT NULL DEFAULT 1` (0 = interactive,
1 = batch; every old row and old client is batch). A constant default does not rewrite the table on Postgres 11+.
It also builds `ix_deep_jobs_claim_order (status, priority, created_at)` with `CREATE INDEX CONCURRENTLY` and then
drops `ix_deep_jobs_claimable` (009) with `DROP INDEX CONCURRENTLY`: only the two claim scans (`claim_agent_runs`,
the LLM dispatcher's `_claim_batch`) used it, and both now `ORDER BY priority, created_at` (`run_after` stays a
filter, not an index column). Apply it BEFORE deploying the code (the ORM model names the column; see the
pgbouncer rule below), with `psql -v ON_ERROR_STOP=1` in autocommit (never `psql -1`: CONCURRENTLY cannot run in
a transaction; `lock_timeout` covers only the ALTER and is RESET before the build, as in 020):

```
docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker   < /var/www/aibroker/infra/sql/migrations/023_deep_jobs_priority.sql
```

`infra/sql/init.sql` mirrors the DDL (plain `CREATE INDEX`: it runs on an empty DB). An interrupted build leaves
an INVALID index that `IF NOT EXISTS` skips: `DROP INDEX CONCURRENTLY IF EXISTS ix_deep_jobs_claim_order` and re-run;
post-check `indisvalid = t` in `pg_index`. Mixed-version window: workers from before this change ignore `priority`
(claim by `created_at`, explicit column lists tolerate the new column), so they keep running between the migration and
the deploy but do not honour priority. Rollback: `CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deep_jobs_claimable ON
deep_jobs (status, run_after, created_at)`, `DROP INDEX CONCURRENTLY IF EXISTS ix_deep_jobs_claim_order`, then drop the column. Set
`AGENT_INTERACTIVE_RESERVE` only after the migration, together with the worker image that reads it.

### Agent file bytes in a table: migration 025 (2026-10-22)

Measured 2026-10-22: `deep_jobs` TOAST 1.44 GB, of which `result_meta.files[].content_b64` is ~706 MB of base64 PNG
(278 files, avg ~2 MB, max 2.9 MB; every inline file is a PNG, the only other files are `.txt` without content);
pglz does not compress it (733 vs 723 MB). Migration 025 (`025_deep_job_files.sql`) creates `deep_job_files`
(`job_id` FK -> `deep_jobs` ON DELETE CASCADE, `idx`, `sha256`, `mime`, `size`, `expires_at`, `data BYTEA`,
`STORAGE EXTERNAL`): additive, one new table, nothing existing is touched, no data is moved or deleted. Locking: the foreign key makes
`CREATE TABLE` take `SHARE ROW EXCLUSIVE` on `deep_jobs` for the duration of the transaction (milliseconds), so writes
to `deep_jobs` (submit, claim, finish) briefly wait; reads do not. The file is one transaction (`BEGIN` ... `COMMIT`)
with `SET LOCAL lock_timeout = '5s'` and `statement_timeout = '60s'`: if a long `deep_jobs` transaction holds the
table it fails with `lock timeout` and applies nothing - run it again a minute later (it is idempotent,
`IF NOT EXISTS`); never raise the timeout to force it.
Retention is unchanged (7 days; the cascade removes the bytes with the job).

Honest expectation: the raw bytes are ~25% smaller than their base64 (3/4), and pglz is no longer run on PNG.
That is ~25% of the base64 payload of the NEW files only, not 25% of the database.

Rollout in two steps, so that a rollback is a config flip:

1. Apply the migration, then deploy code that reads both formats (`aibroker.agent.job_files.live_files`) with
   `AGENT_FILES_IN_TABLE` unset/false: new jobs still write inline exactly as before.
   `psql -v ON_ERROR_STOP=1 < infra/sql/migrations/025_deep_job_files.sql`
2. Set `AGENT_FILES_IN_TABLE=true` (the writer: `store_files` for png/jpeg/webp/gif only; text-like
   files stay inline because they are small and pglz compresses them) and restart.
   The flag lives in the server `.env` (`AGENT_FILES_IN_TABLE=true`); `docker-compose.yml` passes it to `api`
   and the three agent workers (the writer runs in the workers), so a flip is `.env` + `docker compose up -d`
   of those services.

Rollback: set the flag back to false (new jobs write inline; rows already written to the table are still served by
the dual-read code until they are purged, <= 7 days). A plain revert to the pre-025 code is only safe after all
jobs written with the flag on are gone from retention (7 days after the flag was turned off): the old code returns
`content_b64: null` (without `expired`) for such a job. The table is dropped only by a separate decision, after that
(`SET aibroker.allow_destructive_ddl = 'on'; DROP TABLE deep_job_files;`).

Notes. (a) Only PNG was measured (100% of inline content); jpeg/webp/gif are an assumption (codecs are already
compressed), pdf/zip/gz are deliberately left inline (a PDF can be uncompressed). (b) After a full code revert the old
reader would also pass the internal key `content_ref` through to clients for such jobs. (c) `deep_jobs` TOAST does not
shrink physically: freed space is reused after retention, and the files only shrink with `VACUUM FULL` (separate
decision, takes an exclusive lock). (d) `services/compare.py` calls the sync `_job_response(row)`, which would show
`content_b64: null` for a table-stored file; compare does not read files today. (e) `SET LOCAL` timeouts end with the
transaction, which is the same effect as `SET` + `RESET` in 020/022/023.

Verify after enabling (still to be measured live): `pg_total_relation_size('deep_job_files')` against the growth of
`pg_total_relation_size('deep_jobs')` over 24 h, and a `GET /v1/agent/runs/{id}` of a new job returning the PNG.

### Clearing expired agent file bytes (opt-in tool, Room #4040)

`aibroker.scripts.clear_expired_files` clears the CONTENT of already-expired inline files: for `agent:*` jobs in a
terminal status, each `result_meta.files[i]` with inline `content_b64` and `expires_at` strictly before the cutoff
becomes `content_b64: null, expired: true` - the same JSON the API already serves after expiry (`GET /v1/agent/runs/{id}`,
`GET /v1/jobs/{id}` do not change; only JSON key order inside the file object may differ). Jobs, `request`, `result_text`,
usage, traces and every other key are kept. `deep_job_files` rows are NOT touched (only counted in the report).
Nothing schedules it: it runs only when an operator starts it. No retention change, no `VACUUM`.

```
docker exec aibroker-api python -m aibroker.scripts.clear_expired_files                 # dry-run (default), writes nothing
docker exec aibroker-api python -m aibroker.scripts.clear_expired_files --cutoff 2026-10-23T10:00:00Z   # dry-run at a fixed cutoff
# execute: same cutoff as the dry-run you reviewed, and the exact confirmation string
docker exec aibroker-api python -m aibroker.scripts.clear_expired_files --execute     --cutoff 2026-10-23T10:00:00Z --confirm CLEAR-EXPIRED-2026-10-23T10:00:00Z
```

- Cutoff: UTC, whole seconds, never in the future. Default (dry-run only) is now minus `--margin-seconds` (300), so a
  file inside its live window is never touched. `--execute` refuses without an explicit `--cutoff` and the exact
  `--confirm`. Expiry is compared as the API does (ISO string compare, `COLLATE "C"`); a file without a well-formed
  `expires_at` is skipped and reported.
- Batches of `--batch-size` jobs (20), one transaction each with `SET LOCAL lock_timeout '2s'`, `statement_timeout '30s'`;
  rows are locked `FOR UPDATE SKIP LOCKED` (a job a writer holds is skipped and cleared by the next run), the rewrite is
  one SQL `UPDATE` with jsonb functions (no JSON passes through Python). A batch that hits a timeout rolls back alone, is
  counted as failed and the exit code is 1. Idempotent: a second run clears 0. The dry-run runs in a `READ ONLY` transaction.
- The report logs cutoff, jobs scanned, jobs/files/base64 bytes to clear and cleared, skipped (not expired, no
  `expires_at`, files already in `deep_job_files`), the breakdown by project and capability, and the `deep_job_files`
  count / `sum(size)` / expired.
- NOT reversible from the database: after clearing, the bytes exist only in the daily `aibroker` dumps (3 days on the
  server, 60 days on the NAS). The physical size of `deep_jobs` does not shrink (freed TOAST space is reused; only
  `VACUUM FULL` returns it, which is a separate decision with an exclusive lock).

### Migrations vs pgbouncer prepared statements (2026-10-13)

Incident 2026-10-13, 09:29-09:39 UTC: migration 019 (`ALTER TABLE api_keys ADD COLUMN`)
made the API fail repeatedly with `InvalidCachedStatementError`. Jobs were requeued (none
lost) until the idle pgbouncer server backends were terminated.

- **Why.** pgbouncer runs in transaction pooling mode (`max_prepared_statements=500`) and keeps
  server-side prepared statements keyed by query text. After `ADD COLUMN`, a `RETURNING *` or
  `SELECT *` statement changes its result type; Postgres cannot re-plan it, and re-preparing
  from the client reaches the same stale server statement.
- **Rule.** No star projections (`RETURNING *`, `SELECT *`) in raw SQL on application tables.
  Column lists are built from the ORM model (`Model.__table__.columns`); see
  [conventions.md](conventions.md#sqlalchemy). Guarded by `tests/test_stale_statement.py`
  (allowlist: CTE reads in `routes/dashboard_requests.py`).
- **Consequence.** A column present in the model but not yet migrated makes those queries
  fail, so migrations ALWAYS run before the code deploy.
- **Safety net.** `retry_stale_statement` (`db/resilience.py`, matcher `is_stale_statement_error`)
  retries once in a fresh transaction on `InvalidCachedStatementError` (key pick, job claim,
  job finish) and logs one WARNING. It is a safety net for direct connections only: behind
  pgbouncer the server statement is reused by query text, so the retry probably does not help.
  Under pgbouncer the explicit column lists are the fix, plus the operator step below after
  DDL. It does not replace the migration order.
- **`ALTER COLUMN ... TYPE`.** On a column that is in a listed query, the query text stays the
  same but the result type changes, so explicit column lists do not protect it. It needs the
  operator step below (terminate idle pgbouncer backends) right after the migration.
  `DROP COLUMN` follows expand/contract: first deploy code that no longer lists the column,
  then drop it.
- **Operator step after DDL on a hot table.** From the postgres container, terminate only idle
  pgbouncer server backends:

  ```sql
  SELECT pg_terminate_backend(pid) FROM pg_stat_activity
  WHERE datname='aibroker' AND usename='aibroker'
    AND client_addr='<pgbouncer container IP>' AND state='idle';
  ```

  No API restart is needed; a restart does not help, because the stale statements live in
  pgbouncer's server connections. Do not read the postgres admin password for pgbouncer's console.

## Agent runs: operator step before deploying the allowlist

Agent runs are fail-closed behind `AGENT_ALLOWED_PROJECT_IDS` (comma-separated project ids,
read by BOTH the `api` and the `agent-worker` service from the compose `.env`). Empty or unset
= every `/v1/agent/runs` call gets 403 `agent_project_not_allowed`. **Before deploying the
commit that introduces it, add `AGENT_ALLOWED_PROJECT_IDS=11` to `.env`**, otherwise the owner
test project (`owner-agent-test`) is locked out.

Second fail-closed gate, same deploy: `AGENT_PROJECT_POLICY` (JSON, read by `api` and `agent-worker`)
must list the exact model and account of every project that may run agents, otherwise creates get 403
`agent_policy_missing`. **Before deploying, add to `.env` (single-quoted so the JSON survives):**
`AGENT_PROJECT_POLICY='{"11": {"models": ["claude-sonnet-5-5"], "accounts": ["owner"]}}'`. See [agent-runs.md](agent-runs.md).

For the separately approved Orchestra project 12, `"models": ["*"]` allows
only candidate ids of the requested subscription provider; keep its
`"accounts": ["owner"]` and every other project entry unchanged. This does
not mark a candidate verified for ordinary jobs, change scope or budget, or
permit API-billed fallback. A live model still needs the usual billing guard.

The approved internal RPC uses `agent_claude_rpc` and `agent_codex_rpc` named
volumes. The ordinary deploy recreates the API with both mounts, and the agent
workers (`agent-worker`, `agent-worker-claude2`, `agent-worker-codex`) are rebuilt by the same deploy and
recreated only if running (see "Agent workers in every deploy"). The volumes contain only `worker.sock`, owned by
uid 10001 with directory mode `0700` and socket mode `0600`. Do not mount CLI
auth volumes into API. No new Docker network, host port, capability, or sandbox
relaxation is required.

## Agent worker sizing (2026-10-13)

| service | cpus | mem_limit | pids_limit | workspaces tmpfs |
|---|---|---|---|---|
| `agent-worker` (Claude) | 1 | 1g | 256 | unchanged |
| `agent-worker-codex` | 3 | 3g | 768 | 1024m (was 512m) |

`AGENT_CONCURRENCY_CODEX` (`.env`; code default and compose default `1`, production plan `7`)
sets parallel Codex runs per container. The Codex limits above are sized for 7 (measured 2026-10-14 at 4 parallel: peak 174% CPU,
665 MiB, 184 pids, i.e. ~0.45 core and ~46 pids per run on average; CPU is the limit, so 3 of the
host's 4 cores go to Codex and ~1 stays for api/postgres/neighbours. Earlier, one run peaked at about
+140 MiB RSS over a 187 MiB idle worker, about 50 pids, about one core; the host has 4 vCPU and
~3.2 GB available). Raise it only
after reading the auth-refresh guard in `agent-runs.md`: when `last_refresh` in the Codex
`auth.json` is older than 4 days (or unreadable), Codex start-ups are serialized by a short
`__codex_auth__` lease, held until the first completed model event, so that two processes should not
rotate the refresh token at once (the exact refresh timing inside Codex is UNVERIFIED); otherwise
runs are fully parallel. A two-run live canary is still worth doing as a smoke test.

## Redis container (2026-07-16)

`docker-compose.yml` now includes `aibroker-redis` (`redis:7-alpine`) —
shared selector state (cache-affinity + saturation verdicts) across the two
uvicorn workers / future nodes. **No ops action needed**: the next
`docker compose up -d` creates it, and `REDIS_URL` is wired into `api` and
`monitor` by compose itself (nothing to add to `.env`).

Cache semantics on purpose: `--save ""` (no persistence, nothing to back
up), 64 MB `allkeys-lru` cap, no published ports (compose-network only).
If the container is down the app fails open to its old in-process behaviour
— worst case a slightly colder provider prompt-cache, never an outage.

## Deploy entrypoint (versioned since 2026-09-02)

`infra/aibroker-deploy.sh` is the restricted entrypoint pinned in root's
`authorized_keys` (`command="/usr/local/bin/aibroker-deploy"`), so a deploy key
can run that and nothing else. It used to exist ONLY on the server, unversioned
and unreviewed; it now lives in the repo and is installed with
`bash /var/www/aibroker/infra/install-deploy.sh` (which keeps a timestamped
backup and refuses to install a script that fails `bash -n`).

Installing is deliberately NOT part of the deploy: a deploy must not rewrite
the entrypoint currently executing it. CI does not install it either. The server
runs the INSTALLED copy, so a change to `infra/aibroker-deploy.sh` takes effect
only after an operator runs `infra/install-deploy.sh` on the server.

### Agent workers in every deploy (since 2026-10-13)

Services under the compose profile `agent` are invisible to a plain
`docker compose up -d`, so the script names `agent-worker`, `agent-worker-claude2` and
`agent-worker-codex` explicitly. Before the fix, deploy never rebuilt them and the gates did not check
them: on 2026-10-13 `aibroker-agent-worker` kept running PR #56 code while master
was at #62.

- **Build:** both workers are always built, after the base build, because they are
  `FROM aibroker-api:latest`. A stopped worker is built too, so its image is ready.
- **Recreate:** only the workers that were **running** before the deploy are
  recreated (`--profile agent up -d --no-deps`). A worker stopped by the owner (the
  rollback in [agent-runs.md](agent-runs.md#rollback)) stays stopped across deploys.
- **First start is manual:** `agent-worker-claude2` is built but not started by deploy. It starts only
  after the owner's login and `docker compose --profile agent up -d agent-worker-claude2` (see
  [agent-runs.md](agent-runs.md#multiple-claude-subscription-accounts)).
- **Interruption:** recreating an active worker interrupts its in-flight run. Docker's
  default 10s stop grace applies, and the interrupted run is later marked
  `worker_lost` by the reaper. Deploy only when no agent runs are active.
- **Gates:** the drift gate and the health gate cover the active workers. The workers
  have no compose healthcheck, so an active worker must only be `running`.
- **Fail closed:** the running-workers probe and the health-gate `ps` output are
  captured before use. A failing docker/compose query aborts the deploy instead of
  being read as "no workers".
- **One-offs:** the health gate skips leftover `docker compose run` containers
  (names containing `-run-`).
- **`AIBROKER_DEPLOY_DIR`** (default `/var/www/aibroker`) exists only so tests can
  run the script. The forced-command SSH cannot set it, so prod always uses the default.
- `tests/test_deploy_script_profiles.py` guards that every profiled service is listed
  in the script, that the default build runs before the agent build, and that the
  health gate uses `ps -a`.

### What it checks, and why

The old version waited for `aibroker-api` to answer `/healthz` and exited 0.
That was blind in two directions, and both bit us:

1. **A sibling service could fail entirely** and the deploy still reported
   success — nothing but `api` was ever checked.
2. **A container could keep running a STALE config.** After a manual
   `docker run` during a measurement, `docker compose up -d` left
   `aibroker-vision-local` on its previous `-c 8192 --sleep-idle-seconds 900`
   command; `docker ps` reported `healthy` throughout. **Container status says
   nothing about which config a container was started with.**

So it now runs two gates after `compose up -d`:

**Drift gate.** `docker compose up -d --dry-run` reports what a *second* `up`
would still have to do. On a fully-applied deploy every line ends in
`Running` / `Healthy` / `Waiting`; any other verb (`Recreate`, `Create`,
`Starting`, `Started`) means a container is not running the config we just
deployed. Verified both ways: silent on a clean state, and it catches a
container stopped out from under compose. The same check runs for the active
agent workers (`--profile agent up -d --dry-run --no-deps`), because the base
command does not see them. Exit 12.

**Health gate.** Waits up to 180s for every service listed by `docker compose ps -a`,
not just `api`, so an exited container fails it. An owner-stopped agent worker is
skipped. Only `monitor` has its healthcheck disabled, so it only has to be
`running`; postgres, pgbouncer, redis, vision-local and api
must be `healthy`. An active agent worker has no healthcheck and must be
`running`. On failure it dumps `compose ps` plus
the last 30 log lines of each offending service. Exit 11.

On success it prints the resolved `args` of `vision-local`, so
the deploy log records *which configuration* actually started — the question
the old script left unanswerable.

## Quality gates, reconciled (2026-09-07)

- **ruff runs the SAME rules locally and in CI.** `pyproject.toml`'s
  `[tool.ruff.lint]` now equals the explicit `--select/--ignore` in
  `deploy.yml`'s quality job (`E,F,W,I,B,UP,SIM,C4,RET`, ignoring
  `E501,E402,B008`). They had drifted in both directions — the config selected
  `S`/`ASYNC`/`RUF` that CI never ran, CI ran `W`/`SIM`/`C4`/`RET` the config
  never selected — so an editor showed a different verdict than the deploy
  gate. CI stays the authority; the config is the local mirror, cross-referenced
  in both files.
- **Coverage floor 70% → 85%.** The 70 dated from a ~200-test suite; the suite
  is ~800 tests at 90%, so the gate had stopped applying pressure.
- **No more test doubles leaking into production.** `test_health_probes.py`
  faked `httpx.Response` with `AsyncMock()`, which turns every attribute into a
  coroutine; `dict(r.headers)` in `health_probes.py` choked on it, and a
  `try/except` lived in production purely to tolerate the fake (its own comment
  said so). The fake is a plain object now and the workaround is gone — along
  with 8 of the suite's 38 warnings.

## Ceilings, retention, and what the monitor watches (2026-09-07)

**Every aibroker container now has a memory ceiling.** api 1024m, postgres
1024m, monitor 256m, redis 128m, pgbouncer 128m — headroom over measured use,
not reservations. A plain `mem_limit` is durable under the systemd cgroup
driver (only the SWAP limit needs the slice trick used for vision-local), and
these five deliberately keep swap: small, mostly-idle processes are what swap
is for on a host where the vision model pins 4GB. Before this, any of them was
an eligible victim of a host-wide OOM. `redis` and `pgbouncer` also gained
healthchecks (so only `monitor` is still checked as merely `running`).

**`usage_log` and `audit_log` have retention now** — `purge_old_logs` in
`services/job_queue.py`, run by the dispatcher next to `purge_finished_jobs`.
Windows (env-overridable): `USAGE_RETENTION_DAYS=120`,
`AUDIT_CAPBLOCK_RETENTION_DAYS=14` (99.98% of audit rows were `cap_block`
breadcrumbs, useful for weeks, weight for months), `AUDIT_RETENTION_DAYS=365`.
Batched like the jobs sweep, so the first pass over the 1.9M-row backlog cannot
hold a lock; it catches up over several ticks. Before this the only retention
in the codebase was the one written for `deep_jobs` after it reached 1.6GB.

**The monitor now checks three things key liveness never covered:**

| check | alert key | why |
|---|---|---|
| `check_local_services` — GET `VISION_LOCAL_URL/health` | `local:vision` | api's `/healthz` stays green while it is down; the chain falls through to the cloud tier silently |
| `check_queue_backlog` — jobs pending/running past `MONITOR_QUEUE_STUCK_MIN` (30) | `queue:backlog` | a wedged dispatcher was invisible until a client complained |
| `check_backup_freshness` — newest `*.dump` under `MONITOR_BACKUP_DIR` younger than `MONITOR_BACKUP_MAX_AGE_H` (36h); the age decision is the pure `backup_is_fresh` | `backup:stale` | see below |

Each is an alert/recover pair, so a fix clears itself; a failing check never
stops the others.

### Backups are made by vera3, not by this stack

The nightly `pg_dump -Fc` of `aibroker-postgres` (plus a tarball of `.env`) is
produced by **`/usr/local/bin/vera-backup.sh`** — vera3's script, root
crontab 03:30 — into `/var/backups/vera/aibroker/daily/<date>/aibroker.dump`,
verified against `SHA256SUMS`, pulled nightly by a Synology NAS over a
read-only `rrsync` user, and pruned locally only once the NAS has read it.
That is a solid chain, but aibroker did not own or verify any of it, and the
"Disaster recovery" runbook below described a manual `pg_dump` that nobody
runs. If vera3's script ever dropped the `backup_project aibroker` line, this
stack would lose its backups without noticing. `check_backup_freshness` closes
that: the monitor mounts the dump directory read-only (`AIBROKER_BACKUP_DIR`,
default `/var/backups/vera/aibroker/daily`) and alerts once a day while no
dump under 36h old exists.

The tree is `root:verabackup 750` and the monitor runs as uid 10001, so it
joins the host's `verabackup` group (`group_add`, gid via `VERABACKUP_GID`,
default 1001) — the least privilege that can read it. The very first live tick
ran without that and paged a false `backup:stale` ("none found"); the check
also falls back to the newest date-directory's mtime when the files inside are
unreadable, so a permissions regression degrades to a coarser signal rather
than a false alarm.

## Local vision (2026-08-31)

`vision-local` runs **upstream `llama-server`** (`ghcr.io/ggml-org/llama.cpp:server`)
serving **Qwen3-VL-4B-Instruct Q4_K_M** on CPU. Container
`aibroker-vision-local`; `api` reaches it at `VISION_LOCAL_URL` (default
`http://aibroker-vision-local:8080`). Unset or unreachable degrades safely to
`gemini -> sambanova -> openrouter` (free) `-> deepseek -> openai` (paid, final
job-queue retry only).

**Why it exists.** Vision was running an 8% success rate: over 14 days, 1762 ok
against ~20000 errors (12102 `CapBlock`, 3828 gemini `RateLimitError`, 4053
openrouter `RateLimitError`), essentially all of it one client's traffic at
240-300 distinct images/day.

**We write no service code.** `llama-server` already
provides everything a hand-rolled wrapper would have to build: an OpenAI-shaped
`/v1/chat/completions` that accepts `image_url`, `--sleep-idle-seconds` for
idle unload, `/health` that is exempt from the idle timer (so the compose
healthcheck cannot keep the model awake), and `response_format: json_schema`
for grammar-constrained output. All broker-side logic lives in
`providers/local_vision._describe_via_local_vision`.

**Model is MOUNTED, not baked into an image.** Deploys build on the production
host inside a 10-minute CI step (`/usr/local/bin/aibroker-deploy`); pulling
3.3GB of weights into a layer on every Dockerfile touch would put that budget
at risk for nothing. One-time host setup:

    mkdir -p /var/lib/aibroker-vision/model && cd /var/lib/aibroker-vision/model
    curl -fLO https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/Qwen3VL-4B-Instruct-Q4_K_M.gguf
    curl -fLO https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct-GGUF/resolve/main/mmproj-Qwen3VL-4B-Instruct-F16.gguf
    # then rename to qwen3-vl-4b-Q4_K_M.gguf / qwen3-vl-4b-mmproj-F16.gguf,
    # or point VISION_MODEL_DIR elsewhere.

### Measured on this host, 2026-08-31

| | |
|---|---|
| model load | 22s |
| chat screenshot, model resident | **69s** |
| dense document | up to 192s |
| one-shot CLI (reloads model per call) | 81-192s, median 163s |
| **peak RSS** | **5105MB** -> `mem_limit: 5600m` |

Peak RSS climbs 4781 -> 5105MB across consecutive runs and then plateaus:
most of it is page cache for the mmap'd weights, which the kernel reclaims
under pressure. Composition: 2.5GB weights + 0.84GB mmproj (F16) + 1.13GB f16
KV cache at `-c 8192` + compute buffers.

**Images MUST be downscaled to 1024px on the long edge** — done broker-side in
`_downscale` (Pillow), governed by `VISION_LOCAL_MAX_PX`. This is load-bearing,
not an optimization: at native resolution the vision encoder does not fit in
memory here.

### Two failed configurations, recorded so they aren't retried

**Native-resolution input + `--memory=3800m` + quantized KV cache (`--cache-type-k/v q8_0`):**
a single image ran past **600s without ever completing**, at 179-190% CPU — it
was not hung, it was thrashing. At 3.51GB against a 3.8GB cap with swap
disabled, the page cache for the 2.5GB mmap'd GGUF was being evicted and
re-read from disk continuously. The same image at 1024px with a 5600m limit
took 69s. **A too-small memory cap on an mmap-backed model does not OOM — it
silently runs ~10x slower.**

**`response_format: {"type":"json_schema","schema":{...}}`** — the flat form —
is accepted by llama-server and **silently ignored**: it answers with
free-form JSON whose keys have nothing to do with the schema. Caught in
production on the first live image, which came back with a `vision_type`
that is not in the enum at all. Only the OpenAI-style nesting,
`{"type":"json_schema","json_schema":{"schema":{...}}}`, actually constrains
decoding — verified by probing a single-value enum against the running
server: constrained under the nested form, invented keys under the flat one.
There is no error and no warning, so this can only be caught by asserting on
output, never by watching for a failure.

**`--parallel 2`** was not adopted: a second slot buys a second KV cache
(~1.1GB) this host does not have the RAM for.

### 2026-09-02: seven host-wide OOM kills in 24h, and what fixed what

llama-server was killed 7 times in 24h at 4.4-5.5GB anon-rss. Crucially these
were **global** OOM kills (`constraint=CONSTRAINT_NONE`, `global_oom`), NOT the
container's cgroup limit — it died below its own 5.47GiB cap because the HOST
ran out. `docker inspect` reports `OOMKilled: false` for exactly this reason.
The kernel picked llama-server every time on merit: its `oom_score` is 922
against 666-680 for everything else on the box, so tuning `oom_score_adj` would
change nothing.

**Where the memory actually is.** Measured under load: `RssAnon 3446MB` vs
`RssFile 595MB`. The main GGUF *is* mmap'd, but only ~455-591MB of its 2.36GB
stays resident and it thrashes (`workingset_refault_file` 979688 against 10847
for anon). The mmproj has **no mapping at all** — llama.cpp's multimodal loader
reads it into the heap, so all 836MB is unconditionally anonymous. On top of
that `--repack` (default: enabled) converts Q4_K tensors into AVX-friendly
layouts at load time, which means copying them into fresh anonymous buffers.
Hence `file-rss: 0kB` in the OOM records: by kill time there was no reclaimable
page cache left anywhere on the host.

**Adopted, both measured:**
- `-c 8192` -> `6144`. Returns ~288MB of KV cache. NOT 4096: across 183 real
  calls `tokens_in` was p50=851 / p90=979 / p99=1235 / **max=4451**, and
  `tokens_out` peaked at 341 — 4096 would truncate the largest document we have
  actually served.
- `--sleep-idle-seconds 900` -> `120`. Real gaps between bursts are 28-37 min,
  so the model already slept before nearly every burst; 900 bought almost no
  avoided reloads (16 sleeps against 8 reloads in one window; a wake costs
  ~16s) while holding ~5GB through idle windows on a host with ~1.4GB free.

**Rejected, and why — do not retry without new evidence:**
- `--no-repack`. It is the most direct fix for the mechanism above: the weights
  would stay as reclaimable file-backed mmap instead of ~3.3GB of anonymous
  private-dirty copies. But on the same image it ran **310s against 203s,
  +53%**. Against the 300s call timeout that aborts nearly every request.
  Repacking is bought with memory and sold as speed, and at a 130s average we
  have no speed to sell.
- mmproj F16 -> Q8_0 would save 364MiB straight out of anonymous memory, which
  is the right kind of memory to attack. But **no Q8_0 mmproj exists on the
  host** — it has to be produced first, and its accuracy cost on receipts and
  bank screens is unmeasured. Open, not rejected.
- Raising swap / tuning `vm.swappiness` (currently 10): no. The 4-5GB is
  anonymous memory actively read during inference; forcing it to swap trades an
  OOM for multi-second per-page stalls.
- Trimming other containers: nothing to take. All ~34 others total 1.8-2.2GB,
  postgres configs are stock, no restart loops, no duplicate services.

**Open question.** One real image (1920x2560 portrait document, 768x1024 after
downscale, ~768 vision tokens) failed to complete in 900s AND again in 700s,
while neighbours at 589824 pixels finished in 94-203s. A 4x+ time difference
for 33% more pixels is not explained by size. Unresolved; it is part of why
`local` books ~35 TimeoutErrors, which correctly fall through to gemini.

### 2026-09-04: the actual root cause — Docker was handing out host swap

The 09-02 note above got the mechanism right and the reason wrong. It said the
cgroup cap "never fired" without explaining why. Here is why:

**`mem_limit` without `memswap_limit` grants the container TWICE the limit in
swap.** Confirmed on the running container: `Memory=5872025600`,
`MemorySwap=11744051200` — exactly 2x. So llama-server honestly stayed inside
its 5600m of RAM (the cgroup was right to stay silent) while quietly draining
all 4GB of **host** swap. Only once that was gone did the kernel run out
globally and start picking victims across the whole box — 27 kills over 02-03.09,
with postgres as eligible a victim as llama-server.

`memswap_limit: 5600m` (equal to `mem_limit`) denies swap entirely. The cgroup
then reclaims its own page cache under pressure, and an over-limit request is
killed **inside its own cgroup**: one image fails, the container restarts, the
rest of the host is untouched. Verified live over 4 hours on 09-03:

| | before | after |
|---|---|---|
| host available RAM | 754 MB | **3562 MB** |
| host swap | 4095/4095 | 2539/4095, draining |
| OOM kills, host | 27 in 48h | **0** |
| OOM kills, cgroup | — | 0 |

**`mem_limit` is deliberately NOT lowered with it.** A limit is a ceiling, not
a reservation — the host is pressured by what the container actually uses
(~4.3GB), so trimming the ceiling frees nothing and only makes a cgroup kill
more likely. 3800m was already measured to thrash.

### 2026-09-04: mmproj F16 -> Q8_0

The vision encoder is a separate file from the language weights and llama.cpp's
multimodal loader **never mmaps it** — all 836MB lands in anonymous,
unreclaimable memory, precisely the memory that gets a process OOM-killed.
Halving its precision takes that back from the right pocket.

A/B over 10 real production images, same prompt and schema:

| | F16 | Q8_0 |
|---|---|---|
| peak RSS | 4.736 GiB | **4.289 GiB** (-447 MiB) |
| latency, mean | 192.4s | 183.8s (no real difference) |
| printed numbers | — | **identical on every image** |

Receipt 559,00 RUB, refund 1.207.152d, transactions 49,000.00, showtimes
18:00/20:00 — all matched, and neither model invented a number. 5 of 10 answers
were word-for-word identical.

Q8_0 is measurably coarser on **handwriting and fine print only**: a handwritten
"Вера" became "Вераида", and "Налог — 0,00 руб" was clipped to "Нал -". On one
image it was *better*, recovering a status line and shop name F16 missed.
Accepted because memory is the acute problem and printed figures — what this
provider exists to read — were untouched. `VISION_MMPROJ` switches back to the
F16 file without touching anything else.

Both models share one real defect, unrelated to quantisation: they read
Vietnamese dong `₫` as roubles `₽`.

### 2026-09-05: `memswap_limit` was real but did not SURVIVE

Swap was back at 99% two days later, with llama-server holding 1.5GB of it.
The setting had not been undone by any deploy — it was undone by systemd.

With the systemd cgroup driver, docker hands systemd the MEMORY limit as a
systemd property but writes the SWAP limit straight into the cgroup file,
behind systemd's back. `systemctl show` on the container scope shows the split
exactly:

    MemoryMax=5872025600        <- systemd knows it
    MemorySwapMax=infinity      <- systemd has never heard of it

Any `systemctl daemon-reload` then rewrites the attributes systemd manages and
resets the ones it does not, so `memory.swap.max` silently returns to `max`.
Proven directly on this host with a throwaway container:

    before daemon-reload:  memory.swap.max = 0
    after  daemon-reload:  memory.swap.max = max

Three reloads fired at 06:44 that morning — the kind any package install
triggers — and by 14:00 the swap alerts were back.

**The durable fix is a systemd slice.** `infra/aibroker-vision.slice` carries
`MemorySwapMax=0`; a slice is a real unit with a file on disk, so systemd owns
the property and restores it on every reload. cgroup v2 enforces swap limits
hierarchically — a descendant can never exceed an ancestor — so the container
cannot swap no matter what its own file says. `cgroup_parent` in compose puts
it there. Verified across a reload: the slice's `swap.max` stayed 0 both before
and after, while the container's own value drifted as before and no longer
mattered.

Install with `bash /var/www/aibroker/infra/install-host-units.sh`. It is not
part of the deploy on purpose — a deploy key must not be able to write systemd
units. If the slice is missing the container still starts; it simply falls back
to the fragile `memswap_limit`.

**General lesson for this box:** a container-level limit that docker writes
directly to a cgroup is not durable under the systemd driver. Anything that
must survive belongs on a systemd unit. Verify with `systemctl show <scope> -p
MemorySwapMax`, not with `docker inspect` — docker will happily report a limit
the kernel is no longer enforcing.

### While it runs

Available RAM drops to ~1.5GB and load average to ~3.5 on 4 cores. At
240-300 images/day that is 11-14h/day in that state. `mem_limit` is mandatory:
without one the kernel may pick postgres as the OOM victim rather than the
model.

### Peak-hour overflow is expected and correct

Distinct images per hour: median 10, p90 21, **peak 162** (00:00 UTC, every
day). One serialized worker clears ~50/hour, so in the peak hour the cloud tail
of the chain takes the overflow. That is the design, not a failure — `local`
returning nothing simply walks the chain to `gemini`.

## Local ASR retired (2026-10-04)

The self-hosted faster-whisper service (its own compose service, container
`aibroker-asr-local`) is gone, along with its `local` transcription provider
and the chat:fast proofreading pass that only served it. After a 15-clip
bake-off on real voice notes it was the slowest (16-84 s, one 180 s timeout)
and the worst in quality, while `gemini-3.5-transcribe` (1-3.4 s, most
faithful) now leads the transcription chain with groq as the fallback — see
`docs/routing.md`. Only `vision-local` stays self-hosted.

**One-time server cleanup after the deploy** (compose does not remove the
orphan container or image by itself):

```
cd /opt/aibroker   # the compose project directory
docker compose rm -sf asr-local || docker rm -f aibroker-asr-local
docker image rm $(docker images --filter 'reference=*asr-local*' -q | sort -u)
docker image prune -f
docker volume ls | grep -i -E 'whisper|asr'   # model cache, if any; docker volume rm <name>
docker stats --no-stream; free -h             # confirm the ~1.5GB cap / ~200MB resident is back
```

The `local` / `llm:audio` row in `api_keys` is now unused and can be
deactivated at leisure (`UPDATE api_keys SET is_active=false WHERE
provider='local' AND scope='llm:audio'`); nothing reads it.

## Connection scaling

> **Status 2026-07-16: PgBouncer is INSTALLED** (`aibroker-pgbouncer`,
> edoburu/pgbouncer, transaction pooling, DEFAULT_POOL_SIZE=15,
> MAX_CLIENT_CONN=200, MAX_PREPARED_STATEMENTS=500 so asyncpg's prepared
> statements survive pooling). `DATABASE_URL` on api/monitor points at
> `pgbouncer:6432`; `DIRECT_DATABASE_URL` keeps the deep-jobs LISTEN
> connection on `postgres:5432` — NOTIFY subscriptions need a pinned backend
> and silently die under transaction pooling. Rollback = point DATABASE_URL
> back at `postgres:5432` and redeploy; the app has no other coupling to the
> pooler. AUTH_TYPE=plain is confined to the compose-internal network (no
> published ports). (2026-07-16)

**How it runs today.** The `pgbouncer` service in `docker-compose.yml` sits
between the app containers and `postgres`:

- `POOL_MODE=transaction` — a server backend is held only for the duration
  of a transaction, so `DEFAULT_POOL_SIZE=15` real Postgres connections
  serve up to `MAX_CLIENT_CONN=200` client ones, `LISTEN_PORT=6432`.
- `MAX_PREPARED_STATEMENTS=500` (pgbouncer ≥ 1.21) lets asyncpg's
  protocol-level prepared statements survive transaction pooling — without
  it every SQLAlchemy statement re-prepares or errors under the pooler.
- `api` and `monitor` both get
  `DATABASE_URL=…@pgbouncer:6432/aibroker` (all pooled traffic) and
  `DIRECT_DATABASE_URL=…@postgres:5432/aibroker` — the one bypass, used
  only by the deep-jobs dispatcher's asyncpg LISTEN connection
  (`services/job_queue.py`): NOTIFY subscriptions need a pinned backend
  and would silently die under transaction pooling.
- **Rollback**: flip `DATABASE_URL` back to `postgres:5432` and redeploy.
  Nothing else in the app knows the pooler exists.

**Why (threshold history, kept as background).** `db/engine.py` runs
`pool_size=10 + max_overflow=20` = 30 connections max per process; 2
uvicorn workers in `api` → 60 max from the API alone, plus the `monitor`
container's engine (another 30 worst case) — uncomfortably close to
Postgres's default `max_connections = 100` when those were direct backend
connections. The documented threshold was "add PgBouncer when a second
broker node appears or the worker count doubles"; the 2026-07-16 scale
work (Redis shared state, NOTIFY dispatcher — the prep for a second node)
crossed that line, so the pooler went in with it. The app-side SQLAlchemy
pool is unchanged for the api (`DB_POOL_SIZE`/`DB_MAX_OVERFLOW`, 10/20) — its 30 per-process connections now terminate at
pgbouncer, which multiplexes them onto ~15 real backends. Session-level
caveats that shaped the layout: no advisory locks or LISTEN on pooled
connections (hence `DIRECT_DATABASE_URL`), `pool_pre_ping` stays on.

## Manual deploy fallback

```
ssh hetzner-root
cd /var/www/aibroker
git pull
docker compose build
docker compose up -d
```

## Secrets

### Server `.env` (at `/var/www/aibroker/.env`, mode 600)

| Var | Purpose |
|---|---|
| `POSTGRES_PASSWORD` | broker postgres root |
| `TOKEN_SECRET` | Fernet key for `api_keys.token_encrypted` |
| `ADMIN_KEY` | X-Admin-Key for `/admin/*` and dashboard fallback |
| `INTERNAL_SECRET` | reserved for monitor↔api auth (HMAC) |
| `SESSION_SECRET` | HMAC for browser session cookies |
| `TELEGRAM_BOT_TOKEN` | `@aibzapleo_bot` — sends alerts + signs login widget |
| `TELEGRAM_BOT_USERNAME` | for embedding the widget on `/login` |
| `OWNER_TELEGRAM_ID` | only this Telegram user can log into dashboard |
| `GLOBAL_DAILY_CAP_USD` | global daily spend cap |
| `SIGNUP_ENABLED` | kill switch for `POST /v1/signup` (default `true`) |
| `SIGNUP_PER_IP_PER_DAY` / `SIGNUP_PER_DAY` | self-signup rate limits (defaults 3 / 50, rolling 24h) |
| `SIGNUP_REQUEST_CAP` | lifetime request cap given to a new self-signup project (default 100) |
| `SIGNUP_DEFAULT_SCOPES` | comma-separated scopes of a self-signup project (default `llm:chat,llm:embed`) |
| `PUBLIC_HOST` | for absolute URLs on login page |
| `LOG_LEVEL` | INFO / DEBUG; applied by `aibroker.logging_setup.configure_logging` in the api AND in every agent worker (`run_worker`) - before 2026-10-14 the workers never configured logging and dropped all INFO |
| `AGENT_SUBSCRIPTION_IMAGE_OUTPUT_VERIFIED_MODELS` | JSON `{"codex-sub":[...]}` (api; default empty): verified Codex models whose live image generation was confirmed - the catalog lists them with `output_modalities` `["text", "image"]`; prod 2026-10-14: `gpt-6.1-sol` (PNG jobs 551063, 551214) |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | per-process SQLAlchemy pool (defaults 10 / 20, the api). `docker-compose.yml` sets 2 / 3 for `monitor` and the two Claude agent workers, which use the DB lightly, and 4 / 6 for `agent-worker-codex`, which runs `AGENT_CONCURRENCY_CODEX` (6-7) runs at once (each run: event writes, 1 s cancel poll, lease/auth-gate heartbeats, client-tool poll); read in `aibroker.db.engine.init_engine` |
| `AGENT_CODEX_REFRESH_PROOF_S` | codex worker (default 1800, range 0..3600): after a gated run proves the login at the current `last_refresh`, new Codex runs skip the `__codex_auth__` start-up gate this long; 0 = gate every due start-up. See [agent-runs](agent-runs.md) |
| `AGENT_CODEX_REFRESH_PROOF_MAX_AGE_S` | codex worker (default 561600 = 6.5 days, max 7 days): a proof never covers a `last_refresh` this old, so start-ups near Codex's own ~8-day refresh (UNVERIFIED) are gated again. Keep it + `AGENT_CODEX_REFRESH_PROOF_S` below that interval |
| `AGENT_IMAGES_TOTAL_BYTES` | bytes of all images of one run together, generated plus written to `out/` (agent workers, default 25165824 = 24 MiB; compose passes it to `agent-worker`, `agent-worker-claude2` and `agent-worker-codex`): an image that would pass it is listed in `files` with `skipped: "total_budget"` and no content |
| `AGENT_INTERACTIVE_RESERVE` | agent workers (default `1`, range 0-8; passed to the codex worker and both Claude workers in `docker-compose.yml`): slots kept for `priority: interactive` runs. When free slots <= this, the worker claims only interactive runs; the effective value is `min(this, concurrency - 1)`, so a one-slot lane reserves nothing and still runs batch work (`effective_interactive_reserve`). `0` turns the reservation off |
| `AGENT_TOOL_ARG_RETRIES` | schema-breaking client tool calls per Codex subscription run that are answered to the model as a rejected tool result instead of failing the run (default 2, range 0..5); see [subscription tool loop](subscription-tool-loop.md) |
| `AGENT_SUBSCRIPTION_CLAUDE_TOOLS_ENABLED` | api + Claude workers (default false): Claude subscription client tools over the stream-json control channel; see [subscription tool loop](subscription-tool-loop.md#claude-transport) |
| `AGENT_SUBSCRIPTION_CLAUDE_TOOLS_VERIFIED_MODELS` | JSON list of exact `claude-sub/<id>` routes advertised with `supports_tools: true` after a live round-trip (default `[]`) |
| `AGENT_SUBSCRIPTION_CLAUDE_TOOLS_CANARY_MODELS` | JSON list of exact `claude-sub/<id>` routes allowed for the canary projects only (`AGENT_SUBSCRIPTION_CODEX_TOOLS_CANARY_PROJECT_IDS`, default `[]`) |
| `AGENT_ACCOUNT_DISABLED_PAUSE_S` | api (default 3600, `0` = off): after a Claude run fails with `subscription_disabled`, new-run account selection skips that account for this many seconds (`agent/accounts.py`, [account selection](agent-runs.md)) |
| `AGENT_SUBSCRIPTION_TOOLS_ATTESTED_MODELS` | api (JSON list, default `["claude-sub/claude-sonnet-5-5"]`): canary routes with a recorded live client-tool round-trip; the catalog shows `supports_tools: true` for them to the canary projects. Add an id only with the job id of its live round-trip |
| `AGENT_TOOL_REQUIRED_REMINDERS` | follow-up turns per run that tell the model to call the required client tool after it finished with plain text (default 1, range 0..3) |

### GitHub Actions secrets (`zapleoceo/AIbroker`)

| Secret | Notes |
|---|---|
| `HETZNER_HOST` | origin server IP — value lives ONLY in the GitHub secret, never in this repo (see note below) |
| `HETZNER_PORT` | non-standard SSH port — same, secret-only |
| `HETZNER_SSH_KEY` | the restricted private key (`aibroker_gh_deploy`) |

| `TELEGRAM_BOT_TOKEN_VERA` | optional — the bot behind it sends the deploy-failure alerts |
| `OWNER_TELEGRAM_ID` | optional, chat the failure alert goes to |

> **Why the host/port are not written here (2026-07-24):** this repository is
> PUBLIC. The origin sits behind Cloudflare, so publishing the origin IP undoes
> that protection entirely — anyone can then reach nginx directly
> (`curl -H 'Host: aib.zapleo.com' http://<origin-ip>/…`) and bypass
> Cloudflare's WAF, DDoS protection and rate limiting, plus probe SSH on the
> non-standard port. The values were committed here previously, so they must be
> treated as permanently public (git history keeps them) — the durable fix is
> the firewall: allow :80 only from Cloudflare's published ranges, so a direct
> origin request is dropped even by someone who knows the IP.

## Rotating keys

- `ADMIN_KEY`: edit `.env`, `docker compose up -d --force-recreate api`.
- `TOKEN_SECRET`: don't rotate without a re-encryption migration — every
  row in `api_keys.token_encrypted` will become unreadable.
- `SESSION_SECRET`: rotate freely; users will be logged out once.
- `TELEGRAM_BOT_TOKEN`: rotate in BotFather, paste new token in `.env`,
  `up -d --force-recreate api monitor`.

## Health snapshot

- `https://aib.zapleo.com/healthz` — liveness
- `https://aib.zapleo.com/v1/health` — alive/cooldown/dead: one aggregate row for anonymous callers, per-provider rows with `X-Admin-Key` or the owner session
- Logs: `docker compose logs -f api` on the server.

## Disaster recovery

The Postgres volume (`aibroker_pgdata`) is the only persistent state. Backups
are made by vera3, not by this stack (see "Backups are made by vera3" above):
the nightly `pg_dump -Fc` lands in
`/var/backups/vera/aibroker/daily/<date>/aibroker.dump`, produced by vera3's
`vera-backup.sh`. Restore from the newest verified dump of that tree.

A manual dump is only an optional pre-migration snapshot:
```
ssh hetzner-root "docker exec aibroker-postgres pg_dump -U aibroker aibroker | gzip > /var/backups/aibroker-$(date +%F).sql.gz"
```
