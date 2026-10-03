-- Per-(key, model) cooldowns.
--
-- api_keys.cooldown_until parks a WHOLE key. Google meters the Gemini free tier
-- PER MODEL per key (20 requests/day/model): when one model's daily quota is
-- gone (429, quotaId "...PerDay...", retryDelay e.g. "39791s") the key's OTHER
-- models still work, but cooling the key parked all of them until the hint
-- expired. This table parks just the exhausted (key, model) pair; the selector
-- skips a key only when it is cooled for every model the request could use.
--
-- Rows are upserted with GREATEST semantics (a later, shorter cooldown never
-- shortens an earlier, longer one) and cascade away with their key. Expired
-- rows are harmless (filtered by cooldown_until > now()) and overwritten in
-- place by the next cooldown for the same pair.
--
-- Idempotent; apply BEFORE deploying the code that writes it:
--   psql "$DATABASE_URL" -f infra/sql/migrations/014_api_key_model_cooldowns.sql

CREATE TABLE IF NOT EXISTS api_key_model_cooldowns (
  api_key_id     BIGINT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
  model          VARCHAR(120) NOT NULL,
  cooldown_until TIMESTAMP NOT NULL,
  reason         VARCHAR(200),
  PRIMARY KEY (api_key_id, model)
);
