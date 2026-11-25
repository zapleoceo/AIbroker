"""Stateless Streamable HTTP MCP for private, paid Broker video jobs."""
from __future__ import annotations

import base64
import binascii
from typing import Annotated, Any, Literal

import orjson
from fastapi import APIRouter, Depends, HTTPException, Path, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from aibroker import __version__
from aibroker.auth import ProjectCtx, project_by_key
from aibroker.config import get_settings
from aibroker.providers.provider_specs.heygen import HEYGEN_SCOPE
from aibroker.routes.proxy import VIDEO_MAX_PROMPT_CHARS, VideoJobRequest, submit_video_clip
from aibroker.services.deep_jobs import VIDEO_MCP_WORKFLOW, next_poll_after_s
from aibroker.services.job_meta import MAX_TOKEN_LEN
from aibroker.services.request_cap import RequestCapExhausted
from aibroker.services.video_mcp_jobs import get_video_mcp_job

router = APIRouter(prefix="/mcp/video", tags=["video-mcp"])
_BEARER = HTTPBearer(auto_error=False)
_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
_BASE_SCOPE = "llm:video"
_SCOPES = frozenset((_BASE_SCOPE, HEYGEN_SCOPE))
_BODY_LIMIT = 4 * 1024 * 1024
# Stay below nginx's 4 MiB body limit even after encoding and MCP envelope.
_IMAGE_B64_LIMIT = ((2 * 1024 * 1024 + 2) // 3) * 4
_NO_CACHE = {"Cache-Control": "private, no-store"}
_MAX_JOB_ID = 9223372036854775807


async def _video_project(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_BEARER)],
) -> ProjectCtx:
    if request.headers.getlist("origin"):
        raise HTTPException(403, "Browser origins are not allowed", headers=_NO_CACHE)
    token = credentials.credentials if credentials else ""
    project = await project_by_key(token) if token.startswith("aib_prj_") and len(token) <= 80 else None
    if project is None:
        raise HTTPException(401, "Video project bearer token required",
                            headers={"WWW-Authenticate": "Bearer", **_NO_CACHE})
    scopes = set(project.allowed_scopes or [])
    if project.self_signup or _BASE_SCOPE not in scopes or not scopes.issubset(_SCOPES):
        raise HTTPException(403, "A private video-only project is required", headers=_NO_CACHE)
    return ProjectCtx(project, actor=f"project:{project.name}")


class _Generate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["gemini", "heygen"]
    prompt: str = Field(min_length=1, max_length=VIDEO_MAX_PROMPT_CHARS)
    duration_s: int = Field(5, ge=3, le=15)
    aspect_ratio: Literal["9:16", "16:9", "1:1"] = "9:16"
    resolution: Literal["360p", "720p", "480p", "768p"] | None = None
    image_base64: str | None = Field(None, max_length=_IMAGE_B64_LIMIT)
    image_mime: Literal["image/jpeg", "image/png", "image/webp"] = "image/jpeg"
    idempotency_key: str = Field(min_length=1, max_length=MAX_TOKEN_LEN,
                                 pattern=r"^[A-Za-z0-9._:-]+$")
    confirm_paid_generation: StrictBool = Field(description="Set true only with the user's approval")


