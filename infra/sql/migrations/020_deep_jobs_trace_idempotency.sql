-- Apply BEFORE deploying the code that reads deep_jobs (every ORM select of DeepJobRow lists these columns):
--   docker exec -i aibroker-postgres psql -U aibroker aibroker \
--     < /var/www/aibroker/infra/sql/migrations/020_deep_jobs_trace_idempotency.sql
-- Run it with `psql -v ON_ERROR_STOP=1 -f` (autocommit per statement), NOT `psql -1` / a wrapping
-- transaction: the two CREATE INDEX CONCURRENTLY statements cannot run inside one. ON_ERROR_STOP makes a
-- failed ALTER abort the file instead of building indexes on columns that do not exist.
--
-- Job trace / idempotency / cancel (020). Additive only:
--   trace_id          client correlation id (POST /v1/jobs body `trace_id` or `X-Trace-Id`)
--   labels            client key/value labels (<= 8 keys), e.g. {"exam_run_id": "...", "case_id": "..."}
--   idempotency_key   client `Idempotency-Key`; unique per project while the row lives
--   cancel_requested  cooperative cancel flag for a RUNNING plain job (checked at attempt boundaries)
-- New terminal status value `cancelled` needs no DDL: deep_jobs.status is a plain VARCHAR(20), no CHECK/enum.
--
-- Cost: on PostgreSQL >= 11 `ADD COLUMN ... NOT NULL DEFAULT false` is metadata-only (no table rewrite);
-- the other three columns are nullable. lock_timeout makes the ALTERs fail fast instead of queueing behind a
-- long transaction and blocking every job read/write.
SET lock_timeout = '5s';
ALTER TABLE deep_jobs ADD COLUMN IF NOT EXISTS trace_id VARCHAR(128);
ALTER TABLE deep_jobs ADD COLUMN IF NOT EXISTS labels JSONB;
ALTER TABLE deep_jobs ADD COLUMN IF NOT EXISTS idempotency_key VARCHAR(128);
ALTER TABLE deep_jobs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT false;

-- lock_timeout must NOT cover the index builds: a CONCURRENTLY build that times out waiting for old
-- transactions leaves an INVALID index, which `IF NOT EXISTS` then silently skips on re-run.
RESET lock_timeout;
-- CONCURRENTLY: deep_jobs is moderately large and written on every submit/claim/finish; a plain build would
-- block those writes. Both indexes are partial (all existing rows have NULLs), so the builds are tiny.
-- If a build is interrupted it leaves an INVALID index that `IF NOT EXISTS` would then skip:
--   DROP INDEX CONCURRENTLY IF EXISTS ux_deep_jobs_idempotency;  (same for ix_deep_jobs_trace) and re-run.
-- Post-check (both rows must say indisvalid = t):
--   SELECT indexrelid::regclass, indisvalid FROM pg_index
--   WHERE indexrelid IN ('ux_deep_jobs_idempotency'::regclass, 'ix_deep_jobs_trace'::regclass);
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ux_deep_jobs_idempotency
  ON deep_jobs (project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deep_jobs_trace
  ON deep_jobs (project_id, trace_id) WHERE trace_id IS NOT NULL;
