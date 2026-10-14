"""Fail-closed billing-mode guard, run before EVERY adapter start.

An agent run must bill against the owner's personal CLI subscription. Any API-style
credential in the worker environment, or an adapter that cannot positively say
"subscription", refuses the run - there is no fallback to API billing.
"""
from __future__ import annotations

from collections.abc import Mapping

from aibroker.agent.adapter import AgentCLI
from aibroker.agent.events import RunFailed

BILLING_REFUSED = "billing_mode_refused"

FORBIDDEN_ENV: dict[str, tuple[str, ...]] = {
    "agent:claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                     "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                     "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
                     "ANTHROPIC_VERTEX_PROJECT_ID", "AWS_BEARER_TOKEN_BEDROCK",
                     "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN"),
    "agent:codex": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"),
}


# Whole families are blocked too, so a variable added by a newer CLI is refused by default.
FORBIDDEN_PREFIXES: dict[str, tuple[str, ...]] = {
    "agent:claude": ("ANTHROPIC_", "CLAUDE_CODE_USE_"),
    "agent:codex": ("OPENAI_",),     # CODEX_HOME itself is legitimate, so no CODEX_ prefix
}


def forbidden_env_present(capability: str, env: Mapping[str, str]) -> list[str]:
    """Names (never values) of API-billing variables present in `env`. An unknown
    capability has no allow-list, so it is treated as forbidden by the caller."""
    prefixes = FORBIDDEN_PREFIXES.get(capability, ())
    return [name for name in sorted(env)
            if name in FORBIDDEN_ENV.get(capability, ()) or name.startswith(prefixes)]


def paid_continuation_refusal(run_mode: str, risk_ack: bool, verified_off: bool) -> RunFailed | None:
    """Absence of API env does not prove no paid continuation (usage credits / extra usage on
    the account can spend after wrap-up). Until the owner verified it OFF, only a manual run
    that explicitly accepts the risk may start."""
    if verified_off or (run_mode == "manual" and risk_ack):
        return None
    return RunFailed(
        error_class=BILLING_REFUSED,
        message="paid continuation not verified OFF (AGENT_PAID_CONTINUATION_VERIFIED_OFF); "
                f"a {run_mode} run needs the owner's confirmation, or run_mode=manual with "
                "accept_paid_continuation_risk")


async def check_billing_mode(capability: str, adapter: AgentCLI, env: Mapping[str, str], *,
                             run_mode: str = "autonomous", risk_ack: bool = False,
                             verified_off: bool = False) -> tuple[str, RunFailed | None]:
    """-> (mode seen, refusal or None). Only a positive "subscription" passes, and then
    only if paid continuation is verified off (or a manual run accepted the risk)."""
    if capability not in FORBIDDEN_ENV:
        return "unknown", RunFailed(error_class=BILLING_REFUSED,
                                    message=f"no billing policy for {capability}")
    present = forbidden_env_present(capability, env)
    if present:
        return "api", RunFailed(
            error_class=BILLING_REFUSED,
            message="API-billing variables present in the worker environment: "
                    + ", ".join(present))
    try:
        mode = await adapter.billing_mode()
    except Exception:  # noqa: BLE001 - cannot tell -> unknown -> refuse
        mode = "unknown"
    if mode != "subscription":
        return mode, RunFailed(
            error_class=BILLING_REFUSED,
            message=f"CLI did not positively report subscription mode (got {mode})")
    return mode, paid_continuation_refusal(run_mode, risk_ack, verified_off)
