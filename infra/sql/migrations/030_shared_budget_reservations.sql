-- Drain old workers before deploying: legacy in-flight estimates have no identity.
-- No automatic expiry/deletion: unresolved holds require explicit reconciliation.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE IF NOT EXISTS budget_ledger_lock (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  version INTEGER NOT NULL DEFAULT 0
);
INSERT INTO budget_ledger_lock(id, version) VALUES (1, 0) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS budget_reservations (
  id VARCHAR(36) PRIMARY KEY,
  api_key_id BIGINT NOT NULL,
  project_id BIGINT NOT NULL,
  amount NUMERIC(24,8) NOT NULL CHECK (amount > 0),
  day DATE NOT NULL,
  month DATE NOT NULL,
  debit_daily BOOLEAN NOT NULL,
  debit_monthly BOOLEAN NOT NULL,
  state VARCHAR(16) NOT NULL CHECK (state IN ('held', 'settled', 'released')),
  usage_id BIGINT,
  fingerprint VARCHAR(64)
);
CREATE INDEX IF NOT EXISTS ix_budget_reservations_api_key_id ON budget_reservations(api_key_id);
CREATE INDEX IF NOT EXISTS ix_budget_reservations_project_id ON budget_reservations(project_id);
CREATE INDEX IF NOT EXISTS ix_budget_reservations_state ON budget_reservations(state);
COMMIT;
