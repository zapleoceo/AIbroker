-- Self-signup verification (2026-11-21): a self-signed-up project is "verified" once
-- its owner proved a real Telegram account (Telegram Login Widget, signed by the bot
-- token). Unverified projects run on SIGNUP_UNVERIFIED_REQUEST_CAP; verifying raises
-- the lifetime cap to SIGNUP_REQUEST_CAP. signup_telegram_id bounds projects per
-- Telegram account (SIGNUP_PER_TELEGRAM).
--
-- Both nullable: every existing project (self-signup or not) is unaffected.
-- Apply BEFORE deploying (the ORM selects these columns on every auth):
--   psql "$DATABASE_URL" -f infra/sql/migrations/033_projects_signup_verification.sql

ALTER TABLE projects ADD COLUMN IF NOT EXISTS signup_verified_at TIMESTAMP;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS signup_telegram_id BIGINT;
CREATE INDEX IF NOT EXISTS ix_projects_signup_telegram
  ON projects(signup_telegram_id) WHERE signup_telegram_id IS NOT NULL;
