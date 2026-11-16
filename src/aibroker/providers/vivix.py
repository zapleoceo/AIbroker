"""Opt-in Vivix video transport; no retries or background generation."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

from aibroker.providers.video_contract import VideoResult
from aibroker.providers.vivix_contract import (
    VivixError,
    envelope,
    model_ids,
    video_result,
)

_WORLD_MODELS = frozenset({"vivix-w1", "vivix-w1-fast", "vivix-w1-ultrafast", "vivix-w1-auto"})


def _world(model: str) -> bool:
    base = model.split(":", 1)[0]
    if base == "vivix-a1":
        return False
    if base in _WORLD_MODELS:
        return True
    raise ValueError("Unsupported Vivix asynchronous video model")


class VivixVideoTransport:
    def __init__(self, *, http_transport: httpx.AsyncBaseTransport | None = None,
                 enabled_models: frozenset[str] = frozenset()) -> None:
        self._http_transport = http_transport
        self._enabled_models = enabled_models

    async def _request(self, method: str, path: str, api_key: str,
                       body: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(transport=self._http_transport, timeout=30,
                                    follow_redirects=False) as client:
            try:
                response = await client.request(
                    method, "https://api.vivix.ai/v1/" + path,
                    headers={"Authorization": f"Bearer {api_key}"}, json=body)
            except httpx.TransportError:
                # Submission may have been accepted: callers must not retry it blindly.
                raise TimeoutError("Vivix transport failure; submission outcome may be unknown") from None
        if not response.is_success:
            raise VivixError(-1, status_code=response.status_code)
        try:
            payload = response.json()
        except ValueError:
            raise ValueError("Invalid Vivix JSON response") from None
        return envelope(payload)

    async def list_models(self, *, api_key: str) -> frozenset[str]:
        return model_ids(await self._request("GET", "models", api_key))

    async def submit_video(self, *, model: str, inputs: dict[str, Any],
                           api_key: str) -> VideoResult:
        world = _world(model)
        if model not in self._enabled_models:
            raise ValueError("Vivix model disabled: pricing and video admission are not configured")
        if "model" in inputs:
            raise ValueError("Vivix model must be passed separately")
        if world:
            jobs = inputs.get("jobs")
            if not isinstance(jobs, list) or len(jobs) != 1 or not isinstance(jobs[0], dict):
                raise ValueError("Vivix W1 requires exactly one job")
        if model not in await self.list_models(api_key=api_key):
            raise ValueError("Vivix model not enabled for this API key")
        path = "videos/jobs" if world else "videos/generations"
        data = await self._request("POST", path, api_key, {**inputs, "model": model})
        if not world and "error_code" not in data:
            raise ValueError("Missing Vivix business status; submission outcome unknown")
        if world:
            results = data.get("results")
            if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
                raise ValueError("Invalid Vivix submission results; outcome unknown")
            data = results[0]
        task_id = data.get("job_id" if world else "video_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Missing Vivix task ID; submission outcome unknown")
        if data.get("status") != "queued":
            raise ValueError("Unexpected Vivix submission status; outcome unknown")
        return VideoResult(task_id, "queued")

    async def poll_video(self, *, model: str, video_id: str, api_key: str) -> VideoResult:
        world = _world(model)
        if not video_id or video_id in {".", ".."}:
            raise ValueError("Invalid Vivix task ID")
        path = "videos/jobs" if world else "videos/generations"
        data = await self._request("GET", f"{path}/{quote(video_id, safe='')}", api_key)
        return video_result(data, video_id=video_id, world=world)
