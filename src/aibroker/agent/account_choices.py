"""Provider-qualified subscription identities from the supported worker topology."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Stored in agent_models, never accepted by the legacy model form. Keeping a marker
# distinguishes an explicit empty selection from an untouched legacy/env policy.
ACCOUNT_ONLY = "@accounts-v1"
NO_ACCOUNTS = "@none"


@dataclass(frozen=True)
class AccountChoice:
    provider: str
    account_id: str
    kind: str

    @property
    def value(self) -> str:
        return f"{self.provider}/{self.account_id}"


# These are worker IDs, not inferred people or claims that a login is healthy.
ACCOUNT_CHOICES = (
    AccountChoice("claude-sub", "claude-2", "Claude Code subscription"),
    AccountChoice("claude-sub", "owner", "Claude Code subscription"),
    AccountChoice("codex-sub", "owner", "Codex subscription"),
)
CAPABILITY_PROVIDER = {"agent:claude": "claude-sub", "agent:codex": "codex-sub"}


def clean_account_choices(values: Iterable[str]) -> tuple[str, ...]:
    chosen = set(values)
    known = {choice.value for choice in ACCOUNT_CHOICES}
    if chosen - known:
        raise ValueError("Unknown subscription account")
    return tuple(choice.value for choice in ACCOUNT_CHOICES if choice.value in chosen)


def account_ids(values: Iterable[str], capability: str | None) -> tuple[str, ...]:
    provider = CAPABILITY_PROVIDER.get(capability or "")
    if provider is None:
        return ()
    chosen = set(values)
    return tuple(choice.account_id for choice in ACCOUNT_CHOICES
                 if choice.provider == provider and choice.value in chosen)
