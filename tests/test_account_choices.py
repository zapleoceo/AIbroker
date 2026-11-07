"""Provider-qualified identities cannot grant another provider's login."""
import pytest

from aibroker.agent.account_choices import account_ids, clean_account_choices


def test_choices_keep_preference_and_deduplicate():
    assert clean_account_choices([
        "codex-sub/owner", "claude-sub/owner", "claude-sub/claude-2", "claude-sub/owner",
    ]) == ("claude-sub/claude-2", "claude-sub/owner", "codex-sub/owner")


@pytest.mark.parametrize("value", ["owner", "claude-2", "other/owner", "codex-sub/claude-2"])
def test_unknown_or_unqualified_accounts_are_rejected(value):
    with pytest.raises(ValueError):
        clean_account_choices([value])


def test_provider_and_id_together_select_an_account():
    assert account_ids(["codex-sub/owner"], "agent:claude") == ()
    assert account_ids(["claude-sub/owner"], "agent:codex") == ()
    assert account_ids(["codex-sub/owner"], "agent:codex") == ("owner",)
    assert account_ids(["codex-sub/owner"], None) == ()
    assert account_ids([], "agent:claude") == ()


def test_rollout_switch_defaults_closed_in_settings_and_compose():
    from pathlib import Path

    from aibroker.config import Settings

    assert Settings.model_fields["AGENT_ACCOUNT_ONLY_UI_ENABLED"].default is False
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.yml").read_text()
    assert "AGENT_ACCOUNT_ONLY_UI_ENABLED: ${AGENT_ACCOUNT_ONLY_UI_ENABLED:-false}" in compose
