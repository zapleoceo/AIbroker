-- Order: apply any time after 020; purely additive indexes, no column changes, no code dependency.
--   docker exec -i aibroker-postgres psql -v ON_ERROR_STOP=1 -U aibroker aibroker \
--     < /var/www/aibroker/infra/sql/migrations/024_deep_jobs_list_indexes.sql
-- Run it with `psql -v ON_ERROR_STOP=1 -f` (autocommit per statement), NOT `psql -1` / a wrapping
-- transaction: CREATE INDEX CONCURRENTLY cannot run inside one. Each index is its own statement.
--
-- Indexes for GET /v1/jobs?label.<k>=<v> (services/job_list.py):
--   ix_deep_jobs_project_id   (project_id, id): the keyset walk `project_id = ? AND id > ? AND id <= ?
--                             ORDER BY id` and the per-project max(id) watermark.
--   ix_deep_jobs_labels_gin   GIN (labels jsonb_path_ops): the `labels @> '{"k":"v"}'` containment match.
--                             jsonb_path_ops supports only `@>` (smaller and faster than the default class),
--                             which is the only operator the list uses.
--
-- Cost: both builds are CONCURRENTLY (no write lock on deep_jobs, which is written on every submit/claim/
-- finish) but scan the whole table and add write amplification on insert/update of labels. There is no
-- DDL needing a lock_timeout here, so none is set (unlike 020's ALTERs).
-- If a build is interrupted it leaves an INVALID index that `IF NOT EXISTS` then silently skips:
--   DROP INDEX CONCURRENTLY IF EXISTS ix_deep_jobs_project_id;  (same for ix_deep_jobs_labels_gin) and re-run.
-- Post-check (both rows must say indisvalid = t):
--   SELECT indexrelid::regclass, indisvalid FROM pg_index
--   WHERE indexrelid IN ('ix_deep_jobs_project_id'::regclass, 'ix_deep_jobs_labels_gin'::regclass);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deep_jobs_project_id
  ON deep_jobs (project_id, id);
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_deep_jobs_labels_gin
  ON deep_jobs USING gin (labels jsonb_path_ops);
