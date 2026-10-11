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

## Code fixes prepared for repeat review

1. A fenced shared-home lease serializes normal Codex runs across workers. PNG
   export is restricted to the fresh thread UUID reported by this CLI process;
   resumed threads and pre-populated thread directories fail closed for PNGs.
   The [pinned 0.162.1 source](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/ext/image-generation/src/artifact.rs)
   places generated PNGs under
   `generated_images/<thread UUID>/<call ID>.png`. Real headless output and
   absence of concurrent owner CLI activity still need a supervised check.
2. Compose now passes the existing exact `AGENT_PROJECT_POLICY` to the Codex
   worker. No project or model policy was broadened.
3. Codex now uses its own
   `AGENT_CODEX_PAID_CONTINUATION_VERIFIED_OFF` setting, default false;
   Claude's approval cannot authorize a Codex run. API fallback stays prohibited.
4. Compose now forwards `AGENT_SUBSCRIPTION_MODELS` and the default-off
   `AGENT_SUBSCRIPTION_CODEX_ENABLED` to the API. Both Codex submission paths
   and queued Codex runs check the activation gate.

`run.completed` events are metadata-only for PNGs; full bytes remain in the job
result until its normal 7-day purge even though API content expires after one
hour. These fixes still require repeat independent review and the real sandbox/PNG
canary. They do not approve or apply the profile.

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
