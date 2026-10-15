-- Model compare page (021). Additive only: two new tables, no existing table is touched.
-- Apply BEFORE deploying the code that reads them:
--   docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
--     < /var/www/aibroker/infra/sql/migrations/021_compare_page.sql
-- Model compare page (021): one row per submitted prompt, one per (prompt, model) answer.
CREATE TABLE IF NOT EXISTS compare_requests (
  id BIGSERIAL PRIMARY KEY,
  prompt TEXT NOT NULL,
  browser_id VARCHAR(64) NOT NULL,                -- anonymous httpOnly-cookie id (one question at a time)
  client_ip VARCHAR(64) NOT NULL DEFAULT '',      -- same guard, per IP
  created_at TIMESTAMP NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_compare_requests_created ON compare_requests(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_compare_requests_browser ON compare_requests(browser_id, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_compare_requests_ip ON compare_requests(client_ip, created_at DESC);
CREATE TABLE IF NOT EXISTS compare_results (
  id BIGSERIAL PRIMARY KEY,
  request_id BIGINT NOT NULL REFERENCES compare_requests(id) ON DELETE CASCADE,
  model_id VARCHAR(200) NOT NULL,                 -- the exact catalog id that was pinned
  capability VARCHAR(30) NOT NULL,
  intelligence_index DOUBLE PRECISION,            -- AA score at submit time; NULL = unknown
  job_id BIGINT,                                  -- deep_jobs.id; NULL when the submit was refused
  status VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|done|error|refused
  answer TEXT,
  error TEXT,
  model_served VARCHAR(200),                      -- NULL = unknown
  latency_ms INTEGER,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  completed_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_compare_results_request ON compare_results(request_id);
