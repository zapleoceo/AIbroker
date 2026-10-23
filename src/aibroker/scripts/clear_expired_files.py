"""Clear the CONTENT of already-expired inline agent result files (owner request, Room #4040).

Usage (inside the api container, see docs/deploy-ops.md "Clearing expired agent file bytes"):
    python -m aibroker.scripts.clear_expired_files                      # dry-run (default), writes nothing
    python -m aibroker.scripts.clear_expired_files --cutoff 2026-10-23T10:00:00Z   # fix the cutoff
    python -m aibroker.scripts.clear_expired_files --execute --cutoff 2026-10-23T10:00:00Z \
        --confirm CLEAR-EXPIRED-2026-10-23T10:00:00Z

What it does: for `deep_jobs` rows of `agent:*` capabilities in a terminal status, every
`result_meta.files[i]` that still carries inline `content_b64` and whose `expires_at` is STRICTLY
before the cutoff gets `content_b64 = null` and `expired = true` - exactly the JSON the API already
serves for an expired file (`job_files._public(f, content_b64=None, expired=True)`), so the response
does not change. Every other key of the file, every other file, and every other `result_meta` key is
kept. Jobs, `request`, `result_text`, usage, traces and `deep_job_files` rows are never touched
(table rows are only counted in the report). No scheduler, no retention change, no VACUUM.

The bytes are NOT recoverable from the database afterwards (only from the daily aibroker dumps).

Comparison: the API decides expiry with a plain string compare of the ISO `expires_at` against
`datetime.now(UTC).isoformat()` (`job_files._expired`); SQL does the same compare (COLLATE "C" =
codepoint order, like Python) against `cutoff.isoformat()`. Files without a (well-formed) `expires_at`
are never touched and are reported as skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aibroker.db import close_engine, get_session, init_engine

log = logging.getLogger("aibroker.clear_expired_files")

DEFAULT_MARGIN_S = 300
DEFAULT_BATCH = 20
TERMINAL = ("done", "error", "cancelled")
CONFIRM_PREFIX = "CLEAR-EXPIRED-"

# One element `e` of result_meta.files. Shared by dry-run and execute so they select identically.
_INLINE = "jsonb_typeof(e) = 'object' AND jsonb_typeof(e->'content_b64') = 'string'"
_HAS_EXPIRY = ("coalesce(jsonb_typeof(e->'expires_at') = 'string' "
               "AND (e->>'expires_at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T', false)")
_EXPIRED = "((e->>'expires_at') COLLATE \"C\" < CAST(:cutoff AS text))"
_CLEARABLE = f"({_INLINE} AND {_HAS_EXPIRY} AND {_EXPIRED})"
_LIVE = f"({_INLINE} AND {_HAS_EXPIRY} AND NOT {_EXPIRED})"
_NO_EXPIRY = f"({_INLINE} AND NOT ({_HAS_EXPIRY}))"
_FILES = ("CASE WHEN jsonb_typeof(j.result_meta->'files') = 'array' "
          "THEN j.result_meta->'files' ELSE '[]'::jsonb END")
_SCOPE = "j.status IN ('done', 'error', 'cancelled') AND j.capability LIKE :cap_like"

_IDS_SQL = text("SELECT j.id FROM deep_jobs j WHERE " + _SCOPE + " ORDER BY j.id")

# Per job: what would be cleared / skipped. One detoast of result_meta per job.
_SURVEY_SQL = text(f"""
SELECT j.id, j.project_id, j.capability,
       count(*) FILTER (WHERE {_CLEARABLE}) AS files,
       coalesce(sum(length(e->>'content_b64')) FILTER (WHERE {_CLEARABLE}), 0) AS bytes,
       count(*) FILTER (WHERE {_LIVE}) AS live,
       count(*) FILTER (WHERE {_NO_EXPIRY}) AS no_expiry,
       count(*) FILTER (WHERE jsonb_typeof(e) = 'object' AND e ? 'content_ref') AS table_ref
FROM deep_jobs j
CROSS JOIN LATERAL jsonb_array_elements({_FILES}) AS e
WHERE j.id = ANY(CAST(:ids AS bigint[])) AND {_SCOPE}
GROUP BY j.id, j.project_id, j.capability
""")

# Row locks only (no detoast): rows a concurrent writer holds are skipped and picked up next run.
_LOCK_SQL = text("SELECT id FROM deep_jobs WHERE id = ANY(CAST(:ids AS bigint[])) "
                 "ORDER BY id FOR UPDATE SKIP LOCKED")

# The rewrite, entirely in SQL: stats and update see the same locked rows. jsonb_agg keeps file
# order (ORDER BY ord); `e || {...}` replaces just the two keys and keeps every other key.
_CLEAR_SQL = text(f"""
WITH t AS (
  SELECT j.id,
         count(*) FILTER (WHERE {_CLEARABLE}) AS files,
         coalesce(sum(length(e->>'content_b64')) FILTER (WHERE {_CLEARABLE}), 0) AS bytes
  FROM deep_jobs j
  CROSS JOIN LATERAL jsonb_array_elements({_FILES}) AS e
  WHERE j.id = ANY(CAST(:ids AS bigint[])) AND {_SCOPE}
  GROUP BY j.id
)
UPDATE deep_jobs j
SET result_meta = jsonb_set(j.result_meta, '{{files}}', (
      SELECT jsonb_agg(
               CASE WHEN {_CLEARABLE}
                    THEN e || jsonb_build_object('content_b64', NULL::text, 'expired', true)
                    ELSE e END
               ORDER BY ord)
      FROM jsonb_array_elements(j.result_meta->'files') WITH ORDINALITY AS x(e, ord)))
