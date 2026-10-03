-- Prompt-cache token columns on usage_log.
--
-- call_llm already computed cache_read_tokens/cache_write_tokens (anthropic's
-- explicit prompt caching, added 2026-07-01) but discarded them — nothing
-- downstream persisted or surfaced cache activity. This adds storage so the
-- dashboard can show real cache hit/write totals per project/range.
--
-- ADD COLUMN ... DEFAULT is a metadata-only change on Postgres 11+ (no table
-- rewrite, no long lock) since the default is a constant. Idempotent; apply
-- to existing prod DB:
--   psql "$DATABASE_URL" -f infra/sql/migrations/006_usage_log_cache_tokens.sql

ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS cache_read_tokens  INTEGER NOT NULL DEFAULT 0;
ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS cache_write_tokens INTEGER NOT NULL DEFAULT 0;
