# AIbroker — rules for coding agents

Project conventions live in `docs/conventions.md`; this file adds agent-process rules.

## Documentation is always current (owner rule, 2026-10-10)

- Every change to behaviour, API, configuration or deployment gets its documentation updated
  by a **Haiku subagent** before the PR and before deploy: the subagent updates the matching
  files under `docs/` (and `README.md` / docstrings where they describe the change).
- Code and its documentation ship together in the same delivery. The docs gate in CI
  (`.github/workflows/docs-check.yml`, docs name-sync in `deploy.yml`) stays enabled; no
  blanket exemptions and no gate bypasses are added.
- If a Haiku subagent is unavailable, report the concrete blocker. Never skip the
  documentation update.

## Before every push

Run `bash scripts/docs_gate.sh` (local approximation of `.github/workflows/docs-check.yml`: same paths and
opt-out, diff from the merge-base with a freshly fetched `origin/master`). A FAIL means docs/ must be updated first.
It is an approximation: it equals CI only for a fast-forward push to `master` after a
successful fetch; if the fetch fails the gate exits non-zero ("stale base") and never PASSes.
`--offline` skips the fetch and is non-gating (informational only, not CI parity).
CI remains the authority; the local check is not a guarantee.
