"""Agent capabilities and the owner-only scope (docs/agent-runs.md)."""
from __future__ import annotations

AGENT_CAPABILITIES = ("agent:codex", "agent:claude")
AGENT_SCOPE = "llm:agent"
# Never granted by self-signup, whatever SIGNUP_DEFAULT_SCOPES says.
OWNER_ONLY_SCOPES = frozenset({AGENT_SCOPE})
