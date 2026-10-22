-- Order: apply BEFORE deploying the code that reads/writes deep_job_files (additive: one new table,
-- no change to deep_jobs or any existing column, no rewrite of deep_jobs).
--
-- LOCKING: the FK makes CREATE TABLE take SHARE ROW EXCLUSIVE on deep_jobs for the (millisecond)
-- duration of the transaction: writes to deep_jobs (submit/claim/finish) briefly wait, reads do not.
-- The file is ONE transaction with SET LOCAL lock_timeout = 5s, so it gives up instead of queueing
-- behind a long deep_jobs transaction (and, behind it, every other writer). On `lock timeout`
-- nothing is applied (ROLLBACK): just run it again a little later; it is idempotent (IF NOT EXISTS).
--   docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
--     < /var/www/aibroker/infra/sql/migrations/025_deep_job_files.sql
--
-- Raw bytes of the files an agent job produced (PNG from agent:codex), moved out of
-- deep_jobs.result_meta.files[].content_b64 (base64, +33%, pglz-incompressible). For NEW jobs
-- result_meta.files[i] keeps only metadata + "content_ref": i; the API still returns the same
-- JSON (content_b64 is encoded on read; null + expired after expires_at). Old rows keep their
-- inline content_b64 and are untouched.
--   job_id     cascade-deleted with the job, so the existing 7-day retention removes the bytes too.
--   data       STORAGE EXTERNAL: out-of-line TOAST without pglz (PNG does not compress; saves CPU).
--   expires_at informational copy of the file TTL (the API decides from result_meta, as before).
BEGIN;
SET LOCAL lock_timeout = '5s';   -- SET LOCAL: reset by COMMIT/ROLLBACK, like SET + RESET in 020/022/023
SET LOCAL statement_timeout = '60s';
CREATE TABLE IF NOT EXISTS deep_job_files (
  job_id BIGINT NOT NULL REFERENCES deep_jobs(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,
  sha256 VARCHAR(64) NOT NULL,
  mime VARCHAR(127) NOT NULL,
  size INTEGER NOT NULL,
  expires_at TIMESTAMP,
  data BYTEA NOT NULL,
  PRIMARY KEY (job_id, idx)
);
ALTER TABLE deep_job_files ALTER COLUMN data SET STORAGE EXTERNAL;
COMMIT;
--
-- ROLLOUT / ROLLBACK (docs/deploy-ops.md)
--   1. Apply this file, deploy the dual-read code with AGENT_FILES_IN_TABLE=false (writes stay inline).
--   2. Later set AGENT_FILES_IN_TABLE=true. Only png/jpeg/webp/gif go to the table
--      (already compressed: STORAGE EXTERNAL skips useless pglz); text-like files stay inline.
--   Rollback = set the flag to false; rows already in the table are still served until purged (7 days).
--   A plain revert to pre-025 code is safe only after those jobs are gone: the old code serves
--   content_b64: null for them (no `expired`).
--   The table is dropped only by a separate decision, after the above:
--        SET aibroker.allow_destructive_ddl = 'on';   -- migration 012 guard, session only
--        DROP TABLE deep_job_files;
