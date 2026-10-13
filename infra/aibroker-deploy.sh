#!/bin/bash
# Restricted deploy entrypoint — invoked via authorized_keys command=, so it
# takes no arguments and can do nothing else even if the key leaks.
#
# Install with: infra/install-deploy.sh (copies this to /usr/local/bin/).
# It lives in the repo, not only on the box, so a change to how production is
# deployed goes through the same review and history as the code it deploys.
#
# 2026-09-02: previously this waited on `aibroker-api` alone and exited 0. That
# made it blind twice over — a sibling service could fail to start, or could
# keep running with a STALE config, and the deploy still reported success. Both
# happened: after a manual `docker run` during a measurement, `compose up -d`
# left aibroker-vision-local on its old `-c 8192 --sleep-idle-seconds 900`
# command while `docker ps` cheerfully reported "healthy". Container status
# says nothing about which config a container was started with.
set -euo pipefail

# 2026-10-13: agent-worker / agent-worker-codex sit under `profiles: ["agent"]`, so
# profile-less compose calls never saw them: deploy did not rebuild or recreate
# them and both gates were blind (prod ran PR #56 code at master #62). They are
# handled explicitly below. Keep this list equal to the compose services that
# have `profiles:` (tests/test_deploy_script_profiles.py enforces it).
AGENT_SERVICES="agent-worker agent-worker-claude2 agent-worker-codex"

# AIBROKER_DEPLOY_DIR exists only so tests can run this script; the forced-command
# ssh cannot pass env, so prod always uses the default.
REMOTE_DIR="${AIBROKER_DEPLOY_DIR:-/var/www/aibroker}"
cd "$REMOTE_DIR"

echo "--- git fetch ---"
git fetch --quiet origin master
git reset --hard --quiet origin/master
git log --oneline -1

echo "--- compose build ---"
docker compose build --quiet
# Workers are FROM aibroker-api:latest and one `compose build` has no ordering,
# so a worker could be built on the stale base: build them after the base.
# Always built, so the image is ready even while a worker is stopped.
docker compose --profile agent build --quiet $AGENT_SERVICES

# Stopping a worker is the documented rollback (docs/agent-runs.md): deploy must
# not start a worker the owner stopped, only refresh the ones that are running.
# Capture first: a failing `ps` inside `for ... in $(...)` would not trip `set -e`,
# the script would see "no active workers" and go green (the 2026-10-13 incident again).
RUNNING="$(docker compose --profile agent ps --status running --services)"
ACTIVE_AGENT=""
for svc in $RUNNING; do
    case " $AGENT_SERVICES " in *" $svc "*) ACTIVE_AGENT="$ACTIVE_AGENT $svc" ;; esac
done
ACTIVE_AGENT="${ACTIVE_AGENT# }"
echo "active agent workers: ${ACTIVE_AGENT:-none}"

echo "--- compose up ---"
# Without the profile, --remove-orphans leaves profile containers alone (prod:
# the old worker survived every deploy), so a stopped worker stays stopped.
docker compose up -d --remove-orphans
if [ -n "$ACTIVE_AGENT" ]; then
    docker compose --profile agent up -d --no-deps $ACTIVE_AGENT
fi

# ── Drift gate ──────────────────────────────────────────────────────────────
# `up -d --dry-run` reports what a SECOND `up` would still have to do. On a
# fully-applied deploy that is nothing: every line ends in Running / Healthy /
# Waiting. Any other verb (Recreate, Create, Start, Restart) means a container
# is NOT running the config we just deployed — which is exactly the failure the
# old health-only check could not see.
echo "--- drift check ---"
DRIFT="$(docker compose up -d --dry-run 2>&1 \
    | sed 's/^ *DRY-RUN MODE - *//' \
    | grep -vE '(Running|Healthy|Waiting)$' \
    | grep -E '^ *Container ' || true)"
if [ -n "$DRIFT" ]; then
    echo "::error:: containers are NOT running the deployed config:"
    echo "$DRIFT"
    echo "Re-run 'docker compose up -d' and investigate — a manual 'docker run'"
    echo "or 'docker restart' on a compose-managed container is the usual cause."
    exit 12
fi
if [ -n "$ACTIVE_AGENT" ]; then
    DRIFT="$(docker compose --profile agent up -d --dry-run --no-deps $ACTIVE_AGENT 2>&1 \
        | sed 's/^ *DRY-RUN MODE - *//' \
        | grep -vE '(Running|Healthy|Waiting)$' \
        | grep -E '^ *Container ' || true)"
    if [ -n "$DRIFT" ]; then
        echo "::error:: agent workers are NOT running the deployed config:"
        echo "$DRIFT"
        exit 12
    fi
fi
echo "all services match the compose file"

# ── Health gate, every service (not just api) ───────────────────────────────
# Services without a healthcheck (both agent workers) report an empty Health; for
# those "running" is all we can assert. `ps -a` so an exited container shows up
# instead of vanishing from the list. Anything still "starting" keeps us waiting;
# "unhealthy" or a non-running state fails the deploy. `ps -a` also lists leftover
# `docker compose run` one-offs (e.g. an exited aibroker-api-run-<hash>); those are
# skipped by name. PS is captured first so a failing `ps` aborts instead of looking empty.
echo "--- waiting for services (up to 180s) ---"
for i in $(seq 1 60); do
    BAD=""
    PS="$(docker compose --profile agent ps -a --format '{{.Service}}|{{.Health}}|{{.State}}|{{.Name}}')"
    while IFS='|' read -r svc health state name; do
        [ -z "$svc" ] && continue
        case "$name" in *-run-*) continue ;; esac
        # A stopped, owner-parked worker is not part of this deploy.
        case " $AGENT_SERVICES " in
            *" $svc "*) case " $ACTIVE_AGENT " in *" $svc "*) ;; *) continue ;; esac ;;
        esac
        case "$health" in
            healthy|"") [ "$state" = "running" ] || BAD="$BAD $svc(state=$state)" ;;
            starting)   BAD="$BAD $svc(starting)" ;;
            *)          BAD="$BAD $svc(health=$health)" ;;
        esac
    done <<< "$PS"

    if [ -z "$BAD" ]; then
        echo "all services healthy after $((i * 3))s"
        # Record WHAT is running, so the deploy log answers "which config?"
        # without anyone having to guess from a status column.
        for c in aibroker-vision-local; do
            docker inspect -f "{{.Name}} args={{.Args}}" "$c" 2>/dev/null || true
        done
        exit 0
    fi
    sleep 3
done

echo "::error:: services not ready:$BAD"
docker compose --profile agent ps -a
for svc in $BAD; do
    name="${svc%%(*}"
    echo "--- logs: $name ---"
    docker compose --profile agent logs --tail 30 "$name" || true
done
exit 11
