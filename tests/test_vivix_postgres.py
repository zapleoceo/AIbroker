"""Run only against the explicitly configured TEST_DATABASE_URL guarded by conftest."""
import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from aibroker.db.engine import get_session
from aibroker.db.models import ApiKeyRow, ProjectRow, UsageLogRow
from aibroker.db.vivix_models import VivixJobRow
from aibroker.providers.gemini_video import VideoRejected
from aibroker.services.vivix_store import Snapshot
from tests.test_vivix_billing import binding as binding
from tests.test_vivix_billing import prepared, terminal
from tests.test_vivix_service import ledger as ledger

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL", "").startswith(("postgresql", "postgres")),
    reason="NOT RUN: no explicitly authorized PostgreSQL TEST_DATABASE_URL")


async def test_migrations_are_idempotent_on_test_database():
    root = Path(__file__).resolve().parents[1] / "infra/sql/migrations"
    async with get_session() as session:
        assert (await session.scalar(text("SELECT current_database()"))).endswith("_test")
        # Isolate from conftest's create_all: no current ORM tables on search_path.
        # PostgreSQL transactional DDL is rolled back, including this schema.
        schema = "vivix_upgrade_" + uuid4().hex
        try:
            await session.execute(text(f'CREATE SCHEMA "{schema}"'))
            await session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            assert await session.scalar(text("SELECT to_regclass('vivix_jobs')")) is None

            async def apply(filename):
                source = "\n".join(line for line in (root / filename).read_text().splitlines()
                                   if not line.lstrip().startswith("--"))
                for statement in source.split(";"):
                    if statement.strip() and statement.strip() not in {"BEGIN", "COMMIT"}:
                        await session.execute(text(statement))

            await apply("028_vivix_ledger.sql")
            column = text("SELECT data_type FROM information_schema.columns "
                          "WHERE table_schema=:schema AND table_name='vivix_jobs' "
                          "AND column_name='api_key_id'")
            assert await session.scalar(column, {"schema": schema}) is None
            await session.execute(text("INSERT INTO vivix_jobs VALUES "
                                       "('legacy','project','account',3,2.25,0,"
                                       "'{\"state\":\"unknown\"}')"))
            await apply("029_vivix_broker_binding.sql")
            assert await session.scalar(column, {"schema": schema}) == "bigint"
            assert await session.scalar(text("SELECT to_regclass('budget_reservations')")) is None
            await apply("030_shared_budget_reservations.sql")
            await session.execute(text("INSERT INTO budget_reservations "
                                       "(id,api_key_id,project_id,amount,day,month,debit_daily,"
                                       "debit_monthly,state) VALUES "
                                       "('legacy-hold',2,1,2,CURRENT_DATE,CURRENT_DATE,true,true,'held')"))
            for _ in range(2):
                await apply("028_vivix_ledger.sql")
                await apply("029_vivix_broker_binding.sql")
                await apply("030_shared_budget_reservations.sql")
            hold = (await session.execute(text("SELECT * FROM budget_reservations"))).mappings().one()
            assert hold["amount"] == 2 and hold["state"] == "held" and hold["usage_id"] is None
            assert await session.scalar(text("SELECT COUNT(*) FROM budget_ledger_lock WHERE id=1")) == 1
            row = (await session.execute(text("SELECT * FROM vivix_jobs"))).mappings().one()
            assert row["id"] == "legacy" and row["version"] == 3
            assert row["reserved_usd"] == Decimal("2.25") and row["settled_usd"] == 0
            assert row["body"] == {"state": "unknown"} and row["api_key_id"] is None
            assert await session.scalar(text("SELECT COUNT(*) FROM vivix_ledger_lock WHERE id=1")) == 1
            assert await session.scalar(text("SELECT COUNT(*) FROM pg_indexes WHERE "
                                             "schemaname=:schema AND "
                                             "indexname='ix_vivix_jobs_api_key_id'"),
                                        {"schema": schema}) == 1
        finally:
            await session.rollback()


async def test_postgres_concurrent_project_admission(binding):
    async with get_session() as session:
        project = await session.get(ProjectRow, 1)
        project.daily_cost_cap_usd = 3
    results = await asyncio.gather(prepared(binding, "1"), prepared(binding, "2"), return_exceptions=True)
    successes = [result for result in results if isinstance(result, Snapshot)]
    errors = [result for result in results if type(result) is VideoRejected]
    assert len(successes) == len(errors) == 1, repr(results)
    assert str(errors[0]) == "Vivix project daily budget exhausted"
    assert successes[0].body["state"] == "prepared"
    async with get_session() as session:
        rows = (await session.scalars(select(VivixJobRow))).all()
        assert len(rows) == 1 and rows[0].id == successes[0].id
        assert rows[0].reserved_usd == Decimal("2.25") and rows[0].settled_usd == 0
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 0


async def test_postgres_concurrent_settlement_books_once(binding):
    await terminal(binding)
    results = await asyncio.gather(*(binding.reconcile(job_id="1", project_id="1", actual_usd="1",
                                             evidence="fixture-receipt") for _ in range(2)),
                         return_exceptions=True)
    assert any(isinstance(result, Snapshot) for result in results), repr(results)
    for result in results:
        if isinstance(result, Snapshot):
            assert result.body["state"] == "settled"
        else:
            assert type(result) is VideoRejected, repr(result)
            assert str(result) == "Vivix concurrent transition; reread before reconciliation"
    # Read before AND after an explicit idempotent retry: exactly one accounting event.
    for attempt in range(2):
        if attempt:
            await binding.reconcile(job_id="1", project_id="1", actual_usd="1",
                                    evidence="fixture-receipt")
        snapshot = await binding.store.load("1", "1")
        assert snapshot.body["state"] == "settled"
        assert Decimal(snapshot.reserved_usd) == 0 and Decimal(snapshot.settled_usd) == 1
        async with get_session() as session:
            usage = (await session.scalars(select(UsageLogRow))).one()
            assert usage.id == snapshot.body["usage_id"]
            assert (usage.api_key_id, usage.project_id, usage.provider, usage.model,
                    usage.capability, usage.request_id, usage.status, usage.cost_usd) == (
                        1, 1, "vivix", "vivix-w1", "video", "job-1", "ok", 1)
            key = await session.get(ApiKeyRow, 1)
            assert key.daily_cost_used_usd == key.monthly_cost_used_usd == key.total_cost_usd == 1
            assert key.daily_used == 1
            assert key.daily_reset_at == datetime.now(UTC).date()
            assert key.monthly_reset_at == datetime.now(UTC).date().replace(day=1)
