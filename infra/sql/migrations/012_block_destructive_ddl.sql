-- Guard: refuse DROP TABLE / DROP SCHEMA unless explicitly allowed.
--
-- Incident 2026-10-03: pytest ran inside a prod container, the test fixture's
-- Base.metadata.drop_all hit the prod Postgres and dropped every table.
-- tests/conftest.py now refuses non-`_test` databases; this is the second,
-- database-side layer.
--
-- To drop deliberately, in the SAME session first run:
--     SET aibroker.allow_destructive_ddl = 'on';
--
-- Event triggers need superuser to create. The docker-compose postgres user
-- (POSTGRES_USER) is the cluster superuser, so this normally applies. On a
-- managed/non-superuser role the DO block below skips with a NOTICE rather
-- than failing the migration. Idempotent; safe to re-run.
DO $guard$
BEGIN
  IF NOT (SELECT rolsuper FROM pg_roles WHERE rolname = current_user) THEN
    RAISE NOTICE 'aibroker: not superuser, skipping destructive-DDL event trigger';
    RETURN;
  END IF;

  CREATE OR REPLACE FUNCTION aibroker_block_destructive_ddl() RETURNS event_trigger
  LANGUAGE plpgsql AS $fn$
  BEGIN
    IF coalesce(current_setting('aibroker.allow_destructive_ddl', true), '') <> 'on' THEN
      RAISE EXCEPTION '% blocked by aibroker guard: SET aibroker.allow_destructive_ddl = ''on'' in this session to proceed', tg_tag;
    END IF;
  END;
  $fn$;

  DROP EVENT TRIGGER IF EXISTS aibroker_block_destructive_ddl;
  CREATE EVENT TRIGGER aibroker_block_destructive_ddl
    ON ddl_command_start
    WHEN TAG IN ('DROP TABLE', 'DROP SCHEMA')
    EXECUTE FUNCTION aibroker_block_destructive_ddl();
END
$guard$;
