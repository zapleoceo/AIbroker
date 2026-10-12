# Proposed internal APIs for subscription workers

Status: design only. No worker listener, Docker mount, or routing change is enabled by this document.

## Current path

Broker accepts the ordinary `POST /v1/jobs?capability=...` request and stores a
job. The separate Claude and Codex containers each claim only their own
`agent:*` rows through `pgbouncer` on the `agent_db` bridge. Clients poll the
ordinary `GET /v1/jobs/{id}` route. Both workers have an isolated auth volume,
`read_only: true`, `cap_drop: [ALL]`, and `no-new-privileges:true`; Codex also
uses its dedicated seccomp and AppArmor profiles.

## Proposed worker API boundary

Each worker would listen on its own Unix domain socket. Broker would be the
only client. The API would expose provider metadata and a bounded wake signal
for a job already accepted and stored by Broker. The worker would load the job
by ID and repeat its existing capability, project, model, billing, and policy
checks before claiming it. A wake would never carry a prompt, credential, or
tool definition. The database remains authoritative for state, deduplication,
cancel, event history, and `GET /v1/jobs/{id}`. The existing DB polling loop
remains the fallback, so no client migration is required.

Proposed internal operations:

| Operation | Input | Result |
| --- | --- | --- |
| `GET /internal/v1/provider` | none | Provider id, configured model ids, accepted request controls, tool support, worker readiness. No account metadata. |
| `POST /internal/v1/wake` | Numeric `job_id` | `202` acknowledges the signal for every syntactically valid ID. An unknown, terminal, wrong-provider, or unauthorized job is silently ignored. No direct CLI execution in the HTTP handler. |
| `GET /internal/v1/health` | none | Readiness and capability only; no auth or environment values. |

The worker must verify the job's assigned provider and project policy before
claiming it. The wake response never reveals whether an ID exists or belongs
to another project.

Broker would normalize the metadata into the existing `GET /v1/models` rows;
`POST /v1/jobs` would keep its current body, authorization, validation, 202
response, and polling URL. If the socket is unavailable, the stored job stays
pending and the existing worker polling path claims it. No API-billed fallback
is introduced.

## Exact Docker and permission change requiring approval

- Add named volumes `agent_claude_rpc` and `agent_codex_rpc`. Mount them at
  `/run/aibroker-agent-rpc/claude` and `/run/aibroker-agent-rpc/codex` in the
  API container, respectively. Mount only the matching volume at
  `/run/aibroker-agent-rpc` in each worker. Each worker gets only its own socket
  directory.
- Directories would be owned by uid 10001 and mode `0700`; sockets mode `0600`.
  The API and workers already run as uid 10001. A worker would remove only its
  own stale socket path at startup. These volumes hold only the RPC socket;
  CLI auth files remain in the existing separate worker-only auth volumes.
  Neither auth volume is mounted into API, and no credential content crosses
  the proposed RPC boundary.
- **Docker networks:** no new network or attachment. Workers remain only on
  `agent_db`; Broker retains its current network attachments.
- **TCP/UDP ports and host exposure:** none. No `ports:`, `expose:`, host
  networking, or public reverse proxy route is added for workers.
- **Privileges:** no new capabilities, privileged mode, seccomp/AppArmor
  relaxation, or changes to `no-new-privileges`. The two new writable socket
  mounts are the precise new persistent accesses.

This design needs explicit approval of those two socket volumes and the
Broker-to-worker local RPC access before implementation or deployment. The
existing DB queue is adequate until that approval; no partial listener should
be enabled.
