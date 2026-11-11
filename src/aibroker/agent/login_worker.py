"""The login side of a subscription worker: report its own login, run dashboard re-logins.

One worker = one login (docker-compose AGENT_ACCOUNT_ID; the single Codex worker serves the
`owner` login without it). docs/subscription-logins.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

from aibroker.agent import login_sessions as ls
from aibroker.agent.account_choices import CAPABILITY_PROVIDER
from aibroker.agent.login_flow import run_login
from aibroker.agent.login_probe import probe
from aibroker.agent.login_status import known_email, record_status
from aibroker.config import get_settings

log = logging.getLogger(__name__)
SESSION_POLL_S = 2.0
CODEX_ACCOUNT = "owner"


def worker_login_identity(capabilities: tuple[str, ...]) -> tuple[str, str] | None:
    """(provider, account_id) of this worker's login; None for a worker that serves several
    capabilities (it has no single login to report)."""
    if len(capabilities) != 1 or capabilities[0] not in CAPABILITY_PROVIDER:
        return None
    account = get_settings().AGENT_ACCOUNT_ID.strip() or CODEX_ACCOUNT
    return CAPABILITY_PROVIDER[capabilities[0]], account


async def login_step(provider: str, account_id: str, probe_due: bool) -> bool:
    """One pass: run a requested re-login, else probe when due. -> True when the login was
    just (re)checked, so the caller restarts its probe clock."""
    if (session := await ls.claim_requested(provider, account_id)) is not None:
        log.info("re-login %s started for %s/%s", session.id, provider, account_id)
        await run_login(provider, account_id, session.id,
                        await known_email(provider, account_id))
        return True
    if probe_due:
        await record_status(provider, account_id, await probe(provider))
        return True
    return False


async def login_loop(stop: asyncio.Event, capabilities: tuple[str, ...]) -> None:
    settings = get_settings()
    identity = worker_login_identity(capabilities)
    if identity is None or not settings.AGENT_LOGIN_ENABLED:
        return
    provider, account_id = identity
    with contextlib.suppress(Exception):
        await ls.expire_orphans(provider, account_id)
    loop = asyncio.get_running_loop()
    next_probe = 0.0
    while not stop.is_set():
        try:
            if await login_step(provider, account_id, loop.time() >= next_probe):
                next_probe = loop.time() + settings.AGENT_LOGIN_PROBE_S
        except Exception:  # noqa: BLE001 - the next pass retries; jobs are unaffected
            log.exception("login check failed for %s/%s", provider, account_id)
            next_probe = loop.time() + settings.AGENT_LOGIN_PROBE_S
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), SESSION_POLL_S)
