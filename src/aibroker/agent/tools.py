"""Strict ALLOW-list for the CLI tools a caller may request (no denylist logic).

A tool string reaches the CLI command line, so it must be unable to become a flag, to widen
its own scope with absolute paths / `..`, or to smuggle a second tool through a comma. Only the
file tools below exist in phase 1: Bash (and everything else) is not in the list at all.
"""
from __future__ import annotations

import re

_ALLOWED = re.compile(r"^(Read|Glob|Grep|Edit|Write)(\([^\s(),]{1,100}\))?$")
_ABS = re.compile(r"^(/|\\|~|[A-Za-z]:)")
_FILE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")
# Defense in depth on top of the allow-list: credential files wherever they sit.
_CREDENTIAL_GLOBS = ("**/.credentials.json", "**/.claude.json")


def tool_problem(tool: str) -> str | None:
    """Why `tool` is refused, or None when it is on the allow-list with a relative scope."""
    if not _ALLOWED.match(tool):
        return f"tool {tool!r} is not on the allow-list (Read|Glob|Grep|Edit|Write)"
    scope = tool[tool.index("(") + 1:-1] if "(" in tool else ""
    if scope and (_ABS.match(scope) or ".." in scope.replace("\\", "/").split("/")):
        return f"tool scope {scope!r} must be relative and must not contain '..'"
    return None


def validate_tools(tools: list[str]) -> None:
    """ValueError naming the first refused tool."""
    for tool in tools:
        problem = tool_problem(tool)
        if problem:
            raise ValueError(problem)


def disallowed_tools(config_dir: str | None) -> list[str]:
    """Always-denied paths: /proc (the worker environment), the CLI auth dir, credential files."""
    paths = ["//proc/**", *_CREDENTIAL_GLOBS]
    if config_dir:
        paths.append("/" + config_dir.rstrip("/") + "/**")
    return [f"{t}({p})" for p in paths for t in _FILE_TOOLS]
