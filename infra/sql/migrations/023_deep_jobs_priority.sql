-- Job priority (023). Additive: one NOT NULL column with a constant DEFAULT (metadata-only on
-- Postgres >= 11, no table rewrite) plus a claim-order index built CONCURRENTLY.
-- Order: apply this BEFORE deploying the code (the ORM names the column, so new code fails on a DB without it;
-- see "Migrations vs pgbouncer prepared statements" in docs/deploy-ops.md). Run it with
--   docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
--     < /var/www/aibroker/infra/sql/migrations/023_deep_jobs_priority.sql
-- i.e. psql autocommit per statement, NOT `psql -1` / a wrapping transaction: CREATE/DROP INDEX
-- CONCURRENTLY cannot run inside one (same rule as 020). The runner is a manual psql call, there is no
-- migration framework that wraps files in a transaction.
--
-- priority: 0 = interactive, 1 = batch. Old rows and old clients are batch (the default).
-- Claimers (agent worker, LLM dispatcher) order by (priority, created_at); run_after stays a filter,
-- not an index column (it is a range/OR condition and would not give index order).
-- ix_deep_jobs_claimable (009) is used only by those two claim scans, so it is replaced by
-- ix_deep_jobs_claim_order (status, priority, created_at) and dropped last.
-- Mixed-version window: code from before this change ignores `priority` (it claims by created_at only and
-- its explicit column lists tolerate the new column), so old workers keep working between the migration and
-- the deploy; they just do not honour priority until replaced.
--
-- If the index build is interrupted it leaves an INVALID index that `IF NOT EXISTS` then skips:
--   DROP INDEX CONCURRENTLY IF EXISTS ix_deep_jobs_claim_order;  and re-run this file.
-- Post-check (indisvalid must be t):
--   SELECT indexrelid::regclass, indisvalid FROM pg_index WHERE indexrelid = 'ix_deep_jobs_claim_order'::regclass;
-- Rollback: CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deep_jobs_claimable ON deep_jobs (status, run_after, created_at);
--   DROP INDEX CONCURRENTLY IF EXISTS ix_deep_jobs_claim_order; ALTER TABLE deep_jobs DROP COLUMN priority;
SET lock_timeout = '5s';
ALTER TABLE deep_jobs ADD COLUMN IF NOT EXISTS priority SMALLINT NOT NULL DEFAULT 1;

-- lock_timeout must NOT cover the index build: a CONCURRENTLY build that times out waiting for old
-- transactions leaves an INVALID index (see above).
RESET lock_timeout;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deep_jobs_claim_order ON deep_jobs (status, priority, created_at);
-- Only after the new index is valid (post-check above); dropping first would leave claims without an index.
DROP INDEX CONCURRENTLY IF EXISTS ix_deep_jobs_claimable;
