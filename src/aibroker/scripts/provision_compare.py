"""Provision the dedicated project behind the public /ask2many page (idempotent).

Usage at deploy, after migration 021 (inside the container, like bootstrap):
    python -m aibroker.scripts.provision_compare

Creates project `model-compare` with chat scopes plus `llm:agent` (Claude/Codex subscription
models; they additionally need the project id in AGENT_ALLOWED_PROJECT_IDS and an
AGENT_PROJECT_POLICY entry, which are server env and not set here) and daily + monthly money caps of 0 (Broker's
cost guard then refuses every paid model; free ones still run). Re-running only re-asserts those
settings; it never rotates anything. The page backend calls Broker's job path in process as this
project, so the project key is generated, hashed and DISCARDED here: it is never printed, logged,
stored in plaintext or sent to a browser. To raise the limit later, change the caps in the
dashboard (Projects) or in SQL; do not re-create the project.
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import close_engine, get_session, init_engine
from aibroker.db.models import ProjectRow
from aibroker.services.compare import COMPARE_PROJECT, COMPARE_SCOPES


async def provision() -> str:
    """Create or re-assert the project; returns 'created' or 'updated'."""
    async with get_session() as s:
        row = (await s.execute(select(ProjectRow).where(ProjectRow.name == COMPARE_PROJECT)
                               )).scalar_one_or_none()
        if row is None:
            secret = generate_project_key()
            s.add(ProjectRow(
                name=COMPARE_PROJECT, owner_email="ops@aibroker",
                project_key_hash=hash_project_key(secret), project_key_prefix=secret[:12],
                allowed_scopes=list(COMPARE_SCOPES), daily_cost_cap_usd=0.0,
                monthly_cost_cap_usd=0.0, is_active=True,
                notes="public /ask2many page; key unrecoverable by design (in-process calls)"))
            return "created"
        row.allowed_scopes = list(COMPARE_SCOPES)
        row.daily_cost_cap_usd = 0.0
        row.monthly_cost_cap_usd = 0.0
        row.is_active = True
        return "updated"


async def main() -> int:
    await init_engine()
    try:
        print(f"project {COMPARE_PROJECT!r}: {await provision()} (daily/monthly cap = 0)")
        return 0
    finally:
        await close_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
