"""Static checks of the Codex container's seccomp + AppArmor profiles and compose wiring (nothing is
loaded or run), and the worker-side export of generated PNGs."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from aibroker.agent import events as ev
from aibroker.agent.adapter import AgentRunSpec
from aibroker.agent.codex_cli import CodexCLI, build_codex_prompt
from aibroker.agent.constants import AGENT_SCOPE
from aibroker.agent.executor import ExecConfig, execute_run
from aibroker.agent.fake import FakeAgentCLI
from aibroker.agent.images import GENERATED_DIR, export_generated_pngs, snapshot_generated_paths
from aibroker.agent.real_cli import SubprocessCLI
from aibroker.agent.registry import clear_adapters, register_adapter
from aibroker.agent.runs import create_run, list_events
from aibroker.agent.workspace import create_workspace
from aibroker.auth import generate_project_key, hash_project_key
from aibroker.config import get_settings
from aibroker.db import get_session
from aibroker.db.models import DeepJobRow, ProjectRow
from aibroker.main import app

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra" / "agent-worker"
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8").replace("\r\n", "\n")
DOCS = (ROOT / "docs" / "agent-runs.md").read_text(encoding="utf-8")
THREAD = "123e4567-e89b-12d3-a456-426614174000"
OTHER_THREAD = "123e4567-e89b-12d3-a456-426614174001"


def _service(name: str) -> str:
    m = re.search(rf"^  {name}:\n(.*?)(?=^  [a-z][a-z-]*:\n|^networks:|^volumes:)", COMPOSE,
                  re.S | re.M)
    assert m, name
    return m.group(1)


@pytest.fixture(autouse=True)
def _adapters(monkeypatch):
    monkeypatch.setattr(get_settings(), "AGENT_SUBSCRIPTION_CODEX_ENABLED", True)
    clear_adapters()
    yield
    clear_adapters()


# --- seccomp -----------------------------------------------------------------------------------------------

NS_FLAGS = {"NEWNS": 0x00020000, "NEWCGROUP": 0x02000000, "NEWUTS": 0x04000000, "NEWIPC": 0x08000000,
            "NEWUSER": 0x10000000, "NEWPID": 0x20000000, "NEWNET": 0x40000000}


def _load(name):
    return json.loads((INFRA / name).read_text(encoding="utf-8"))


def test_codex_seccomp_is_the_moby_default_plus_exactly_two_documented_entries():
    base, codex = _load("moby-seccomp-default.json"), _load("codex-seccomp.json")
    extra = codex["syscalls"][len(base["syscalls"]):]
    assert codex["syscalls"][:len(base["syscalls"])] == base["syscalls"]       # nothing else touched
    assert {k: v for k, v in codex.items() if k != "syscalls"} == \
        {k: v for k, v in base.items() if k != "syscalls"}                    # same default action, arches
    assert len(extra) == 2 and base["defaultAction"] == "SCMP_ACT_ERRNO"
    unshare, others = extra
    assert unshare["names"] == ["unshare"] and unshare["action"] == "SCMP_ACT_ALLOW"
    assert others["names"] == ["clone", "setns", "mount", "umount2", "pivot_root"]
    assert others["action"] == "SCMP_ACT_ALLOW"
    for rule in extra:
        assert "includes" not in rule and "excludes" not in rule
        assert rule["comment"].startswith("aibroker-codex")
    assert "args" not in others


def test_unshare_is_allowed_only_with_namespace_flags():
    base_mask = sum(NS_FLAGS.values())
    assert base_mask == 2114060288                               # moby's own namespace mask
    (arg,) = (r for r in _load("codex-seccomp.json")["syscalls"][-2]["args"])
    assert arg["index"] == 0 and arg["op"] == "SCMP_CMP_MASKED_EQ" and arg["valueTwo"] == 0
    assert arg["value"] == (~base_mask) & 0xFFFFFFFF == 0x81FDFFFF         # 32-bit: upper garbage ignored

    def allowed(flags):                                           # (flags & mask) == 0
        return flags & arg["value"] == 0

    assert allowed(NS_FLAGS["NEWUSER"] | NS_FLAGS["NEWNS"] | NS_FLAGS["NEWPID"] | NS_FLAGS["NEWNET"])
    assert allowed(sum(NS_FLAGS.values()))
    assert not allowed(0x80)                                      # CLONE_NEWTIME
    assert not allowed(0x200)                                     # CLONE_FS (a non-namespace flag)
    assert not allowed(NS_FLAGS["NEWUSER"] | 0x100)               # CLONE_VM mixed in


def test_the_additions_do_not_open_anything_else():
    base, codex = _load("moby-seccomp-default.json"), _load("codex-seccomp.json")
    added = {n for r in codex["syscalls"][len(base["syscalls"]):] for n in r["names"]}
    assert added == {"unshare", "clone", "setns", "mount", "umount2", "pivot_root"}
    assert "clone3" not in added                                  # stays ENOSYS (libc falls back to clone)
    for dangerous in ("bpf", "ptrace", "keyctl", "add_key", "request_key", "init_module", "finit_module",
                      "kexec_load", "reboot", "swapon", "open_by_handle_at", "perf_event_open"):
        assert dangerous not in added
    clone3 = [r for r in codex["syscalls"] if r["names"] == ["clone3"]]
    assert clone3 and clone3[0]["action"] == "SCMP_ACT_ERRNO" and clone3[0]["errnoRet"] == 38


def test_the_baseline_copy_records_its_source_in_the_docs():
    for needle in ("6fe7deb1b9fb7c0397a4593480d7d22b9ee8caef", "2026-09-17", "moby/profiles",
                   "a21872828a8e5745d79e2fc1a07ee6dad8aedffa", "2026-09-29"):
        assert needle in DOCS, needle


# --- AppArmor ----------------------------------------------------------------------------------------------


def _rules(text: str) -> list[str]:
    return [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def test_apparmor_profile_equals_docker_default_except_the_mount_block():
    base = (INFRA / "apparmor-docker-default.rendered").read_text(encoding="utf-8")
    prof = (INFRA / "apparmor-codex").read_text(encoding="utf-8")
    block = re.search(r"  # BEGIN aibroker-codex additions.*?\n(.*?)  # END aibroker-codex additions\n",
                      prof, re.S)
    assert block
    outside = prof.replace(block.group(0), "  deny mount,\n")
    assert _rules(outside) == _rules(base)                        # the ONLY difference: that block
    added = [a.strip() for a in _rules(block.group(1))]
    assert added and all(a.startswith(("mount ", "pivot_root")) for a in added)
    assert "  deny mount," in base.splitlines()


def test_apparmor_keeps_every_docker_default_deny_and_adds_no_blanket_rule():
    prof = _rules((INFRA / "apparmor-codex").read_text(encoding="utf-8"))
    joined = "\n".join(prof)
    for must in ("deny @{PROC}/* w,", "deny @{PROC}/sysrq-trigger rwklx,", "deny @{PROC}/kcore rwklx,",
                 "deny /sys/firmware/** rwklx,", "deny /sys/kernel/security/** rwklx,",
                 "deny network vsock,", "deny network alg,", 'profile "aibroker-codex"'):
        assert must in joined, must
    assert "deny mount," not in prof and "  mount," not in prof and "  mount ," not in prof
    assert "unconfined" not in [p for p in prof if p.startswith("profile")]


def test_apparmor_additions_are_the_expected_bwrap_set():
    prof = (INFRA / "apparmor-codex").read_text(encoding="utf-8")
    block = prof.split("# BEGIN aibroker-codex additions", 1)[1].split("# END aibroker-codex additions", 1)[0]
    for rule in ("mount options=(rw, rslave) -> /,", "mount options=(rw, rprivate) -> /,", "pivot_root,",
                 "mount fstype=tmpfs,", "mount fstype=proc -> /**,"):
        assert rule in block, rule
    assert "apparmor_parser -r" in prof and "UNVERIFIED" in prof


# --- compose wiring -------------------------------------------------------------------------------------------


def _security_opt(svc: str) -> list[str]:
    lines = [ln for ln in svc.splitlines() if not ln.strip().startswith("#")]
    inline = [re.findall(r'"([^"]+)"', ln) for ln in lines if ln.strip().startswith("security_opt: [")]
    if inline:
        return inline[0]
    out: list[str] = []
    grab = False
    for ln in lines:
        if ln.strip() == "security_opt:":
            grab = True
        elif grab and ln.strip().startswith("- "):
            out.append(ln.strip()[2:])
        elif grab:
            break
    return out


def test_only_the_codex_service_gets_the_custom_profiles():
    assert _security_opt(_service("agent-worker-codex")) == [
        "no-new-privileges:true", "seccomp=./infra/agent-worker/codex-seccomp.json",
        "apparmor=aibroker-codex"]
    assert _security_opt(_service("agent-worker")) == ["no-new-privileges:true"]      # Claude unchanged
    for other in ("api", "monitor", "postgres", "pgbouncer", "redis"):
        assert "seccomp" not in _service(other) and "apparmor" not in _service(other)


@pytest.mark.parametrize("name", ["agent-worker", "agent-worker-codex"])
def test_neither_agent_service_loosens_anything_else(name):
    code = "\n".join(ln for ln in _service(name).splitlines() if not ln.strip().startswith("#"))
    for banned in ("unconfined", "privileged", "cap_add", "docker.sock", "devices", "network_mode",
                   "pid: host", "ipc: host", "userns_mode", "sysctls"):
        assert banned not in code, (name, banned)
    for kept in ("cap_drop: [ALL]", "read_only: true", "networks: [agent_db]", "init: true"):
        assert kept in code, (name, kept)


def test_the_files_compose_points_at_exist_and_are_valid():
    assert (ROOT / "infra" / "agent-worker" / "codex-seccomp.json").exists()
    assert (INFRA / "apparmor-codex").exists()
    json.loads((INFRA / "codex-seccomp.json").read_text(encoding="utf-8"))
    assert "ROLLBACK" in _service("agent-worker-codex")


# --- generated image export (worker side) -------------------------------------------------------------------------


def _png(w=4, h=3, color=(200, 10, 10)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, "PNG")
    return buf.getvalue()


def _setup(tmp_path):
    home = tmp_path / "codex-home"
    (home / GENERATED_DIR / THREAD).mkdir(parents=True)
    ws = create_workspace(str(tmp_path / "ws"), 1)
    return home, Path(ws)


def _export(home, ws, since=None, **kw):
    args = {"max_bytes": 1_000_000, "max_count": 4, "ttl_s": 60, "session_id": THREAD}
    args.update(kw)
    return export_generated_pngs(str(home), str(ws), time.time() - 5 if since is None else since, **args)


def test_a_new_png_is_copied_to_out_with_dimensions_and_sha256(tmp_path):
    home, ws = _setup(tmp_path)
    data = _png(7, 5)
    (home / GENERATED_DIR / THREAD / "a.png").write_bytes(data)
    (ref,) = _export(home, ws)
    assert (ref.width, ref.height, ref.size) == (7, 5, len(data))
    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.path == f"out/generated-1-{ref.sha256[:12]}.png" and ref.expires_at
    assert base64.b64decode(ref.content_b64) == data
    assert (ws / ref.path).read_bytes() == data                   # copied into the run's out/


def test_nested_session_dirs_are_found_but_not_too_deep(tmp_path):
    home, ws = _setup(tmp_path)
    nest = home / GENERATED_DIR / THREAD / "nested"
    nest.mkdir()
    (nest / "ok.png").write_bytes(_png(2, 2, (1, 2, 3)))
    deep = home / GENERATED_DIR / THREAD / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "too-deep.png").write_bytes(_png(3, 3, (9, 9, 9)))
    assert [(r.width, r.height) for r in _export(home, ws)] == [(2, 2)]


def test_only_files_created_during_this_run_are_taken(tmp_path):
    home, ws = _setup(tmp_path)
    old = home / GENERATED_DIR / THREAD / "old.png"
    old.write_bytes(_png(2, 2, (1, 1, 1)))
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    (home / GENERATED_DIR / THREAD / "new.png").write_bytes(_png(3, 3, (2, 2, 2)))
    assert [(r.width) for r in _export(home, ws, since=time.time() - 60)] == [3]


def test_symlinks_traversal_and_special_files_are_rejected(tmp_path):
    home, ws = _setup(tmp_path)
    secret = tmp_path / "outside.png"
    secret.write_bytes(_png(5, 5, (7, 7, 7)))
    gen = home / GENERATED_DIR / THREAD
    try:
        (gen / "link.png").symlink_to(secret)                    # symlinked file
        (gen / "linkdir").symlink_to(tmp_path)                   # symlinked directory
        symlinks = True
    except (OSError, NotImplementedError):
        symlinks = False
    if hasattr(os, "mkfifo"):
        os.mkfifo(gen / "fifo.png")                              # not a regular file
    assert _export(home, ws) == []
    if symlinks:
        (home / GENERATED_DIR).rename(home / "gi-real")          # the source dir itself a symlink
        (home / GENERATED_DIR).symlink_to(home / "gi-real")
        (home / "gi-real" / "x.png").write_bytes(_png(2, 2))
        assert _export(home, ws) == []


def test_undecodable_oversized_and_excess_files_are_skipped(tmp_path):
    home, ws = _setup(tmp_path)
    gen = home / GENERATED_DIR / THREAD
    good = _png(4, 4)
    (gen / "truncated.png").write_bytes(good[:len(good) // 2])
    (gen / "fake.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"not really an image")
    (gen / "text.png").write_bytes(b"hello")
    (gen / "empty.png").write_bytes(b"")
    (gen / "huge.png").write_bytes(_png(300, 300))
    (gen / "zz-ok.png").write_bytes(good)
    refs = _export(home, ws, max_bytes=len(good) + 10)
    assert [(r.width, r.height) for r in refs] == [(4, 4)]


def test_count_cap_and_duplicate_content(tmp_path):
    home, ws = _setup(tmp_path)
    gen = home / GENERATED_DIR / THREAD
    for i in range(5):
        (gen / f"i{i}.png").write_bytes(_png(2 + i, 2))
    (gen / "dup.png").write_bytes(_png(2, 2))
    refs = _export(home, ws, max_count=3)
    assert len(refs) == 3 and len({r.sha256 for r in refs}) == 3


def test_no_directory_or_no_home_is_just_empty(tmp_path):
    assert export_generated_pngs(None, str(tmp_path), 0, max_bytes=10, max_count=1, ttl_s=1) == []
    assert export_generated_pngs(str(tmp_path / "nope"), str(tmp_path), 0, max_bytes=10, max_count=1,
                                 ttl_s=1) == []


def test_codex_cli_exports_from_its_fixed_codex_home_only(tmp_path):
    home, ws = _setup(tmp_path)
    cli = CodexCLI(env={"CODEX_HOME": str(home)})
    cli.snapshot_artifacts()
    (home / GENERATED_DIR / THREAD / "a.png").write_bytes(_png(6, 6))
    cli._image_session_id = THREAD
    (home / "elsewhere.png").write_bytes(_png(9, 9, (3, 3, 3)))            # outside generated_images
    (tmp_path / "other-dir").mkdir()
    (tmp_path / "other-dir" / "b.png").write_bytes(_png(8, 8, (4, 4, 4)))
    spec = AgentRunSpec(run_id=1, capability="agent:codex", prompt="p", workspace=str(ws), account_id="a")
    refs = cli.export_artifacts(spec, time.time() - 5)
    assert [(r.width, r.height) for r in refs] == [(6, 6)]


def test_a_touched_preexisting_png_cannot_be_attributed_to_the_next_run(tmp_path):
    home, ws = _setup(tmp_path)
    old = home / GENERATED_DIR / THREAD / "previous.png"
    old.write_bytes(_png(3, 3))
    baseline = snapshot_generated_paths(str(home))
    old.write_bytes(_png(4, 4))  # changed during this run, but the path is not its own
    fresh = home / GENERATED_DIR / THREAD / "current.png"
    fresh.write_bytes(_png(5, 5))
    refs = export_generated_pngs(str(home), str(ws), time.time() - 5,
                                 max_bytes=1_000_000, max_count=4, ttl_s=60,
                                 baseline=baseline, session_id=THREAD)
    assert len(refs) == 1 and (refs[0].width, refs[0].height) == (5, 5)
    assert refs[0].sha256 == hashlib.sha256(fresh.read_bytes()).hexdigest()


def test_concurrent_other_thread_and_lease_takeover_cannot_export_its_png(tmp_path):
    home, ws = _setup(tmp_path)
    other = home / GENERATED_DIR / OTHER_THREAD
    other.mkdir()
    other_png = _png(9, 9, (7, 8, 9))
    (other / "late.png").write_bytes(other_png)  # stale CLI writes after lease takeover
    own_png = _png(4, 6, (1, 2, 3))
    (home / GENERATED_DIR / THREAD / "own.png").write_bytes(own_png)
    refs = _export(home, ws)
    assert [base64.b64decode(ref.content_b64) for ref in refs] == [own_png]


def test_missing_or_invalid_thread_identity_exports_nothing(tmp_path):
    home, ws = _setup(tmp_path)
    (home / GENERATED_DIR / THREAD / "own.png").write_bytes(_png(4, 4))
    assert _export(home, ws, session_id=None) == []
    assert _export(home, ws, session_id="../" + THREAD) == []
    assert _export(home, ws, session_id=OTHER_THREAD) == []


def test_codex_resume_and_preexisting_thread_directory_export_nothing(tmp_path):
    home, ws = _setup(tmp_path)
    cli = CodexCLI(env={"CODEX_HOME": str(home)})
    (home / GENERATED_DIR / THREAD / "preexisting.png").write_bytes(_png(2, 2))
    cli.snapshot_artifacts()
    cli._image_session_id = THREAD
    (home / GENERATED_DIR / THREAD / "new.png").write_bytes(_png(3, 3))
    fresh = AgentRunSpec(run_id=1, capability="agent:codex", prompt="p", workspace=str(ws), account_id="a")
    assert cli.export_artifacts(fresh, time.time() - 5) == []
    resumed = AgentRunSpec(run_id=2, capability="agent:codex", prompt="p", workspace=str(ws),
                           account_id="a", session_id=THREAD)
    assert cli.export_artifacts(resumed, time.time() - 5) == []


async def test_codex_uses_only_the_thread_id_emitted_by_its_own_process(tmp_path, monkeypatch):
    home, ws = _setup(tmp_path)
    cli = CodexCLI(env={"CODEX_HOME": str(home)})
    cli.snapshot_artifacts()
    spec = AgentRunSpec(run_id=1, capability="agent:codex", prompt="p", workspace=str(ws), account_id="a")

    async def fake_start(self, run):
        yield ev.SessionAssigned(session_id=THREAD)
        yield ev.RunCompleted(final_text="done")

    monkeypatch.setattr(SubprocessCLI, "start", fake_start)
    assert [e.type async for e in cli.start(spec)] == ["session.assigned", "run.completed"]
    assert cli._image_session_id == THREAD
    resumed = AgentRunSpec(run_id=2, capability="agent:codex", prompt="p", workspace=str(ws),
                           account_id="a", session_id=THREAD)
    assert [e.type async for e in cli.start(resumed)] == ["session.assigned", "run.completed"]
    assert cli._image_session_id is None


def test_the_model_never_sees_codex_home_in_its_prompt():
    spec = AgentRunSpec(run_id=1, capability="agent:codex", prompt="draw a cat", workspace="/w",
                        account_id="a", attachments=["inputs/ref.png"])
    prompt = build_codex_prompt(spec)
    assert "codex-state" not in prompt and "CODEX_HOME" not in prompt and GENERATED_DIR not in prompt


async def _project(pid=9300):
    plain = generate_project_key()
    async with get_session() as s:
        await s.execute(insert(ProjectRow).values(
            id=pid, name=f"img_{pid}", project_key_hash=hash_project_key(plain),
            project_key_prefix=plain[:12], allowed_scopes=[AGENT_SCOPE], is_active=True, notes=""))
    rid, _ = await create_run(
        project=SimpleNamespace(id=pid), capability="agent:codex", prompt="p", task_id="t",
        account_id="a", session_id=None, idempotency_key=None, wall_timeout_s=30,
        idle_timeout_s=30, allowed_tools=[])
    async with get_session() as s:
        (await s.get(DeepJobRow, rid)).status = "running"
    return plain, rid


class _ImageFake(FakeAgentCLI):
    """A fake whose export_artifacts uses the real exporter against a prepared CODEX_HOME."""

    def __init__(self, script, home):
        super().__init__(script)
        self.home = home
        self.image_max_bytes, self.image_max_count, self.image_ttl_s = 1_000_000, 4, 60

    def export_artifacts(self, run, since):
        return export_generated_pngs(str(self.home), run.workspace, since, max_bytes=self.image_max_bytes,
                                     max_count=self.image_max_count, ttl_s=self.image_ttl_s,
                                     session_id=THREAD)


async def test_two_codex_runs_sharing_a_home_cannot_execute_concurrently(tmp_path):
    _, first = await _project(9310)
    _, second = await _project(9311)
    started = asyncio.Event()
    release = asyncio.Event()
    begun: list[int] = []

    class Blocking(FakeAgentCLI):
        async def start(self, run):
            begun.append(run.run_id)
            if run.run_id == first:
                started.set()
                await release.wait()
            yield ev.RunCompleted(final_text="ok")

    register_adapter("agent:codex", lambda: Blocking([]))
    cfg = ExecConfig(workspace_root=str(tmp_path / "w"), env={},
                     codex_paid_continuation_verified_off=True)
    first_task = asyncio.create_task(execute_run(first, cfg))
    await asyncio.wait_for(started.wait(), 5)
    assert await execute_run(second, cfg) is None
    assert begun == [first]
    async with get_session() as s:
        queued = await s.get(DeepJobRow, second)
        assert queued.status == "pending" and queued.retry_count == 1
    release.set()
    assert await asyncio.wait_for(first_task, 5) == "completed"
    async with get_session() as s:
        (await s.get(DeepJobRow, second)).status = "running"
    assert await execute_run(second, cfg) == "completed"
    assert begun == [first, second]


async def test_a_run_returns_the_generated_png_with_sha256_and_dimensions(tmp_path):
    key, rid = await _project()
    home = tmp_path / "codex-home"
    (home / GENERATED_DIR / THREAD).mkdir(parents=True)
    data = _png(11, 7, (5, 6, 7))

    class WritesImage(_ImageFake):
        async def start(self, run):
            (home / GENERATED_DIR / THREAD / "pic.png").write_bytes(data)
            async for e in super().start(run):
                yield e

    register_adapter("agent:codex", lambda: WritesImage([ev.RunCompleted(final_text="drawn")], home))
    cfg = ExecConfig(workspace_root=str(tmp_path / "w"), env={}, codex_paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "completed"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        got = (await c.get(f"/v1/agent/runs/{rid}", headers={"X-Project-Key": key})).json()
    (f,) = (x for x in got["files"] if x["path"].startswith("out/"))
    assert (f["width"], f["height"]) == (11, 7) and f["sha256"] == hashlib.sha256(data).hexdigest()
    assert base64.b64decode(f["content_b64"]) == data and f["size"] == len(data)
    from PIL import Image
    assert Image.open(io.BytesIO(base64.b64decode(f["content_b64"]))).size == (11, 7)  # decodable by the critic
    completed = [row for row in await list_events(rid) if row.type == "run.completed"]
    assert len(completed) == 1
    assert "content_b64" not in completed[0].payload["files"][0]


async def test_an_image_from_before_the_run_is_not_returned(tmp_path):
    key, rid = await _project(9301)
    home = tmp_path / "codex-home"
    (home / GENERATED_DIR / THREAD).mkdir(parents=True)
    stale = home / GENERATED_DIR / THREAD / "stale.png"
    stale.write_bytes(_png(3, 3))
    os.utime(stale, (time.time() - 7200, time.time() - 7200))
    register_adapter("agent:codex", lambda: _ImageFake([ev.RunCompleted()], home))
    cfg = ExecConfig(workspace_root=str(tmp_path / "w"), env={}, codex_paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "completed"
    async with get_session() as s:
        job = await s.get(DeepJobRow, rid)
    assert job.result_meta["files"] == []


async def test_a_failing_export_never_fails_the_run(tmp_path):
    key, rid = await _project(9302)

    class Broken(FakeAgentCLI):
        def export_artifacts(self, run, since):
            raise OSError("disk gone")

    register_adapter("agent:codex", lambda: Broken([ev.RunCompleted(final_text="ok")]))
    cfg = ExecConfig(workspace_root=str(tmp_path / "w"), env={}, codex_paid_continuation_verified_off=True)
    assert await execute_run(rid, cfg) == "completed"


def test_docs_state_readiness_images_and_the_profile_diffs():
    for needle in ("generated_images", "export_generated_pngs", "sha256", "decodable PNG",
                   "codex-seccomp.json", "apparmor-codex", "apparmor_parser -r", "clone3",
                   "Failed to make / slave", "ROLLBACK"):
        assert needle in DOCS, needle
