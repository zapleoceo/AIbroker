"""Google Gemini image generation (Nano Banana) via the raw generateContent endpoint.

One synchronous call: the reference images go first as `inline_data` parts, the
prompt last, and `generationConfig` asks for an IMAGE-only reply with
`imageConfig.aspectRatio` / `imageConfig.imageSize`. The reply part carries
`inlineData` (image/jpeg as of 2026-11). litellm has no image-output route for
these models, so it is a raw transport like gemini_tts.

Live-verified 2026-11-21 on gemini-3.1-flash-image with a paid key: two PNG
references + a prompt -> a 768x1376 JPEG for aspectRatio 9:16 / imageSize 1K in
8.4 s, usage 544 prompt tokens, 1429 candidate tokens (1120 of them IMAGE).

Cost is our own per-token list price (ModelSpec input/output_usd_per_mtok): every
candidate and thinking token at the image output price (an upper bound: Google
bills the non-image share at the lower text rate).
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from aibroker.config import get_settings
from aibroker.providers.model_identity import served_model
from aibroker.providers.registry import model_spec

log = logging.getLogger(__name__)

_GEMINI_IMAGE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{name}:generateContent"
IMAGE_ASPECT_RATIOS = ("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9")
IMAGE_SIZES = ("1K", "2K", "4K")
# Output tokens of one image by size (2026-11 pricing page, the larger of Flash/Pro).
_IMAGE_OUT_TOKENS = {"1K": 1120, "2K": 1680, "4K": 2520}
# Pre-call reservation only: thinking + text the model may add, prompt chars per
# token, and what one reference image is assumed to cost on input.
_EXTRA_OUT_TOKENS = 1000
_CHARS_PER_TOKEN = 4
_REFERENCE_TOKENS = 1200


class ImageRejected(ValueError):
    """The request itself was refused (HTTP 400, a blocked prompt, a reply with no
    image): another key or a retry would be refused the same way."""


@dataclass(frozen=True)
class ImageRequest:
    """One image: what the caller asked for, already validated by the route.
    `references` are (bytes, mime) pairs, sent in order before the prompt."""
    prompt: str
    references: tuple[tuple[bytes, str], ...] = ()
    aspect_ratio: str | None = None
    image_size: str = "1K"


def image_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Our list price for one call; 0.0 when the model is not priced."""
    spec = model_spec(model)
    if spec is None or spec.input_usd_per_mtok is None or spec.output_usd_per_mtok is None:
        log.warning("no per-token pricing for %s — image cost recorded as 0", model)
        return 0.0
    return (tokens_in * spec.input_usd_per_mtok + tokens_out * spec.output_usd_per_mtok) / 1e6


def estimate_image_cost(model: str, request: ImageRequest) -> float:
    """Worst-case-leaning cost for the cost guard's reservation BEFORE the call."""
    tokens_in = (len(request.prompt) // _CHARS_PER_TOKEN + 16
                 + _REFERENCE_TOKENS * len(request.references))
    tokens_out = _IMAGE_OUT_TOKENS[request.image_size] + _EXTRA_OUT_TOKENS
    return image_cost(model, tokens_in, tokens_out)


def image_body(request: ImageRequest) -> dict[str, Any]:
    """The generateContent JSON: references first, then the prompt."""
    parts: list[dict[str, Any]] = [
        {"inline_data": {"mime_type": mime, "data": base64.b64encode(raw).decode()}}
        for raw, mime in request.references]
    parts.append({"text": request.prompt})
    image_config: dict[str, str] = {"imageSize": request.image_size}
    if request.aspect_ratio:
        image_config["aspectRatio"] = request.aspect_ratio
    return {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["IMAGE"],
                                 "imageConfig": image_config}}


async def _post_gemini_image(url: str, api_key: str, body: dict[str, Any],
                             timeout: float) -> httpx.Response:  # pragma: no cover — thin network I/O, exercised via the mocked generate_image tests
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.post(url, json=body, headers={"x-goog-api-key": api_key})


def _image_part(data: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """(the first inline image part, why there is none)."""
    feedback = data.get("promptFeedback") or {}
    if feedback.get("blockReason"):
        return None, f"prompt blocked ({feedback['blockReason']})"
    reason = "no candidates"
    for cand in data.get("candidates") or []:
        reason = f"finishReason={cand.get('finishReason') or 'unknown'}"
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                return inline, ""
            if part.get("text"):
                reason += f", text: {str(part['text'])[:200]}"
    return None, reason


async def generate_via_gemini(
    *, model: str, request: ImageRequest, api_key: str,
) -> tuple[bytes, dict[str, Any]]:
    """Prompt (+ references) -> image bytes. ImageRejected on a refusal or HTTP 400;
    RuntimeError (status in the text, for classify_provider_error) on anything else."""
    timeout = get_settings().GEMINI_IMAGE_TIMEOUT_S
    url = _GEMINI_IMAGE_URL.format(name=model.split("/", 1)[1])
    t0 = time.time()
    try:
        resp = await _post_gemini_image(url, api_key, image_body(request), timeout)
    except httpx.TimeoutException as e:
        # Deliberately NOT a TimeoutError: a slow render says nothing about the key, and
        # cooling it would also stop speech and chat calls on it.
        raise RuntimeError(f"gemini-image gave up after {timeout:g}s") from e
    except httpx.HTTPError as e:
        raise RuntimeError(f"gemini-image unreachable: {e}") from e
    if resp.status_code == 400:
        raise ImageRejected(f"gemini-image HTTP 400: {resp.text[:300]}")
    if resp.status_code >= 400:
        raise RuntimeError(f"gemini-image HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    part, why = _image_part(data)
    if part is None:
        raise ImageRejected(f"gemini-image returned no image ({why})")
    image = base64.b64decode(part["data"])
    usage = data.get("usageMetadata") or {}
    tokens_in = int(usage.get("promptTokenCount") or 0)
    tokens_out = (int(usage.get("candidatesTokenCount") or 0)
                  + int(usage.get("thoughtsTokenCount") or 0)
                  or _IMAGE_OUT_TOKENS[request.image_size])
    return image, {
        "model": model,
        "model_served": served_model(model, data.get("modelVersion")),
        "mime_type": part.get("mimeType") or part.get("mime_type") or "image/png",
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": image_cost(model, tokens_in, tokens_out),
        "latency_ms": int((time.time() - t0) * 1000),
    }


class GeminiImageTransport:
    """ImageTransport for the Gemini image models."""

    async def generate_image(
        self, *, model: str, request: ImageRequest, api_key: str,
    ) -> tuple[bytes, dict[str, Any]]:
        return await generate_via_gemini(model=model, request=request, api_key=api_key)
