"""Read-only list of one project's jobs filtered by a label (GET /v1/jobs?label.k=v).

Keyset paging on `id` with a high-watermark snapshot: the first page records max(id) of the
project and every later page is bounded by it (`id <= hw`), so a walk covers the jobs that existed
when it started. Not atomic: a listed job's status can change between pages. Only metadata columns
are selected - never the request, result or error text.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

from sqlalchemy import func, select, type_coerce
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow
from aibroker.services.job_queue import JOB_RETENTION_DAYS

MAX_ID = 2**63 - 1   # BIGINT ceiling: a larger cursor value would be a DB error, not a 422
_AGENT_SCAN_CHUNK = 500
MAX_LIMIT = 200
DEFAULT_LIMIT = 100
NON_TERMINAL = ("pending", "running")
_AGENT_PREFIX = "agent:"
_B64 = re.compile(r"[A-Za-z0-9_-]{1,200}")

# Explicit column list (never a star projection): see the pgbouncer note in job_queue._JOB_COLS_SQL.
_LIST_COLS = (
    DeepJobRow.id, DeepJobRow.status, DeepJobRow.capability, DeepJobRow.created_at,
    DeepJobRow.started_at, DeepJobRow.completed_at, DeepJobRow.labels, DeepJobRow.trace_id,
    DeepJobRow.idempotency_key, DeepJobRow.retry_count, DeepJobRow.cancel_requested,
    # Two scalar JSON extracts - never the whole request / result_meta documents.
    DeepJobRow.result_meta["model"].as_string().label("model"),
    func.coalesce(DeepJobRow.request["client_job"]["model"].as_string(),
                  DeepJobRow.request["model"].as_string()).label("requested_model"),
)


class BadCursor(ValueError):
    """The cursor is not one this endpoint issued (malformed, or ids out of range)."""


class Cursor(NamedTuple):
    after: int   # last id of the previous page (0 = start)
    hw: int      # high watermark: ids above it are not part of this walk
    quiet: bool  # no_nonterminal_in_filter, computed once on the first page and carried


class JobPage(NamedTuple):
    jobs: list[dict[str, Any]]
    next_cursor: str | None
    pagination_complete: bool
    no_nonterminal_in_filter: bool
    high_watermark: int
    retention_days: int
    as_of: datetime


def encode_cursor(cursor: Cursor) -> str:
    raw = json.dumps({"after": cursor.after, "hw": cursor.hw, "nn": cursor.quiet}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(token: str) -> Cursor:
    """Opaque token -> Cursor; BadCursor on anything not issued by encode_cursor. Not a secret and
    not signed: project isolation comes from the WHERE clause, never from the cursor."""
    if not _B64.fullmatch(token):
        raise BadCursor("malformed cursor")
    try:
        data = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (binascii.Error, ValueError, UnicodeDecodeError) as exc:
        raise BadCursor("malformed cursor") from exc
    if not isinstance(data, dict) or set(data) != {"after", "hw", "nn"}:
        raise BadCursor("malformed cursor")
    after, hw, quiet = data["after"], data["hw"], data["nn"]
    ok = all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_ID for v in (after, hw))
    if not ok or hw < after or not isinstance(quiet, bool):
        raise BadCursor("malformed cursor")
    return Cursor(after, hw, quiet)


def _has_label(col: Any, key: str, value: str, dialect: str) -> Any:
    if dialect == "sqlite":   # tests; labels is plain JSON there, no `@>`
        return func.json_extract(col, f'$."{key}"') == value   # key is check_token-validated
    # the model's type is JSON().with_variant(JSONB): its base JSON comparator would turn
    # contains() into LIKE '%...%', so coerce to JSONB to get `@>` (GIN ix_deep_jobs_labels_gin)
    return type_coerce(col, PG_JSONB).contains({key: value})


def _filters(project_id: int, key: str, value: str, trace_id: str | None, dialect: str,
             agent_open: bool) -> list[Any]:
    conds = [DeepJobRow.project_id == project_id,
             _has_label(DeepJobRow.labels, key, value, dialect)]
    if trace_id is not None:
        conds.append(DeepJobRow.trace_id == trace_id)
    if not agent_open:
        conds.append(~DeepJobRow.capability.like(_AGENT_PREFIX + "%"))
    return conds


def _item(row: Any) -> dict[str, Any]:
    return {"job_id": row.id, "status": row.status, "capability": row.capability,
            "model": row.model, "requested_model": row.requested_model,
            "created_at": row.created_at, "started_at": row.started_at,
            "completed_at": row.completed_at, "labels": row.labels, "trace_id": row.trace_id,
            "idempotency_key": row.idempotency_key, "retry_count": row.retry_count,
            "cancel_requested": row.cancel_requested}


async def _quiet(s: Any, base: list[Any], hw: int, agent_open: bool,
                 agent_readable: Callable[[str, dict[str, Any]], bool]) -> bool:
    """True when the whole filtered set up to `hw` has no pending/running job the caller may see.

    Status filter NOT applied (a status=done listing must not claim "nothing in flight" about jobs it
    filtered out). Plain rows: one EXISTS-style probe. Agent rows: only the two request keys the access
    check reads (never the whole `request` document), scanned in id-ordered chunks until one is readable."""
    live = (*base, DeepJobRow.id <= hw, DeepJobRow.status.in_(NON_TERMINAL))
    plain = (await s.execute(select(DeepJobRow.id).where(
        *live, ~DeepJobRow.capability.like(_AGENT_PREFIX + "%")).limit(1))).first()
    if plain is not None:
        return False
    if not agent_open:
        return True
    last = 0
    while True:
        rows = (await s.execute(
            select(DeepJobRow.id, DeepJobRow.capability, DeepJobRow.request["model"],
                   DeepJobRow.request["account_id"])
            .where(*live, DeepJobRow.id > last, DeepJobRow.capability.like(_AGENT_PREFIX + "%"))
            .order_by(DeepJobRow.id).limit(_AGENT_SCAN_CHUNK))).all()
        for _id, cap, model, account in rows:
            req = {k: v for k, v in (("model", model), ("account_id", account)) if v is not None}
            if agent_readable(cap, req):
                return False
        if len(rows) < _AGENT_SCAN_CHUNK:
            return True
        last = rows[-1][0]


async def list_jobs(
    project_id: int, key: str, value: str, *, trace_id: str | None = None, status: str | None = None,
    limit: int = DEFAULT_LIMIT, cursor: Cursor | None = None, agent_open: bool,
    agent_readable: Callable[[str, dict[str, Any]], bool],
) -> JobPage:
    """One page of the project's jobs whose labels contain {key: value}.

    `agent_open`: the caller may read agent runs at all (scope + allowlist); when False every
    `agent:*` row is excluded in SQL. `agent_readable(capability, request)` is the per-row read gate
    for the rest (the same check the poll route applies); a row it refuses is not listed.
    `no_nonterminal_in_filter` is computed on the first page only and carried in the cursor."""
    async with get_session() as s:
        dialect = s.get_bind().dialect.name
        base = _filters(project_id, key, value, trace_id, dialect, agent_open)
        if cursor is None:
            hw = (await s.execute(select(func.max(DeepJobRow.id)).where(
                DeepJobRow.project_id == project_id))).scalar() or 0
            after = 0
            quiet = await _quiet(s, base, hw, agent_open, agent_readable)
        else:
            after, hw, quiet = cursor
        stmt = select(*_LIST_COLS).where(*base, DeepJobRow.id > after, DeepJobRow.id <= hw)
        if status is not None:
            stmt = stmt.where(DeepJobRow.status == status)
        rows = (await s.execute(stmt.order_by(DeepJobRow.id).limit(limit + 1))).all()
        page, more = rows[:limit], len(rows) > limit

        agent_ids = [r.id for r in page if r.capability.startswith(_AGENT_PREFIX)]
        requests: dict[int, dict[str, Any]] = {}
        if agent_ids:
            requests = {i: {k: v for k, v in (("model", m), ("account_id", a)) if v is not None}
                        for i, m, a in (await s.execute(
                            select(DeepJobRow.id, DeepJobRow.request["model"],
                                   DeepJobRow.request["account_id"])
                            .where(DeepJobRow.id.in_(agent_ids)))).all()}

    items = [_item(r) for r in page
             if not r.capability.startswith(_AGENT_PREFIX) or agent_readable(r.capability, requests[r.id])]
    next_cursor = encode_cursor(Cursor(page[-1].id, hw, quiet)) if more else None
    return JobPage(items, next_cursor, not more, quiet, hw, JOB_RETENTION_DAYS, datetime.now(UTC))
