"""Real adapter: CodexCLI drives the OFFICIAL OpenAI Codex CLI (`codex exec --json`).

UNVERIFIED against a real run (phase 4): the argv shape was checked against `codex exec --help` /
`codex exec resume --help` / `codex login --help` of the pinned 0.162.1 image (2026-10-11); the
JSONL parser and the login-status wording come from the public docs. Registered only when the
worker lane starts (register_real_adapters).

Safety, by construction (mirrors ClaudeCLI):
- Codex has no per-tool allow-list: it is confined by `sandbox_mode="workspace-write"` (writes only
  in the run workspace, network off - no network_access override) and `approval_policy="never"`;
  `allowed_tools` must be empty. There is NO `-a` flag on `codex exec` in 0.162.1, so the policy is
  a `-c` override. Never `--dangerously-bypass-approvals-and-sandbox`, never a wider sandbox;
- `--ignore-user-config` (nothing in CODEX_HOME/config.toml is read - auth still uses CODEX_HOME)
  and `--ignore-rules`; the login method is forced with constant `-c` overrides
  (`forced_login_method="chatgpt"`, `cli_auth_credentials_store="file"`);
- `-m` / `--model=<id>` exactly as given; resume ONLY with the exact thread id (a UUID): the CLI
  would also resolve a "thread name", so anything else is refused, and never `--last`;
- the prompt goes on stdin (`-`); CLI and probe get a minimal environment (CODEX_HOME, no keys);
- auth is read for ONE field only: `auth_mode` of $CODEX_HOME/auth.json - never a token.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path

from aibroker.agent.adapter import AgentRunSpec, AuthAttestation, BillingMode
from aibroker.agent.parsers import CodexParser, parse_codex_login_status
from aibroker.agent.real_cli import Probe, SubprocessCLI

CODEX_ENV_KEYS = ("PATH", "HOME", "CODEX_HOME", "LANG", "TMPDIR")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# Constant overrides - never built from caller input.
CONFIG_OVERRIDES = (
    'sandbox_mode="workspace-write"', 'approval_policy="never"',
    'forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"',
)
_LOGIN_OVERRIDES = ('forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"')
SUBSCRIPTION_PROVIDER = "openai-chatgpt-subscription"


def codex_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The ONLY environment Codex and its login probe ever see: built from scratch from an
    allow-list (no OPENAI_*, no CODEX_API_KEY, no broker secret)."""
    src = os.environ if source is None else source
    return {k: src[k] for k in CODEX_ENV_KEYS if k in src}


def read_auth_mode(codex_home: str | None) -> str | None:
    """The `auth_mode` string of $CODEX_HOME/auth.json - and nothing else: the file also holds
    tokens, which are never kept, returned or logged. None when absent / unreadable."""
    if not codex_home:
        return None
    try:
        data = json.loads((Path(codex_home) / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mode = data.get("auth_mode") if isinstance(data, dict) else None
    return mode if isinstance(mode, str) else None


def build_codex_argv(run: AgentRunSpec, binary: str = "codex") -> list[str]:
    """`codex exec [resume <thread id>] --json ... -` with constant overrides; ValueError when
    the request cannot be expressed safely."""
    if run.allowed_tools:
        raise ValueError("agent:codex is sandboxed to the workspace; allowed_tools must be empty")
    argv = [binary, "exec"]
    if run.session_id:
        if not UUID_RE.match(run.session_id):
            raise ValueError("resume needs the exact thread id (a UUID), never a name or --last")
        argv += ["resume", run.session_id]
    argv += [f"--image={p}" for p in run.attachments]       # real files, not descriptions
    argv.append("--json")
    if run.model:
        argv.append(f"--model={run.model}")
    for override in CONFIG_OVERRIDES:
        argv += ["-c", override]
    argv += ["--skip-git-repo-check", "--ignore-user-config", "--ignore-rules", "-"]
    return argv


def build_codex_prompt(run: AgentRunSpec) -> str:
    if not run.attachments:
        return run.prompt
    listing = "\n".join(f"- {p}" for p in run.attachments)
    return f"{run.prompt}\n\nAttached input files (real files in the working directory):\n{listing}\n"


class CodexCLI(SubprocessCLI):
    """OpenAI Codex CLI (`codex exec --json`). UNVERIFIED against a real run."""

    def __init__(self, binary: str = "codex", probe: Probe | None = None,
                 kill_grace_s: float = 5.0, env: Mapping[str, str] | None = None) -> None:
        super().__init__(binary, probe, kill_grace_s, env)

    def make_env(self, source: Mapping[str, str] | None) -> dict[str, str]:
        return codex_env(source)

    def build_argv(self, run: AgentRunSpec) -> list[str]:
        return build_codex_argv(run, self.binary)

    def new_parser(self, run: AgentRunSpec) -> CodexParser:
        return CodexParser()

    def stdin_payload(self, run: AgentRunSpec) -> str:
        return build_codex_prompt(run)

    def preflight(self, run: AgentRunSpec) -> tuple[str, str] | None:
        if not self._env.get("CODEX_HOME"):
            return "settings_refused", "CODEX_HOME is not set"
        if (Path(run.workspace) / ".codex").exists():       # project-level config layer
            return "settings_refused", "the run workspace carries a .codex directory"
        return None

    async def billing_mode(self) -> BillingMode:
        """Subscription only if BOTH `codex login status` states a ChatGPT login AND
        $CODEX_HOME/auth.json says auth_mode == "chatgpt"; an API-key login is "api"; anything
        else (not logged in, unreadable, unknown wording) is "unknown" and the guard refuses."""
        self._auth = None
        try:
            text = await self._probe(
                [self.binary, "login", "-c", _LOGIN_OVERRIDES[0], "-c", _LOGIN_OVERRIDES[1],
                 "status"], self._env)
            probed = parse_codex_login_status(text)
            auth_mode = read_auth_mode(self._env.get("CODEX_HOME"))
        except Exception:  # noqa: BLE001 - cannot tell -> unknown -> the guard refuses
            return "unknown"
        if probed == "api" or (auth_mode or "").lower().replace("_", "") in ("apikey",):
            return "api"
        if probed == "subscription" and auth_mode == "chatgpt":
            self._auth = AuthAttestation("chatgpt", SUBSCRIPTION_PROVIDER)
            return "subscription"
        return "unknown"
