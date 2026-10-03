# Domain model

Source of truth: `infra/sql/init.sql` + `src/aibroker/db/models.py` (they must mirror).

## Tables

### `projects`

Client apps. Each has its own scopes and cost caps.

| Column | Notes |
|---|---|
| `id` | BIGSERIAL PK |
| `name` | UNIQUE, lowercase, `^[a-z][a-z0-9_-]*$` |
| `owner_email` | Display only, no email is sent |
| `project_key_hash` | `sha256(plain)` hex |
| `project_key_prefix` | First 12 chars of plaintext, for ops display |
| `allowed_scopes` | JSONB array; routes check membership |
| `daily_cost_cap_usd` / `monthly_cost_cap_usd` | NULL = no cap; `0` = free providers only |
| `total_request_cap` / `total_requests_used` | Lifetime client-request cap and its atomic counter (migration 016); NULL cap = unlimited |
| `self_signup` / `signup_ip` | Created via `POST /v1/signup`; origin IP for the per-IP signup limit |
| `is_active` | Soft-disable |

### `api_keys`

The actual provider credentials.

| Column | Notes |
|---|---|
| `id` | BIGSERIAL PK |
| `provider` | `cerebras`, `gemini`, … |
| `label` | Free-form (the account name, e.g. `eatmeat`) |
| UNIQUE | `(provider, label)` |
| `tier` | `free` / `paid` / `trial` |
| `scopes` | JSONB array; selector filters by `scopes ? :scope` |
| `token_encrypted` | Fernet ciphertext |
| `is_active` / `is_alive` | `is_active` = ops toggle; `is_alive` = monitor-set |
| `daily_used` / `daily_limit` | Request counter; `daily_limit=0` = no limit |
| `daily_cost_used_usd` / `daily_cost_cap_usd` | Cap is NULL for free keys |
| `monthly_cost_used_usd` / `monthly_cost_cap_usd` | Monthly analogue of the daily pair; cap NULL = no monthly cap |
| `total_cost_usd` | Lifetime spend counter, never reset |
| `daily_reset_at` | Date the two `daily_*` counters above were last touched. Read/write always goes through `FRESH_DAILY_USED_SQL`/`FRESH_DAILY_COST_SQL` (`selector.py`), which treats a non-today value as 0 — self-healing, no cron reset job. See **Cost guard** in [routing.md](routing.md). |
| `is_reserve` | Reserved-lane flag: picked LAST within its (provider, scope) group. A key scoped only to `llm:edit` with `is_reserve=true` is the Coach safety net — used only when all shared edit keys are exhausted, invisible to bot `llm:chat` traffic |
| `cooldown_until` | Set on 429 |
| `error_count` | Cleared on success ping by monitor |
| `last_error` | Human-readable reason for the CURRENT dead/cooldown state (short provider message or probe hint — "no funds", "rate limit"), not a full error log. Cleared back to NULL the moment the key is confirmed alive again |
| `last_used_at` | Drives LRU |
| `last_alive_check_at` | Drives monitor cadence |
| `discovered_req_limit` / `discovered_tok_limit` / `limits_discovered_at` | Discovered free-tier limits, parsed from response rate-limit headers at first probe (key-create flows). NULL ⇒ fall back to `PROVIDER_QUOTAS` defaults in `providers/quotas.py` |
| `manual_req_limit` / `manual_tok_limit` / `manual_tok_in_limit` / `manual_tok_out_limit` | Manual per-key quota override — highest priority (manual > discovered > default). Set when the operator knows the real cap (e.g. a corporate Gemini key: 3M in / 80k out). NULL on an axis ⇒ defer down the chain |
| `account_id` | Non-secret per-provider config that rides along with the key. Only cloudflare needs it so far (account ID is embedded in its API URL, not a header litellm can take separately) — NULL for every other provider |
| `notes` | Free-form operator notes, default `''` |

### `leases`

Active checkouts of the REMOVED vending mode (2026-07-12) — kept as
historical data only; nothing writes here anymore.

| Column | Notes |
|---|---|
| `id` | `lse_<random>` |
| `api_key_id` | FK |
| `project_id` | FK |
| `lease_until` | Server time + DEFAULT_LEASE_SECONDS |
| `released_at` | NULL while active |

Expired-but-not-released leases are technically still in the table — no
cleanup job today. Acceptable because the row count grows ~linearly with
vend operations, not requests.

Indexed on `(project_id, leased_at)` (migration 007).

### `usage_log`

Billing + analytics; rows are never updated, only deleted by retention (120 days).

