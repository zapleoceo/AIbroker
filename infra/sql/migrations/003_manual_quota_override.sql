-- Per-key manual quota override on 4 axes + split in/out tracking.
-- Manual values win over discovered_* (from headers) which win over
-- PROVIDER_QUOTAS defaults. NULL = "this axis not capped manually".
-- Idempotent; apply to existing prod DB:
--   psql "$DATABASE_URL" -f infra/sql/migrations/003_manual_quota_override.sql

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS manual_req_limit      BIGINT,
  ADD COLUMN IF NOT EXISTS manual_tok_limit      BIGINT,   -- total in+out
  ADD COLUMN IF NOT EXISTS manual_tok_in_limit   BIGINT,
  ADD COLUMN IF NOT EXISTS manual_tok_out_limit  BIGINT;
