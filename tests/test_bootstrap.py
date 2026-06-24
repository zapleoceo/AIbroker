"""scripts/bootstrap — admin-ops project provisioning."""
from __future__ import annotations

import os

import pytest
from sqlalchemy import select

from aibroker.auth import generate_project_key
from aibroker.db import get_session
from aibroker.db.models import ProjectRow

ON_SQLITE = "sqlite" in os.environ.get("DATABASE_URL", "")


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_bootstrap_creates_admin_ops_project():
    from aibroker.scripts.bootstrap import main
    rc = await main()
    assert rc == 0
    async with get_session() as s:
        row = (await s.execute(
            select(ProjectRow).where(ProjectRow.name == "admin-ops")
        )).scalar_one_or_none()
    assert row is not None
    assert row.allowed_scopes == [
        "llm:chat", "llm:embed", "llm:vision", "vending:*", "admin:read"
    ]


@pytest.mark.skipif(ON_SQLITE, reason="BIGSERIAL needs Postgres")
async def test_bootstrap_re_rotates_when_already_exists():
    """Running bootstrap twice rotates the key, not duplicate row."""
    from aibroker.scripts.bootstrap import main
    rc1 = await main()
    rc2 = await main()
    assert rc1 == 0 and rc2 == 0
    async with get_session() as s:
        count = (await s.execute(
            select(ProjectRow).where(ProjectRow.name == "admin-ops")
        )).scalars().all()
    assert len(count) == 1   # still one row


def test_generate_project_key_is_random():
    """Sanity: two keys differ."""
    k1 = generate_project_key()
    k2 = generate_project_key()
    assert k1 != k2
    assert k1.startswith("aib_prj_")
    # urlsafe_b64 of 32 bytes ≈ 43 chars + 8 prefix
    assert len(k1) > 40
