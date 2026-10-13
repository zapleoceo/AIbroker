-- Apply BEFORE deploying code that enforces monthly cost caps:
--   psql "$DATABASE_URL" -f infra/sql/migrations/019_api_keys_monthly_reset.sql
-- Monthly cap enforcement (019): monthly_reset_at holds the first day of the UTC month that
-- api_keys.monthly_cost_used_usd belongs to. A NULL/older value reads as 0 (lazy reset, same idea
-- as daily_reset_at). Additive, nullable.
-- Backfill (one-time, idempotent via the monthly_reset_at IS NULL guard): a key that already has a
-- monthly cap gets its CURRENT UTC month's real spend from usage_log (created_at is naive UTC), so
-- it keeps its month-to-date spend instead of restarting at 0. Keys without a cap are left NULL.
SET lock_timeout = '5s';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS monthly_reset_at DATE;
UPDATE api_keys k
SET monthly_cost_used_usd = COALESCE((
      SELECT SUM(cost_usd) FROM usage_log u
      WHERE u.api_key_id = k.id
        AND u.created_at >= date_trunc('month', now() AT TIME ZONE 'UTC')), 0),
    monthly_reset_at = (date_trunc('month', now() AT TIME ZONE 'UTC'))::date
WHERE k.monthly_cost_cap_usd IS NOT NULL AND k.monthly_reset_at IS NULL;