| Column | Notes |
|---|---|
| `api_key_id` | FK, NULL after key delete (SET NULL) |
| `project_id` | FK, NULL after project delete |
| `lease_id` | FK, historical (vending mode, removed 2026-07-12) |
| `provider` | Denormalized for fast queries |
| `model` | The **routing** name the broker asked for (e.g. `deepseek/deepseek-flash`, `local/qwen3vl`); prices the call and keys the by-model aggregates. The exact model that answered is in `model_served` |
| `model_served` | Exact served model id (migration 011), nullable — `NULL` when the routing name already is the exact id. Derived by `providers/model_identity.py:served_model`; see [api.md](api.md#model-vs-model_served-2026-09-13) |
| `capability` | `chat:fast`, `chat:smart`, ... |
| `workflow` | Optional caller-provided tag (e.g. `triage`, `search`) |
| `tokens_in` / `tokens_out` | From provider response |
| `cache_read_tokens` / `cache_write_tokens` | Prompt-cache subset of `tokens_in` (migration 006). Anthropic populates both via explicit `cache_control` marks (`providers/prompt_cache.py:apply_prompt_cache`); deepseek/cerebras/zai report automatic server-side cache reads (`prompt_tokens_details.cached_tokens` — write stays 0). Surfaced on the project-detail dashboard as the "Cache hit" KPI and a per-model hit-rate column (the health signal for the deepseek-backed lanes). |
| `cost_usd` | LiteLLM-computed; cache-aware (a cache read prices at ~0.1x, a cache write at its real premium rate) |
| `latency_ms` | End-to-end |
| `status` | `ok` / `rate_limit` / `auth_fail` / `error` |
| `error_kind` | Exception class name |
| `http_status` | The status the provider REALLY returned when the exception carries one (`http_status_of`), NULL otherwise (timeouts, network errors). Never inferred from our classification; the adaptive-backoff signal is `status = 'rate_limit'` instead |

`api_key_model_cooldowns (api_key_id, model, cooldown_until, reason)` (migration
014, row `ApiKeyModelCooldownRow`) holds per-(key, model) cooldowns for quotas metered
per model (Gemini's daily free tier); see `docs/routing.md`.

Indexes on `(project_id, created_at)`, `(api_key_id, created_at)`,
`(provider, created_at)`, and a plain `(created_at)` (migration 005) cover the
dashboard queries — the last one for aggregates filtered on time alone
("calls in last 1h", "tokens today"), which the composite indexes can't serve
since none of them lead with `created_at`.

### `audit_log`

Admin trail; rows are never updated, only deleted by retention (14 days for `cap_block`, 365 for the rest).

| Column | Notes |
|---|---|
| `actor` | `admin` / `project:<name>` / `tg:<user_id>` / `dashboard` |
| `action` | `project.create`, `key.create`, `cap_block`, `login.success`, ... (`vend` rows are historical — vending removed 2026-07-12) |
| `target` | Free-form identifier |
| `metadata` | JSONB |
| `ip` | best-effort, from `X-Forwarded-For` or `client.host` |

Rows are never updated. Retention (`purge_old_logs`, `services/job_queue.py`) deletes `cap_block` rows after 14 days and every other row after 365 days; `usage_log` rows are deleted after 120 days.

### `deep_jobs`

Async job queue — one row per `POST /v1/jobs` submit (built for `chat:deep`,
now generic over every chat capability; the table name stays for
continuity). Submit only enqueues (`pending`); a per-worker dispatcher
(`services/job_queue.py`) claims and runs rows. See **Async jobs** in
[architecture.md](architecture.md).

| Column | Notes |
|---|---|
| `id` | BIGSERIAL PK — the `job_id` clients poll |
| `project_id` | FK, CASCADE — a job belongs to exactly one project |
| `capability` | Which async-job capability this runs (default `chat:deep`) |
| `status` | `pending` / `running` / `done` / `error` |
| `request` | Full request payload, JSONB |
| `payload_hash` | md5 of `project:capability:canonical request JSON` (migration 010). Lets submit return an EXISTING in-flight job for an identical resubmit inside a 30-min window instead of enqueueing a duplicate — only `pending`/`running` jobs dedup; `done`/`error` never do (the client may legitimately retry after a failure). Nullable — rows predating the migration have none |
| `result_text` / `result_meta` | Set on `done` |
| `error_message` | Set on `error` |
| `retry_count` | Queue state (migration 009): bumped each requeue, errors past `JOB_MAX_RETRIES` |
| `run_after` | Earliest eligible time (exponential backoff); NULL = eligible now |
| `started_at` | When a dispatcher claimed the row (→ `running`); a row stuck past the stale window is re-queued by the next tick |
| `created_at` / `completed_at` | Lifecycle timestamps |

Indexes: `(project_id, created_at)` for polling,
`(status, run_after, created_at)` for the dispatcher's claim scan, and
`(project_id, capability, payload_hash, created_at DESC)` for the dedup
lookup (migration 010).

### `provider_observations`

Self-learned, provider-level facts from real responses — hardcoded
constants are only seeds, overridden once reality is observed.

| Column | Notes |
|---|---|
| `provider` | PK |
| `learned_max_request_tokens` | Observed request-size ceiling |
| `learned_at` | When last updated |
| `sample_count` | How many observations back the learned value |

## Migrations

Hand-applied idempotent `psql` files in `infra/sql/migrations/` — there is
no Alembic. `infra/sql/init.sql` runs once on Postgres first boot and
mirrors every migration for fresh-DB bootstrap. Apply a migration BEFORE
merging the code that depends on it — see **Schema migrations** in
[deploy-ops.md](deploy-ops.md).
