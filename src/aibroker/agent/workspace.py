"""Per-run isolated workspace: created 0700, files collected under caps, removed after."""
from __future__ import annotations

import base64
import logging
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aibroker.agent.events import FileRef
from aibroker.agent.redact import redact_bytes

log = logging.getLogger(__name__)


def create_workspace(root: str, run_id: int) -> str:
    """Fresh empty directory root/run-<id>, mode 0700 (a leftover one is wiped first)."""
    base = Path(root)
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"run-{run_id}"
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return str(path)


def cleanup_workspace(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)


def safe_file(workspace: Path, rel: str) -> Path | None:
    """The file only if it is a regular, non-symlink file physically inside workspace."""
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        return None
    target = workspace / rel
    try:
        resolved = target.resolve(strict=True)
        root = workspace.resolve(strict=True)
    except OSError:
        return None
    if root not in resolved.parents or target.is_symlink() or not resolved.is_file():
        return None
    if any(p.is_symlink() for p in target.parents if p != workspace and workspace in p.parents):
        return None
    return resolved


def _workspace_relative(workspace: Path, rel: str) -> str | None:
    """`rel` unchanged when relative; an absolute path becomes workspace-relative when it lies
    inside the workspace (lexically, either spelling of the root), else None (outside: dropped)."""
    if not Path(rel).is_absolute():
        return rel
    target = os.path.normpath(rel)
    for root in (os.path.normpath(workspace), os.path.normpath(workspace.resolve())):
        if target.startswith(root + os.sep):
            return Path(target[len(root) + 1:]).as_posix()
    return None


def collect_files(workspace: str, paths: list[str], *, max_bytes: int, total_bytes: int,
                  max_count: int, ttl_s: int) -> list[FileRef]:
    """Read the named files into FileRef entries under the caps. Anything over a cap,
    unsafe or missing is listed WITHOUT content and with the reason in `skipped`."""
    ws = Path(workspace)
    expires = (datetime.now(UTC) + timedelta(seconds=ttl_s)).isoformat()
    out: list[FileRef] = []
    used = delivered = 0
    names: list[str] = []
    for p in paths:
        if (rel := _workspace_relative(ws, p)) is None:
            log.debug("file outside the workspace not listed: %s", p)
        else:
            names.append(rel)
    for rel in dict.fromkeys(names):
        real = safe_file(ws, rel)
        if real is None:
            out.append(FileRef(path=rel, skipped="unsafe_path_or_missing"))
            continue
        if delivered >= max_count:
            out.append(FileRef(path=rel, size=real.stat().st_size, skipped="cap_reached"))
            continue
        size = real.stat().st_size
        if size > max_bytes:
            out.append(FileRef(path=rel, size=size, skipped="too_large"))
        elif used + size > total_bytes:
            out.append(FileRef(path=rel, size=size, skipped="cap_reached"))
        else:
            used += size
            delivered += 1
            content = redact_bytes(real.read_bytes())      # no token leaves in a returned file
            out.append(FileRef(path=rel, size=len(content), expires_at=expires,
                               content_b64=base64.b64encode(content).decode()))
    return out
