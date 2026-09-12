"""Submit-time inline-image validation (2026-09-12): an MP4 labelled
image/jpeg by vera's ingestor walked the whole vision chain 8 times in one
day. It must be refused at submit, permanently, with a reason."""
from __future__ import annotations

import base64
import io

from aibroker.services.vision_payload import inline_image_problem


def _png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG")
    return buf.getvalue()


def _msgs(raw: bytes | None = None, *, mime: str = "image/jpeg", url: str | None = None) -> list[dict]:
    blocks: list[dict] = [{"type": "text", "text": "describe"}]
    if raw is not None:
        blocks.append({"type": "image_url", "image_url": {
            "url": f"data:{mime};base64," + base64.b64encode(raw).decode()}})
    elif url is not None:
        blocks.append({"type": "image_url", "image_url": {"url": url}})
    return [{"role": "user", "content": blocks}]


_MP4 = b"\x00\x00\x00 ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00" * 64


def test_decodable_png_passes():
    assert inline_image_problem(_msgs(_png(), mime="image/png")) is None


def test_mp4_disguised_as_jpeg_is_named_as_video():
    problem = inline_image_problem(_msgs(_MP4))
    assert problem is not None
    assert "MP4" in problem and "image/jpeg" in problem and "#1" in problem


def test_random_bytes_are_refused_generically():
    problem = inline_image_problem(_msgs(b"\x01\x02\x03definitely not an image" * 4))
    assert problem is not None and "not a decodable image" in problem


def test_remote_url_and_plain_text_are_not_checked():
    assert inline_image_problem(_msgs(url="https://example.com/a.jpg")) is None
    assert inline_image_problem([{"role": "user", "content": "hi"}]) is None


def test_bad_base64_and_empty_are_refused():
    bad = [{"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "data:image/jpeg;base64,!!!not-base64!!!"}}]}]
    assert "base64" in (inline_image_problem(bad) or "")
    empty = [{"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "data:image/jpeg;base64,"}}]}]
    assert "empty" in (inline_image_problem(empty) or "")


def test_second_image_is_numbered():
    msgs = _msgs(_png(), mime="image/png")
    msgs[0]["content"].append({"type": "image_url", "image_url": {
        "url": "data:image/jpeg;base64," + base64.b64encode(_MP4).decode()}})
    assert "#2" in (inline_image_problem(msgs) or "")
