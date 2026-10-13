"""Regression: the restricted deploy script must cover compose-profiled services.

Services under `profiles:` are invisible to a plain `docker compose build/up/ps`,
so deploy never rebuilt or checked them (agent-worker ran stale code on prod,
2026-10-13). The script names them explicitly; this test keeps that list equal to
the compose file and pins the build order and the `ps -a` health gate.
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = (ROOT / "infra" / "aibroker-deploy.sh").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")


def _profiled_services() -> set[str]:
    found: set[str] = set()
    in_services = False
    current = ""
    for line in COMPOSE.splitlines():
        if re.match(r"^services:\s*$", line):
            in_services = True
        elif re.match(r"^\S", line):
            in_services = False
        elif in_services:
            if m := re.match(r"^  ([\w.-]+):\s*$", line):
                current = m.group(1)
            elif current and re.match(r"^    profiles:", line):
                found.add(current)
    return found


def _agent_services_var() -> set[str]:
    m = re.search(r'^AGENT_SERVICES="([^"]*)"', SCRIPT, re.M)
    return set(m.group(1).split()) if m else set()


def test_agent_services_list_matches_compose_profiles():
    profiled = _profiled_services()
    assert profiled, "expected at least one profiled service (agent-worker)"
    assert _agent_services_var() == profiled


def test_agent_build_line_covers_profiled_services_after_default_build():
    lines = SCRIPT.splitlines()
    default = [i for i, ln in enumerate(lines) if re.match(r"^docker compose build\b", ln)]
    agent = [i for i, ln in enumerate(lines) if re.match(r"^docker compose --profile agent build\b", ln)]
    assert default, "default `docker compose build` missing"
    assert agent, "agent-profile build missing"
    assert max(default) < min(agent), "default services must build before the workers (base image)"
    assert "$AGENT_SERVICES" in lines[agent[0]] or all(
        s in lines[agent[0]] for s in _profiled_services()
    )


def test_health_gate_sees_exited_containers():
    assert re.search(r'^\s*PS="\$\(docker compose .*\bps -a --format', SCRIPT, re.M)


def test_worker_up_and_dry_run_use_active_agent_only():
    lines = [ln for ln in SCRIPT.splitlines() if "--profile agent up" in ln]
    assert len(lines) == 2, lines  # the real up and its dry-run
    for ln in lines:
        assert "$ACTIVE_AGENT" in ln
        assert "$AGENT_SERVICES" not in ln


# --- behaviour, with a fake docker on PATH -----------------------------------

DEPLOY_SH = ROOT / "infra" / "aibroker-deploy.sh"
needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
AGENT_UP = "docker compose --profile agent up -d --no-deps"
AGENT_BUILD = "docker compose --profile agent build --quiet agent-worker agent-worker-codex"

STUB_DOCKER = r"""#!/bin/bash
echo "docker $*" >> "$STUB_LOG"
args=" $* "
case "$args" in
  *" ps --status running --services "*)
    [ -n "${STUB_PS_FAIL:-}" ] && exit 1
    for s in $STUB_RUNNING; do echo "$s"; done ;;
  *" ps -a --format "*)
    echo "api|healthy|running|aibroker-api"
    echo "redis||running|aibroker-redis"
    echo "api||exited|aibroker-api-run-abc123"
    for s in $STUB_RUNNING; do echo "$s||running|aibroker-$s"; done ;;
esac
exit 0
"""


def _run_deploy(tmp_path: Path, running: str, ps_fail: bool = False):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (
        ("docker", STUB_DOCKER),
        ("git", "#!/bin/bash\nexit 0\n"),
        ("sleep", "#!/bin/bash\nexit 0\n"),
    ):
        (bindir / name).write_bytes(body.encode())
        (bindir / name).chmod(0o755)
    log = tmp_path / "log.txt"
    log.write_text("")
    env = {
        **os.environ,
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "AIBROKER_DEPLOY_DIR": str(tmp_path),
        "STUB_LOG": str(log),
        "STUB_RUNNING": running,
    }
    if ps_fail:
        env["STUB_PS_FAIL"] = "1"
    proc = subprocess.run(
        [shutil.which("bash"), str(DEPLOY_SH)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return proc, log.read_text().splitlines()


@needs_bash
@pytest.mark.parametrize(
    "running",
    ["agent-worker agent-worker-codex", "agent-worker", ""],
    ids=["both", "one", "none"],
)
def test_deploy_recreates_only_running_workers(tmp_path, running):
    proc, log = _run_deploy(tmp_path, running)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert log.index("docker compose build --quiet") < log.index(AGENT_BUILD)
    ups = [ln for ln in log if ln.startswith(AGENT_UP)]
    dry = [ln for ln in log if ln.startswith("docker compose --profile agent up -d --dry-run")]
    if running:
        assert ups == [f"{AGENT_UP} {running}"]
        assert dry == [f"docker compose --profile agent up -d --dry-run --no-deps {running}"]
    else:
        assert ups == []
        assert dry == []


@needs_bash
def test_deploy_aborts_when_running_probe_fails(tmp_path):
    proc, log = _run_deploy(tmp_path, "agent-worker", ps_fail=True)
    assert proc.returncode != 0
    assert not any(ln.startswith("docker compose up") for ln in log)
