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
- auth is read for TWO non-secret fields only: `auth_mode` and `last_refresh` of $CODEX_HOME/auth.json - never a token.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from aibroker.agent.adapter import AgentRunSpec, AuthAttestation, BillingMode, StreamEvent
from aibroker.agent.events import FileRef, SessionAssigned
from aibroker.agent.images import export_generated_pngs, snapshot_generated_paths
from aibroker.agent.parsers import CodexParser, parse_codex_login_status, sandbox_failure
from aibroker.agent.real_cli import Probe, SubprocessCLI

CODEX_ENV_KEYS = ("PATH", "HOME", "CODEX_HOME", "LANG", "TMPDIR")
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
# Constant overrides - never built from caller input.
CONFIG_OVERRIDES = (
    'sandbox_mode="workspace-write"', 'approval_policy="never"',
    'forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"',
)
PERMISSION_PROFILE = "broker"
# `--disable` features for a no_shell run (codex-cli 0.162.1 `codex features list`). Verified live
# 2026-10-16: with these off, "Run: id" is answered "command execution is disabled" and the JSON
# stream has no command_execution item. image_generation stays on (no shell involved).
NO_SHELL_FEATURES = ("shell_tool", "unified_exec", "code_mode_host", "browser_use",
                     "browser_use_external", "computer_use", "in_app_browser", "apps")
_ABS_PATH = re.compile(r"^/[A-Za-z0-9_./-]+\Z")


def permission_overrides(codex_home: str | None) -> list[str]:
    """`-c` values for the READ-isolation profile (Codex permission profiles, 0.162.1): the profile
    extends `:workspace` (writes only in the workspace) and DENIES reads+writes of CODEX_HOME (the
    ChatGPT auth.json) and /proc (other processes' environ) to every command the model runs. The CLI
    itself still reads its own auth - the sandbox only wraps the model's commands. The whole profile is
    one inline TOML table because `-c` does not unquote dotted keys (paths contain dots/slashes) and a
    `/proc/*/environ` glob makes Codex scan /proc and fail. ValueError for an unusable CODEX_HOME."""
    if not codex_home or not _ABS_PATH.match(codex_home) or ".." in codex_home.split("/"):
        raise ValueError("CODEX_HOME must be an absolute plain path to build the read-deny profile")
    deny = f'{{"{codex_home.rstrip("/")}"="deny","/proc"="deny"}}'
    return [f'default_permissions="{PERMISSION_PROFILE}"',
            f'permissions={{{PERMISSION_PROFILE}={{extends=":workspace",filesystem={deny}}}}}']
_LOGIN_OVERRIDES = ('forced_login_method="chatgpt"', 'cli_auth_credentials_store="file"')
SUBSCRIPTION_PROVIDER = "openai-chatgpt-subscription"


SandboxProbe = Callable[[list[str], Mapping[str, str], str], Awaitable[tuple[int, str]]]
AGENT_HOME_DIR = ".agent-home"      # per-run empty HOME, inside the run workspace


async def _run_sandbox_probe(argv: list[str], env: Mapping[str, str],
                             cwd: str) -> tuple[int, str]:  # pragma: no cover - runs the real CLI
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, cwd=cwd, env=dict(env), start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return 124, "sandbox self-test timed out"
    return proc.returncode or 0, out.decode(errors="replace")


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


# Codex refreshes the ChatGPT token in $CODEX_HOME/auth.json when `last_refresh` is old (its
# interval is ~8 days) or after a 401. Refresh tokens rotate, so two processes refreshing at once
# can invalidate the login. UNVERIFIED: the 8-day interval is from memory of the Codex source and
# could not be checked offline; the field name and its ISO-8601 format are UNVERIFIED too (no real
# file is kept in the repo), so anything unreadable counts as due. We use a conservative 4 days so a refresh is serialized well before it is due.
# A 401-triggered refresh cannot be predicted from the file and is not covered.
CODEX_REFRESH_DUE_AFTER_S = 4 * 24 * 3600.0
AUTH_READ_MAX_BYTES = 1024 * 1024        # a real auth.json is a few KB; anything bigger is refused


