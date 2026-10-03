-- Generalise the async-job table from chat:deep-only to ANY capability.
--
-- deep_jobs was built for chat:deep (nemotron's ~8-min latency exceeds the
-- nginx/Cloudflare read timeouts, so a sync HTTP response can't carry the
-- result — submit + poll instead). The same submit/poll pattern is now offered
-- for every chat capability via POST /v1/jobs?capability=X (roadmap Phase 4),
-- so clients can migrate off sync at their own pace with a guaranteed answer
-- and no held connection. The table name stays `deep_jobs` (renaming a live,
-- FK-referenced table is risky for zero real benefit) but it now holds jobs of
-- any capability — the new `capability` column records which.
--
-- Existing rows are all chat:deep by construction, so the DEFAULT backfills
-- them correctly. Idempotent; apply to existing prod DB:
--   psql "$DATABASE_URL" -f infra/sql/migrations/008_deep_jobs_capability.sql

ALTER TABLE deep_jobs
  ADD COLUMN IF NOT EXISTS capability VARCHAR(30) NOT NULL DEFAULT 'chat:deep';
