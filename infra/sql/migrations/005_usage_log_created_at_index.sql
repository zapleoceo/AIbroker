-- Plain btree index on usage_log.created_at.
--
-- The three existing composite indexes ((project_id, created_at),
-- (api_key_id, created_at), (provider, created_at)) all lead with a
-- different column, so a query filtering ONLY on created_at (dashboard
-- "calls in last 1h", "tokens today" per-key) couldn't use any of them and
-- fell back to a full sequential scan of the whole table (451k+ rows and
-- growing). Combined with dropping the created_at::date casts (which are
-- non-sargable even against an index) in routes/dashboard.py, this turns
-- those scans into fast index range scans over just the recent slice.
--
-- CONCURRENTLY: avoids locking usage_log (append-only, high write volume)
-- for the duration of the build. Must run OUTSIDE a transaction block.
-- Idempotent; apply to existing prod DB:
--   psql "$DATABASE_URL" -f infra/sql/migrations/005_usage_log_created_at_index.sql

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_usage_created_at
    ON usage_log (created_at);
