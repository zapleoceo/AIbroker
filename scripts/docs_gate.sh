#!/usr/bin/env bash
# Local approximation of .github/workflows/docs-check.yml (CI stays the authority).
# Usage: scripts/docs_gate.sh [BASE] [HEAD]   — defaults: origin/master HEAD.
# Compares merge-base(BASE, HEAD)..HEAD. Equal to CI's base (github.event.before) for a
# fast-forward push to master with a freshly fetched origin/master; otherwise approximate.
set -euo pipefail
if [ $# -eq 0 ]; then git fetch -q origin master || echo "docs gate: warning - fetch failed, origin/master may be stale"; fi
BASE="${1:-origin/master}"
HEAD="${2:-HEAD}"
MB="$(git merge-base "$BASE" "$HEAD")"
CHANGED="$(git diff --name-only "$MB" "$HEAD")"
LOG="$(git log --format=%B "${MB}..${HEAD}")"
if printf '%s\n' "$LOG" | grep -iF "docs-not-needed" >/dev/null; then
  echo "docs gate: 'docs-not-needed' in a commit message - skipped (same as CI)"; exit 0
fi
CODE="$(echo "$CHANGED" | grep -E '^(src|infra|services|monitor)/' || true)"
DOCS="$(echo "$CHANGED" | grep -E '^docs/' || true)"
if [ -n "$CODE" ] && [ -z "$DOCS" ]; then
  echo "docs gate: FAIL - code under src/ infra/ services/ monitor/ changed but docs/ was not updated:"
  echo "$CODE"; exit 1
fi
echo "docs gate: PASS"
