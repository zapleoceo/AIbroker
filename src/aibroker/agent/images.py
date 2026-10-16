"""Export of images a Codex run generated (`$CODEX_HOME/generated_images`) - done by the WORKER
process after the run, never by the model or the sandbox: CODEX_HOME is never handed to the model.

Only real, decodable PNG files are taken: the fresh CLI thread's UUID directory, no symlinks
(files or directories), regular files only, inside the directory after resolving, new since this
run's snapshot, size and
count capped, content verified with Pillow (dimensions come from the decoded image). Each is copied into
the run's `out/` and reported with its sha256, width and height so a critic can be given the real image.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aibroker.agent.events import FileRef

GENERATED_DIR = "generated_images"      # fixed, relative to CODEX_HOME
OUT_DIR = "out"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_DEPTH = 3
_MAX_PIXELS = 50_000_000
_SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


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


def snapshot_generated_paths(codex_home: str | None) -> set[str] | None:
    """Record names before a run, without reading image contents or auth state.

    None means the directory cannot be trusted; the live Codex adapter then exports
    nothing. Existing paths stay excluded even if another process touches them.
    """
    if not codex_home:
        return None
    root = Path(codex_home) / GENERATED_DIR
    if root.is_symlink():
        return None
    if not root.exists():
        return set()
    if not root.is_dir():
        return None
    try:
        return {p.relative_to(root).as_posix() for p in _candidates(root)}
    except OSError:
        return None


def _too_large(path: Path, root: Path, size: int) -> FileRef:
    """An over-cap generated image: named by its Codex call id only (never a host path)."""
    call_id = path.stem if path.parent == root and _CALL_ID.fullmatch(path.stem) else None
    return FileRef(path=f"{OUT_DIR}/{call_id or 'generated'}.png", size=size,
                   skipped="too_large", image_call_id=call_id,
                   image_provenance="artifact_filename" if call_id else None)


def export_generated_pngs(codex_home: str | None, workspace: str, since: float, *,
                          max_bytes: int, max_count: int, ttl_s: int,
                          baseline: set[str] | None = None,
                          session_id: str | None = None) -> list[FileRef]:
    """Copy PNGs from this new Codex thread's directory into <workspace>/out.
    `since` is the run's start (epoch seconds): older files are excluded. Anything unsafe,
    undecodable or over the count is skipped silently (never partially copied). An image over
    `max_bytes` is reported as a FileRef with `skipped="too_large"` and its size (no content),
    so the caller sees why the picture is missing instead of an empty `files`."""
    if not codex_home or not session_id or not _SESSION_ID.fullmatch(session_id):
        return []
    root = Path(codex_home) / GENERATED_DIR
    if root.is_symlink() or not root.is_dir():
        return []
    # Codex 0.162.1 writes generated_images/<thread UUID>/<call ID>.png.
    # Never scan a sibling thread directory, including during a lease takeover.
    root = root / session_id
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
        if baseline is not None and f"{session_id}/{path.relative_to(root).as_posix()}" in baseline:
            continue
        try:
            st = os.lstat(path)
            if not stat.S_ISREG(st.st_mode) or st.st_size == 0 or st.st_mtime < since:
                continue
            if st.st_size > max_bytes:
                if real_root in path.resolve().parents:
                    refs.append(_too_large(path, root, st.st_size))
                continue
            if real_root not in path.resolve().parents:         # traversal / symlinked parent
                continue
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:
                data = fh.read(max_bytes + 1)
        except OSError:
            continue
        if len(data) > max_bytes:          # grew between lstat and read
            refs.append(_too_large(path, root, len(data)))
            continue
        dims = _decode_png(data)
        digest = hashlib.sha256(data).hexdigest()
        if dims is None or digest in seen:
            continue
        seen.add(digest)
        out_dir.mkdir(mode=0o700, exist_ok=True)
        name = f"generated-{len(refs) + 1}-{digest[:12]}.png"
        (out_dir / name).write_bytes(data)
        call_id = path.stem if path.parent == root and _CALL_ID.fullmatch(path.stem) else None
        refs.append(FileRef(
            path=f"{OUT_DIR}/{name}", size=len(data), sha256=digest, width=dims[0], height=dims[1],
            content_b64=base64.b64encode(data).decode(), expires_at=expires,
            image_call_id=call_id,
            image_provenance="artifact_filename" if call_id else None))
    return refs
