"""Inline video and PDF blocks in chat messages (the `vision` lane).

Two block shapes carry them, both next to the usual `image_url` blocks:

    {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}}
    {"type": "file", "file": {"file_data": "data:application/pdf;base64,...",
                              "filename": "scan.pdf"}}

`file` is OpenAI's own shape for a PDF; `video_url` mirrors `image_url`. A `file`
block may also carry a video. Only Gemini models read either inline
(`ModelSpec.media_inputs`); the walk keeps to those models when a request
carries one (services/llm_service.run_chat). Submit-time validation lives in
services/vision_payload.py.
"""
from __future__ import annotations

from typing import Any

VIDEO_MIMES = frozenset({"video/mp4", "video/webm", "video/quicktime"})
PDF_MIME = "application/pdf"
# What a Gemini chat model reads inline next to images (verified live 2026-11-21:
# mp4 via `video_url` and `file`, PDF via `file`, gemini-3.5-flash-lite).
GEMINI_MEDIA_INPUTS = frozenset({"video", "pdf"})
MEDIA_BLOCK_TYPES = frozenset({"video_url", "file"})
# Token proxy for the cost guard / context filter, per decoded MB of media.
# Gemini samples video at 1 fps (~300 tokens/s with audio); a Telegram clip is
# roughly 0.3-1 MB/s. A PDF page is ~258 tokens and ~30-100 KB.
_TOKENS_PER_MB = {"video": 1000, "pdf": 4000}
_MIN_TOKENS = 1000


def data_url_of(block: dict[str, Any]) -> str | None:
    """The URL a video_url / file block carries (None when absent or not a string)."""
    if block.get("type") == "video_url":
        inner = block.get("video_url")
        url = inner.get("url") if isinstance(inner, dict) else inner
    elif block.get("type") == "file":
        inner = block.get("file")
        url = inner.get("file_data") if isinstance(inner, dict) else None
    else:
        return None
    return url if isinstance(url, str) else None


def declared_mime(url: str) -> str:
    """`data:video/mp4;base64,...` -> `video/mp4` ('' for anything else)."""
    if not url.startswith("data:") or ";" not in url[:100]:
        return ""
    return url[5:url.index(";")].strip().lower()


def block_kind(block: Any) -> str | None:
    """'video' / 'pdf' for a media block, 'unsupported' for a media block of any
    other type, None for every non-media block (text, image_url, ...)."""
    if not isinstance(block, dict) or block.get("type") not in MEDIA_BLOCK_TYPES:
        return None
    mime = declared_mime(data_url_of(block) or "")
    if mime in VIDEO_MIMES:
        return "video"
    if mime == PDF_MIME and block.get("type") == "file":
        return "pdf"
    return "unsupported"


def _blocks(messages: list[dict[str, Any]]):
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            yield from content


def media_kinds(messages: list[dict[str, Any]]) -> frozenset[str]:
    """The media kinds a request carries ({'video', 'pdf'} subset; 'unsupported'
    too when a media block has a type no model reads)."""
    return frozenset(k for b in _blocks(messages) if (k := block_kind(b)) is not None)


def media_chars(block: dict[str, Any]) -> int:
    """Character proxy of one media block for estimate_prompt_tokens (4 chars/token)."""
    kind = block_kind(block) or "pdf"
    url = data_url_of(block) or ""
    decoded_mb = len(url) * 3 / 4 / 1_000_000
    tokens = max(_MIN_TOKENS, int(decoded_mb * _TOKENS_PER_MB.get(kind, _TOKENS_PER_MB["pdf"])))
    return tokens * 4


def gemini_media_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`messages` with every video_url block rewritten as the `file` block litellm's
    Gemini transformation reads (it has no `video_url` branch and would drop it).
    The input is never mutated: it is the stored job request."""
    if not any(isinstance(b, dict) and b.get("type") == "video_url" for b in _blocks(messages)):
        return messages
    out = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            out.append(message)
            continue
        blocks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "video_url":
                url = data_url_of(block) or ""
                block = {"type": "file", "file": {"file_data": url, "format": declared_mime(url)}}
            blocks.append(block)
        out.append({**message, "content": blocks})
    return out
