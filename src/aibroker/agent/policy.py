"""Per-project agent policy (AGENT_PROJECT_POLICY): which exact models and which account labels a
project may use. Narrowing only and FAIL-CLOSED: no entry (or empty lists) = nothing allowed, a
malformed setting = every project refused (and one clear error log). Enforced on create (API) and
re-checked by the worker before a run starts. The project cap stays plain metadata.

Setting (JSON in the environment):
    {"11": {"models": ["claude-sonnet-5-5"], "accounts": ["owner"]}}
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache

from aibroker.config import get_settings

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

    def allows_model(self, model: str | None) -> bool:
        """The EXACT model id must be listed; a missing model is never allowed (no implicit default)."""
        return model is not None and model in self.models

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
                lists.append(frozenset(v.strip() for v in values if v.strip()))
            out[int(key)] = ProjectPolicy(models=lists[0], accounts=lists[1])
        return out
    except ValueError as exc:     # json.JSONDecodeError is a ValueError
        log.error("AGENT_PROJECT_POLICY is malformed (%s): refusing EVERY agent run", exc)
        return None


def policy_for(project_id: int) -> ProjectPolicy | None:
    """The project's usable policy, or None (no entry, empty models/accounts, malformed setting)."""
    table = _parse(get_settings().AGENT_PROJECT_POLICY)
    policy = (table or {}).get(project_id)
    return policy if policy is not None and policy.models and policy.accounts else None


def policy_violation(project_id: int, model: str | None, account_id: str) -> str | None:
    """The `error` code of the first rule broken, or None when the request fits the policy."""
    policy = policy_for(project_id)
    if policy is None:
        return "agent_policy_missing"
    if not policy.allows_model(model):
        return "model_not_allowed"
    if not policy.allows_account(account_id):
        return "account_not_allowed"
    return None
