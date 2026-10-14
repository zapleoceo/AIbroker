"""Async jobs (submit + poll) — generic over any chat capability.

Why this exists: originally for chat:deep — nemotron-3-ultra has been observed
taking up to ~8 minutes on NVIDIA's free pool, past Cloudflare's (~100s) and
this broker's nginx (proxy_read_timeout 120s) read timeouts, so a sync HTTP
response can't carry the result: the client 504s while the broker is still
waiting and later logs a perfectly good "ok" nobody's left to see.

The same submit/poll shape now serves EVERY chat capability (POST
/v1/jobs?capability=X, roadmap Phase 4): a client can migrate off the sync
endpoint at its own pace and get a guaranteed answer (exhaustive rotation, no
held connection). Sync endpoints stay — this is additive, backward-compatible.
`submit_deep_job` is a thin wrapper over `submit_job(capability="chat:deep")`.

Submit only ENQUEUES a `pending` row and returns its id immediately; the
dispatcher loop (services/job_queue.py) claims and runs it. Poll reads the row
from Postgres, so it works no matter which worker answers; no in-process handle
needs to survive. All lifecycle transitions (running → done/error, requeue,
give-up after retries) are owned by the dispatcher — `get_job` is a pure read.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from sqlalchemy import select, text, update
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.db.resilience import retry_stale_statement, retry_terminal_write
from aibroker.services.job_meta import log_tag

# Where async transcription parks its base64 audio inside `request`. The queue
# stores payloads as JSONB, which can't hold raw bytes — and putting the blob in
# its own column would need a migration for one capability. base64 costs +33%
# but the blob is cleared the moment the job finishes (see _finish), so it never
# accumulates. Bounded by the route's own _MAX_AUDIO_BYTES.
AUDIO_FIELD = "audio_b64"

log = logging.getLogger(__name__)

# NOTIFY channel shared with the dispatcher's LISTEN connection (job_queue.py).
# Lives here (not job_queue) because job_queue already imports from this module.
JOBS_CHANNEL = "aib_jobs"

# In-flight dedup window. Measured on prod (2026-07-16): one client resubmitted
# the SAME vision payload up to 33x (480 jobs/24h vs 156 distinct payloads);
# with the dispatcher's own up-to-8 retries that's ~260 provider attempts for
# one image. 30 min is wide enough to swallow that whole resubmit storm, narrow
# enough that a genuinely repeated question tomorrow gets a fresh answer. Only
# pending/running jobs dedup — a done/error job never does: after a failure the
# client may legitimately want a retry.
_DEDUP_WINDOW_S = 30 * 60

# Flips False the first time the dedup lookup hits a missing payload_hash
# column (code deployed before migration 010 was applied) — degrade to plain
# duplicate-tolerant inserts instead of 500ing every submit. Warn once.
_dedup_available = True


def _without_null_message_fields(request: dict[str, Any]) -> dict[str, Any]:
    """`{"role":"user","content":"x","tool_calls":null}` and the same message without the key
    are one request: drop null-valued keys of each message before hashing."""
    messages = request.get("messages")
    if not isinstance(messages, list):
        return request
    return {**request, "messages": [
        {k: v for k, v in m.items() if v is not None} if isinstance(m, dict) else m
        for m in messages]}


def payload_hash(project_id: int, capability: str, request: dict[str, Any]) -> str:
    """md5 over project + capability + canonical (sorted, compact) request JSON; explicit nulls in
    messages and omitted fields hash alike."""
    canonical = json.dumps(_without_null_message_fields(request), sort_keys=True,
                           separators=(",", ":"))
    return hashlib.md5(f"{project_id}:{capability}:{canonical}".encode()).hexdigest()


class IdempotencyConflict(Exception):
    """The Idempotency-Key was already used by this project with a different payload."""

    def __init__(self, existing_job_id: int) -> None:
        self.existing_job_id = existing_job_id
        super().__init__(f"idempotency key already used by job {existing_job_id} with a different payload")


class SubmitOutcome(NamedTuple):
    job_id: int
    replayed: bool   # True only for an Idempotency-Key replay (legacy in-flight dedup stays False)


def build_request(
    *, messages: list[dict[str, Any]], model: str | None, max_tokens: int, temperature: float,
    response_format: dict[str, Any] | None, workflow: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The stored request == the payload_hash input: every result-affecting field, and nothing
    transport-level (trace_id, labels, idempotency key and timestamps never enter it)."""
    return {
        "messages": messages, "model": model,
        "max_tokens": max_tokens, "temperature": temperature,
        "response_format": response_format, "workflow": workflow,
        **(extra or {}),
    }


