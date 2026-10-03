# Schema migrations

There is no Alembic. Schema changes are hand-written SQL files in
`infra/sql/migrations/NNN_*.sql`, applied by hand with `psql`.

`infra/sql/init.sql` is the first-boot bootstrap (runs on the first postgres
start) and must mirror every migration, so a fresh database equals a migrated
one. `tests/test_init_sql_mirrors_migrations.py` enforces this.
