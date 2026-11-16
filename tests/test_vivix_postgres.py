"""Run only against the explicitly configured TEST_DATABASE_URL guarded by conftest."""
import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy import func, select, text

from aibroker.db.engine import get_session
from aibroker.db.models import ProjectRow, UsageLogRow
from aibroker.providers.gemini_video import VideoRejected
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
        for _ in range(2):
            for filename in ("028_vivix_ledger.sql", "029_vivix_broker_binding.sql"):
                source = "\n".join(line for line in (root / filename).read_text().splitlines()
                                   if not line.lstrip().startswith("--"))
                for statement in source.split(";"):
                    if statement.strip() and statement.strip() not in {"BEGIN", "COMMIT"}:
                        await session.execute(text(statement))
        assert await session.scalar(text("SELECT COUNT(*) FROM vivix_ledger_lock WHERE id=1")) == 1


async def test_postgres_concurrent_project_admission(binding):
    async with get_session() as session:
        project = await session.get(ProjectRow, 1)
        project.daily_cost_cap_usd = 3
    results = await asyncio.gather(prepared(binding, "1"), prepared(binding, "2"), return_exceptions=True)
    assert sum(isinstance(result, VideoRejected) for result in results) == 1


async def test_postgres_concurrent_settlement_books_once(binding):
    await terminal(binding)
    await asyncio.gather(*(binding.reconcile(job_id="1", project_id="1", actual_usd="1",
                                             evidence="fixture-receipt") for _ in range(2)),
                         return_exceptions=True)
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(UsageLogRow)) == 1
