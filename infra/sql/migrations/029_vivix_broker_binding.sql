-- Apply after 028, before loading code that includes persistent holds in cost guards.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
ALTER TABLE vivix_jobs ADD COLUMN IF NOT EXISTS api_key_id BIGINT;
CREATE INDEX IF NOT EXISTS ix_vivix_jobs_api_key_id ON vivix_jobs(api_key_id);
COMMIT;
