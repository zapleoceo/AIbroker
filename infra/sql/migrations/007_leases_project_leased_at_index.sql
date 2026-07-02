-- Index backing the per-project vending rate limit (routes/vending.py).
--
-- POST /v1/key had no rate limiting at all — a compromised or malicious
-- X-Project-Key could hammer it unboundedly, each call returning a REAL
-- plaintext provider token. The rate limit counts recent leases per project
-- (leases.leased_at, already written on every /v1/key call — no new table),
-- which needs an index to stay fast as the table grows.
--
-- CONCURRENTLY: avoids locking leases for the duration of the build. Must run
-- OUTSIDE a transaction block. Idempotent; apply to existing prod DB:
--   psql "$DATABASE_URL" -f infra/sql/migrations/007_leases_project_leased_at_index.sql

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_leases_project_leased_at
    ON leases (project_id, leased_at);
