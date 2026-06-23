"""One-shot import — copy all tokens from Vera 3 DB into broker DB.

Vera's `tokens` table is the source. We re-encrypt with broker's TOKEN_SECRET.
Idempotent: existing (provider, label) rows are updated.

Map of vera capabilities → broker scopes:
- voyage  → ['llm:embed']
- others  → ['llm:chat', 'llm:vision']  (vision only on gemini/anthropic/openai by default)

Usage (inside broker container, with VERA_DATABASE_URL + VERA_TOKEN_SECRET env vars set):
    python -m aibroker.scripts.migrate_from_vera
"""
from __future__ import annotations

import asyncio
import os
import sys

from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from aibroker.crypto import encrypt as broker_encrypt
from aibroker.db import close_engine, get_session, init_engine
from aibroker.db.models import ApiKeyRow


VISION_PROVIDERS = {"gemini", "anthropic", "openai"}


def vera_decrypt(ciphertext: str, secret: str) -> str:
    """Vera uses the same Fernet scheme — different key."""
    f = Fernet(secret.encode())
    return f.decrypt(ciphertext.encode()).decode()


def map_scopes(provider: str) -> list[str]:
    if provider == "voyage":
        return ["llm:embed"]
    scopes = ["llm:chat"]
    if provider in VISION_PROVIDERS:
        scopes.append("llm:vision")
    return scopes


async def main() -> int:
    vera_url = os.environ.get("VERA_DATABASE_URL")
    vera_secret = os.environ.get("VERA_TOKEN_SECRET")
    if not vera_url or not vera_secret:
        print("VERA_DATABASE_URL and VERA_TOKEN_SECRET env vars required")
        return 2

    eng = create_async_engine(vera_url, pool_pre_ping=True)
    async with eng.connect() as c:
        vera_rows = (
            await c.execute(
                text(
                    "SELECT provider, label, tier, token_encrypted, "
                    "       daily_cost_cap_usd, monthly_cost_cap_usd, notes "
                    "FROM tokens WHERE is_active = TRUE"
                )
            )
        ).all()
    await eng.dispose()

    print(f"found {len(vera_rows)} active tokens in Vera")

    await init_engine()
    n_new, n_upd, n_fail = 0, 0, 0
    try:
        async with get_session() as s:
            for r in vera_rows:
                provider, label, tier, enc, dcap, mcap, notes = r
                try:
                    plain = vera_decrypt(enc, vera_secret)
                except Exception as e:
                    print(f"  ✗ decrypt {provider}/{label} failed: {e}")
                    n_fail += 1
                    continue
                existing = (
                    await s.execute(
                        text("SELECT id FROM api_keys WHERE provider=:p AND label=:l"),
                        {"p": provider, "l": label},
                    )
                ).first()
                if existing:
                    await s.execute(
                        text(
                            "UPDATE api_keys SET token_encrypted=:t, tier=:ti, "
                            "  daily_cost_cap_usd=:dc, monthly_cost_cap_usd=:mc, "
                            "  notes=:n, is_active=TRUE WHERE id=:id"
                        ),
                        {
                            "id": existing[0], "t": broker_encrypt(plain),
                            "ti": tier, "dc": dcap, "mc": mcap,
                            "n": (notes or "") + " | imported from vera",
                        },
                    )
                    n_upd += 1
                else:
                    s.add(ApiKeyRow(
                        provider=provider, label=label, tier=tier,
                        scopes=map_scopes(provider),
                        token_encrypted=broker_encrypt(plain),
                        daily_cost_cap_usd=dcap,
                        monthly_cost_cap_usd=mcap,
                        notes=(notes or "") + " | imported from vera",
                    ))
                    n_new += 1
        print(f"done: {n_new} new, {n_upd} updated, {n_fail} failed")
        return 0 if n_fail == 0 else 1
    finally:
        await close_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
