"""Export of images a Codex run generated (`$CODEX_HOME/generated_images`) - done by the WORKER
process after the run, never by the model or the sandbox: CODEX_HOME is never handed to the model.
`collect_out_images` also picks up PNG/JPEG/WebP files the run itself wrote under `out/` (a shell).

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
import itertools
import logging
import os
import re
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aibroker.agent.events import FileRef
from aibroker.agent.workspace import safe_file

log = logging.getLogger(__name__)

GENERATED_DIR = "generated_images"      # fixed, relative to CODEX_HOME
OUT_DIR = "out"
_MAGIC = {"PNG": b"\x89PNG\r\n\x1a\n", "JPEG": b"\xff\xd8\xff", "WEBP": b"RIFF"}
_EXT_FORMAT = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}
_MAX_OUT_FILES = 200                    # image-extension files looked at under out/
_MAX_OUT_SCANNED = 5000                 # directory entries looked at under out/, files or not
_MAX_OUT_DEPTH = 8
_MAX_OUT_SKIPPED = 20                   # rejected files listed with a reason
_CALL_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_DEPTH = 3
_MAX_PIXELS = 50_000_000
_SESSION_ID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _decode_image(data: bytes, fmt: str = "PNG") -> tuple[int, int] | None:
    """(width, height) of an image of format `fmt` (PNG/JPEG/WEBP) Pillow can fully decode, else
    None: the bytes must carry that format's magic and Pillow must report the same format."""
    if not data.startswith(_MAGIC[fmt]) or (fmt == "WEBP" and data[8:12] != b"WEBP"):
        return None
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            if im.format != fmt or im.width * im.height > _MAX_PIXELS:
                return None
            width, height = im.size
        with Image.open(io.BytesIO(data)) as im:
            im.load()                                    # a truncated / corrupt file raises here
    except Exception:  # noqa: BLE001 - not a decodable image
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


def _too_large(path: Path, root: Path, size: int, reason: str = "too_large") -> FileRef:
    """An over-cap generated image: named by its Codex call id only (never a host path)."""
    call_id = path.stem if path.parent == root and _CALL_ID.fullmatch(path.stem) else None
    return FileRef(path=f"{OUT_DIR}/{call_id or 'generated'}.png", size=size,
                   skipped=reason, image_call_id=call_id,
                   image_provenance="artifact_filename" if call_id else None)


def export_generated_pngs(codex_home: str | None, workspace: str, since: float, *,
                          max_bytes: int, max_count: int, ttl_s: int,
                          baseline: set[str] | None = None,
                          session_id: str | None = None,
                          total_bytes: int | None = None) -> list[FileRef]:
    """Copy PNGs from this new Codex thread's directory into <workspace>/out.
    `since` is the run's start (epoch seconds): older files are excluded. Anything unsafe,
    undecodable or over the count is skipped silently (never partially copied). An image over
    `max_bytes` is reported as a FileRef with `skipped="too_large"` and its size (no content),
    so the caller sees why the picture is missing instead of an empty `files`. `total_bytes` is the
    budget for all exported images together: one that would pass it is `skipped="total_budget"`."""
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
    used = 0
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
        dims = _decode_image(data)
        digest = hashlib.sha256(data).hexdigest()
        if dims is None or digest in seen:
            continue
        if total_bytes is not None and used + len(data) > total_bytes:
            refs.append(_too_large(path, root, len(data), "total_budget"))
            continue
        seen.add(digest)
        used += len(data)
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


def _norm(path: str) -> str:
    """A relative path as one spelling: `./out/a.png` == `out/a.png`, `/` separators."""
    return os.path.normpath(path).replace("\\", "/")


