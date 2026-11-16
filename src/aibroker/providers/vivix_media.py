"""Validate trusted, URL-bound media inspection evidence without downloading anything."""
import math
from dataclasses import dataclass
from typing import Any, Literal

from aibroker.providers.gemini_video import VideoRejected
from aibroker.providers.vivix import _world


@dataclass(frozen=True)
class MediaEvidence:
    kind: Literal["image", "audio", "video"]
    size_bytes: int
    source: str
    width: int | None = None
    height: int | None = None
    duration_s: float | None = None


def validate_media(model: str, inputs: dict[str, Any], evidence: dict[str, MediaEvidence]) -> None:
    world = _world(model)
    references: list[tuple[str, str]] = []
    if world:
        job = inputs["jobs"][0]
        for kind, field in (("image", "ref_images"), ("audio", "ref_audios"), ("video", "ref_videos")):
            references.extend((r["url"], kind) for r in job.get(field, []))
        references.extend((job[f], "image") for f in ("first_frame_image", "last_frame_image") if job.get(f))
    else:
        references.append((inputs["first_frame_image"], "image"))
        if inputs.get("audio"):
            references.append((inputs["audio"], "audio"))
        for segment in inputs.get("segments") or []:
            if segment.get("first_frame_image"):
                references.append((segment["first_frame_image"], "image"))
            if segment.get("audio_url"):
                references.append((segment["audio_url"], "audio"))
    totals = {"audio": 0.0, "video": 0.0}
    for url, kind in references:
        item = evidence.get(url)
        limit = (20 if kind == "video" else 10) * 1024 * 1024
        if (item is None or item.kind != kind or not item.source.strip()
                or type(item.size_bytes) is not int or not 0 < item.size_bytes <= limit):
            raise VideoRejected("Vivix inspected media evidence is missing or invalid")
        if kind == "image":
            if (not item.width or not item.height or item.width <= 0 or item.height <= 0
                    or (world and not 0.25 <= item.width / item.height <= 4)):
                raise VideoRejected("Vivix image dimensions are invalid")
        elif world:
            duration = item.duration_s
            if duration is None or not math.isfinite(duration) or not 0 < duration <= 15:
                raise VideoRejected("Vivix reference duration is invalid")
            totals[kind] += duration
    if any(total > 20 for total in totals.values()):
        raise VideoRejected("Vivix combined reference duration exceeds 20 seconds")
