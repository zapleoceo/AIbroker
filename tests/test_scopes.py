"""Scope catalogue — llm:audio must be a first-class, editable scope so the
dashboard accepts it and 'transcription' (chains.CAPABILITY_SCOPE → llm:audio)
can be granted to a project."""
from __future__ import annotations

from aibroker.routes.dashboard_scopes import (
    _KNOWN_SCOPES,
    _scope_checkboxes,
    _validate_scope_list,
)
from aibroker.routing.chains import CAPABILITY_SCOPE


def test_audio_is_a_known_scope():
    assert "llm:audio" in _KNOWN_SCOPES


def test_every_capability_scope_is_known():
    """Any scope a capability routes to must be selectable in the dashboard —
    otherwise a project can never be granted it (Stepan2 voice, 2026-07-11)."""
    for scope in CAPABILITY_SCOPE.values():
        assert scope in _KNOWN_SCOPES, scope


def test_validate_scope_list_accepts_audio():
    assert _validate_scope_list(["llm:chat", "llm:audio"]) == ["llm:chat", "llm:audio"]


def test_scope_checkboxes_include_audio():
    assert 'value="llm:audio"' in _scope_checkboxes(["llm:audio"])