FROM t
WHERE j.id = t.id AND t.files > 0
RETURNING j.id, t.files, t.bytes
""")

_TABLE_SQL = text("""
SELECT count(*), coalesce(sum(size), 0),
       count(*) FILTER (WHERE expires_at < :cutoff_naive),
       coalesce(sum(size) FILTER (WHERE expires_at < :cutoff_naive), 0),
       count(*) FILTER (WHERE expires_at IS NULL)
FROM deep_job_files
""")


@dataclass
class Report:
    cutoff: datetime
    execute: bool
    jobs_scanned: int = 0               # agent jobs in a terminal status looked at
    jobs_clearable: int = 0             # jobs with >= 1 file to clear at the cutoff
    files_clearable: int = 0
    bytes_clearable: int = 0            # sum(length(content_b64)), i.e. base64 characters
    skipped_live: int = 0               # inline content, expires_at not before the cutoff: untouched
    skipped_no_expires_at: int = 0      # inline content without a well-formed expires_at: untouched
    files_table_ref: int = 0            # result_meta files that live in deep_job_files (not touched)
    jobs_updated: int = 0
    files_cleared: int = 0
    bytes_cleared: int = 0
    jobs_locked_skipped: int = 0        # clearable but row-locked by a writer: next run
    batches_failed: int = 0
    by_project_capability: dict[tuple[int, str], list[int]] = field(
        default_factory=lambda: defaultdict(lambda: [0, 0, 0]))        # jobs, files, bytes
    table_rows: int = 0
    table_bytes: int = 0
    table_rows_expired: int = 0
    table_bytes_expired: int = 0
    table_rows_no_expires_at: int = 0

    def lines(self) -> list[str]:
        out = [
            f"mode: {'EXECUTE' if self.execute else 'DRY-RUN (nothing written)'}",
            f"cutoff (UTC): {self.cutoff.isoformat()}  (files with expires_at < cutoff are expired)",
            f"jobs scanned: {self.jobs_scanned}",
            f"jobs with files to clear: {self.jobs_clearable}",
            f"files to clear: {self.files_clearable}",
            f"base64 bytes to clear: {self.bytes_clearable}",
            f"skipped, not expired at cutoff (inline): {self.skipped_live}",
            f"skipped, no/invalid expires_at (inline): {self.skipped_no_expires_at}",
            f"files already in deep_job_files (not touched): {self.files_table_ref}",
        ]
        if self.execute:
            out += [
                f"jobs updated: {self.jobs_updated}",
                f"files cleared: {self.files_cleared}",
                f"base64 bytes cleared: {self.bytes_cleared}",
                f"jobs skipped (row locked by a writer, next run): {self.jobs_locked_skipped}",
                f"batches failed (lock/statement timeout): {self.batches_failed}",
            ]
        out.append("by project_id / capability (jobs, files, base64 bytes):")
        for (pid, cap), (jobs, files, nbytes) in sorted(self.by_project_capability.items()):
            out.append(f"  project {pid} {cap}: {jobs} jobs, {files} files, {nbytes} bytes")
        out += [
            "deep_job_files (REPORT ONLY, never cleared by this tool):",
            f"  rows: {self.table_rows}, sum(size): {self.table_bytes}",
            f"  expired at cutoff: {self.table_rows_expired} rows, sum(size): {self.table_bytes_expired}",
            f"  without expires_at: {self.table_rows_no_expires_at}",
        ]
        return out


def default_cutoff(now: datetime | None = None, margin_s: int = DEFAULT_MARGIN_S) -> datetime:
    """now - margin, whole seconds, UTC: nothing inside its live window can be older than this."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    return (now - timedelta(seconds=margin_s)).replace(microsecond=0)


def parse_cutoff(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip())
    if parsed.tzinfo is None:
        raise ValueError("cutoff must carry a UTC offset (e.g. 2026-10-23T10:00:00Z)")
    parsed = parsed.astimezone(UTC)
    if parsed.microsecond:
        raise ValueError("cutoff must be whole seconds")
    return parsed


def confirm_token(cutoff: datetime) -> str:
    return CONFIRM_PREFIX + cutoff.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _params(cutoff: datetime) -> dict[str, object]:
    # The API compares isoformat strings (job_files._expired); do the same.
    return {"cutoff": cutoff.astimezone(UTC).isoformat(), "cap_like": "agent:%"}


async def _begin(s, *, read_only: bool) -> None:
    if read_only:
        await s.execute(text("SET TRANSACTION READ ONLY"))     # must be the first statement
    await s.execute(text("SET LOCAL lock_timeout = '2s'"))
    await s.execute(text("SET LOCAL statement_timeout = '30s'"))


