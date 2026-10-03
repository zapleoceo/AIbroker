# Runbook: run the app as a non-superuser, non-owner DB role

Goal: the app connects as `aibroker_app` (DML only). `aibroker` (superuser,
owns the schema) is used only for migrations and the event trigger. A process
holding the app credentials can no longer DROP/ALTER/TRUNCATE anything.
Incident this prevents: 2026-10-03 prod wipe (pytest + owner credentials).

Nothing here is applied by deploys. Do it by hand, on the server, in order.
Migration `013_app_role_grants.sql` is idempotent and does nothing until the
role exists. `<PG>` below means
`docker exec -i aibroker-postgres psql -U aibroker -d aibroker -v ON_ERROR_STOP=1`.

## 0. Preconditions
- A fresh backup exists (`MONITOR_BACKUP_DIR`) and a restore was tested.
- `<PG> -c "SELECT rolname, rolsuper FROM pg_roles WHERE rolname='aibroker'"` shows `t`.

## 1. Create the role (password from a secret, never in git)
```sh
APP_PW=$(openssl rand -hex 24)   # store as AIBROKER_APP_PASSWORD in the server .env
<PG> -c "CREATE ROLE aibroker_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD '$APP_PW';"
<PG> -c "GRANT CONNECT ON DATABASE aibroker TO aibroker_app;"
```

## 2. Grant DML
```sh
<PG> < infra/sql/migrations/013_app_role_grants.sql
```
Verify (the last statement must fail):
```sh
<PG> -c "SET ROLE aibroker_app; SELECT count(*) FROM projects; DROP TABLE projects;"
```

## 3. Point the app at it
In `docker-compose.yml`, for the api and worker services, change both URLs to
use `aibroker_app:${AIBROKER_APP_PASSWORD}`:
- `DATABASE_URL` (through pgbouncer) and `DIRECT_DATABASE_URL` (the LISTEN
  connection, straight to postgres).

PgBouncer runs `AUTH_TYPE: plain` with ONE backend credential in its own
`DATABASE_URL` env (`postgres://aibroker:...`), so every pooled connection is
currently the owner. Change that to `aibroker_app` with the same password and
use `-U aibroker_app` in its healthcheck. Then:
```sh
docker compose up -d pgbouncer api worker
```
Migrations keep being applied by hand as `-U aibroker`. Tables created by the
owner afterwards get app grants automatically (default privileges).

## 4. Verify
- `/health` is OK, a chat call succeeds, a deep job completes (exercises LISTEN and sequences).
- `<PG> -c "SELECT usename, count(*) FROM pg_stat_activity GROUP BY 1"` shows `aibroker_app` for the app.

## Rollback
Revert the compose URLs and the pgbouncer env to `aibroker`, then
`docker compose up -d pgbouncer api worker`. The role and grants are harmless
to leave. To remove them fully:
```sh
<PG> -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM aibroker_app"
<PG> -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM aibroker_app"
<PG> -c "REVOKE ALL ON ALL TABLES IN SCHEMA public FROM aibroker_app"
<PG> -c "REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM aibroker_app"
<PG> -c "REVOKE ALL ON SCHEMA public FROM aibroker_app"
<PG> -c "REVOKE CONNECT ON DATABASE aibroker FROM aibroker_app"
<PG> -c "DROP ROLE aibroker_app"
```
