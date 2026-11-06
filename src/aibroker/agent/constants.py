"""Agent capabilities and the owner-only scope (docs/agent-runs.md)."""
from __future__ import annotations

AGENT_CAPABILITIES = ("agent:codex", "agent:claude")
AGENT_SCOPE = "llm:agent"
# Never granted by self-signup, whatever SIGNUP_DEFAULT_SCOPES says.
OWNER_ONLY_SCOPES = frozenset({AGENT_SCOPE})


class AgentProjectNotAllowed(Exception):
    """The project is neither in AGENT_ALLOWED_PROJECT_IDS nor allowed on the dashboard
    (HTTP 403 agent_project_not_allowed)."""


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
    """In AGENT_ALLOWED_PROJECT_IDS, or "agent runs allowed" ticked on the dashboard
    (agent/project_access.py; a lazy import keeps this module free of the database layer)."""
    if project_id in allowed_project_ids():
        return True
    from aibroker.agent.project_access import access_for
    access = access_for(project_id)
    return access is not None and access.enabled
