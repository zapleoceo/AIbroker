#!/usr/bin/env bash
# Local mirror of .github/workflows/docs-check.yml (CI stays the authority).
# Usage: scripts/docs_gate.sh [BASE] [HEAD]   — defaults: origin/master HEAD,
# i.e. exactly what CI compares on a push (github.event.before .. github.sha).
set -euo pipefail
BASE="${1:-origin/master}"
HEAD="${2:-HEAD}"
CHANGED="$(git diff --name-only "$BASE" "$HEAD")"
if git log --format=%B "${BASE}..${HEAD}" | grep -qiF "docs-not-needed"; then
  echo "docs gate: 'docs-not-needed' in a commit message - skipped (same as CI)"; exit 0
fi
CODE="$(echo "$CHANGED" | grep -E '^(src|infra|services|monitor)/' || true)"
DOCS="$(echo "$CHANGED" | grep -E '^docs/' || true)"
if [ -n "$CODE" ] && [ -z "$DOCS" ]; then
  echo "docs gate: FAIL - code under src/ infra/ services/ monitor/ changed but docs/ was not updated:"
  echo "$CODE"; exit 1
fi
echo "docs gate: PASS"
