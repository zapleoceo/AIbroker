"""Export of images a Codex run generated (`$CODEX_HOME/generated_images`) - done by the WORKER
process after the run, never by the model or the sandbox: CODEX_HOME is never handed to the model.

Only real, decodable PNG files are taken: fixed source directory, no symlinks (files or directories),
regular files only, inside the directory after resolving, created/modified during THIS run, size and
count capped, content verified with Pillow (dimensions come from the decoded image). Each is copied into
the run's `out/` and reported with its sha256, width and height so a critic can be given the real image.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aibroker.agent.events import FileRef

GENERATED_DIR = "generated_images"      # fixed, relative to CODEX_HOME
OUT_DIR = "out"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_MAX_DEPTH = 3
_MAX_PIXELS = 50_000_000


def _decode_png(data: bytes) -> tuple[int, int] | None:
    """(width, height) of a PNG Pillow can fully decode, else None."""
    if not data.startswith(_PNG_MAGIC):
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            if im.format != "PNG" or im.width * im.height > _MAX_PIXELS:
                return None
            width, height = im.size
        with Image.open(io.BytesIO(data)) as im:
            im.load()                                    # a truncated / corrupt file raises here
    except Exception:  # noqa: BLE001 - not a decodable PNG
        return None
    return width, height


def _candidates(root: Path) -> list[Path]:
    """Regular, non-symlink *.png files under `root` (depth-limited), no symlinked directories."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        depth = len(Path(dirpath).relative_to(root).parts)
        dirnames[:] = [d for d in dirnames
                       if depth < _MAX_DEPTH and not (Path(dirpath) / d).is_symlink()]
        found += [Path(dirpath) / f for f in sorted(filenames) if f.lower().endswith(".png")]
    return found


def export_generated_pngs(codex_home: str | None, workspace: str, since: float, *,
                          max_bytes: int, max_count: int, ttl_s: int) -> list[FileRef]:
    """Copy new PNGs from <codex_home>/generated_images into <workspace>/out and describe them.
    `since` is the run's start (epoch seconds): older files are not this run's. Anything unsafe,
    oversized, undecodable or over the count is skipped silently (never partially copied)."""
    if not codex_home:
        return []
    root = Path(codex_home) / GENERATED_DIR
    if root.is_symlink() or not root.is_dir():
        return []
    real_root = root.resolve()
    out_dir = Path(workspace) / OUT_DIR
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_s)).isoformat()
    refs: list[FileRef] = []
    seen: set[str] = set()
    for path in _candidates(root):
        if len(refs) >= max_count:
            break
        try:
            st = os.lstat(path)
            if (not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes or st.st_size == 0
                    or st.st_mtime < since):
                continue
            if real_root not in path.resolve().parents:         # traversal / symlinked parent
                continue
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(max_bytes + 1)
        except OSError:
            continue
        if len(data) > max_bytes:
            continue
        dims = _decode_png(data)
        digest = hashlib.sha256(data).hexdigest()
        if dims is None or digest in seen:
            continue
        seen.add(digest)
        out_dir.mkdir(mode=0o700, exist_ok=True)
        name = f"generated-{len(refs) + 1}-{digest[:12]}.png"
        (out_dir / name).write_bytes(data)
        refs.append(FileRef(
            path=f"{OUT_DIR}/{name}", size=len(data), sha256=digest, width=dims[0], height=dims[1],
            content_b64=base64.b64encode(data).decode(), expires_at=expires))
    return refs
