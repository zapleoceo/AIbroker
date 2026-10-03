-- Self-signup (POST /v1/signup): a lifetime client-request cap per project, its
-- atomic counter, and the self-signup marker + origin IP (per-IP signup limit).
--
-- total_request_cap NULL = unlimited, so every existing project is unaffected.
-- total_requests_used counts ADMITTED client requests (one per request, not per
-- provider attempt); services/request_cap.py bumps it with a race-safe
--   UPDATE ... WHERE used < cap RETURNING.
--
-- Apply BEFORE deploying (the ORM selects these columns on every auth):
--   psql "$DATABASE_URL" -f infra/sql/migrations/016_projects_self_signup.sql

ALTER TABLE projects ADD COLUMN IF NOT EXISTS total_request_cap INTEGER;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS total_requests_used INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS self_signup BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS signup_ip VARCHAR(64);
CREATE INDEX IF NOT EXISTS ix_projects_self_signup_created
  ON projects(created_at) WHERE self_signup;
