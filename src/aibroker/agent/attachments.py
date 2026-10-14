"""Input attachments (images): the REAL bytes are written into the run workspace and the
CLI reads them as files - never replaced by a text description.

Caps: MAX_FILES files, MAX_BYTES each, MAX_TOTAL_BYTES together; only the types below, and
the bytes must match the declared type (magic number)."""
from __future__ import annotations

import base64
import binascii
import os
import re
from pathlib import Path
from typing import Any

MAX_FILES = 5
MAX_BYTES = 5 * 1024 * 1024
MAX_TOTAL_BYTES = 10 * 1024 * 1024
NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$"
INPUT_DIR = "inputs"

_TYPES: dict[str, tuple[str, ...]] = {
    "image/png": (".png",), "image/jpeg": (".jpg", ".jpeg"),
    "image/webp": (".webp",), "image/gif": (".gif",),
}


def _magic_ok(media_type: str, data: bytes) -> bool:
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return data[:6] in (b"GIF87a", b"GIF89a")


def decode_attachment(att: dict[str, Any]) -> bytes:
    """Bytes of one attachment, or ValueError naming the problem."""
    name, mt = str(att.get("name")), str(att.get("media_type"))
    if not re.fullmatch(NAME_PATTERN, name):
        raise ValueError(f"{name!r}: invalid file name")
    if mt not in _TYPES:
        raise ValueError(f"{name}: media_type {mt} not allowed")
    if not name.lower().endswith(_TYPES[mt]):
        raise ValueError(f"{name}: extension does not match {mt}")
    try:
        data = base64.b64decode(str(att.get("data_b64") or ""), validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"{name}: invalid base64") from e
    if not data or len(data) > MAX_BYTES:
        raise ValueError(f"{name}: empty or larger than {MAX_BYTES} bytes")
    if not _magic_ok(mt, data):
        raise ValueError(f"{name}: content is not a valid {mt}")
    return data


def validate_attachments(atts: list[dict[str, Any]]) -> None:
    """Whole-request check (count, duplicates, total size) -> ValueError."""
    if len(atts) > MAX_FILES:
        raise ValueError(f"at most {MAX_FILES} attachments")
    if len({a.get("name") for a in atts}) != len(atts):
        raise ValueError("attachment names must be unique")
    if sum(len(decode_attachment(a)) for a in atts) > MAX_TOTAL_BYTES:
        raise ValueError(f"attachments exceed {MAX_TOTAL_BYTES} bytes in total")


def write_attachments(workspace: str, atts: list[dict[str, Any]]) -> list[str]:
    """Write the files under <workspace>/inputs (0600) -> their workspace-relative paths."""
    validate_attachments(atts)
    out: list[str] = []
    base = Path(workspace) / INPUT_DIR
    if atts:
        base.mkdir(mode=0o700, exist_ok=True)
    for a in atts:
        target = base / str(a["name"])
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(decode_attachment(a))
        out.append(f"{INPUT_DIR}/{a['name']}")
    return out


def strip_attachment_data(atts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep name/type/size, drop the bytes (applied when the run is terminal)."""
    return [{"name": a.get("name"), "media_type": a.get("media_type"), "data_b64": None}
            for a in atts]
