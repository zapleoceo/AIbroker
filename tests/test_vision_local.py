"""Self-hosted vision (llama.cpp / Qwen3-VL-4B) — the `local` provider on the
vision chain.

The service itself is upstream `llama-server` (no code of ours), so everything
worth testing lives in the broker's adapter branch: pulling the prompt and the
image out of an OpenAI-shape multimodal message, downscaling the image, and —
most importantly — collapsing llama-server's structured JSON back to PROSE
before it reaches the caller.
"""
from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aibroker.config import get_settings
from aibroker.providers.litellm_adapter import (
    _describe_via_local_vision,
    _downscale,
    _split_vision_messages,
    call_llm,
    model_for,
)
from aibroker.services.llm_service import _call_timeout

_URL = "http://vision-local:8080"


def _png(w: int, h: int) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 90, 150)).save(buf, "PNG")
    return buf.getvalue()


def _msgs(image: bytes | None = None, text: str = "что на фото?",
          url: str | None = None) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": text}]
    if image is not None:
        blocks.append({"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(image).decode()}})
    elif url is not None:
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    return [{"role": "user", "content": blocks}]


def _reply(content: dict | str, status: int = 200) -> SimpleNamespace:
    body = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
    return SimpleNamespace(
        status_code=status, text=body,
        json=lambda: {"choices": [{"message": {"content": body}}],
                      "usage": {"prompt_tokens": 379, "completion_tokens": 123}},
    )


# ─── wiring ─────────────────────────────────────────────────────────────────


def test_model_for_local_vision():
    assert model_for("local", "vision") == "local/qwen3vl"


def test_call_timeout_is_provider_aware():
    """A flat 60s would abort every local call: one image measured 69s on this
    hardware, 192s for a dense document."""
    assert _call_timeout("vision", "gemini") == 60.0
    assert _call_timeout("vision", "local") > 200.0
    # 2026-09-12: the ceiling also covers the bounded wait for the local slot
    # (the semaphore wait happens INSIDE call_llm).
    s = get_settings()
    assert _call_timeout("vision", "local") == (
        s.VISION_LOCAL_TIMEOUT_S + s.VISION_LOCAL_QUEUE_WAIT_S + 30.0)
    # chat:deep keeps precedence over the provider rule.
    assert _call_timeout("chat:deep", "local") == 19 * 60.0


# ─── message splitting ──────────────────────────────────────────────────────


def test_split_extracts_prompt_and_inline_image():
    raw = _png(20, 10)
    prompt, images = _split_vision_messages(_msgs(raw, "опиши"))
    assert prompt == "опиши"
    assert images == [raw]


def test_split_returns_every_image_not_just_the_first():
    """REGRESSION: it used to keep only the first and drop the rest silently."""
    a, b = _png(20, 10), _png(30, 15)
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "сравни"},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(a).decode()}},
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(b).decode()}},
    ]}]
    _, images = _split_vision_messages(msgs)
    assert images == [a, b]


def test_split_ignores_remote_url():
    """A remote URL is left for the cloud providers, which can fetch it —
    llama-server would have to egress from our host to do the same."""
    prompt, images = _split_vision_messages(_msgs(url="https://example.com/a.jpg"))
    assert images == []
    assert prompt == "что на фото?"


def test_split_survives_undecodable_base64():
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,!!!not!!!"}}]}]
    _, images = _split_vision_messages(msgs)
    assert images == []


def test_split_handles_plain_string_content():
    prompt, images = _split_vision_messages([{"role": "user", "content": "просто текст"}])
    assert prompt == "просто текст"
    assert images == []


# ─── downscaling ────────────────────────────────────────────────────────────


def test_downscale_shrinks_long_edge_and_keeps_aspect():
    from PIL import Image

    out = _downscale(_png(2000, 1000), 1024)
    im = Image.open(io.BytesIO(out))
    assert max(im.size) == 1024
    assert im.size == (1024, 512)


def test_downscale_leaves_small_image_byte_identical():
    """REGRESSION: an already-small image used to be re-encoded to JPEG q90
    anyway. This provider's own prompt tells the model to copy numbers exactly;
    putting a receipt screenshot through a lossy pass first works against that,
    for no benefit."""
    src = _png(300, 200)
    assert _downscale(src, 1024) == src


def test_downscale_passes_through_unreadable_bytes():
    """An exotic format degrades to 'let the model try' rather than failing."""
    assert _downscale(b"not an image at all", 1024) == b"not an image at all"