async def find_by_idempotency_key(project_id: int, key: str) -> tuple[int, str | None] | None:
    """(job id, payload_hash) of this project's job that used `key`, or None."""
    async with get_session() as s:
        row = (await s.execute(
            select(DeepJobRow.id, DeepJobRow.payload_hash)
            .where(DeepJobRow.project_id == project_id, DeepJobRow.idempotency_key == key)
        )).first()
    return (row[0], row[1]) if row else None


async def replayed_job_id(project_id: int, key: str, phash: str) -> int | None:
    """The job an Idempotency-Key already created for the same payload, None when the key is
    new; IdempotencyConflict when the key was used with a different payload."""
    hit = await find_by_idempotency_key(project_id, key)
    if hit is None:
        return None
    if hit[1] != phash:
        raise IdempotencyConflict(hit[0])
    return hit[0]


async def _find_inflight_duplicate(
    project_id: int, capability: str, phash: str
) -> int | None:
    """id of an identical pending/running job inside the dedup window, or None.

    Best-effort, not a uniqueness constraint: two truly simultaneous identical
    submits can still both insert — fine, the target is the serial resubmit
    storm, not a race window of milliseconds."""
    global _dedup_available
    if not _dedup_available:
        return None
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=_DEDUP_WINDOW_S)
    try:
        async with get_session() as s:
            return (await s.execute(
                select(DeepJobRow.id)
                .where(
                    DeepJobRow.project_id == project_id,
                    DeepJobRow.capability == capability,
                    DeepJobRow.payload_hash == phash,
                    DeepJobRow.status.in_(("pending", "running")),
                    DeepJobRow.created_at > cutoff,
                )
                .order_by(DeepJobRow.created_at.desc())
                .limit(1)
            )).scalars().first()
    except (ProgrammingError, OperationalError) as e:
        # Missing column = migration 010 not applied yet. The deploy must not
        # 500 over that — disable dedup until the operator runs the migration.
        _dedup_available = False
        log.warning(
            "job dedup disabled — payload_hash lookup failed (%s); apply "
            "infra/sql/migrations/010_deep_jobs_payload_hash.sql and RESTART "
            "(dedup stays off until process restart)", e,
        )
        return None


async def _notify_dispatcher(s: AsyncSession) -> None:
    """Queue a NOTIFY on the enqueue's own session: Postgres delivers it when the INSERT
    commits, so the dispatcher never wakes before the row is visible and no second
    session/connection is spent. Best-effort — the SAVEPOINT confines a failure to the
    NOTIFY, the enqueue still commits and the dispatcher's timed poll covers the wake-up."""
    if s.bind.dialect.name != "postgresql":
        return
    try:
        async with s.begin_nested():
            await s.execute(text(f"SELECT pg_notify('{JOBS_CHANNEL}', '')"))
    except Exception as e:  # noqa: BLE001 — never fail a durable enqueue over a wake-up hint
        log.debug("pg_notify(%s) failed: %s — poll fallback covers it", JOBS_CHANNEL, e)


# The legacy dedup/insert paths are exercised by the Postgres-only tests in
# tests/test_deep_jobs.py (test_submit_same_payload_twice_returns_same_id_single_row
# and friends) plus the end-to-end
# test_deep_submit_enqueues_and_dispatcher_drains_to_done in tests/test_routes_proxy.py;
# the idempotency/trace/labels paths also run on SQLite (tests/test_job_idempotency.py).
async def submit_job(  # pragma: no cover
    *,
    project: ProjectRow,
    capability: str,
    messages: list[dict[str, Any]],
    model: str | None,
    max_tokens: int,
    temperature: float,
    response_format: dict[str, Any] | None,
    workflow: str | None,
    extra: dict[str, Any] | None = None,
) -> int:
    """`submit_job_ex` for callers that only need the id (no Idempotency-Key)."""
    return (await submit_job_ex(
        project=project, capability=capability, messages=messages, model=model,
        max_tokens=max_tokens, temperature=temperature, response_format=response_format,
        workflow=workflow, extra=extra,
    )).job_id


