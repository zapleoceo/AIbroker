# Codex worker activation review (2026-10-11)

This is a review package, not an instruction to activate the worker. The Codex
subscription route is disabled by default. No sandbox profile has been loaded,
no owner login has been performed for this worker, and no real headless PNG has
been verified.

## Exact profile change proposed

Only `agent-worker-codex` selects `infra/agent-worker/codex-seccomp.json` and
the `aibroker-codex` AppArmor profile. The seccomp file derives from the
recorded Moby default and adds two allow rules: `unshare` restricted to the
listed namespace flags, then `clone`, `setns`, `mount`, `umount2`, and
`pivot_root`. `clone3` remains refused. The AppArmor profile replaces the
default `deny mount` with namespace mount, bind/remount, and pivot rules. It
otherwise retains the recorded Docker default. The container retains a
nonroot user, `cap_drop: ALL`, `read_only`, `no-new-privileges`, tmpfs
workspaces, a dedicated Codex auth volume, no Docker socket, and only the
`agent_db` Docker network. Internet egress through that network remains
possible for the CLI.

The change increases the kernel namespace and mount surface available to this
container. Its real enforcement and compatibility with bubblewrap remain
unverified; a supervised canary must confirm that model commands cannot read
the auth file, Claude state, or another process's environment. Do not use
`unconfined` or `privileged` for that test.

## Review blockers before activation

1. `images.py` scans a shared `$CODEX_HOME/generated_images` by mtime. Overlapping
   runs can attach another run's PNG. Bind export to a run: serialize all Codex
   runs sharing that home with a fenced lease, snapshot candidate paths before
   the CLI starts, and export only new paths. Verify the real CLI output path.
2. `agent-worker-codex` does not receive `AGENT_PROJECT_POLICY`, so its worker
   policy recheck refuses submitted runs. Add the existing exact policy setting
   only after approval; do not broaden project or model access.
3. The Codex worker currently inherits Claude's
   `AGENT_PAID_CONTINUATION_VERIFIED_OFF` setting. Codex needs a separate
   owner-verified flag, default false, before autonomous runs. API-billed
   fallback stays prohibited.
4. The API Compose environment does not forward
   `AGENT_SUBSCRIPTION_CODEX_ENABLED` or `AGENT_SUBSCRIPTION_MODELS`. Keep the
   first flag false until approval and canaries pass. The direct
   `/v1/agent/runs` Codex path needs the same activation gate.

## Minimal project access decision

For project 12's universal `chat:smart` client, `POST /v1/jobs` requires
`llm:chat`; an explicit subscription pin additionally requires `llm:agent`,
the existing project allowlist, and the exact model/account policy. Its
current `llm:agent` grant alone cannot submit `chat:smart`. `GET /v1/models`
requires an authenticated project key, with no added scope. A successful
subscription job and its PNG files are read through `GET /v1/jobs/{id}`, which
rechecks `llm:agent`, allowlist, and policy. There is no separate image scope
or image endpoint. The only scope addition proposed for project 12 is
`llm:chat`, subject to owner approval.

## Operator sequence after explicit approval

Drain both Codex submission paths, allow active runs to finish or cancel them,
then stop `agent-worker-codex`. Review and load only the named profile on the
host, start only that worker, and perform the isolated canary without real
credentials. The owner alone performs interactive device login in
`agent-worker-codex` and separately checks Codex paid continuation settings.
Enable the route only after billing, read isolation, exact model routing, and
real decodable PNG tests pass. Do not log, print, or transfer auth contents.

For rollback, disable both Codex submission paths, drain or cancel active
runs, stop `agent-worker-codex`, remove its `seccomp=` and `apparmor=` options,
and unload `aibroker-codex`. Leave the named auth volume intact. Without the
profiles the sandbox preflight must fail closed as `sandbox_unavailable`.
