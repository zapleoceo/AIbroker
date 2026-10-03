-- The exact model that answered, next to the one the router asked for.
--
-- usage_log.model holds the ROUTING name (`deepseek/deepseek-flash`,
-- `local/qwen3vl`). It prices the call and keys every by-model aggregate, so
-- it must stay as it is — but it does not always name the model that ran:
-- DeepSeek answers the retired `deepseek-v4-flash`/`deepseek-v4-pro` names
-- with V4.1-Flash, and `local/qwen3vl` is a label this broker invented that
-- never leaves it. Asked by the owner (2026-09-13): the log must say which
-- model actually served the request, and the client must get it too.
--
-- Nullable: rows predating this migration keep showing the routing name, and
-- so do calls where we have nothing more precise than it (most cloud models —
-- their routing name IS the exact model id). See providers/model_identity.py.
--
-- record_usage degrades gracefully if this hasn't been applied (it retries the
-- INSERT without the column and warns once), but the project-detail page
-- SELECTs it, so apply it BEFORE deploying:
--   psql "$DATABASE_URL" -f infra/sql/migrations/011_usage_log_model_served.sql

ALTER TABLE usage_log ADD COLUMN IF NOT EXISTS model_served VARCHAR(120);
