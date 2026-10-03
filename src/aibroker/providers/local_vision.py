"""Self-hosted Qwen3-VL via llama.cpp — the `local` vision transport.

Free, private, no external quota. Raw HTTP to llama-server's OpenAI-compatible
endpoint; implements the same `chat` Protocol as the litellm-backed providers.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any

import httpx

from aibroker.config import get_settings
from aibroker.providers.model_identity import served_model

log = logging.getLogger(__name__)

# Schema llama-server decodes UNDER GRAMMAR CONSTRAINT, so the JSON is
# guaranteed well-formed rather than hoped for — a 4B model at Q4 asked to
# emit a JSON header as free text gets it wrong often enough to matter.
# `content` carries the answer to whatever the CALLER asked; type/format are
# classified on the same single pass (a second pass would double the CPU cost
# on a box where one image already costs ~69s).
_VISION_TYPES = ["чек", "накладная", "банковский экран", "переписка",
                 "постер", "документ", "таблица", "фото", "другое"]
_VISION_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": _VISION_TYPES},
        "format": {"type": "string", "enum": ["text", "markdown", "json"]},
        "content": {"type": "string"},
    },
    "required": ["type", "format", "content"],
}
_VISION_SYSTEM = (
    "Ты распознаёшь изображения. Верни JSON: type — вид изображения, "
    "format — в каком виде подан content (text для обычного текста, markdown "
    "для таблиц и чеков, json для строго структурированных данных), "
    "content — ответ на запрос пользователя. Числа переписывай точно как на "
    "изображении, не округляй. Не выдумывай того, чего не видно."
)
# Guard mirroring asr-local's _MAX_AUDIO_BYTES: a caller must not be able to
# push an arbitrarily large blob through the resize step.
_MAX_IMAGE_BYTES = 20 * 1024 * 1024


def _split_vision_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[bytes]]:
    """(prompt text, decoded inline images) out of OpenAI-shape multimodal
    messages. Only data: URLs are decoded — a remote URL is left for the cloud
    providers, which can fetch it; llama-server would have to egress from our
    host to do the same, which this deliberately does not do.

    Returns EVERY inline image, not just the first. The caller decides what to
    do with more than one; silently keeping only the first would answer a
    two-image question from one image and report success (see
    _describe_via_local_vision)."""
    import binascii

    texts: list[str] = []
    images: list[bytes] = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            texts.append(content)
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                texts.append(block.get("text") or "")
            elif block.get("type") == "image_url":
                url = (block.get("image_url") or {}).get("url") or ""
                if url.startswith("data:") and "base64," in url:
                    try:
                        images.append(base64.b64decode(url.split("base64,", 1)[1]))
                    except (binascii.Error, ValueError):
                        continue
    return "\n".join(t for t in texts if t).strip(), images


def _downscale(image: bytes, max_px: int) -> bytes:
    """Longest edge to `max_px`. Load-bearing, not an optimization: at native
    resolution the vision encoder does not fit in memory on this host — a probe
    ran past 600s without completing, while the same image at 1024px took 69s.
    Returns the original bytes unchanged if Pillow can't read it, so an exotic
    format degrades to "let the model try" instead of failing the request.

    An image already within `max_px` is returned BYTE-FOR-BYTE. It used to be
    re-encoded to JPEG q90 regardless, which put every small screenshot through
    a lossy pass — directly at odds with this provider's own instruction to
    copy numbers exactly, on receipts and bank screens where a mangled digit is
    the whole failure mode."""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover — Pillow is a hard dep of the image
        return image
    import io as _io

    try:
        im = Image.open(_io.BytesIO(image))
        if max(im.size) <= max_px:
            return image          # already small enough — do not touch a pixel
        im = im.convert("RGB")
    except Exception:  # noqa: BLE001 — any unreadable image: pass it through
        return image
    scale = max_px / max(im.size)
    im = im.resize((max(1, round(im.width * scale)),
                    max(1, round(im.height * scale))), Image.LANCZOS)
    buf = _io.BytesIO()
    im.save(buf, "JPEG", quality=90)
    return buf.getvalue()


# One request at a time into llama-server, per process (2 uvicorn workers →
# at most 2 in flight server-side, one running + one queued). Keyed by event
# loop because an asyncio.Semaphore binds to the loop it first waits on and
# the test-suite runs many loops; production has one loop per worker.
_local_vision_slots: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _local_vision_slot() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    slot = _local_vision_slots.get(loop)
    if slot is None:
        slot = _local_vision_slots[loop] = asyncio.Semaphore(1)
    return slot


async def _post_local_vision(url: str, payload: dict[str, Any],
                             timeout: float) -> httpx.Response:  # pragma: no cover — thin network I/O, exercised via _describe_via_local_vision's mocked tests
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=payload)


async def _describe_via_local_vision(
    *, messages: list[dict[str, Any]], max_tokens: int, temperature: float,
) -> tuple[str, dict[str, Any]]:
    """Self-hosted Qwen3-VL via llama.cpp — free, private, no external quota.

    Returns PROSE in the text slot, exactly like gemini/openrouter/openai do
    for this capability. That is deliberate: local sits at the head of the
    vision chain and any of those can answer the very next call, so a
    provider-dependent response shape would break the caller precisely on
    fallback. The structured extras ride in meta instead."""
    settings = get_settings()
    base = settings.VISION_LOCAL_URL
    if not base:
        raise RuntimeError("VISION_LOCAL_URL not configured")
    prompt, images = _split_vision_messages(messages)
    if not images:
        # No inline image: nothing a local model can do that the chain's cloud
        # providers can't do better (they can fetch a remote URL). Hand it on.
        raise RuntimeError("no inline image for local vision")
    if len(images) > 1:
        # Refuse rather than answer from image 1 of N. This provider LEADS the
        # vision chain, so quietly describing only the first image would return
        # a confident, successful, wrong answer and the request would never
        # reach gemini/openai — which do receive the whole message list. One
        # slot, one image at a time; multi-image belongs upstream.
        raise RuntimeError(
            f"local vision takes one image, got {len(images)} — escalating")
    image = images[0]
    if len(image) > _MAX_IMAGE_BYTES:
        raise RuntimeError(f"image > {_MAX_IMAGE_BYTES} bytes")
    small = await asyncio.to_thread(_downscale, image, settings.VISION_LOCAL_MAX_PX)
    payload = {
        "messages": [
            {"role": "system", "content": _VISION_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(small).decode()}},
            ]},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        # NESTING MATTERS and is verified against the running server, not the
        # docs: {"type":"json_schema","schema":{...}} is accepted and SILENTLY
        # IGNORED — llama-server answers with free-form JSON whose keys have
        # nothing to do with the schema. Only the OpenAI-style
        # json_schema.schema nesting actually constrains decoding (probe:
        # a single-value enum came back as the only legal value under this
        # form, and as unrelated invented keys under the flat one).
        "response_format": {"type": "json_schema",
                            "json_schema": {"schema": _VISION_SCHEMA}},
    }
    # Wait for the slot OUTSIDE the HTTP timeout: the 300s budget is for the
    # model working on THIS image, not for the image ahead of it. A slot that
    # stays busy past VISION_LOCAL_QUEUE_WAIT_S is a plain RuntimeError — no
    # cooldown on the local key (it is healthy, merely busy) and the walk
    # moves on to the free cloud pool; the job comes back to local on its
    # next retry if the cloud is dry.
    slot = _local_vision_slot()
    try:
        await asyncio.wait_for(slot.acquire(), timeout=settings.VISION_LOCAL_QUEUE_WAIT_S)
    except TimeoutError as e:
        raise RuntimeError(
            f"vision-local busy: slot not free within "
            f"{settings.VISION_LOCAL_QUEUE_WAIT_S:.0f}s — escalating") from e
    t0 = time.time()
    try:
        resp = await _post_local_vision(
            f"{base}/v1/chat/completions", payload, settings.VISION_LOCAL_TIMEOUT_S)
    except httpx.HTTPError as e:
        # Same reclassification as asr-local: a plain 'error' gets NO cooldown,
        # so every following request would re-hit a dead endpoint with zero
        # backoff. TimeoutError cools the key instead.
        raise TimeoutError(f"vision-local unreachable: {e}") from e
    finally:
        slot.release()
    latency_ms = int((time.time() - t0) * 1000)
    if resp.status_code >= 500:
        raise TimeoutError(f"vision-local {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 400:
        raise RuntimeError(f"vision-local {resp.status_code}: {resp.text[:200]}")
    body = resp.json()
    raw = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = body.get("usage") or {}
    vtype = vformat = None
    try:
        parsed = json.loads(raw)
        text = (parsed.get("content") or "").strip()
        vtype, vformat = parsed.get("type"), parsed.get("format")
    except (ValueError, AttributeError):
        # Grammar-constrained decoding should make this unreachable. If it
        # happens anyway (server started without schema support, a truncated
        # body), a body that still LOOKS like JSON must not be handed to the
        # caller as prose — that is exactly the shape break this provider is
        # designed to avoid, and it would only ever hit the fallback path, so
        # it would be rare and confusing. Return empty instead: run_chat books
        # EmptyBody and walks the chain to gemini. Genuine prose is kept.
        text = raw.strip()
        if text.startswith(("{", "[")):
            log.warning("vision-local returned unparseable JSON-like body "
                        "(%d chars) — treating as empty so the chain "
                        "escalates rather than leaking JSON to the caller",
                        len(text))
            text = ""
    meta = {
        "model": "local/qwen3vl",
        # llama-server names the gguf it loaded — the only place the REAL local
        # model is knowable, and it follows a model swap without a code change.
        "model_served": served_model("local/qwen3vl", body.get("model")),
        "tokens_in": usage.get("prompt_tokens", 0) or 0,
        "tokens_out": usage.get("completion_tokens", 0) or 0,
        # Self-hosted: free by construction. Set directly rather than through
        # estimate_llm_cost, which would log an "unpriced model" warning.
        "cost_usd": 0.0, "latency_ms": latency_ms,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "finish_reason": None,
        "vision_type": vtype, "vision_format": vformat,
    }
    return text, meta


class LocalVisionTransport:
    """ChatTransport for local/qwen3vl: image in, prose out (+ vision extras in meta)."""

    async def chat(
        self, *, model: str, messages: list[dict[str, Any]], api_key: str,
        max_tokens: int = 1024, temperature: float = 0.7, **_ignored: Any,
    ) -> tuple[str, dict[str, Any]]:
        return await _describe_via_local_vision(
            messages=messages, max_tokens=max_tokens, temperature=temperature)

    def call_timeout(self, capability: str) -> float | None:
        # Deliberately above the HTTP client's own timeout so the client times out
        # FIRST with a labelled TimeoutError, plus the bounded wait for the single
        # local slot (the semaphore wait happens inside the call).
        s = get_settings()
        return s.VISION_LOCAL_TIMEOUT_S + s.VISION_LOCAL_QUEUE_WAIT_S + 30.0
