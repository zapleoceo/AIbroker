"""Scope helpers — the canonical llm:* scope list and the validate /
checkbox-render helpers built on it. Both the CRUD form handlers
(dashboard.py) and the dashboard render layer depend on these, so they
live in their own leaf module to keep those two from importing each other.
"""
from __future__ import annotations

from aibroker.routing.chains import CAPABILITY_SCOPE, usable_scopes_for_provider

# Derived from the capability table (2026-07-16) — the old hand-copied tuple
# could silently miss a newly-routed scope, making it un-grantable in the
# dashboard (that's how llm:audio went missing for Stepan2 voice, 2026-07-11).
# dict.fromkeys keeps first-appearance order, so llm:chat renders first.
_KNOWN_SCOPES = tuple(dict.fromkeys(CAPABILITY_SCOPE.values()))


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


def _scope_checkboxes(
    selected: list[str] | None, name: str = "scopes", provider: str | None = None,
) -> str:
    """Render the known scopes as checkboxes (multi-select via repeated POST).

    `provider` (key forms only — a project isn't tied to one, so it passes None)
    greys out every scope that provider can never serve. The broker only reaches
    a provider for a capability it's BOTH chained for and has a model for, so any
    other scope on its key is inert and the checkbox just misleads: it looked
    like anthropic was assigned voice+images while Claude has no speech-to-text
    at all and is off the vision chain (2026-07-15). Disabled boxes don't POST,
    so saving a key also cleans inert scopes off it."""
    sel = set(selected or [])
    usable = usable_scopes_for_provider(provider) if provider else None
    out = []
    for s in _KNOWN_SCOPES:
        na = usable is not None and s not in usable
        title = f' title="{provider} cannot serve {s}"' if na else ""
        out.append(
            f'<label class="scope-cb{" scope-na" if na else ""}"{title}>'
            f'<input type="checkbox" name="{name}" value="{s}"'
            f'{" checked" if s in sel else ""}{" disabled" if na else ""}> {s}'
            f'</label>'
        )
    return "".join(out)
