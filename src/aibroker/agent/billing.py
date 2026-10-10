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
                     "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"),
    "agent:codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
}


def forbidden_env_present(capability: str, env: Mapping[str, str]) -> list[str]:
    """Names (never values) of API-billing variables present in `env`. An unknown
    capability has no allow-list, so it is treated as forbidden by the caller."""
    return [name for name in FORBIDDEN_ENV.get(capability, ()) if name in env]


async def check_billing_mode(capability: str, adapter: AgentCLI,
                             env: Mapping[str, str]) -> tuple[str, RunFailed | None]:
    """-> (mode seen, refusal or None). Only a positive "subscription" passes."""
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
    return mode, None