def _out_candidates(out_dir: Path) -> list[Path]:
    """Image-extension files under out/ (symlinked files included, symlinked directories not),
    sorted. The walk is bounded: every entry scanned counts (`_MAX_OUT_SCANNED`), directories deeper
    than `_MAX_OUT_DEPTH` are not entered, and it stops at once when a cap is hit."""
    found: list[Path] = []
    scanned = 0
    stack = [(out_dir, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            with os.scandir(directory) as it:
                # at most the scan budget left, then sorted: the same selection on every run
                entries = sorted(itertools.islice(it, _MAX_OUT_SCANNED - scanned + 1),
                                 key=lambda e: e.name)
                subdirs: list[Path] = []
                for entry in entries:
                    scanned += 1
                    if scanned > _MAX_OUT_SCANNED or len(found) >= _MAX_OUT_FILES:
                        log.debug("out/ scan stopped at %d entries, %d images", scanned, len(found))
                        return sorted(found)
                    if entry.is_dir(follow_symlinks=False):
                        if depth < _MAX_OUT_DEPTH:
                            subdirs.append(Path(entry.path))
                        else:
                            log.debug("out/ scan: not entering %s (depth cap)", entry.path)
                    elif Path(entry.name).suffix.lower() in _EXT_FORMAT:
                        found.append(Path(entry.path))
                stack += [(d, depth + 1) for d in reversed(subdirs)]   # popped in sorted order
        except OSError:
            continue
    return sorted(found)


def _opened_regular(real: Path, expect: os.stat_result, max_bytes: int) -> bytes | None:
    """At most max_bytes + 1 bytes of `real`, only if the opened descriptor is the same regular,
    single-link file `expect` described: a swap for a FIFO or a symlink, or a hardlink to a file
    outside the workspace, is refused. O_NONBLOCK keeps a swapped-in FIFO from hanging the open."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(real, flags)
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                    or (st.st_ino, st.st_dev) != (expect.st_ino, expect.st_dev)):
                return None
            return fh.read(max_bytes + 1)
    except OSError:
        return None


def _load_out_image(ws: Path, path: Path, max_bytes: int) -> tuple[bytes, int, str | None]:
    """(bytes, size, skip reason) of one candidate; bytes are empty when a reason is set."""
    real = safe_file(ws, path.relative_to(ws).as_posix())
    if real is None:
        return b"", 0, "unsafe_path"
    try:
        st = real.stat()
    except OSError:
        return b"", 0, "unsafe_path"
    if st.st_size == 0:
        return b"", 0, "empty"
    if st.st_size > max_bytes:
        return b"", st.st_size, "too_large"
    data = _opened_regular(real, st, max_bytes)
    if data is None:
        return b"", st.st_size, "unsafe_path"
    if len(data) > max_bytes:               # grew between stat and read
        return b"", len(data), "too_large"
    return data, len(data), None


def collect_out_images(workspace: str, *, max_bytes: int, max_count: int, ttl_s: int,
                       total_bytes: int | None = None,
                       exclude_sha256: frozenset[str] | set[str] = frozenset(),
                       exclude_paths: frozenset[str] | set[str] = frozenset()) -> list[FileRef]:
    """Images the run itself wrote under <workspace>/out/ (a shell, a script), as FileRefs shaped
    like `export_generated_pngs`: sha256, width, height, content. Never listed twice: a path in
    `exclude_paths` (already reported; compared normalised) or a sha256 in `exclude_sha256`
    (already exported, or seen here) is dropped. A file that fails a check is listed with
    `skipped` and no content: `unsafe_path` (symlink, not a regular single-link file), `empty`,
    `too_large`, `not_an_image` (the bytes are not a decodable image of the format the extension
    claims), `total_budget` (it would pass `total_bytes`, the budget for all images of the run)."""
    ws = Path(workspace)
    out_dir = ws / OUT_DIR
    if out_dir.is_symlink() or not out_dir.is_dir():
        return []
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_s)).isoformat()
    skip_paths = {_norm(p) for p in exclude_paths}
    seen = set(exclude_sha256)
    refs: list[FileRef] = []
    delivered = rejected = used = 0
    for path in _out_candidates(out_dir):
        rel = path.relative_to(ws).as_posix()
        if rel in skip_paths:
            continue
        if delivered >= max_count:
            break
        data, size, reason = _load_out_image(ws, path, max_bytes)
        dims = None if reason else _decode_image(data, _EXT_FORMAT[path.suffix.lower()])
        if reason is None and dims is None:
            reason = "not_an_image"
        digest = hashlib.sha256(data).hexdigest() if reason is None else ""
        if reason is None and digest in seen:
            continue
        if reason is None and total_bytes is not None and used + size > total_bytes:
            reason = "total_budget"
        if reason:
            if rejected < _MAX_OUT_SKIPPED:
                rejected += 1
                refs.append(FileRef(path=rel, size=size, skipped=reason))
            continue
        seen.add(digest)
        used += size
        delivered += 1
        refs.append(FileRef(path=rel, size=size, sha256=digest, width=dims[0], height=dims[1],
                            content_b64=base64.b64encode(data).decode(), expires_at=expires))
    return refs
