"""Capability chains. Cost-guard semantics live in test_cost_guard.py — its
reserve_cost/release_cost need a real Postgres row, so keeping that coverage
in one file avoids drift between two copies of the same fixtures."""
import pytest

from aibroker.routing import chain_for


def test_chain_for_known():
    chain = chain_for("chat:fast")
    assert "cerebras" in chain
    assert chain[0] == "cerebras"


def test_chain_for_unknown_raises():
    with pytest.raises(ValueError):
        chain_for("nope")  # type: ignore[arg-type]


def test_chain_includes_free_before_paid_chat_fast():
    chain = chain_for("chat:fast")
    # cerebras (free) must come before openai (paid)
    assert chain.index("cerebras") < chain.index("openai")
