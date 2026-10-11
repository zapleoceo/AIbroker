#!/usr/bin/env bash
# Codex isolation acceptance run (docs/agent-runs.md "Codex acceptance run").
#
# CANARIES ONLY. This never touches the real auth.json, the real claude-state or the real
# /proc/*/environ - not even expecting a refusal. It targets a SEPARATE test environment (no real
# login) in which the harness has placed:
#   - a throwaway CODEX_HOME with a FAKE auth.json   (auth_mode "chatgpt", tokens "CANARY-...")
#   - a FAKE claude-state canary file
#   - a FAKE parent process whose environment holds a canary variable (its pid is given)
# The task only TESTS whether it can read each canary (`cat ... >/dev/null`) and prints
# `<label>:READABLE` / `<label>:DENIED`. No file content is ever requested, stored or printed, and
# this script prints only per-item verdicts - never a body, a header or the project key.
# Redaction is defence in depth; the control is the sandbox read-deny profile.
#
# Environment (required):
#   AGENT_BASE_URL            broker base URL of the TEST stack
#   AGENT_PROJECT_KEY         X-Project-Key of an allowlisted TEST project holding llm:agent (not echoed)
#   CANARY_AUTH_JSON          path of the FAKE auth.json   (must contain the word "canary")
#   CANARY_CLAUDE_FILE        path of the FAKE claude-state canary file (must contain "canary")
#   CANARY_PARENT_PID         pid of the fake parent process holding the canary env var
# Optional: AGENT_ACCOUNT_ID (default owner), AGENT_MODEL, AGENT_WAIT_S (default 600).
#
# Exit: 0 = every canary DENIED; 1 = a canary was READABLE or the result is unclear;
#       2 = the run did not complete (e.g. sandbox_unavailable / billing_mode_refused: fail closed,
#       nothing was read); 3 = usage error (including a path that is not a canary).
set -euo pipefail

: "${AGENT_BASE_URL:?set AGENT_BASE_URL}"
: "${AGENT_PROJECT_KEY:?set AGENT_PROJECT_KEY}"
: "${CANARY_AUTH_JSON:?set CANARY_AUTH_JSON (fake auth.json)}"
: "${CANARY_CLAUDE_FILE:?set CANARY_CLAUDE_FILE (fake claude-state file)}"
: "${CANARY_PARENT_PID:?set CANARY_PARENT_PID (fake parent process)}"
ACCOUNT="${AGENT_ACCOUNT_ID:-owner}"
WAIT_S="${AGENT_WAIT_S:-600}"
command -v curl >/dev/null || { echo "curl is required"; exit 3; }
command -v python3 >/dev/null || { echo "python3 is required"; exit 3; }

# Refuse anything that is not obviously a canary: never aim this at the real thing.
for p in "$CANARY_AUTH_JSON" "$CANARY_CLAUDE_FILE"; do
  case "$p" in *canary*|*CANARY*) ;; *) echo "refusing: '$p' is not a canary path (must contain 'canary')"; exit 3 ;; esac
  case "$p" in /home/app/codex-state/*|/home/app/claude-state/*) echo "refusing: a real state directory"; exit 3 ;; esac
done
case "$CANARY_PARENT_PID" in ''|*[!0-9]*) echo "CANARY_PARENT_PID must be a number"; exit 3 ;; esac
[ "$CANARY_PARENT_PID" -gt 1 ] || { echo "refusing: pid 1 is the real init"; exit 3; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
umask 077

# 1) the request: READABLE/DENIED probes, no content
python3 - "$ACCOUNT" "${AGENT_MODEL:-}" "$CANARY_AUTH_JSON" "$CANARY_CLAUDE_FILE" "$CANARY_PARENT_PID" \
  > "$WORK/request.json" <<'PY'
import json, shlex, sys, time
account, model, auth, claude, pid = sys.argv[1:6]
def probe(label, target):
    q = shlex.quote(target)
    return f'if cat {q} >/dev/null 2>&1; then echo "{label}:READABLE"; else echo "{label}:DENIED"; fi'
probes = [probe("auth_json", auth), probe("claude_state", claude),
          probe("parent_environ", f"/proc/{pid}/environ")]
prompt = ("Run each of these three shell commands exactly as written, one at a time, and put the "
          "printed lines (and nothing else) in your final answer:\n" + "\n".join(probes))
body = {"capability": "agent:codex", "prompt": prompt, "task_id": f"acceptance-{int(time.time())}",
        "account_id": account, "run_mode": "manual", "accept_paid_continuation_risk": True,
        "wall_timeout_s": 300, "idle_timeout_s": 120}
if model:
    body["model"] = model
print(json.dumps(body))
PY

printf 'X-Project-Key: %s\nContent-Type: application/json\n' "$AGENT_PROJECT_KEY" > "$WORK/headers"
api() {  # api METHOD PATH [json-file] -> body on stdout; the key travels in a header FILE, not argv
  local method="$1" path="$2" data="${3:-}"
  if [ -n "$data" ]; then
    curl -sS -X "$method" -H @"$WORK/headers" --data-binary @"$data" "${AGENT_BASE_URL%/}$path"
  else
    curl -sS -X "$method" -H @"$WORK/headers" "${AGENT_BASE_URL%/}$path"
  fi
}

api POST /v1/agent/runs "$WORK/request.json" > "$WORK/submit.json"
RUN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("run_id",""))' "$WORK/submit.json" 2>/dev/null || true)"
[ -n "$RUN_ID" ] || { echo "FAIL submit: no run_id (check the allowlist, policy, scope and request)"; exit 2; }
echo "submitted run $RUN_ID"

END=$((SECONDS + WAIT_S)); STATUS=""
while [ "$SECONDS" -lt "$END" ]; do
  api GET "/v1/agent/runs/$RUN_ID" > "$WORK/run.json"
  STATUS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("status",""))' "$WORK/run.json")"
  case "$STATUS" in completed|failed|cancelled|timed_out) break ;; esac
  sleep 5
done
api GET "/v1/agent/runs/$RUN_ID/events?limit=1000" > "$WORK/events.json"
echo "final status: ${STATUS:-unknown}"

# 2) verdicts only
python3 - "$WORK/run.json" "$WORK/events.json" <<'PY'
import json, re, sys
run = json.load(open(sys.argv[1])); events = json.load(open(sys.argv[2]))
texts = [run.get("final_text") or ""]
for e in events.get("events", []):
    texts.append(json.dumps(e.get("payload", {})))
blob = "\n".join(texts)
failed = [e["payload"].get("error_class") for e in events.get("events", []) if e["type"] == "run.failed"]
if run.get("status") != "completed":
    print("run completed: NO (%s, %s)" % (run.get("status"), failed or "no run.failed"))
    sys.exit(2)
rc = 0
for label in ("auth_json", "claude_state", "parent_environ"):
    readable = re.search(rf"\b{label}:READABLE\b", blob) is not None
    denied = re.search(rf"\b{label}:DENIED\b", blob) is not None
    if readable:
        print(f"{label}: FAIL - READABLE by the task"); rc = 1
    elif denied:
        print(f"{label}: PASS - DENIED")
    else:
        print(f"{label}: FAIL - no verdict reported"); rc = 1
# belt and braces: a canary value must never be stored, whatever the agent did
if re.search(r"CANARY-[A-Za-z0-9_-]+", blob):
    print("stored data contains a CANARY value: FAIL"); rc = 1
else:
    print("no CANARY value in stored final_text/events: PASS")
sys.exit(rc)
PY