async def submit_job_ex(
    *,
    project: ProjectRow,
    capability: str,
    messages: list[dict[str, Any]],
    model: str | None,
    max_tokens: int,
    temperature: float,
    response_format: dict[str, Any] | None,
    workflow: str | None,
    extra: dict[str, Any] | None = None,
    trace_id: str | None = None,
    labels: dict[str, str] | None = None,
    idempotency_key: str | None = None,
) -> SubmitOutcome:
    """ENQUEUE a job for any chat `capability` and return its id immediately.

    `idempotency_key` (unique per project): the same key + payload returns the existing job
    (replayed=True), a different payload raises IdempotencyConflict, and the legacy in-flight
    payload dedup is OFF for that request. `trace_id`/`labels` are stored on the row only.

    `extra` merges non-chat payload into the stored request — async
    transcription uses it for the base64 audio + filename (see AUDIO_FIELD),
    since the queue's JSONB column can't hold raw bytes. It also participates
    in the dedup hash, so two different voice notes are never deduped into one.

    Submit no longer runs the call — it only inserts a `pending` row. The
    dispatcher loop (services/job_queue.py) claims and drains pending rows with
    bounded concurrency. This is what makes the queue survive a worker restart
    (a deploy) and apply backpressure: the request is durably enqueued the
    moment submit returns, whatever the broker/provider pool is doing."""
    request = build_request(messages=messages, model=model, max_tokens=max_tokens,
                            temperature=temperature, response_format=response_format,
                            workflow=workflow, extra=extra)
    phash = payload_hash(project.id, capability, request)
    if idempotency_key:
        existing = await replayed_job_id(project.id, idempotency_key, phash)
        if existing is not None:
            return SubmitOutcome(existing, True)
    else:
        existing = await _find_inflight_duplicate(project.id, capability, phash)
        if existing is not None:
            # Identical request already in flight — hand back its id (the client's
            # own resubmit storm now polls ONE job) and don't wake the dispatcher.
            log.info("dedup: project=%s %s resubmitted in-flight job %s — returning it",
                     project.id, capability, existing)
            return SubmitOutcome(existing, False)
    if _dedup_available or idempotency_key:
        try:
            async with get_session() as s:
                row = DeepJobRow(project_id=project.id, capability=capability,
                                  status="pending", request=request,
                                  payload_hash=phash, trace_id=trace_id, labels=labels,
                                  idempotency_key=idempotency_key)
                s.add(row)
                await s.flush()
                job_id = row.id
                await _notify_dispatcher(s)      # delivered at this transaction's commit
        except IntegrityError:
            # Lost the (project_id, idempotency_key) race: the winner's job is the answer.
            winner = (await replayed_job_id(project.id, idempotency_key, phash)
                      if idempotency_key else None)
            if winner is None:
                raise
            return SubmitOutcome(winner, True)
    else:  # pragma: no cover — only before migration 010
        # Degraded (migration 010 not applied yet): raw INSERT naming ONLY the
        # pre-010 columns. An ORM insert is unsafe here — SQLAlchemy's compiled
        # cache may reuse a statement that references payload_hash from before
        # the flag flipped (bit us in CI, 2026-07-16).
        import json as _json
        async with get_session() as s:
            # retry_count named explicitly: its 0-default is Python-side (ORM),
            # so a raw INSERT can't rely on it existing in the DDL.
            job_id = (await s.execute(text(
                "INSERT INTO deep_jobs "
                "(project_id, capability, status, request, retry_count) "
                "VALUES (:p, :c, 'pending', :r, 0) RETURNING id"
            ), {"p": project.id, "c": capability,
                "r": _json.dumps(request)})).scalar_one()
            await _notify_dispatcher(s)
    # The wake-up NOTIFY (instant claim for an interactive call) was queued inside the
    # INSERT transaction above and is delivered when it commits; best-effort, the
    # dispatcher's timed poll is the guaranteed fallback.
    log.info("job %d enqueued: project=%s %s%s", job_id, project.id, capability,
             log_tag(trace_id, labels))
    return SubmitOutcome(job_id, False)


async def submit_deep_job(  # pragma: no cover
    *,
    project: ProjectRow,
    messages: list[dict[str, Any]],
    model: str | None,
    max_tokens: int,
    temperature: float,
    workflow: str | None,
) -> int:
    """Backward-compatible chat:deep wrapper (POST /v1/deep). nemotron isn't
    JSON-reliable, so response_format is always None here — see chains.py."""
    return await submit_job(
        project=project, capability="chat:deep", messages=messages, model=model,
        max_tokens=max_tokens, temperature=temperature, response_format=None,
        workflow=workflow,
    )