class _Poll(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    job_id: int = Field(ge=1, le=_MAX_JOB_ID)


_TOOLS = [
    {"name": "video_providers", "description": "List this project's allowed video providers and billing.",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
    {"name": "video_generate", "description":
     "Queue one short paid API video. Get user approval first. Reuse the same idempotency_key "
     "for retries of the same render. Returns a job id immediately; use video_status to poll.",
     "inputSchema": _Generate.model_json_schema(),
     "annotations": {"readOnlyHint": False, "destructiveHint": True,
                     "idempotentHint": True, "openWorldHint": True}},
    {"name": "video_status", "description":
     "Check a video queued by this MCP project. A completed job returns a private MP4 link "
     "that requires the same project bearer token for HTTPS download.",
     "inputSchema": _Poll.model_json_schema(),
     "annotations": {"readOnlyHint": True, "openWorldHint": False}},
]


def _providers(ctx: ProjectCtx) -> list[str]:
    return ["gemini", "heygen"] if ctx.has_scope(HEYGEN_SCOPE) else ["gemini"]


def _tool_result(data: dict[str, Any], *, error: bool = False, link: str | None = None) -> dict[str, Any]:
    contents = [{"type": "text", "text": orjson.dumps(data).decode()}]
    if link:
        contents.append({"type": "resource_link", "uri": link, "name": "video.mp4",
                         "mimeType": "video/mp4"})
    return {"structuredContent": data, "content": contents, "isError": error}


async def _tool(name: str, args: dict[str, Any], ctx: ProjectCtx) -> dict[str, Any]:
    try:
        if name == "video_providers":
            if args:
                raise HTTPException(400, "video_providers takes no arguments")
            return _tool_result({"providers": _providers(ctx), "billing": "paid_api",
                                 "credential": "project_bearer"})
        if name == "video_generate":
            submission = _Generate.model_validate(args)
            if not submission.confirm_paid_generation:
                raise HTTPException(400, "Paid generation was not approved")
            ref = submission.image_base64
            b64_size = (len(ref) // 4) * 3 - (len(ref) - len(ref.rstrip("="))) if ref else 0
            if b64_size > 2 * 1024 * 1024:
                raise HTTPException(413, "Reference image exceeds 2 MiB")
            body = VideoJobRequest.model_validate(submission.model_dump(
                exclude={"confirm_paid_generation", "idempotency_key"}) | {"workflow": VIDEO_MCP_WORKFLOW})
            job_id = await submit_video_clip(body, ctx, submission.idempotency_key)
            return _tool_result({"job_id": job_id, "poll_after_s": 10})

        poll = _Poll.model_validate(args)
        job = await get_video_mcp_job(ctx.project.id, poll.job_id, _providers(ctx))
        if job is None:
            raise HTTPException(404, "Video job not found")
        status = "pending" if job.status == "running" else job.status
        result: dict[str, Any] = {"job_id": poll.job_id, "status": status, "provider": job.provider}
        link = None
        if status == "pending":
            result["poll_after_s"] = next_poll_after_s(job.created_at)
        if status == "done":
            result["result_available"] = job.has_clip
            if job.has_clip:
                link = f"https://{get_settings().PUBLIC_HOST}/mcp/video/jobs/{poll.job_id}/result"
                result["result_url"] = link
        return _tool_result(result, link=link)
    except ValidationError:
        # Validation errors can contain the entire input, including the reference or script.
        return _tool_result({"error": "Invalid tool arguments"}, error=True)
    except RequestCapExhausted:
        return _tool_result({"error": "Project request limit reached", "status_code": 429}, error=True)
    except HTTPException as exc:
        return _tool_result({"error": exc.detail, "status_code": exc.status_code}, error=True)


def _rpc(rpc_id: str | int | None, *, result: dict[str, Any] | None = None,
         error: tuple[int, str] | None = None, status_code: int = 200) -> JSONResponse:
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": rpc_id}
    if error is None:
        message["result"] = result
    else:
        message["error"] = {"code": error[0], "message": error[1]}
    return JSONResponse(message, status_code=status_code, headers=_NO_CACHE)


@router.post("")
async def video_mcp(request: Request, ctx: Annotated[ProjectCtx, Depends(_video_project)]) -> Response:
    """Accept one authenticated MCP JSON-RPC request or notification."""
    proto = request.headers.get("mcp-protocol-version")
    if proto is not None and proto not in _VERSIONS:
        return _rpc(None, error=(-32600, "Unsupported MCP-Protocol-Version"), status_code=400)
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return _rpc(None, error=(-32600, "Expected application/json"), status_code=415)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > _BODY_LIMIT:
            return _rpc(None, error=(-32600, "Request too large"), status_code=413)
        raw.extend(chunk)
    try:
        message = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return _rpc(None, error=(-32700, "Parse error"), status_code=400)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return _rpc(None, error=(-32600, "Invalid request"), status_code=400)
    if "id" not in message:
        # Notifications are never allowed to enqueue a paid job.
        return Response(status_code=202, headers=_NO_CACHE)
    rpc_id, method, params = message["id"], message.get("method"), message.get("params", {})
    if type(rpc_id) not in (str, int) or not isinstance(method, str):
        return _rpc(None, error=(-32600, "Invalid request"), status_code=400)
    if not isinstance(params, dict):
        return _rpc(rpc_id, error=(-32602, "Expected an object for params"))
    if method == "initialize":
        offered = params.get("protocolVersion")
        return _rpc(rpc_id, result={"protocolVersion": offered if offered in _VERSIONS else _VERSIONS[0],
                                   "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "aibroker-video", "version": __version__}})
    if method == "ping":
        return _rpc(rpc_id, result={})
    if method == "tools/list":
        return _rpc(rpc_id, result={"tools": _TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments", {})
        if name not in ("video_providers", "video_generate", "video_status") or not isinstance(args, dict):
            return _rpc(rpc_id, error=(-32602, "Unknown tool or invalid arguments object"))
        return _rpc(rpc_id, result=await _tool(name, args, ctx))
    return _rpc(rpc_id, error=(-32601, "Method not found"))


@router.get("/jobs/{job_id}/result", response_class=Response)
async def video_mcp_result(
    job_id: Annotated[int, Path(ge=1, le=_MAX_JOB_ID)],
    ctx: Annotated[ProjectCtx, Depends(_video_project)],
) -> Response:
    """Download a completed MP4 only with its originating project's token."""
    job = await get_video_mcp_job(ctx.project.id, job_id, _providers(ctx), clip=True)
    if job is None or not job.clip_b64:
        raise HTTPException(404, "Video result not found", headers=_NO_CACHE)
    try:
        clip = base64.b64decode(job.clip_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise HTTPException(404, "Video result unavailable", headers=_NO_CACHE) from exc
    return Response(clip, media_type="video/mp4",
                    headers={"Content-Disposition": f'attachment; filename="video-{job_id}.mp4"',
                             **_NO_CACHE})
