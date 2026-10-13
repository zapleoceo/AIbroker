"""Regression: the restricted deploy script must cover compose-profiled services.

Services under `profiles:` are invisible to a plain `docker compose build/up/ps`,
so deploy never rebuilt or checked them (agent-worker ran stale code on prod,
2026-10-13). The script names them explicitly; this test keeps that list equal to
the compose file and pins the build order and the `ps -a` health gate.
"""

import re
from pathlib import Path

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
    assert re.search(r"done < <\(docker compose .*\bps -a\b", SCRIPT)
