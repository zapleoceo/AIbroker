"""Per-project agent policy (AGENT_PROJECT_POLICY): which exact models and which account labels a
project may use. Narrowing only and FAIL-CLOSED: no entry (or empty lists) = nothing allowed, a
malformed setting = every project refused (and one clear error log). Enforced on create (API) and
re-checked by the worker before a run starts. The project cap stays plain metadata.

Setting (JSON in the environment):
    {"11": {"models": ["claude-sonnet-5-5"], "accounts": ["owner"]}}
A project's models and accounts ticked on the dashboard (agent/project_access.py) replace its env
entry; the env entry stays the fallback for untouched legacy projects only.
Account-only mode uses provider-qualified identities, admits catalog models, and
never falls back to env. An unloaded initial snapshot refuses every policy.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from functools import lru_cache

from aibroker.agent.account_choices import account_ids
from aibroker.agent.project_access import access_for, snapshot_loaded
from aibroker.config import get_settings
from aibroker.providers.subscriptions import PROVIDERS, subscription_models

log = logging.getLogger(__name__)


class AgentPolicyViolation(Exception):
    """A request/run is outside the project's agent policy (HTTP 403, body {"error": <error>})."""

    def __init__(self, error: str) -> None:
        super().__init__(error)
        self.error = error


@dataclass(frozen=True)
class ProjectPolicy:
    models: frozenset[str]
    accounts: frozenset[str]
    # The accounts in the order the setting lists them (the preference order for selection).
    account_order: tuple[str, ...] = field(default=(), compare=False)

    account_only: bool = False

    def ordered_accounts(self, capability: str | None = None) -> tuple[str, ...]:
        ordered = self.account_order or tuple(sorted(self.accounts))
        return account_ids(ordered, capability) if self.account_only else ordered

    def allows_model(self, model: str | None) -> bool:
        """An exact model id allowed by this project."""
        return not self.account_only and model is not None and model in self.models

    def allows_candidate(self, model: str | None, capability: str | None) -> bool:
        """A known candidate of the requested provider under an explicit wildcard."""
        if model is None:
            return False
        if "*" not in self.models or capability not in PROVIDERS.values():
            return False
        return any(PROVIDERS[spec.provider] == capability
                   and spec.id.split("/", 1)[1] == model for spec in subscription_models())

    def allows_account(self, account_id: str) -> bool:
        return account_id in self.accounts


@lru_cache(maxsize=8)
def _parse(raw: str) -> dict[int, ProjectPolicy] | None:
    """-> {project id: policy}, {} for an empty setting, None when malformed (refuse everyone)."""
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("top level must be an object keyed by project id")
        out: dict[int, ProjectPolicy] = {}
        for key, entry in data.items():
            if not str(key).isdigit() or not isinstance(entry, dict):
                raise ValueError(f"bad entry {key!r}")
            lists = []
            for field in ("models", "accounts"):
                values = entry.get(field, [])
                if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                    raise ValueError(f"{key}.{field} must be a list of strings")
                lists.append(tuple(dict.fromkeys(v.strip() for v in values if v.strip())))
            out[int(key)] = ProjectPolicy(models=frozenset(lists[0]), accounts=frozenset(lists[1]),
                                          account_order=lists[1])
        return out
    except ValueError as exc:     # json.JSONDecodeError is a ValueError
        log.error("AGENT_PROJECT_POLICY is malformed (%s): refusing EVERY agent run", exc)
        return None


def env_policy_for(project_id: int) -> ProjectPolicy | None:
    """The project's AGENT_PROJECT_POLICY entry alone (None: no entry or malformed setting)."""
    return (_parse(get_settings().AGENT_PROJECT_POLICY) or {}).get(project_id)


def policy_for(project_id: int) -> ProjectPolicy | None:
    """The project's usable policy, or None (no entry, empty models/accounts, malformed setting).
    Account-only overrides never fall back to env. Legacy dashboard policy requires
    both models and accounts; otherwise the legacy env entry applies."""
    if not snapshot_loaded():
        return None
    access = access_for(project_id)
    if access is not None and access.account_only:
        return ProjectPolicy(models=frozenset({"*"}), accounts=frozenset(access.accounts),
                             account_order=access.accounts, account_only=True)
    if access is not None and access.has_policy:
        return ProjectPolicy(models=frozenset(access.models), accounts=frozenset(access.accounts),
                             account_order=access.accounts)
    policy = env_policy_for(project_id)
    return policy if policy is not None and policy.models and policy.accounts else None


def policy_violation(project_id: int, model: str | None, account_id: str,
                     capability: str | None = None) -> str | None:
    """The `error` code of the first rule broken, or None when the request fits the policy."""
    policy = policy_for(project_id)
    if policy is None:
        return "agent_policy_missing"
    allowed = policy.allows_model(model)
    if not allowed and isinstance(policy, ProjectPolicy):
        allowed = policy.allows_candidate(model, capability)
    if not allowed:
        return "model_not_allowed"
    account_allowed = (account_id in policy.ordered_accounts(capability)
                       if isinstance(policy, ProjectPolicy) and policy.account_only
                       else policy.allows_account(account_id))
    if not account_allowed:
        return "account_not_allowed"
    return None
