"""Structural REST validation; media inspection/entitlement stay trusted admission inputs.

Sources: docs.vivix.ai/video-generation/{avatar,world}-video-generation (2026-11-16).
"""
from __future__ import annotations

import base64
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aibroker.providers.vivix import _world

Text = Annotated[str, Field(min_length=1, max_length=120_000)]


def media_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.fragment or len(value) > 8192):
        raise ValueError("expected an HTTP(S) media URL without credentials or fragment")
    return value


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Reference(StrictInput):
    url: str
    _url = field_validator("url")(media_url)


class WorldJob(StrictInput):
    prompt: Text
    duration: int = Field(ge=5, le=15)
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:3", "3:4"] = "16:9"
    ref_images: list[Reference] = Field(default_factory=list, max_length=8)
    ref_audios: list[Reference] = Field(default_factory=list, max_length=4)
    ref_videos: list[Reference] = Field(default_factory=list, max_length=4)
    first_frame_image: str | None = None
    last_frame_image: str | None = None
    output_format: Literal["mp4"] = "mp4"

    @model_validator(mode="after")
    def valid_material(self) -> WorldJob:
        if not self.prompt.strip():
            raise ValueError("prompt must not be blank")
        for url in (self.first_frame_image, self.last_frame_image):
            if url is not None:
                media_url(url)
        if (self.first_frame_image or self.last_frame_image) and (
                self.ref_images or self.ref_audios or self.ref_videos):
            raise ValueError("keyframes and reference material cannot be mixed")
        return self


class WorldInputs(StrictInput):
    jobs: list[WorldJob] = Field(min_length=1, max_length=1)
    pe_mode: Literal["standard", "enhanced"] = "standard"
    seed: int | None = Field(None, ge=0, le=2147483647)


class TTSConfig(StrictInput):
    vol: float | None = None
    seed: int | None = None
    speed: float | None = None
    stability: float | None = None
    tts_model_id: Text | None = None
    tts_provider: Text | None = None


class AvatarSegment(StrictInput):
    mode: Literal["a2v", "ta2v"]
    prompt: Text
    first_frame_image: str | None = None
    audio_url: str | None = None
    audio_data: str | None = None
    dialogue: Text | None = None
    voice_id: Text | None = None
    tts_config: TTSConfig | None = None
    duration_ms: int | None = Field(None, gt=0)

    @model_validator(mode="after")
    def valid_voice(self) -> AvatarSegment:
        if not self.prompt.strip():
            raise ValueError("prompt must not be blank")
        if self.first_frame_image is not None:
            media_url(self.first_frame_image)
        if self.audio_url is not None:
            media_url(self.audio_url)
        if self.audio_data is not None:
            if len(self.audio_data) > 14 * 1024 * 1024:
                raise ValueError("audio exceeds Broker inline limit")
            raw = base64.b64decode(self.audio_data, validate=True)
            if not raw or len(raw) > 10 * 1024 * 1024:
                raise ValueError("audio must be nonempty and at most 10MB")
        if self.audio_url is not None and self.audio_data is not None:
            raise ValueError("audio URL and data are mutually exclusive")
        if self.mode == "a2v" and any(x is not None for x in (
                self.dialogue, self.voice_id, self.tts_config)):
            raise ValueError("audio mode cannot contain TTS fields")
        if self.mode == "ta2v":
            if self.audio_url is not None or self.audio_data is not None:
                raise ValueError("TTS mode cannot contain audio")
            if self.dialogue is not None and (not self.dialogue.strip() or not self.voice_id):
                raise ValueError("dialogue requires a voice")
        if self.duration_ms is not None and any(x is not None for x in (
                self.audio_url, self.audio_data, self.dialogue)):
            raise ValueError("duration_ms is only for silent segments")
        return self


class AvatarInputs(StrictInput):
    first_frame_image: str
    prompt: Text | None = None
    audio: str | None = None
    audio_data: str | None = None
    dialogue: Text | None = None
    voice_id: Text | None = None
    tts_config: TTSConfig | None = None
    segments: list[AvatarSegment] | None = Field(None, min_length=1, max_length=20)
    resolution: Literal["720p", "480p"] = "720p"
    video_ratio: Literal["16:9", "9:16"] = "16:9"

    _image = field_validator("first_frame_image")(media_url)

    @model_validator(mode="after")
    def valid_mode(self) -> AvatarInputs:
        if self.segments is not None:
            if any(x is not None for x in (self.prompt, self.audio, self.audio_data,
                                           self.dialogue, self.voice_id, self.tts_config)):
                raise ValueError("segments cannot be combined with root speech/prompt fields")
        else:
            if self.prompt is None or sum(x is not None for x in (
                    self.audio, self.audio_data, self.dialogue)) != 1:
                raise ValueError("single-segment A1 requires prompt and exactly one voice source")
            AvatarSegment(mode="ta2v" if self.dialogue is not None else "a2v",
                          prompt=self.prompt, audio_url=self.audio, audio_data=self.audio_data,
                          dialogue=self.dialogue, voice_id=self.voice_id, tts_config=self.tts_config)
        return self


def validate_inputs(model: str, inputs: dict[str, Any]) -> None:
    schema = WorldInputs if _world(model) else AvatarInputs
    schema.model_validate(inputs)


class VivixJobRequest(StrictInput):
    provider: Literal["vivix"]
    model: Text
    inputs: dict[str, Any]
    workflow: str | None = Field(None, max_length=50)

    @model_validator(mode="after")
    def valid_request(self) -> VivixJobRequest:
        validate_inputs(self.model, self.inputs)
        return self
