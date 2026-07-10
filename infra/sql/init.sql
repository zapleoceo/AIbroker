-- Full broker schema. Idempotent. Runs on first postgres start via
-- /docker-entrypoint-initdb.d, and is the disaster-recovery source of truth for
-- a fresh volume — so it MUST stay in sync with infra/sql/migrations/*.sql.
-- There is no Alembic; migrations are hand-applied via psql on prod, and every
-- one of them is folded in below. When you add a migration, fold it in here too.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ─── Projects (client apps that consume the broker) ─────────────────────────
CREATE TABLE IF NOT EXISTS projects (
  id BIGSERIAL PRIMARY KEY,
  name VARCHAR(100) NOT NULL UNIQUE,
  owner_email VARCHAR(255),
  project_key_hash VARCHAR(255) NOT NULL,   -- bcrypt of X-Project-Key
  project_key_prefix VARCHAR(20) NOT NULL,  -- first chars for ops display
  allowed_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
  daily_cost_cap_usd DOUBLE PRECISION,
  monthly_cost_cap_usd DOUBLE PRECISION,
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  notes TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_projects_active ON projects(is_active);

-- ─── API keys (the actual provider credentials) ─────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
  id BIGSERIAL PRIMARY KEY,
  provider VARCHAR(50) NOT NULL,
  label VARCHAR(100) NOT NULL,
  tier VARCHAR(10) NOT NULL DEFAULT 'free',   -- free|paid|trial
  scopes JSONB NOT NULL DEFAULT '[]'::jsonb,  -- ['llm:chat','llm:embed',...]
  token_encrypted TEXT NOT NULL,              -- Fernet
  is_active BOOLEAN NOT NULL DEFAULT TRUE,
  is_alive BOOLEAN NOT NULL DEFAULT TRUE,     -- set by monitor
  is_reserve BOOLEAN NOT NULL DEFAULT FALSE,  -- reserved lane: picked last in its group
  daily_limit INT NOT NULL DEFAULT 999999,
  daily_used INT NOT NULL DEFAULT 0,
  daily_cost_cap_usd DOUBLE PRECISION,
  daily_cost_used_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  monthly_cost_cap_usd DOUBLE PRECISION,
  monthly_cost_used_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  total_cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  daily_reset_at DATE,
  cooldown_until TIMESTAMP,
  error_count INT NOT NULL DEFAULT 0,
  last_used_at TIMESTAMP,
  last_alive_check_at TIMESTAMP,
  last_error VARCHAR(200),  -- why the key is currently dead/cooldown ("no funds", etc.)
  account_id VARCHAR(64),  -- non-secret provider config (e.g. cloudflare account ID)
  notes TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  CONSTRAINT uq_api_keys_provider_label UNIQUE (provider, label)
);
CREATE INDEX IF NOT EXISTS ix_api_keys_provider_active ON api_keys(provider, is_active, is_alive);
CREATE INDEX IF NOT EXISTS ix_api_keys_lru ON api_keys(is_reserve, last_used_at NULLS FIRST);

-- ─── Active leases (vending mode: key checked out, not yet reported) ────────
CREATE TABLE IF NOT EXISTS leases (
  id VARCHAR(64) PRIMARY KEY,                 -- lse_<random>
  api_key_id BIGINT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow VARCHAR(50),
  request_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
  leased_at TIMESTAMP NOT NULL DEFAULT now(),
  lease_until TIMESTAMP NOT NULL,
  released_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_leases_open ON leases(lease_until) WHERE released_at IS NULL;
CREATE INDEX IF NOT EXISTS ix_leases_project ON leases(project_id, leased_at);

-- ─── Usage log (every successful + failed call, source of truth for billing)─
CREATE TABLE IF NOT EXISTS usage_log (
  id BIGSERIAL PRIMARY KEY,
  api_key_id BIGINT REFERENCES api_keys(id) ON DELETE SET NULL,
  project_id BIGINT REFERENCES projects(id) ON DELETE SET NULL,
  lease_id VARCHAR(64) REFERENCES leases(id) ON DELETE SET NULL,
  provider VARCHAR(50) NOT NULL,
  model VARCHAR(100),
  capability VARCHAR(30),
  workflow VARCHAR(50),
  tokens_in INT NOT NULL DEFAULT 0,
  tokens_out INT NOT NULL DEFAULT 0,
  cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
  latency_ms INT,
  status VARCHAR(20) NOT NULL,                -- ok|rate_limit|auth_fail|error
  error_kind VARCHAR(80),
  http_status INT,
  created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_usage_project_date ON usage_log(project_id, created_at);
CREATE INDEX IF NOT EXISTS ix_usage_key_date ON usage_log(api_key_id, created_at);
CREATE INDEX IF NOT EXISTS ix_usage_provider_date ON usage_log(provider, created_at);

-- ─── Audit log (every admin op, every key checkout) ─────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
  id BIGSERIAL PRIMARY KEY,
  actor VARCHAR(100) NOT NULL,                -- 'admin' or 'project:<name>'
  action VARCHAR(50) NOT NULL,
  target VARCHAR(120),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  ip VARCHAR(45),
  created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_audit_actor_date ON audit_log(actor, created_at);
CREATE INDEX IF NOT EXISTS ix_audit_action_date ON audit_log(action, created_at);

-- ─── Async jobs (submit + poll). Built for chat:deep, now generic over any ──
-- chat capability (POST /v1/jobs?capability=X) — see services/deep_jobs.py.
-- Table name kept as `deep_jobs`; `capability` records the real type.
CREATE TABLE IF NOT EXISTS deep_jobs (
  id BIGSERIAL PRIMARY KEY,
  project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  capability VARCHAR(30) NOT NULL DEFAULT 'chat:deep',
  status VARCHAR(20) NOT NULL DEFAULT 'pending',   -- pending|running|done|error
  request JSONB NOT NULL,
  result_text TEXT,
  result_meta JSONB,
  error_message TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0,
  run_after TIMESTAMP,          -- earliest eligible time (backoff); NULL = now
  started_at TIMESTAMP,         -- when a dispatcher claimed it (→ running)
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_deep_jobs_project_date ON deep_jobs(project_id, created_at);
-- Dispatcher claim scan (services/job_queue.py): eligible pending rows.
CREATE INDEX IF NOT EXISTS ix_deep_jobs_claimable ON deep_jobs(status, run_after, created_at);

-- ─── Folded-in migrations (kept in sync with infra/sql/migrations/) ─────────
-- 002 discovered free-tier limits (parsed from provider response headers)
-- 003 per-key manual quota overrides (4 axes; win over discovered_* / defaults)
ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS discovered_req_limit  BIGINT,
  ADD COLUMN IF NOT EXISTS discovered_tok_limit  BIGINT,
  ADD COLUMN IF NOT EXISTS limits_discovered_at  TIMESTAMP,
  ADD COLUMN IF NOT EXISTS manual_req_limit      BIGINT,
  ADD COLUMN IF NOT EXISTS manual_tok_limit      BIGINT,
  ADD COLUMN IF NOT EXISTS manual_tok_in_limit   BIGINT,
  ADD COLUMN IF NOT EXISTS manual_tok_out_limit  BIGINT;

-- 006 prompt-cache token columns on usage_log
ALTER TABLE usage_log
  ADD COLUMN IF NOT EXISTS cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0;

-- 004 self-learned provider-level facts (size ceiling, etc.)
CREATE TABLE IF NOT EXISTS provider_observations (
  provider                   TEXT PRIMARY KEY,
  learned_max_request_tokens BIGINT,
  learned_at                 TIMESTAMP,
  sample_count               INTEGER NOT NULL DEFAULT 0
);

-- 005 plain btree on usage_log.created_at (created_at-only dashboard queries).
-- 007 index backing the per-project vending rate limit.
-- No CONCURRENTLY here: init runs against an empty DB (nothing to lock) and
-- CONCURRENTLY can't run inside the init transaction. The migration files use
-- CONCURRENTLY because they apply to a live, populated prod table.
CREATE INDEX IF NOT EXISTS ix_usage_created_at ON usage_log (created_at);
CREATE INDEX IF NOT EXISTS ix_leases_project_leased_at ON leases (project_id, leased_at);