async def table_report(cutoff: datetime, rep: Report) -> None:
    async with get_session() as s:
        await _begin(s, read_only=True)
        row = (await s.execute(_TABLE_SQL, {
            "cutoff_naive": cutoff.astimezone(UTC).replace(tzinfo=None)})).one()
    (rep.table_rows, rep.table_bytes, rep.table_rows_expired,
     rep.table_bytes_expired, rep.table_rows_no_expires_at) = (int(x) for x in row)


async def _candidate_ids() -> list[int]:
    async with get_session() as s:
        await _begin(s, read_only=True)
        return [r[0] for r in (await s.execute(_IDS_SQL, {"cap_like": "agent:%"}))]


async def _process_batch(ids: list[int], cutoff: datetime, rep: Report, *, execute: bool) -> None:
    """One transaction: survey, then (execute) lock + rewrite. Isolated: a failure rolls back only
    this batch."""
    params = {**_params(cutoff), "ids": ids}
    async with get_session() as s:
        await _begin(s, read_only=not execute)
        rows = (await s.execute(_SURVEY_SQL, params)).all()
        clearable = []
        for jid, pid, cap, files, nbytes, live, no_expiry, table_ref in rows:
            rep.skipped_live += live
            rep.skipped_no_expires_at += no_expiry
            rep.files_table_ref += table_ref
            if files:
                clearable.append(jid)
                rep.jobs_clearable += 1
                rep.files_clearable += files
                rep.bytes_clearable += int(nbytes)
                acc = rep.by_project_capability[(pid, cap)]
                acc[0] += 1
                acc[1] += files
                acc[2] += int(nbytes)
        if not (execute and clearable):
            return
        locked = [r[0] for r in (await s.execute(_LOCK_SQL, {"ids": clearable}))]
        rep.jobs_locked_skipped += len(clearable) - len(locked)
        if not locked:
            return
        done = (await s.execute(_CLEAR_SQL, {**params, "ids": locked})).all()
        rep.jobs_updated += len(done)
        rep.files_cleared += sum(int(r[1]) for r in done)
        rep.bytes_cleared += sum(int(r[2]) for r in done)


async def run(cutoff: datetime, *, execute: bool, batch_size: int = DEFAULT_BATCH) -> Report:
    rep = Report(cutoff=cutoff, execute=execute)
    log.info("clear_expired_files: %s, cutoff=%s, batch=%d",
             "EXECUTE" if execute else "dry-run", cutoff.isoformat(), batch_size)
    ids = await _candidate_ids()
    rep.jobs_scanned = len(ids)
    for i in range(0, len(ids), batch_size):
        chunk = ids[i:i + batch_size]
        try:
            await _process_batch(chunk, cutoff, rep, execute=execute)
        except DBAPIError as exc:       # lock_timeout / statement_timeout: that batch rolled back
            rep.batches_failed += 1
            log.warning("batch %d..%d failed and was rolled back: %s", chunk[0], chunk[-1],
                        str(exc.orig)[:200])
    await table_report(cutoff, rep)
    for line in rep.lines():
        log.info("%s", line)
    return rep


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m aibroker.scripts.clear_expired_files",
                                description=__doc__.split("\n\n")[0])
    p.add_argument("--cutoff", help="UTC ISO timestamp, whole seconds (default: now - margin)")
    p.add_argument("--margin-seconds", type=int, default=DEFAULT_MARGIN_S,
                   help="safety margin for the default cutoff (default 300)")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH, help="jobs per transaction")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="default: report only")
    mode.add_argument("--execute", action="store_true", help="really clear; needs --confirm")
    p.add_argument("--confirm", help="CLEAR-EXPIRED-<cutoff as YYYY-MM-DDTHH:MM:SSZ>")
    return p


def resolve(args: argparse.Namespace, now: datetime | None = None) -> tuple[datetime, bool]:
    """Validate the arguments BEFORE any database access. Raises ValueError."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    if args.batch_size < 1 or args.margin_seconds < 0:
        raise ValueError("batch-size must be >= 1 and margin-seconds >= 0")
    cutoff = parse_cutoff(args.cutoff) if args.cutoff else default_cutoff(now, args.margin_seconds)
    if cutoff > now:
        raise ValueError(f"cutoff {cutoff.isoformat()} is in the future: live files would be touched")
    if args.execute:
        if not args.cutoff:
            raise ValueError("--execute needs an explicit --cutoff (the one printed by the dry-run)")
        if args.confirm != confirm_token(cutoff):
            raise ValueError(f"refusing to execute: --confirm must be exactly {confirm_token(cutoff)}")
    elif args.confirm:
        raise ValueError("--confirm only makes sense with --execute")
    return cutoff, bool(args.execute)


async def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    try:
        cutoff, execute = resolve(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    await init_engine()
    try:
        rep = await run(cutoff, execute=execute, batch_size=args.batch_size)
    finally:
        await close_engine()
    return 1 if rep.batches_failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
