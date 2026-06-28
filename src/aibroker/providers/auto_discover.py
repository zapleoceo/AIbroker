"""Auto-discover a key's real free-tier limits by probing once + parsing
the rate-limit response headers. Called from the key-create flow (admin +
dashboard) so the dashboard bar reflects the actual cap on day-one.

Stores into api_keys.discovered_req_limit / discovered_tok_limit /
limits_discovered_at. Falls back gracefully to PROVIDER_QUOTAS defaults
in providers/quotas.py when headers don't carry usable values.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import text

from aibroker.db import get_session
from aibroker.providers.health_probes import extract_quota_headers, probe_with_headers

log = logging.getLogger(__name__)


async def discover_and_store(api_key_id: int, provider: str, plain_token: str) -> None:
    """Probe once, parse rate-limit headers, persist what we learn.

    Best-effort: any failure (network, missing headers, malformed) is logged
    and swallowed — the key still works, the dashboard just falls back to
    defaults. We never block key creation on this.
    """
    try:
        verdict, http, hint, headers = await probe_with_headers(provider, plain_token)
    except Exception as e:
        log.warning("auto-discover probe failed for key %s/%s: %s",
                    provider, api_key_id, e)
        return

    req_limit, tok_limit = extract_quota_headers(provider, headers)
    if req_limit is None and tok_limit is None:
        log.info("auto-discover: %s key=%s — no quota headers exposed (verdict=%s)",
                 provider, api_key_id, verdict)
        return

    async with get_session() as s:
        await s.execute(
            text(
                "UPDATE api_keys "
                "SET discovered_req_limit = :req, "
                "    discovered_tok_limit = :tok, "
                "    limits_discovered_at = :ts "
                "WHERE id = :id"
            ),
            {
                "req": req_limit,
                "tok": tok_limit,
                "ts": datetime.now(timezone.utc).replace(tzinfo=None),
                "id": api_key_id,
            },
        )
    log.info("auto-discover: %s key=%s req=%s tok=%s",
             provider, api_key_id, req_limit, tok_limit)
