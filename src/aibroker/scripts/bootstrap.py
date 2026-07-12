"""One-shot bootstrap — create initial 'admin-ops' project for direct API use.

Usage (inside container):
    python -m aibroker.scripts.bootstrap
"""
from __future__ import annotations

import asyncio
import sys

from sqlalchemy import select

from aibroker.auth import generate_project_key, hash_project_key
from aibroker.db import close_engine, get_session, init_engine
from aibroker.db.models import ProjectRow

ALL_SCOPES = ["llm:chat", "llm:embed", "llm:vision", "admin:read"]


async def main() -> int:
    await init_engine()
    try:
        async with get_session() as s:
            existing = (
                await s.execute(select(ProjectRow).where(ProjectRow.name == "admin-ops"))
            ).scalar_one_or_none()
            if existing:
                print(f"project 'admin-ops' already exists (id={existing.id}); "
                      "re-rotating its key")
                plain = generate_project_key()
                existing.project_key_hash = hash_project_key(plain)
                existing.project_key_prefix = plain[:12]
            else:
                plain = generate_project_key()
                s.add(ProjectRow(
                    name="admin-ops",
                    owner_email="ops@aibroker",
                    project_key_hash=hash_project_key(plain),
                    project_key_prefix=plain[:12],
                    allowed_scopes=ALL_SCOPES,
                    notes="auto-created by bootstrap",
                ))
        print()
        print("=" * 60)
        print("PROJECT KEY (save now — not retrievable):")
        print(f"  {plain}")
        print("=" * 60)
        return 0
    finally:
        await close_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