# ─── the call ───────────────────────────────────────────────────────────────


async def test_local_vision_returns_prose_not_json(monkeypatch):
    """THE regression this whole design turns on. local leads the vision chain
    and gemini can answer the very next call, so `text` must be prose for both
    — a provider-dependent response shape would break the caller precisely on
    fallback. The structured extras ride in meta instead."""
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    reply = _reply({"type": "чек", "format": "markdown",
                    "content": "Чек из FESTIVAL MARKET на 200,000"})
    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                AsyncMock(return_value=reply)):
        text, meta = await _describe_via_local_vision(
            messages=_msgs(_png(50, 50)), max_tokens=400, temperature=0.1)
    assert text == "Чек из FESTIVAL MARKET на 200,000"
    assert not text.startswith("{")
    assert meta["vision_type"] == "чек"
    assert meta["vision_format"] == "markdown"
    assert meta["cost_usd"] == 0.0          # self-hosted: free by construction
    assert meta["model"] == "local/qwen3vl"
    assert meta["tokens_in"] == 379


async def test_local_vision_downscales_before_sending(monkeypatch):
    """Not an optimization: at native resolution the encoder does not fit in
    memory on the prod host — a probe ran past 600s without completing."""
    from PIL import Image

    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    captured = {}

    async def fake_post(url, payload, timeout):
        captured["payload"] = payload
        return _reply({"type": "фото", "format": "text", "content": "кот"})

    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                side_effect=fake_post):
        await _describe_via_local_vision(
            messages=_msgs(_png(3000, 1500)), max_tokens=400, temperature=0.1)

    sent = captured["payload"]["messages"][-1]["content"][-1]["image_url"]["url"]
    im = Image.open(io.BytesIO(base64.b64decode(sent.split("base64,", 1)[1])))
    assert max(im.size) == get_settings().VISION_LOCAL_MAX_PX
    # Grammar-constrained decoding, so the JSON is guaranteed, not hoped for.
    # The NESTING is the load-bearing part and was verified against a running
    # llama-server: the flat {"type":"json_schema","schema":...} form is
    # accepted and silently ignored (the server answers with free-form JSON),
    # so a probe of a single-value enum came back as invented keys. Only
    # json_schema.schema actually constrains decoding. Asserted because
    # regressing it fails silently in production, not loudly in CI.
    rf = captured["payload"]["response_format"]
    assert rf["type"] == "json_schema"
    assert rf["json_schema"]["schema"]["properties"]["type"]["enum"]
    assert "schema" not in rf, "flat nesting is silently ignored by llama-server"


async def test_local_vision_falls_back_to_raw_body_when_not_json(monkeypatch):
    """The grammar should make this unreachable; if the server was started
    without schema support the body is still a usable answer."""
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                AsyncMock(return_value=_reply("  просто описание  "))):
        text, meta = await _describe_via_local_vision(
            messages=_msgs(_png(50, 50)), max_tokens=400, temperature=0.1)
    assert text == "просто описание"
    assert meta["vision_type"] is None


async def test_local_vision_json_like_garbage_becomes_empty(monkeypatch):
    """A body that fails to parse but still LOOKS like JSON must NOT reach the
    caller as prose — that is the exact shape break this provider exists to
    avoid. Empty makes run_chat book EmptyBody and walk on to gemini."""
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                AsyncMock(return_value=_reply('{"type": "чек", "content": trunc'))):
        text, _ = await _describe_via_local_vision(
            messages=_msgs(_png(50, 50)), max_tokens=400, temperature=0.1)
    assert text == ""


async def test_local_vision_without_url_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", "")
    with pytest.raises(RuntimeError, match="VISION_LOCAL_URL"):
        await _describe_via_local_vision(
            messages=_msgs(_png(10, 10)), max_tokens=400, temperature=0.1)


async def test_local_vision_refuses_multiple_images(monkeypatch):
    """local LEADS the vision chain, so answering a two-image question from
    image 1 would return a confident wrong answer and the request would never
    reach gemini/openai, which do get the whole message list."""
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    a, b = _png(20, 10), _png(30, 15)
    msgs = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(x).decode()}}
        for x in (a, b)]}]
    with pytest.raises(RuntimeError, match="one image"):
        await _describe_via_local_vision(
            messages=msgs, max_tokens=400, temperature=0.1)


