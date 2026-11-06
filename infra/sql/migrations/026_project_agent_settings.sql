-- Order: apply BEFORE deploying the code that reads projects.agent_* (additive: three columns with
-- constant defaults; Postgres 11+ adds them without rewriting the table).
--   docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
--     < /var/www/aibroker/infra/sql/migrations/026_project_agent_settings.sql
--
-- Per-project agent access, edited with checkboxes on the dashboard project Settings tab and read
-- live by api and workers (agent/project_access.py, refreshed every few seconds), instead of only
-- the env AGENT_ALLOWED_PROJECT_IDS / AGENT_PROJECT_POLICY that need a container restart:
--   agent_enabled   the project may use agent runs (env allowlist still also admits it).
--   agent_models    exact subscription model ids (bare, e.g. "claude-opus-5-5") or "*".
--   agent_accounts  account labels in preference order (e.g. ["claude-2", "owner"]).
-- Non-empty models AND accounts replace the env AGENT_PROJECT_POLICY entry of that project.
--
-- LOCKING: ALTER TABLE takes ACCESS EXCLUSIVE on projects for milliseconds (no rewrite). projects
-- is read on every authenticated request, so lock_timeout makes it give up instead of queueing
-- behind a long transaction; on `lock timeout` nothing is applied: run it again (idempotent).
BEGIN;
SET LOCAL lock_timeout = '5s';
ALTER TABLE projects
  ADD COLUMN IF NOT EXISTS agent_enabled BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS agent_models JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS agent_accounts JSONB NOT NULL DEFAULT '[]'::jsonb;
COMMIT;
