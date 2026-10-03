-- Self-learned, provider-level facts derived from real responses.
-- Lets the broker stop relying on hardcoded constants as the SOLE source:
-- the code seed is used only until reality teaches us the real value.
-- Idempotent; apply to existing prod DB:
--   psql "$DATABASE_URL" -f infra/sql/migrations/004_provider_observations.sql

CREATE TABLE IF NOT EXISTS provider_observations (
    provider                  TEXT PRIMARY KEY,
    -- Smallest prompt size (estimated tokens) that the provider rejected as
    -- "too large" / 413 / context-exceeded. NULL until observed. The size
    -- filter uses min(this, code seed) so we self-calibrate per provider.
    learned_max_request_tokens BIGINT,
    learned_at                TIMESTAMP,
    sample_count              INTEGER NOT NULL DEFAULT 0
);
