# Codex worker activation review (updated 2026-10-12)

This is a review package, not an instruction to activate the worker. The Codex
subscription route is disabled by default. A seven-rule AppArmor profile
passed one fake-data isolation canary and is loaded in enforce mode on the
host. The worker is stopped; owner login, real text completion, and a real
headless PNG have not been verified.

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
container. The exact profile with SHA-256
`63389ed14e3e9cb0e7c3a610f7625474dc34ee836537099b8c6f98fe94931052`
adds seven rules relative to the original Docker-derived baseline. Silent
`rslave` on `/` was deployed earlier; this change adds the six remaining
rules: a `devpts` mount at `/newroot/dev/pts/`, an exact read-only remount at
`/newroot/`, three exact-flag read-only remounts at `/newroot/**`, and silent
`rprivate` at `/oldroot/`. The subtree patterns match other descendants with
the same flags beyond the ten paths in the earlier audit. They do not grant a
general `mount,` rule, `unconfined`, `privileged`, or an extra capability.

The owner-authorized network-disabled canary on 2026-10-12 used only fake auth
and exited 0: `claude:ABSENT`, `auth:DENIED`, `parent:DENIED`. No
`aibroker-codex` AppArmor denial appeared in the following audit window. The
loaded profile has these seven rules; the deployed file still has only silent
`rslave` until this exact profile is merged and deployed. A reload from that
older file would lose the six tested additions. The canary is evidence for
those three fake-data checks, not proof of all reads or a real model run.

## Code fixes prepared for repeat review

1. Superseded 2026-10-13: the global shared-home lease was removed; normal Codex runs now
   run in parallel, and only a resumed session is exclusive (see `agent-runs.md`). PNG
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
hour. The profile in this change needs independent diff review and CI. Real
text and PNG checks still require owner login; these fixes alone do not
activate the route.

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

Keep both Codex submission paths disabled and the worker stopped through the
profile deploy. The normal application deploy does not install AppArmor
profiles. After it succeeds, install the reviewed checkout file at
`/etc/apparmor.d/aibroker-codex` with mode 0644, verify that copy's SHA-256
against the value above, syntax-check it with
`apparmor_parser -Q /etc/apparmor.d/aibroker-codex`, load it with
`apparmor_parser -r -W /etc/apparmor.d/aibroker-codex`, and verify
`aibroker-codex (enforce)`.
`apparmor.service` is enabled on this host; the `/etc/apparmor.d` copy is what
will reload after reboot. The enabled service does not read the repository
checkout automatically.
Check Broker health and the other worker before the owner begins login. The
owner alone enables device-code authorization in ChatGPT settings and runs the
interactive `codex login --device-auth` command documented in
`docs/agent-runs.md` inside `agent-worker-codex`. The owner then checks the
ChatGPT login status and paid continuation setting without sharing device
codes, auth files, or tokens. Run supervised text and real decodable PNG checks
before enabling the route; preserve the existing universal jobs contract.

For rollback, disable both Codex submission paths, drain or cancel active
runs, stop `agent-worker-codex`, restore
`/var/backups/aibroker-codex-apparmor-530ff54.profile` (SHA-256
`a6c4303751234861502f9f85f6517ede09576afbfc296046ef53efc23ef9acf4`)
to `/etc/apparmor.d/aibroker-codex`, and reload it with
`apparmor_parser -r -W /etc/apparmor.d/aibroker-codex` in enforce mode. Leave the
worker stopped and the named auth volume intact.
If the `seccomp=` and `apparmor=` options are later removed, the sandbox
preflight must fail closed as `sandbox_unavailable`.