def _strip_audio_sql(dialect: str) -> str:
    """SQL for the new `request`: the base64 audio nulled (the rest kept), or the
    column untouched when there is no audio. Same truthiness the old Python check
    had: a missing, null or empty `audio_b64` is left alone."""
    if dialect == "postgresql":
        has = f"COALESCE(request->>'{AUDIO_FIELD}', '') <> ''"
        strip = f"jsonb_set(request, '{{{AUDIO_FIELD}}}', 'null'::jsonb)"
    else:   # SQLite (tests)
        has = f"COALESCE(json_extract(request, '$.{AUDIO_FIELD}'), '') <> ''"
        strip = f"json_set(request, '$.{AUDIO_FIELD}', json('null'))"
    return f"CASE WHEN {has} THEN {strip} ELSE request END"


@retry_stale_statement
@retry_terminal_write
async def _finish(
    job_id: int, *, status: str,
    result_text: str | None = None,
    result_meta: dict[str, Any] | None = None,
    error_message: str | None = None,
    expect_started_at: datetime | None = None,
) -> None:
    """One UPDATE, no row load. Claim guard: if the caller passes the started_at it
    claimed the job with and the row now holds a different value, the job was
    re-queued (started_at→NULL) and re-claimed by another worker — this is a stale
    worker returning late; the WHERE then matches nothing and the live claim's
    result is not clobbered. A deleted row matches nothing either.

    Async transcription carries the whole audio file base64'd in the request
    payload. Once the job is terminal that blob is dead weight — nobody re-reads
    it, and deep_jobs is already the biggest table we back up nightly. It is nulled
    in the same statement (rest of the request kept for debugging), so audio lives
    in the DB only while it's being worked on."""
    stmt = update(DeepJobRow).where(DeepJobRow.id == job_id)
    if expect_started_at is not None:
        stmt = stmt.where(DeepJobRow.started_at == expect_started_at)
    async with get_session() as s:
        await s.execute(stmt.values(
            status=status, result_text=result_text, result_meta=result_meta,
            error_message=error_message,
            completed_at=datetime.now(UTC).replace(tzinfo=None),
            request=text(_strip_audio_sql(s.bind.dialect.name)),
        ).execution_options(synchronize_session=False))


class JobView(NamedTuple):
    """The slice of a deep_jobs row the poll routes read. `request` holds ONLY the
    keys they use (`model`, `account_id`, `client_job`) — never the prompt or the
    base64 audio that make the full JSONB up to ~80 KB and more."""
    id: int
    project_id: int
    capability: str
    status: str
    result_text: str | None
    result_meta: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    trace_id: str | None
    labels: dict[str, str] | None
    cancel_requested: bool
    request: dict[str, Any]


_POLL_REQUEST_KEYS = ("model", "account_id", "client_job")


async def get_job(job_id: int, project_id: int) -> JobView | None:
    """Fetch a job, scoped to the caller's own project, WITHOUT loading the
    `request` JSONB: only the three JSON paths the poll needs are selected.
    Pure read — every
    lifecycle transition (running → done/error, requeue, give-up after
    retries) is owned by the dispatcher (services/job_queue.py). It used to
    lazily flip a stale `pending` row to error, but that raced the dispatcher
    and prematurely failed jobs still legitimately retrying under backoff."""
    async with get_session() as s:
        r = (await s.execute(
            select(
                DeepJobRow.id, DeepJobRow.project_id, DeepJobRow.capability,
                DeepJobRow.status, DeepJobRow.result_text, DeepJobRow.result_meta,
                DeepJobRow.error_message, DeepJobRow.created_at, DeepJobRow.trace_id,
                DeepJobRow.labels, DeepJobRow.cancel_requested,
                *(DeepJobRow.request[k] for k in _POLL_REQUEST_KEYS),
            ).where(DeepJobRow.id == job_id, DeepJobRow.project_id == project_id)
        )).one_or_none()
    if r is None:
        return None
    # A missing key and a JSON null both read back as None; the callers treat
    # them alike, so only non-null keys are rebuilt (`.get(k, default)` keeps working).
    request = {k: v for k, v in zip(_POLL_REQUEST_KEYS, r[11:], strict=True) if v is not None}
    return JobView(*r[:11], request=request)


def next_poll_after_s(created_at: datetime) -> int:
    """Suggested poll interval — start slow (this is a minutes-long job, not
    a fast one), don't hammer the endpoint. Widens with a small cap so a
    long-pending job doesn't get polled every second nor once a minute."""
    age = (datetime.now(UTC).replace(tzinfo=None) - created_at)
    if age < timedelta(seconds=30):
        return 5
    if age < timedelta(minutes=2):
        return 10
    return 20