def read_last_refresh(codex_home: str | None) -> datetime | None:
    """The `last_refresh` timestamp of $CODEX_HOME/auth.json - and nothing else (the file also
    holds tokens: never read, kept or logged). None when absent / unreadable / unparseable."""
    if not codex_home:
        return None
    try:
        with (Path(codex_home) / "auth.json").open("rb") as fh:
            blob = fh.read(AUTH_READ_MAX_BYTES + 1)
        if len(blob) > AUTH_READ_MAX_BYTES:
            return None                      # not a plausible auth.json: treat as unreadable
        data = json.loads(blob.decode("utf-8"))
        raw = data.get("last_refresh") if isinstance(data, dict) else None
        if not isinstance(raw, str):
            return None
        # Codex is documented to write nanoseconds ("...05.123456789Z"); fromisoformat takes at most 6 digits.
        raw = re.sub(r"(\.\d{6})\d+", r"\1", raw.strip())
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (OSError, ValueError, RecursionError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def refresh_maybe_due(codex_home: str | None, now: datetime | None = None,
                      after_s: float = CODEX_REFRESH_DUE_AFTER_S) -> bool:
    """True when a token refresh may happen soon. A missing or unreadable timestamp counts as due
    (serialize conservatively); a timestamp in the future (clock skew) does not."""
    last = read_last_refresh(codex_home)
    if last is None:
        return True
    return ((now or datetime.now(UTC)) - last).total_seconds() >= after_s


def build_codex_argv(run: AgentRunSpec, binary: str = "codex",
                     codex_home: str | None = None) -> list[str]:
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
    for override in (*CONFIG_OVERRIDES, *permission_overrides(codex_home)):
        argv += ["-c", override]
    if run.no_shell:
        argv += [f"--disable={feature}" for feature in NO_SHELL_FEATURES]
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
                 kill_grace_s: float = 5.0, env: Mapping[str, str] | None = None,
                 sandbox_probe: SandboxProbe | None = None) -> None:
        super().__init__(binary, probe, kill_grace_s, env)
        self._sandbox_probe = sandbox_probe or _run_sandbox_probe
        self.image_max_bytes = 2 * 1024 * 1024        # set by the executor from ExecConfig
        self.image_max_count = 4
        self.image_ttl_s = 3600
        self._image_baseline: set[str] | None = None
        self._image_session_id: str | None = None

    def refresh_maybe_due(self, after_s: float = CODEX_REFRESH_DUE_AFTER_S) -> bool:
        return refresh_maybe_due(self._env.get("CODEX_HOME"), after_s=after_s)

    def snapshot_artifacts(self) -> None:
        """Called just before this run starts; only the run's own thread dir is compared."""
        self._image_baseline = snapshot_generated_paths(self._env.get("CODEX_HOME"))

    def make_env(self, source: Mapping[str, str] | None) -> dict[str, str]:
        return codex_env(source)

    def build_argv(self, run: AgentRunSpec) -> list[str]:
        return build_codex_argv(run, self.binary, self._env.get("CODEX_HOME"))

    def new_parser(self, run: AgentRunSpec) -> CodexParser:
        return CodexParser()

    async def start(self, run: AgentRunSpec) -> AsyncIterator[StreamEvent]:
        """Bind artifacts to the fresh thread ID emitted by this CLI process.

        A resumed thread can also be driven by a stale process after lease expiry;
        its shared image directory has no per-run ownership marker, so no PNG is
        exported for resumes until the CLI supplies exact per-call provenance.
        """
        self._image_session_id = None
        session_seen = False
        if run.client_tools:
            from aibroker.agent.codex_app_server import run_tool_turn
            stream = run_tool_turn(self, run)
        else:
            stream = super().start(run)
        async for item in stream:
            if isinstance(item, SessionAssigned):
                if session_seen:
                    self._image_session_id = None  # ambiguous CLI identity
                elif run.session_id is None and UUID_RE.fullmatch(item.session_id):
                    self._image_session_id = item.session_id
                session_seen = True
            yield item

    def stdin_payload(self, run: AgentRunSpec) -> str:
        return build_codex_prompt(run)

    def preflight(self, run: AgentRunSpec) -> tuple[str, str] | None:
        if not self._env.get("CODEX_HOME"):
            return "settings_refused", "CODEX_HOME is not set"
        if (Path(run.workspace) / ".codex").exists():       # project-level config layer
            return "settings_refused", "the run workspace carries a .codex directory"
        return None

    def export_artifacts(self, run: AgentRunSpec, since: float) -> list[FileRef]:
        """Generated images: copied by THIS (worker) process from the fixed
        $CODEX_HOME/generated_images into the run's out/ - the model never sees CODEX_HOME."""
        if run.session_id is not None or self._image_baseline is None or self._image_session_id is None:
            return []
        if any(p.startswith(f"{self._image_session_id}/") for p in self._image_baseline):
            return []  # a claimed new thread already has artifacts: cannot prove ownership
        return export_generated_pngs(
            self._env.get("CODEX_HOME"), run.workspace, since, max_bytes=self.image_max_bytes,
            max_count=self.image_max_count, ttl_s=self.image_ttl_s,
            baseline=self._image_baseline, session_id=self._image_session_id)

    def process_env(self, run: AgentRunSpec) -> dict[str, str]:
        """HOME is a fresh EMPTY directory inside the run workspace - never the Claude state dir and
        never CODEX_HOME (auth stays reachable only through CODEX_HOME itself)."""
        home = Path(run.workspace) / AGENT_HOME_DIR
        home.mkdir(mode=0o700, exist_ok=True)
        return {**self._env, "HOME": str(home)}

    async def preflight_async(self, run: AgentRunSpec) -> tuple[str, str] | None:
        """Sandbox self-test before EVERY run (`codex sandbox -P broker -c ... true` executes `true`
        under the Codex Linux sandbox WITH the read-deny profile, so a profile Codex cannot apply fails
        here too): if the sandbox cannot be applied (no user namespaces, landlock,
        seccomp, ...) the run fails closed as `sandbox_unavailable` - there is no retry without it."""
        try:
            argv = [self.binary, "sandbox", "-P", PERMISSION_PROFILE,
                    *(a for o in permission_overrides(self._env.get("CODEX_HOME")) for a in ("-c", o)),
                    "true"]
            rc, text = await self._sandbox_probe(argv, self.process_env(run), run.workspace)
        except Exception as exc:  # noqa: BLE001 - cannot tell -> unavailable (fail closed)
            return "sandbox_unavailable", f"sandbox self-test could not run: {exc}"[:300]
        if rc != 0 or sandbox_failure(text):
            return "sandbox_unavailable", (text.strip() or f"sandbox self-test exited {rc}")[:300]
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
