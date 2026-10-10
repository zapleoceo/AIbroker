-- Agent runs: owner-only CLI workers driven by the deep_jobs queue (docs/agent-runs.md).
-- Additive; apply BEFORE deploying code that selects these tables:
--   psql "$DATABASE_URL" -f infra/sql/migrations/017_agent_runs.sql

-- Agent runs (017): side record + typed event log for `agent:*` deep_jobs rows.
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id BIGINT PRIMARY KEY REFERENCES deep_jobs(id) ON DELETE CASCADE,
  project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  capability VARCHAR(30) NOT NULL,
  task_id VARCHAR(128) NOT NULL,
  account_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(128),
  resume_session_id VARCHAR(128),
  idempotency_key VARCHAR(128),
  workspace TEXT,
  cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
  last_event_at TIMESTAMP,
  last_seq INTEGER NOT NULL DEFAULT 0,
  outcome VARCHAR(20),
  model VARCHAR(128),
  billing_mode VARCHAR(16),
  usage_source VARCHAR(24),
  usage_input_tokens INTEGER,
  usage_output_tokens INTEGER,
  usage_cache_read_tokens INTEGER,
  usage_turns INTEGER,
  cost_usd DOUBLE PRECISION
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_agent_runs_idem
  ON agent_runs(project_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_agent_runs_session ON agent_runs(project_id, account_id, session_id);
CREATE TABLE IF NOT EXISTS agent_run_events (
  run_id BIGINT NOT NULL REFERENCES deep_jobs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  ts TIMESTAMP NOT NULL,
  type VARCHAR(32) NOT NULL,
  payload JSONB NOT NULL,
  PRIMARY KEY (run_id, seq)
);
