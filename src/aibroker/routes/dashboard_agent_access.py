"""The "Agent runs" card of the dashboard project Settings tab: what the checkboxes show and how a
submitted form becomes projects.agent_* (agent/project_access.py holds the runtime side)."""
from __future__ import annotations

from typing import Any

from aibroker.agent.account_choices import ACCOUNT_CHOICES, ACCOUNT_ONLY
from aibroker.agent.constants import AGENT_SCOPE, allowed_project_ids
from aibroker.agent.policy import env_policy_for
from aibroker.agent.project_access import ALL_MODELS, KNOWN_ACCOUNTS, clean_accounts, clean_models
from aibroker.config import get_settings
from aibroker.db.models import ProjectRow
from aibroker.providers.subscriptions import subscription_models


def known_models() -> list[tuple[str, str]]:
    """(bare id stored in the policy, catalog id shown) of every subscription candidate."""
    return [(spec.id.split("/", 1)[1], spec.id) for spec in subscription_models()]


def agent_form_values(enabled: str, models: list[str] | None,
                      accounts: list[str] | None) -> tuple[bool, list[str], list[str]]:
    """The submitted checkboxes, reduced to known values (unknown ones are dropped)."""
    return (bool(enabled), list(clean_models(models or [], (m for m, _ in known_models()))),
            list(clean_accounts(accounts or [])))


def agent_view(project: ProjectRow) -> dict[str, Any]:
    """Checkbox state plus the env settings that still apply to this project."""
    models = set(project.agent_models or [])
    accounts = set(project.agent_accounts or [])
    env_policy = env_policy_for(project.id)
    return {
        "enabled": bool(project.agent_enabled),
        "account_ui_enabled": get_settings().AGENT_ACCOUNT_ONLY_UI_ENABLED,
        "account_only": project.agent_models == [ACCOUNT_ONLY],
        "account_choices": [{"value": a.value, "label": a.account_id, "kind": a.kind,
                             "checked": a.value in accounts} for a in ACCOUNT_CHOICES],
        "legacy_models": list(project.agent_models or []),
        "legacy_accounts": list(project.agent_accounts or []),
        "all_models": ALL_MODELS in models,
        "models": [{"value": bare, "label": full, "checked": bare in models}
                   for bare, full in known_models()],
        "accounts": [{"value": a, "checked": a in accounts} for a in KNOWN_ACCOUNTS],
        "has_scope": AGENT_SCOPE in (project.allowed_scopes or []),
        "env_allowed": project.id in allowed_project_ids(),
        "env_policy": ({"models": sorted(env_policy.models),
                        "accounts": list(env_policy.ordered_accounts())}
                       if env_policy is not None else None),
    }
