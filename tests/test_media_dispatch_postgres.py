"""Prepared real PostgreSQL migration/restart checks; never use production."""
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from aibroker.db.budget_models import BudgetReservationRow
from aibroker.db.engine import get_session
from aibroker.db.media_models import MediaDispatchRow
from aibroker.db.models import DeepJobRow
from aibroker.routing.cost_guard import reserve_cost
from aibroker.services.job_queue import _requeue_stale_running
from tests.test_media_dispatch import media_job as media_job

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL", "").startswith("postgres"),
                               reason="NOT RUN: no authorized isolated PostgreSQL runtime")


async def test_real_stale_reclaim_quarantines_pre_post_crash(media_job):
    dispatch = media_job[2]
    await dispatch.load()
    rid = await reserve_cost(api_key=media_job[1], project=media_job[0], estimated_cost=1)
    dispatch.bind(media_job[1], rid, "fixture-model")
    await dispatch.begin()  # process dies here, cannot prove whether POST was sent
    await _requeue_stale_running()
    async with get_session() as session:
        assert (await session.get(DeepJobRow, 1)).status == "error"
        assert (await session.get(MediaDispatchRow, 1)).phase == "unknown"
        assert (await session.get(BudgetReservationRow, rid)).state == "held"


async def test_migration_031_from_prior_schema_quarantines_only_legacy_gemini():
    sql = (Path(__file__).resolve().parents[1] / "infra/sql/migrations/031_media_dispatch.sql").read_text()
    source = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    schema = "media_upgrade_" + uuid4().hex
    async with get_session() as session:
        assert (await session.scalar(text("SELECT current_database()"))).endswith("_test")
        try:
            await session.execute(text(f'CREATE SCHEMA "{schema}"'))
            await session.execute(text(f'SET LOCAL search_path TO "{schema}"'))
            await session.execute(text("CREATE TABLE deep_jobs (id BIGINT PRIMARY KEY, project_id BIGINT,"
                                       "capability TEXT, status TEXT, request JSONB, started_at TIMESTAMP,"
                                       "completed_at TIMESTAMP, error_message TEXT)"))
            await session.execute(text("INSERT INTO deep_jobs (id,project_id,capability,status,request) VALUES"
                                       "(1,1,'video','running','{}'),(2,1,'video','pending','{\"vivix\":{}}'),"
                                       "(3,1,'chat:fast','running','{}')"))
            assert await session.scalar(text("SELECT to_regclass('media_dispatch')")) is None
            for _ in range(2):
                for statement in source.split(";"):
                    if statement.strip() and statement.strip() not in {"BEGIN", "COMMIT"}:
                        await session.execute(text(statement))
            receipts = (await session.execute(text("SELECT job_id,phase FROM media_dispatch"))).all()
            assert receipts == [(1, "unknown")]
            jobs = (await session.execute(text("SELECT id,status FROM deep_jobs ORDER BY id"))).all()
            assert jobs == [(1, "error"), (2, "pending"), (3, "running")]
        finally:
            await session.rollback()
