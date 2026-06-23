# Alembic migrations

Schema bootstrap is in `infra/sql/init.sql` (runs on first postgres start).
Use this directory for future schema changes once we go past v0.1.

```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
```
