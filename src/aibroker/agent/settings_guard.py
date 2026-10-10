"""Fail-closed check of the CLI settings files before every run.

settings.json / settings.local.json in the CLI config dir (and in the run workspace) can carry
an apiKeyHelper (an alternative, possibly paid, credential), an `env` block (could inject
ANTHROPIC_* variables the env guard cannot see) or a permissive defaultMode /
bypassPermissions. Any of those, or an unreadable file, refuses the run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FORBIDDEN_KEYS = frozenset({"apiKeyHelper", "env", "defaultMode", "bypassPermissions"})
_FILES = ("settings.json", "settings.local.json")


def _walk(node: Any) -> str | None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in FORBIDDEN_KEYS:
                return f"key {key!r}"
            found = _walk(value)
            if found:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _walk(item)
            if found:
                return found
    elif isinstance(node, str) and node == "bypassPermissions":
        return "value 'bypassPermissions'"
    return None


def settings_problem(config_dir: str | None, workspace: str) -> str | None:
    """-> why the run must be refused, or None when every settings file is clean."""
    if not config_dir:
        return "CLAUDE_CONFIG_DIR is not set"
    candidates = [Path(config_dir) / n for n in _FILES]
    candidates += [Path(workspace) / ".claude" / n for n in _FILES]
    for path in candidates:
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return f"{path} is unreadable or not valid JSON"
        found = _walk(data)
        if found:
            return f"{path} contains {found}"
    return None
