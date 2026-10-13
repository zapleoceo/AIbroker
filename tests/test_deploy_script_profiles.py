"""Regression: the restricted deploy script must activate every compose profile.

Services under `profiles:` are invisible to `docker compose build/up/ps` unless the
profile is active. The deploy script takes no arguments, so it has to set
COMPOSE_PROFILES itself, otherwise those services are never rebuilt or checked
(agent-worker ran stale code on prod).
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SH = ROOT / "infra" / "aibroker-deploy.sh"
COMPOSE = ROOT / "docker-compose.yml"


def _compose_profiles() -> dict[str, set[str]]:
    services = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]
    return {name: set(svc["profiles"]) for name, svc in services.items() if svc.get("profiles")}


def _activated_profiles(script: str) -> set[str]:
    active: set[str] = set()
    for m in re.finditer(r"^\s*(?:export\s+)?COMPOSE_PROFILES=([^\s#]+)", script, re.M):
        active |= {p.strip("\"'") for p in m.group(1).split(",") if p}
    for m in re.finditer(r"--profile[ =]([\w.-]+)", script):
        active.add(m.group(1))
    return active


def test_deploy_script_activates_all_compose_profiles():
    profiled = _compose_profiles()
    assert profiled, "expected at least one profiled service (agent-worker)"
    active = _activated_profiles(DEPLOY_SH.read_text(encoding="utf-8"))
    missing = {name: sorted(p - active) for name, p in profiled.items() if p - active}
    assert not missing, f"deploy script does not activate profiles for: {missing}"
