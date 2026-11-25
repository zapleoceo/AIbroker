"""Narrow read model for the project's MCP video jobs and their output."""
from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

from sqlalchemy import func, select

from aibroker.db import get_session
from aibroker.db.models import DeepJobRow
from aibroker.services.deep_jobs import VIDEO_MCP_WORKFLOW
from aibroker.services.video_service import VIDEO_FIELD


class VideoMcpJob(NamedTuple):
    status: str
    provider: str
    created_at: datetime
    has_clip: bool
    clip_b64: str | None


async def get_video_mcp_job(
    project_id: int, job_id: int, providers: list[str], *, clip: bool = False,
) -> VideoMcpJob | None:
    """Poll without loading the clip; fetch bytes only for a permitted completed job."""
    provider = func.coalesce(DeepJobRow.request[VIDEO_FIELD]["provider"].as_string(), "gemini")
    columns = [DeepJobRow.status, provider, DeepJobRow.created_at,
               (DeepJobRow.result_text.is_not(None) & (DeepJobRow.result_text != ""))]
    stmt = select(*columns, *([DeepJobRow.result_text] if clip else [])).where(
        DeepJobRow.id == job_id, DeepJobRow.project_id == project_id,
        DeepJobRow.capability == "video",
        DeepJobRow.request["workflow"].as_string() == VIDEO_MCP_WORKFLOW,
        provider.in_(providers),
    )
    if clip:
        stmt = stmt.where(DeepJobRow.status == "done")
    async with get_session() as session:
        row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    return VideoMcpJob(row[0], row[1], row[2], bool(row[3]), row[4] if clip else None)
