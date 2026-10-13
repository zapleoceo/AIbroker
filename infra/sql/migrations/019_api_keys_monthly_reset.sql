-- Apply BEFORE deploying code that enforces monthly cost caps:
--   psql "$DATABASE_URL" -f infra/sql/migrations/019_api_keys_monthly_reset.sql
-- Monthly cap enforcement (019): monthly_reset_at holds the first day of the UTC month that
-- api_keys.monthly_cost_used_usd belongs to. A NULL/older value reads as 0 (lazy reset, same idea
-- as daily_reset_at), so the pre-existing never-reset lifetime sum is dropped on first touch.
-- Additive, nullable, no backfill.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS monthly_reset_at DATE;
