#!/usr/bin/env bash
# Local approximation of .github/workflows/docs-check.yml (CI stays the authority).
# Usage: scripts/docs_gate.sh [BASE] [HEAD]   — defaults: origin/master HEAD.
# Compares merge-base(BASE, HEAD)..HEAD. Equal to CI's base (github.event.before) for a
# fast-forward push to master with a freshly fetched origin/master; otherwise approximate.
#        scripts/docs_gate.sh --offline [BASE] [HEAD]   — `--offline` must come first; no fetch;
#        NON-GATING (always exit 0),
#        result is informational only and is NOT CI parity.
set -euo pipefail
OFFLINE=0
if [ "${1:-}" = "--offline" ]; then OFFLINE=1; shift; fi
BASE="${1:-origin/master}"
HEAD="${2:-HEAD}"
# Any base on the remote (the default, or an explicit origin/...) is fetched first.
if [ "$OFFLINE" -eq 0 ] && [[ "$BASE" == origin/* ]]; then
  if ! git fetch -q origin "${BASE#origin/}"; then
    echo "docs gate: FAIL - stale base: fetch failed, gate not evaluated"; exit 2
  fi
fi
if ! MB="$(git merge-base "$BASE" "$HEAD")"; then
  echo "docs gate: FAIL - cannot compute merge-base of $BASE and $HEAD, gate not evaluated"
  if [ "$OFFLINE" -eq 1 ]; then exit 0; fi
  exit 2
fi
CHANGED="$(git diff --name-only "$MB" "$HEAD")"
LOG="$(git log --format=%B "${MB}..${HEAD}")"
if printf '%s\n' "$LOG" | grep -iF "docs-not-needed" >/dev/null; then
  echo "docs gate: 'docs-not-needed' in a commit message - skipped (same as CI)"; exit 0
fi
CODE="$(echo "$CHANGED" | grep -E '^(src|infra|services|monitor)/' || true)"
DOCS="$(echo "$CHANGED" | grep -E '^docs/' || true)"
if [ -n "$CODE" ] && [ -z "$DOCS" ]; then
  echo "docs gate: FAIL - code under src/ infra/ services/ monitor/ changed but docs/ was not updated:"
  echo "$CODE"
  if [ "$OFFLINE" -eq 1 ]; then echo "docs gate: offline mode is non-gating - exit 0"; exit 0; fi
  exit 1
fi
if [ "$OFFLINE" -eq 1 ]; then echo "docs gate: PASS (offline, informational, not CI parity)"; exit 0; fi
echo "docs gate: PASS"
