-- Job priority (023). Additive: one NOT NULL column with a constant DEFAULT (no table rewrite on
-- Postgres 11+), plus a claim-order index. Apply BEFORE deploying the code that writes it:
--   docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
--     < /var/www/aibroker/infra/sql/migrations/023_deep_jobs_priority.sql
-- priority: 0 = interactive, 1 = batch. Old rows and old clients are batch (the default).
-- Claimers (agent worker, LLM dispatcher) order by (priority, created_at).
-- ix_deep_jobs_claimable (009) is used only by those two claim scans, which now sort by priority,
-- so it is replaced by ix_deep_jobs_claim_order (status, priority, created_at); run_after stays a filter.
SET lock_timeout = '5s';
ALTER TABLE deep_jobs ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 1;
CREATE INDEX IF NOT EXISTS ix_deep_jobs_claim_order ON deep_jobs (status, priority, created_at);
DROP INDEX IF EXISTS ix_deep_jobs_claimable;
