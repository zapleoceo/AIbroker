-- Request trace: group every provider attempt of one client request.
--
-- usage_log has one row per ATTEMPT (each key tried, each fallback hop). Until now
-- nothing tied the rows of one request together, so a fallback trail
-- (groq 429 -> gemini ok) could only be reconstructed by timestamp guesswork.
-- request_id is set per client request (a uuid for sync endpoints, `job-<id>` for
-- queued jobs, shared by every dispatcher retry) and returned to the client in
-- the X-Request-Id response header.
--
-- Nullable: rows predating this migration keep NULL. record_usage degrades
-- gracefully if this is not applied (it retries the INSERT without the column and
-- warns once), but apply it BEFORE deploying:
--   psql "$DATABASE_URL" -f infra/sql/migrations/015_usage_log_request_id.sql
--
-- (014 is the per-(key, model) cooldown table, already applied on prod.)
--
-- The index is partial (NULL rows are the whole history) and non-concurrent here;
-- on the live table prefer running the CREATE INDEX line separately with
-- CONCURRENTLY outside a transaction.

ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS request_id VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_usage_request_id ON usage_log(request_id) WHERE request_id IS NOT NULL;
