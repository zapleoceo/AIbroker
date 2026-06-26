-- Reserved-lane support on api_keys. Idempotent; apply to existing prod DB:
--   psql "$DATABASE_URL" -f infra/sql/migrations/001_add_is_reserve.sql
-- Fresh installs get this from init.sql directly.

ALTER TABLE api_keys
  ADD COLUMN IF NOT EXISTS is_reserve BOOLEAN NOT NULL DEFAULT FALSE;

-- LRU index now orders reserve keys last within their group.
DROP INDEX IF EXISTS ix_api_keys_lru;
CREATE INDEX IF NOT EXISTS ix_api_keys_lru
  ON api_keys(is_reserve, last_used_at NULLS FIRST);
