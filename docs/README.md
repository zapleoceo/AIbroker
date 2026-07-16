# AIbroker — documentation index

Every change in `src/`, `infra/`, `monitor/` MUST update the relevant doc.
CI fails otherwise. Opt-out: `docs-not-needed` in the commit message
(for pure refactors that don't shift behavior).

## Files

| Doc | What's in it |
|---|---|
| [architecture.md](./architecture.md) | High-level diagram, request flow, scaling story |
| [api.md](./api.md) | Every public endpoint (proxy + admin + dashboard) |
| [auth.md](./auth.md) | X-Admin-Key / X-Project-Key / Telegram login flow, scopes |
| [routing.md](./routing.md) | Capability → provider chains, LRU + cap-aware selector, cost guard |
| [providers.md](./providers.md) | Per-provider mapping (model defaults, LiteLLM behavior, health probes) |
| [deploy-ops.md](./deploy-ops.md) | Restricted SSH key flow, GH Actions secrets, manual deploy fallback |
| [security.md](./security.md) | Threat model, encryption at rest, audit log, key leak response |
| [conventions.md](./conventions.md) | Code style, file layout, naming, test patterns |
| [domain-model.md](./domain-model.md) | Postgres schema with rationale (projects, api_keys, leases, usage_log, audit_log, deep_jobs, provider_observations) |
| [roadmap.md](./roadmap.md) | Restructuring plan, tech-debt register, phase status + done-log |

## How to write a doc change

1. Touch the file under `docs/` that maps to the area you changed.
2. Either: actual content update OR an explicit dated line "no behavioral change" if you really only renamed an internal symbol.
3. Push. CI verifies a doc file changed in the same commit range.
