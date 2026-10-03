-- Turn deep_jobs from fire-and-forget into a real drained QUEUE.
--
-- Before: submit did `asyncio.create_task(_run_job)` on the web worker — the
-- job died if that worker restarted (a deploy!), and there was no backpressure
-- (a flood of submits = a flood of concurrent provider calls). Now: submit
-- only ENQUEUES a pending row; a dispatcher loop (services/job_queue.py, one
-- per uvicorn worker, coordinated by FOR UPDATE SKIP LOCKED) claims pending
-- rows with bounded concurrency and drains them gradually. A job whose worker
-- died mid-run (status='running' past the stale window) is re-queued, and a
-- job that finds no capacity right now is re-queued with backoff instead of
-- failing — so a few minutes of broker downtime just delays answers, never
-- drops requests.
--
-- New columns:
--   started_at   — when a dispatcher claimed the row (status→'running'); used
--                  to detect a stale 'running' row (worker died) and re-queue.
--   retry_count  — how many times this job has been re-queued; capped so an
--                  impossible job eventually errors instead of looping forever.
--   run_after    — earliest time the job is eligible again (backoff); NULL =
--                  eligible now.
--
-- Idempotent; apply to existing prod DB BEFORE deploying the code:
--   psql "$DATABASE_URL" -f infra/sql/migrations/009_deep_jobs_queue.sql

ALTER TABLE deep_jobs
  ADD COLUMN IF NOT EXISTS started_at  TIMESTAMP,
  ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS run_after   TIMESTAMP;

-- The dispatcher's claim scans for eligible pending rows every ~1s; this keeps
-- it index-driven instead of a seq-scan as the table grows.
CREATE INDEX IF NOT EXISTS ix_deep_jobs_claimable
  ON deep_jobs (status, run_after, created_at);
