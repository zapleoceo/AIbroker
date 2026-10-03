-- Least-privilege app role: DML only, no ownership, no DDL.
--
-- Why: the 2026-10-03 wipe ran as the table-owning superuser. A role that
-- neither owns the tables nor is superuser cannot DROP/ALTER/TRUNCATE them,
-- whatever process connects with its credentials (a stray pytest included).
--
-- Roles (see docs/runbooks/db-roles.md):
--   aibroker      migration/owner role (POSTGRES_USER, owns the schema) - unchanged
--   aibroker_app  the role the app connects as (created by hand with a password;
--                 this file never contains secrets)
--
-- Run as the owner role. If aibroker_app does not exist yet the block skips
-- with a NOTICE, so this is safe in init.sql on a fresh DB and idempotent
-- to re-run. Applying it does NOT switch the app: that is the runbook's step.
DO $roles$
DECLARE
  app_role constant text := 'aibroker_app';
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = app_role) THEN
    RAISE NOTICE 'aibroker: role % missing, skipping app grants', app_role;
    RETURN;
  END IF;

  EXECUTE format('GRANT USAGE ON SCHEMA public TO %I', app_role);
  EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO %I', app_role);
  EXECUTE format('GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO %I', app_role);
  -- Tables/sequences the owner creates in FUTURE migrations inherit the same.
  EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', app_role);
  EXECUTE format('ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO %I', app_role);
  -- Never let the app role create objects, even if PUBLIC has CREATE.
  EXECUTE format('REVOKE CREATE ON SCHEMA public FROM %I', app_role);
END
$roles$;
