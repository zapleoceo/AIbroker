"""Strict Vivix REST envelopes and asynchronous video results."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from aibroker.providers.video_contract import VideoResult


class VivixError(RuntimeError):
    def __init__(self, code: int, *, status_code: int | None = None) -> None:
        self.code = code
        self.status_code = status_code
        # Upstream messages can echo credentials, prompts and signed media URLs.
        super().__init__(f"Vivix upstream failure ({code})")


def envelope(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or type(payload.get("code")) is not int:
        raise ValueError("Invalid Vivix response envelope")
    if payload["code"] != 0:
        raise VivixError(payload["code"])
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValueError("Invalid Vivix response data")
    code = data.get("error_code", 0)
    if type(code) is not int:
        raise ValueError("Invalid Vivix business error code")
    if code:
        raise VivixError(code)
    return data


def model_ids(data: dict[str, Any]) -> frozenset[str]:
    entries = data.get("models")
    if not isinstance(entries, list):
        raise ValueError("Invalid Vivix models list")
    result = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Invalid Vivix model entry")
        name, tag = entry.get("name"), entry.get("tag")
        if not isinstance(name, str) or not name or ":" in name:
            raise ValueError("Invalid Vivix model name")
        if "tag" in entry and (not isinstance(tag, str) or not tag):
            raise ValueError("Invalid Vivix model tag")
        result.add(f"{name}:{tag}" if tag is not None else name)
    return frozenset(result)


def video_result(data: dict[str, Any], *, video_id: str, world: bool) -> VideoResult:
    if not world:
        items = data.get("items")
        if not isinstance(items, list):
            raise ValueError("Invalid Vivix task list")
        if not items:
            return VideoResult(video_id, "pending")
        if len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("Invalid Vivix task result")
        data = items[0]
    id_field, url_field = ("job_id", "video_url") if world else ("video_id", "playback_url")
    if data.get(id_field) != video_id:
        raise ValueError("Vivix task identity mismatch")
    status = data.get("status")
    allowed = {"queued", "processing", "succeeded", "failed"}
    if not world:
        allowed |= {"ready", "timeout"}
    if not isinstance(status, str) or status not in allowed:
        raise ValueError("Invalid Vivix task status")
    url = data.get(url_field)
    if status == "succeeded":
        if not isinstance(url, str) or not url.startswith("https://") or not urlsplit(url).netloc:
            raise ValueError("Vivix succeeded without a playable HTTPS URL")
        return VideoResult(video_id, status, url)
    return VideoResult(video_id, status)
