-- Staged, disabled Vivix ledger. No provider activation, credentials or grants.
-- Apply before any future service wiring. Never purge unresolved holds with job retention.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE IF NOT EXISTS vivix_ledger_lock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL DEFAULT 0
);
INSERT INTO vivix_ledger_lock(id, version) VALUES (1, 0) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS vivix_jobs (
  id VARCHAR(128) PRIMARY KEY,
  project_id VARCHAR(128) NOT NULL,
  account_id VARCHAR(128) NOT NULL,
  version INTEGER NOT NULL,
  reserved_usd NUMERIC(24,8) NOT NULL CHECK (reserved_usd >= 0),
  settled_usd NUMERIC(24,8) NOT NULL CHECK (settled_usd >= 0),
  body JSON NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vivix_jobs_project_id ON vivix_jobs(project_id);
CREATE INDEX IF NOT EXISTS ix_vivix_jobs_account_id ON vivix_jobs(account_id);
COMMIT;
