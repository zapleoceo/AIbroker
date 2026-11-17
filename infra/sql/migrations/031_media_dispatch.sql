-- LOCAL ONLY: stop/drain ALL old workers before migration and new code rollout.
-- Existing nonterminal Gemini video jobs have no trustworthy dispatch history.
-- Fail closed: quarantine them for explicit reconciliation; never infer no POST.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';
CREATE TABLE IF NOT EXISTS media_dispatch (
  job_id BIGINT PRIMARY KEY,
  project_id BIGINT NOT NULL,
  fingerprint VARCHAR(64) NOT NULL,
  phase VARCHAR(16) NOT NULL CHECK (phase IN ('prepared','submitting','submitted','settled','unknown')),
  claim TIMESTAMP,
  key_id BIGINT,
  account_id VARCHAR(255),
  key_fingerprint VARCHAR(64),
  reservation_id VARCHAR(36),
  model VARCHAR(200),
  provider_id VARCHAR(255),
  usage_id BIGINT
);
CREATE INDEX IF NOT EXISTS ix_media_dispatch_project_id ON media_dispatch(project_id);
INSERT INTO media_dispatch(job_id,project_id,fingerprint,phase,claim)
SELECT id,project_id,'legacy-unverified','unknown',started_at FROM deep_jobs
WHERE capability='video' AND status IN ('pending','running')
  AND NOT (request::jsonb ? 'vivix')
ON CONFLICT (job_id) DO NOTHING;
UPDATE deep_jobs SET status='error',completed_at=NULL,
 error_message='media outcome unknown after upgrade, automatic resubmit prohibited'
WHERE id IN (SELECT job_id FROM media_dispatch WHERE fingerprint='legacy-unverified');
COMMIT;
