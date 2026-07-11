"""Scope helpers — the canonical llm:* scope list and the validate /
checkbox-render helpers built on it. Both the CRUD form handlers
(dashboard.py) and the dashboard render layer depend on these, so they
live in their own leaf module to keep those two from importing each other.
"""
from __future__ import annotations

_KNOWN_SCOPES = ("llm:chat", "llm:embed", "llm:vision", "llm:edit", "llm:deep", "llm:audio")


def _validate_scope_list(scopes: list[str]) -> list[str] | None:
    """For checkbox-driven forms — strip dups, reject empty / unknown."""
    seen: list[str] = []
    for s in scopes:
        s = s.strip()
        if not s:
            continue
        if s not in _KNOWN_SCOPES:
            return None
        if s not in seen:
            seen.append(s)
    return seen or None


def _scope_checkboxes(selected: list[str] | None, name: str = "scopes") -> str:
    """Render the known scopes as checkboxes (multi-select via repeated POST)."""
    sel = set(selected or [])
    return "".join(
        f'<label class="scope-cb">'
        f'<input type="checkbox" name="{name}" value="{s}"'
        f'{" checked" if s in sel else ""}> {s}'
        f'</label>'
        for s in _KNOWN_SCOPES
    )
