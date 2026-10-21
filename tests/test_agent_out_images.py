"""Images the run wrote under out/ itself (a shell) are collected; absolute paths are normalised."""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path

import pytest
from PIL import Image

from aibroker.agent.executor import _collect_out_images
from aibroker.agent.images import collect_out_images, export_generated_pngs
from aibroker.agent.workspace import collect_files

_CAPS = {"max_bytes": 1_000_000, "max_count": 4, "ttl_s": 60}


def _img(fmt: str, size=(3, 2), color=(200, 10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format=fmt)
    return buf.getvalue()


def _put(ws: Path, name: str, data: bytes) -> Path:
    path = ws / "out" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _by_path(refs):
    return {r.path: r for r in refs}


def test_shell_written_png_jpeg_webp_are_collected_with_sha_and_size(tmp_path):
    _put(tmp_path, "a.png", _img("PNG", (3, 2)))
    _put(tmp_path, "sub/b.JPG", _img("JPEG", (5, 4)))
    _put(tmp_path, "c.webp", _img("WEBP", (7, 6)))
    refs = _by_path(collect_out_images(str(tmp_path), **_CAPS))
    assert {p: (r.width, r.height) for p, r in refs.items()} == {
        "out/a.png": (3, 2), "out/sub/b.JPG": (5, 4), "out/c.webp": (7, 6)}
    for ref in refs.values():
        assert len(ref.sha256) == 64 and ref.content_b64 and ref.expires_at and not ref.skipped


def test_rejected_files_are_listed_with_a_reason(tmp_path):
    _put(tmp_path, "empty.png", b"")
    _put(tmp_path, "text.png", b"not an image at all")
    _put(tmp_path, "renamed.png", _img("JPEG"))              # JPEG bytes, .png name
    noise = Image.frombytes("RGB", (60, 60), os.urandom(60 * 60 * 3))
    buf = io.BytesIO()
    noise.save(buf, format="PNG")
    _put(tmp_path, "big.png", buf.getvalue())
    refs = _by_path(collect_out_images(str(tmp_path), **{**_CAPS, "max_bytes": 3000}))
    assert {p: r.skipped for p, r in refs.items()} == {
        "out/empty.png": "empty", "out/text.png": "not_an_image",
        "out/renamed.png": "not_an_image", "out/big.png": "too_large"}
    assert refs["out/big.png"].size > 3000
    assert all(r.content_b64 is None and r.sha256 is None for r in refs.values())


def test_a_symlink_is_listed_unsafe_and_never_read(tmp_path):
    outside = tmp_path / "secret.png"
    outside.write_bytes(_img("PNG"))
    (tmp_path / "out").mkdir(exist_ok=True)
    link = tmp_path / "out" / "link.png"
    try:
        os.symlink(outside, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    refs = collect_out_images(str(tmp_path), **_CAPS)
    assert [(r.path, r.skipped, r.content_b64) for r in refs] == [("out/link.png", "unsafe_path", None)]


def test_a_generated_png_already_exported_is_not_listed_twice(tmp_path):
    png = _img("PNG", (4, 4))
    home = tmp_path / "home"
    sid = "11111111-2222-3333-4444-555555555555"
    gen = home / "generated_images" / sid
    gen.mkdir(parents=True)
    (gen / "call_1.png").write_bytes(png)
    ws = tmp_path / "ws"
    ws.mkdir()
    images = export_generated_pngs(str(home), str(ws), 0, session_id=sid, **_CAPS)
    assert [i.path.startswith("out/generated-1-") for i in images] == [True]
    _put(ws, "copy-by-shell.png", png)                       # same bytes under another name
    _put(ws, "other.png", _img("PNG", (5, 5)))
    refs = _collect_out_images(str(ws), [], images, _cfg())
    assert [r.path for r in refs] == ["out/other.png"]


def test_a_path_collect_files_already_returned_is_not_listed_again(tmp_path):
    _put(tmp_path, "x.png", _img("PNG"))
    files = collect_files(str(tmp_path), ["out/x.png"], max_bytes=10**6, total_bytes=10**6,
                          max_count=5, ttl_s=60)
    assert files[0].content_b64
    assert _collect_out_images(str(tmp_path), files, [], _cfg()) == []


def test_identical_shell_images_are_listed_once(tmp_path):
    _put(tmp_path, "one.png", _img("PNG"))
    _put(tmp_path, "two.png", _img("PNG"))
    assert [r.path for r in collect_out_images(str(tmp_path), **_CAPS)] == ["out/one.png"]


def test_no_out_dir_or_a_symlinked_out_dir_yields_nothing(tmp_path):
    assert collect_out_images(str(tmp_path), **_CAPS) == []
    real = tmp_path / "real"
    real.mkdir()
    (real / "a.png").write_bytes(_img("PNG"))
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        os.symlink(real, ws / "out", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    assert collect_out_images(str(ws), **_CAPS) == []


def test_count_cap_and_collector_errors_never_fail_the_run(tmp_path, monkeypatch, caplog):
    for i in range(6):
        _put(tmp_path, f"i{i}.png", _img("PNG", (2 + i, 2)))
    assert len(collect_out_images(str(tmp_path), **_CAPS)) == 4
    monkeypatch.setattr("aibroker.agent.executor.collect_out_images",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with caplog.at_level(logging.ERROR):
        assert _collect_out_images(str(tmp_path), [], [], _cfg()) == []
    assert "out/ image collection failed" in caplog.text


def test_an_absolute_path_inside_the_workspace_becomes_relative_outside_is_dropped(
        tmp_path, caplog):
    ws = tmp_path / "ws"
    (ws / "out").mkdir(parents=True)
    (ws / "out" / "p.png").write_bytes(_img("PNG"))
    outside = tmp_path / "poster2-base64.txt"
    outside.write_text("x")
    paths = [str(ws / "out" / "p.png"), str(outside),
             "../escape.txt", f"{ws}/../ws2/x.txt"]
    with caplog.at_level(logging.DEBUG, logger="aibroker.agent.workspace"):
        refs = collect_files(str(ws), paths, max_bytes=10**6, total_bytes=10**6, max_count=9,
                             ttl_s=60)
    got = {r.path: r.skipped for r in refs}
    assert got == {"out/p.png": None, "../escape.txt": "unsafe_path_or_missing"}
    assert "outside the workspace" in caplog.text


def _cfg():
    from aibroker.agent.executor import ExecConfig
    return ExecConfig(workspace_root="/unused", env={}, image_max_bytes=10**6, image_max_count=4)



def test_an_unsafe_path_verdict_is_listed_without_reading(tmp_path, monkeypatch):
    _put(tmp_path, "a.png", _img("PNG"))
    monkeypatch.setattr("aibroker.agent.images._safe_file", lambda *_: None)   # symlink-free hosts
    refs = collect_out_images(str(tmp_path), **_CAPS)
    assert [(r.path, r.skipped, r.content_b64) for r in refs] == [("out/a.png", "unsafe_path", None)]
