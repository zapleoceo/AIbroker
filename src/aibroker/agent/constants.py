"""Agent capabilities and the owner-only scope (docs/agent-runs.md)."""
from __future__ import annotations

AGENT_CAPABILITIES = ("agent:codex", "agent:claude")
AGENT_SCOPE = "llm:agent"
# Never granted by self-signup, whatever SIGNUP_DEFAULT_SCOPES says.
OWNER_ONLY_SCOPES = frozenset({AGENT_SCOPE})


class AgentProjectNotAllowed(Exception):
    """The project is not in AGENT_ALLOWED_PROJECT_IDS (HTTP 403 agent_project_not_allowed)."""


def allowed_project_ids() -> frozenset[int]:
    """AGENT_ALLOWED_PROJECT_IDS as ints. Empty or unset -> nobody (fail closed); a token that
    is not an integer is ignored (never widens the list)."""
    from aibroker.config import get_settings
    out: set[int] = set()
    for part in get_settings().AGENT_ALLOWED_PROJECT_IDS.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return frozenset(out)


def project_allowed(project_id: int) -> bool:
    return project_id in allowed_project_ids()
