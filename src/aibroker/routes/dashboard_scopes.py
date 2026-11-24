"""Scope helpers — the canonical llm:* scope list and the validate /
checkbox-render helpers built on it. Both the CRUD form handlers
(dashboard.py) and the dashboard render layer depend on these, so they
live in their own leaf module to keep those two from importing each other.
"""
from __future__ import annotations

from html import escape as esc

from aibroker.agent.constants import AGENT_SCOPE
from aibroker.providers.registry import get_spec
from aibroker.routing.chains import CAPABILITY_SCOPE, usable_scopes_for_provider

# Derived from the capability table (2026-07-16) — the old hand-copied tuple
# could silently miss a newly-routed scope, making it un-grantable in the
# dashboard (that's how llm:audio went missing for Stepan2 voice, 2026-07-11).
# dict.fromkeys keeps first-appearance order, so llm:chat renders first.
_KNOWN_SCOPES = tuple(dict.fromkeys(CAPABILITY_SCOPE.values()))
_PROJECT_SCOPES = tuple(dict.fromkeys((*_KNOWN_SCOPES, AGENT_SCOPE)))


def _is_known_provider(provider: str) -> bool:
    """Provider must be one the adapter has a model table for — the create
    endpoints took any string, which then reached the scope-checkbox title
    attribute unescaped (2026-10-02 review)."""
    return get_spec(provider) is not None


def _validate_scope_list(scopes: list[str], *, project: bool = False) -> list[str] | None:
    """For checkbox-driven forms — strip dups, reject empty / unknown."""
    known = _PROJECT_SCOPES if project else _KNOWN_SCOPES
    seen: list[str] = []
    for s in scopes:
        s = s.strip()
        if not s:
            continue
        if s not in known:
            return None
        if s not in seen:
            seen.append(s)
    return seen or None


def _scope_checkboxes(
    selected: list[str] | None, name: str = "scopes", provider: str | None = None,
    *, project: bool = False,
) -> str:
    """Render the known scopes as checkboxes (multi-select via repeated POST).

    `project=True` adds the owner-only agent scope to project forms only.
    `provider` (key forms only - a project isn't tied to one, so it passes None)
    greys out every scope that provider can never serve. The broker only reaches
    a provider for a capability it's BOTH chained for and has a model for, so any
    other scope on its key is inert and the checkbox just misleads: it looked
    like anthropic was assigned voice+images while Claude has no speech-to-text
    at all and is off the vision chain (2026-07-15). Disabled boxes don't POST,
    so saving a key also cleans inert scopes off it."""
    sel = set(selected or [])
    usable = usable_scopes_for_provider(provider) if provider else None
    out = []
    for s in (_PROJECT_SCOPES if project else _KNOWN_SCOPES):
        na = usable is not None and s not in usable
        title = f' title="{esc(provider or "")} cannot serve {esc(s)}"' if na else ""
        help_attr = ' aria-describedby="pf-agent-scope-help"' if project and s == AGENT_SCOPE else ""
        out.append(
            f'<label class="scope-cb{" scope-na" if na else ""}"{title}>'
            f'<input type="checkbox" name="{name}" value="{s}"'
            f'{" checked" if s in sel else ""}{" disabled" if na else ""}{help_attr}> {s}'
            f'</label>'
        )
    return "".join(out)
