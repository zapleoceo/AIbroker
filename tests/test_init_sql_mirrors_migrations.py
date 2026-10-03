"""infra/sql/init.sql is the ONLY first-boot script docker-compose mounts, so
every column a migration adds must also exist in init.sql's CREATE TABLE (or
its folded-in ALTER). 2026-10-02: migration 011 added usage_log.model_served
but init.sql never got it, so on a fresh DB the dashboard project drill-down
(`SELECT u.model_served`) failed."""
from __future__ import annotations

import re
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parent.parent / "infra" / "sql"

_ALTER = re.compile(r"ALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?(\w+)\s+(.*?);", re.I | re.S)
_ADD = re.compile(r"ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+(\w+)", re.I)
_CREATE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(\w+)\s*\((.*?)\n\)\s*;", re.I | re.S)


def _strip_comments(sql: str) -> str:
    return re.sub(r"--[^\n]*", "", sql)


def _migration_columns() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((SQL_DIR / "migrations").glob("*.sql")):
        sql = _strip_comments(path.read_text(encoding="utf-8"))
        for table, body in _ALTER.findall(sql):
            for col in _ADD.findall(body):
                found.add((table.lower(), col.lower()))
    return found


def _init_columns() -> set[tuple[str, str]]:
    sql = _strip_comments((SQL_DIR / "init.sql").read_text(encoding="utf-8"))
    found: set[tuple[str, str]] = set()
    for table, body in _CREATE.findall(sql):
        for line in body.split("\n"):
            m = re.match(r"\s*(\w+)\s+\w+", line)
            if m and m.group(1).upper() not in {"PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "CHECK"}:
                found.add((table.lower(), m.group(1).lower()))
    for table, body in _ALTER.findall(sql):
        for col in _ADD.findall(body):
            found.add((table.lower(), col.lower()))
    return found


def test_migrations_declare_some_columns():
    assert ("usage_log", "model_served") in _migration_columns()


def test_init_sql_has_every_migration_column():
    missing = sorted(_migration_columns() - _init_columns())
    assert not missing, f"init.sql lacks columns added by migrations: {missing}"


def test_init_sql_usage_log_has_model_served():
    assert ("usage_log", "model_served") in _init_columns()
