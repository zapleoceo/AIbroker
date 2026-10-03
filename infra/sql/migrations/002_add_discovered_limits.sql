-- Discovered free-tier limits (parsed from provider response headers).
-- Falls back to PROVIDER_QUOTAS defaults when NULL.
-- Idempotent; apply to existing prod DB:
--   psql "$DATABASE_URL" -f infra/sql/migrations/002_add_discovered_limits.sql

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS discovered_req_limit BIGINT,
  ADD COLUMN IF NOT EXISTS discovered_tok_limit BIGINT,
  ADD COLUMN IF NOT EXISTS limits_discovered_at TIMESTAMP;
