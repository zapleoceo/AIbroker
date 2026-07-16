-- In-flight job dedup: kill client resubmit amplification.
--
-- Measured on prod (2026-07-16, Stepan / project 4): the client resubmits the
-- SAME vision payload up to 33 times (480 jobs/24h vs 156 distinct payloads),
-- and each job independently retries up to 8x in the dispatcher — up to ~260
-- provider attempts for ONE image. Fix is broker-side so clients need no
-- changes: `submit_job` hashes the request (md5 of project+capability+
-- canonical JSON) and, if an identical job is already in flight
-- (pending/running, < 30 min old), returns the EXISTING job_id instead of
-- enqueueing a duplicate. Done/error jobs never dedup — a retry after failure
-- stays legitimate.
--
-- Nullable column: rows predating this migration simply never match.
-- The code degrades gracefully if this migration hasn't been applied yet
-- (dedup SELECT failure → plain insert), but apply it BEFORE merging:
--   psql "$DATABASE_URL" -f infra/sql/migrations/010_deep_jobs_payload_hash.sql

ALTER TABLE deep_jobs ADD COLUMN IF NOT EXISTS payload_hash VARCHAR(32);

-- Backs the dedup lookup in submit_job (project+capability+hash, newest first).
CREATE INDEX IF NOT EXISTS ix_deep_jobs_dedup
  ON deep_jobs (project_id, capability, payload_hash, created_at DESC);
