-- Model compare page usage (022). Additive only: nullable columns on compare_results, no rewrite.
-- Apply BEFORE deploying the code that writes them:
--   docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
--     < /var/www/aibroker/infra/sql/migrations/022_compare_usage.sql
-- tokens_in / tokens_out / cost_usd / billing_type come from the job's result_meta; finish_reason
-- 'length' marks an answer cut off by the length limit (status 'truncated'). NULL = not reported.
SET lock_timeout = '5s';
ALTER TABLE compare_results ADD COLUMN IF NOT EXISTS tokens_in INTEGER;
ALTER TABLE compare_results ADD COLUMN IF NOT EXISTS tokens_out INTEGER;
ALTER TABLE compare_results ADD COLUMN IF NOT EXISTS cost_usd DOUBLE PRECISION;
ALTER TABLE compare_results ADD COLUMN IF NOT EXISTS finish_reason VARCHAR(32);
ALTER TABLE compare_results ADD COLUMN IF NOT EXISTS billing_type VARCHAR(20);
