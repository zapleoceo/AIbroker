-- Key tier per attempt: usage_log.key_tier records the tier ('free' | 'paid' | 'trial')
-- of the api key that served the attempt, copied from api_keys.tier at insert time, so
-- latency and cost by tier are visible without a join (and stay right if a key's tier
-- is changed later). Callers choose the tier per request with `key_tier` (2026-11-21).
--
-- Nullable: rows predating this migration keep NULL. record_usage degrades gracefully
-- if this is not applied (it retries the INSERT without the column and warns once),
-- but apply it BEFORE deploying:
--   psql "$DATABASE_URL" -f infra/sql/migrations/032_usage_log_key_tier.sql

ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS key_tier VARCHAR(10);
