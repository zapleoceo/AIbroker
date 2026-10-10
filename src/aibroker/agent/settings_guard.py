"""Fail-closed check of the CLI settings files before every run (ALLOW-list).

settings.json / settings.local.json (CLI config dir and run workspace) may contain ONLY the
inert display keys below. Anything else - hooks, permissions, statusLine, mcpServers,
enableAllProjectMcpServers, otelHeadersHelper, awsAuthRefresh, awsCredentialExport,
forceLoginMethod, apiKeyHelper, env, defaultMode, bypassPermissions, unknown keys - refuses
the run, as does unreadable / invalid JSON. `<config_dir>/.claude.json` (the CLI's own state)
must not define MCP servers anywhere. The CLI keeps its login in `.credentials.json` /
`.claude.json`, not in settings.json, so this list stays minimal; the first supervised run
confirms that a logged-in config dir still passes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ALLOWED_SETTINGS_KEYS = frozenset({"$schema", "theme", "verbose"})
_FILES = ("settings.json", "settings.local.json")


def _has_key(node: Any, key: str) -> bool:
    """True when `key` appears anywhere with a NON-EMPTY value. The CLI writes an
    empty `"mcpServers": {}` into each project entry of .claude.json after login;
    that defines no server and must not refuse every run."""
    if isinstance(node, dict):
        if node.get(key):
            return True
        return any(_has_key(v, key) for v in node.values())
    if isinstance(node, list):
        return any(_has_key(v, key) for v in node)
    return False


def _load(path: Path) -> tuple[Any, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError):
        return None, f"{path} is unreadable or not valid JSON"


def settings_problem(config_dir: str | None, workspace: str) -> str | None:
    """-> why the run must be refused, or None when every file is clean."""
    if not config_dir:
        return "CLAUDE_CONFIG_DIR is not set"
    candidates = [Path(config_dir) / n for n in _FILES]
    candidates += [Path(workspace) / ".claude" / n for n in _FILES]
    for path in candidates:
        if not path.exists():
            continue
        data, err = _load(path)
        if err:
            return err
        if not isinstance(data, dict):
            return f"{path} is not a JSON object"
        extra = sorted(set(data) - ALLOWED_SETTINGS_KEYS)
        if extra:
            return f"{path} contains key {extra[0]!r} (not in the allow-list)"
        if any(isinstance(v, dict | list) for v in data.values()):
            return f"{path} contains a nested value"
    state = Path(config_dir) / ".claude.json"
    if state.exists():
        data, err = _load(state)
        if err:
            return err
        if _has_key(data, "mcpServers"):
            return f"{state} defines mcpServers"
    return None
