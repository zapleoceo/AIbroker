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
| `daily_cost_cap_usd` / `monthly_cost_cap_usd` | NULL = no cap |
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
| `cooldown_until` | Set on 429 |
| `error_count` | Cleared on success ping by monitor |
| `last_used_at` | Drives LRU |
| `last_alive_check_at` | Drives monitor cadence |

### `leases`

Active checkouts in vending mode.

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

### `usage_log`

Append-only billing + analytics.

| Column | Notes |
|---|---|
| `api_key_id` | FK, NULL after key delete (SET NULL) |
| `project_id` | FK, NULL after project delete |
| `lease_id` | FK, only for vending mode |
| `provider` | Denormalized for fast queries |
| `model` | Actual model name from LiteLLM |
| `capability` | `chat:fast`, `chat:smart`, ... |
| `workflow` | Optional caller-provided tag (e.g. `triage`, `search`) |
| `tokens_in` / `tokens_out` | From provider response |
| `cache_read_tokens` / `cache_write_tokens` | Prompt-cache subset of `tokens_in` (migration 006). Anthropic only today — see `providers/litellm_adapter.py:apply_prompt_cache`. 0 for every other call. |
| `cost_usd` | LiteLLM-computed; cache-aware (a cache read prices at ~0.1x, a cache write at its real premium rate) |
| `latency_ms` | End-to-end |
| `status` | `ok` / `rate_limit` / `auth_fail` / `error` |
| `error_kind` | Exception class name |

Indexes on `(project_id, created_at)`, `(api_key_id, created_at)`,
`(provider, created_at)`, and a plain `(created_at)` (migration 005) cover the
dashboard queries — the last one for aggregates filtered on time alone
("calls in last 1h", "tokens today"), which the composite indexes can't serve
since none of them lead with `created_at`.

### `audit_log`

Append-only admin trail.

| Column | Notes |
|---|---|
| `actor` | `admin` / `project:<name>` / `tg:<user_id>` / `dashboard` |
| `action` | `project.create`, `key.create`, `vend`, `login.success`, ... |
| `target` | Free-form identifier |
| `metadata` | JSONB |
| `ip` | best-effort, from `X-Forwarded-For` or `client.host` |

Never mutated, never deleted. Manually prune older than 1 year if it ever matters.

## Migrations

`infra/sql/init.sql` runs once on Postgres first boot. After that, use
Alembic (`migrations/` — currently empty placeholder).
