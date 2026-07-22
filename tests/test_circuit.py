"""In-process timeout circuit-breaker (routing/circuit) — pure, DB-free."""
from __future__ import annotations

from aibroker.routing import circuit


def test_note_and_recent_key_ids():
    circuit.reset()
    circuit.note_timeout("gemini", 1)
    circuit.note_timeout("gemini", 2)
    assert circuit.recent_timeout_key_ids() == frozenset({1, 2})


def test_provider_storm_needs_min_distinct_keys():
    circuit.reset()
    circuit.note_timeout("gemini", 1)
    assert circuit.providers_in_timeout_storm(2) == frozenset()   # 1 key < threshold
    circuit.note_timeout("gemini", 2)
    assert circuit.providers_in_timeout_storm(2) == frozenset({"gemini"})
    # Re-noting the SAME key doesn't inflate the distinct-key count.
    circuit.note_timeout("gemini", 2)
    assert circuit.providers_in_timeout_storm(3) == frozenset()


def test_storm_is_per_provider():
    circuit.reset()
    circuit.note_timeout("gemini", 1)
    circuit.note_timeout("groq", 2)
    assert circuit.providers_in_timeout_storm(2) == frozenset()   # 1 each


def test_entries_expire(monkeypatch):
    circuit.reset()
    circuit.note_timeout("groq", 5)
    circuit.note_timeout("groq", 6)
    monkeypatch.setattr(circuit, "_TIMEOUT_MEMORY_S", -1.0)   # everything now stale
    assert circuit.recent_timeout_key_ids() == frozenset()
    assert circuit.providers_in_timeout_storm(1) == frozenset()


# ─── empty-body storm (billed-but-answerless provider degradation) ───────────


def test_empty_storm_needs_min_distinct_keys():
    circuit.reset()
    circuit.note_empty_body("deepseek", 1)
    assert circuit.providers_in_empty_storm(2) == frozenset()   # 1 key < threshold
    circuit.note_empty_body("deepseek", 2)
    assert circuit.providers_in_empty_storm(2) == frozenset({"deepseek"})
    # Re-noting the SAME key doesn't inflate the distinct-key count.
    circuit.note_empty_body("deepseek", 2)
    assert circuit.providers_in_empty_storm(3) == frozenset()


def test_empty_storm_is_per_provider_and_independent_of_timeouts():
    circuit.reset()
    circuit.note_empty_body("deepseek", 1)
    circuit.note_empty_body("gemini", 2)
    assert circuit.providers_in_empty_storm(2) == frozenset()   # 1 each
    # empties must not register as timeouts (different action: defer vs skip)
    assert circuit.providers_in_timeout_storm(1) == frozenset()
    assert circuit.recent_timeout_key_ids() == frozenset()


def test_empty_entries_expire(monkeypatch):
    circuit.reset()
    circuit.note_empty_body("deepseek", 5)
    circuit.note_empty_body("deepseek", 6)
    assert circuit.providers_in_empty_storm(2) == frozenset({"deepseek"})
    monkeypatch.setattr(circuit, "_EMPTY_MEMORY_S", -1.0)   # everything now stale
    assert circuit.providers_in_empty_storm(1) == frozenset()
