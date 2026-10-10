-- Apply BEFORE deploying code that takes session leases:
--   psql "$DATABASE_URL" -f infra/sql/migrations/018_agent_session_leases.sql
-- Agent session leases (018): replaces the Postgres advisory lock, so the agent worker needs only
-- the pgbouncer connection (no direct connection, no session-level lock). Additive.
CREATE TABLE IF NOT EXISTS agent_session_leases (
  account_id VARCHAR(64) NOT NULL,
  session_id VARCHAR(128) NOT NULL,
  run_id BIGINT NOT NULL,
  lease_until TIMESTAMP NOT NULL,
  heartbeat_at TIMESTAMP NOT NULL,
  fence BIGINT NOT NULL DEFAULT 1,   -- +1 per acquisition; writes of a run carry it (fencing)
  PRIMARY KEY (account_id, session_id)
);