async def test_local_vision_rejects_request_without_inline_image(monkeypatch):
    """Hand it to the chain's cloud providers, which can fetch a remote URL."""
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    with pytest.raises(RuntimeError, match="no inline image"):
        await _describe_via_local_vision(
            messages=_msgs(url="https://example.com/a.jpg"),
            max_tokens=400, temperature=0.1)


async def test_local_vision_unreachable_becomes_timeout_error(monkeypatch):
    """TimeoutError, not a bare HTTPError: classify_provider_error gives a
    plain 'error' NO cooldown, so every following request would re-hit a dead
    endpoint with zero backoff."""
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                AsyncMock(side_effect=httpx.ConnectError("refused"))),          pytest.raises(TimeoutError, match="vision-local unreachable"):
        await _describe_via_local_vision(
            messages=_msgs(_png(10, 10)), max_tokens=400, temperature=0.1)


@pytest.mark.parametrize("status,exc", [(503, TimeoutError), (400, RuntimeError)])
async def test_local_vision_http_error_classes(monkeypatch, status, exc):
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                AsyncMock(return_value=_reply("boom", status=status))),          pytest.raises(exc):
        await _describe_via_local_vision(
            messages=_msgs(_png(10, 10)), max_tokens=400, temperature=0.1)


async def test_call_llm_routes_local_prefix_away_from_litellm(monkeypatch):
    """call_llm is the single entry point for nine capabilities and `local` is
    not a LiteLLM provider — acompletion has never heard of the prefix."""
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                AsyncMock(return_value=_reply(
                    {"type": "фото", "format": "text", "content": "кот на диване"}))), \
         patch("aibroker.providers.litellm_adapter.litellm.acompletion",
                AsyncMock(side_effect=AssertionError("must not reach litellm"))):
        text, meta = await call_llm(
            model="local/qwen3vl", messages=_msgs(_png(40, 40)),
            api_key="unused", capability="vision")
    assert text == "кот на диване"


# ─── one local slot per process (2026-09-12) ─────────────────────────────────


async def test_local_vision_serialises_on_one_slot_and_escalates_when_busy(monkeypatch):
    """llama-server runs --parallel 1. Before the slot, a second request queued
    INSIDE the server against the 300s HTTP timeout, timed out, cooled the
    local key and spilled every image behind it to the rate-limited cloud
    (45 TimeoutErrors/day). Now the second caller waits in-process for up to
    VISION_LOCAL_QUEUE_WAIT_S; past that it is a RuntimeError (no cooldown —
    local is healthy, merely busy) and the walk moves on."""
    import asyncio

    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_QUEUE_WAIT_S", 0.05)
    release = asyncio.Event()
    inflight = {"n": 0, "max": 0}

    async def slow_post(url, payload, timeout):
        inflight["n"] += 1
        inflight["max"] = max(inflight["max"], inflight["n"])
        await release.wait()
        inflight["n"] -= 1
        return _reply({"type": "чек", "format": "text", "content": "ok"})

    with patch("aibroker.providers.litellm_adapter._post_local_vision", slow_post):
        first = asyncio.create_task(_describe_via_local_vision(
            messages=_msgs(_png(50, 50)), max_tokens=100, temperature=0.1))
        await asyncio.sleep(0.01)                      # first holds the slot
        with pytest.raises(RuntimeError, match="busy"):
            await _describe_via_local_vision(
                messages=_msgs(_png(50, 50)), max_tokens=100, temperature=0.1)
        release.set()
        text, _ = await first
    assert text == "ok"
    assert inflight["max"] == 1                        # never two in the server


async def test_local_vision_slot_is_released_after_an_error(monkeypatch):
    """A failed call must not leak the slot, or every later image would time
    out on the semaphore and escalate forever."""
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_URL", _URL)
    monkeypatch.setattr(get_settings(), "VISION_LOCAL_QUEUE_WAIT_S", 0.05)
    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                AsyncMock(side_effect=httpx.ConnectError("down"))), \
         pytest.raises(TimeoutError):
        await _describe_via_local_vision(
            messages=_msgs(_png(50, 50)), max_tokens=100, temperature=0.1)
    with patch("aibroker.providers.litellm_adapter._post_local_vision",
                AsyncMock(return_value=_reply({"type": "x", "format": "text", "content": "again"}))):
        text, _ = await _describe_via_local_vision(
            messages=_msgs(_png(50, 50)), max_tokens=100, temperature=0.1)
    assert text == "again"
