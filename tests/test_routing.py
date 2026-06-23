"""Capability chains + cost-guard semantics."""
import pytest

from aibroker.routing import CostGuardError, chain_for
from aibroker.routing.cost_guard import check_caps
from aibroker.db.models import ApiKeyRow, ProjectRow


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


async def test_cost_guard_free_key_passthrough():
    k = ApiKeyRow(
        provider="cerebras", label="x", tier="free", token_encrypted="x",
        scopes=["llm:chat"], daily_cost_used_usd=0,
    )
    p = ProjectRow(name="t", project_key_hash="x", project_key_prefix="x",
                   allowed_scopes=["llm:chat"])
    await check_caps(api_key=k, project=p, estimated_cost=0.0)


async def test_cost_guard_paid_cap_blocks():
    k = ApiKeyRow(
        provider="deepseek", label="x", tier="paid", token_encrypted="x",
        scopes=["llm:chat"], daily_cost_used_usd=1.99, daily_cost_cap_usd=2.0,
    )
    p = ProjectRow(name="t", project_key_hash="x", project_key_prefix="x",
                   allowed_scopes=["llm:chat"])
    with pytest.raises(CostGuardError) as exc:
        await check_caps(api_key=k, project=p, estimated_cost=0.05)
    assert exc.value.kind == "api_key"
