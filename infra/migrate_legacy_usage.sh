#!/usr/bin/env bash
# Consolidated import of legacy usage_log rows from Vera + Stepan databases
# into broker.usage_log. Idempotent: each rerun deletes the previous import
# (rows with workflow LIKE 'legacy:%') for the given project, then re-COPYs.
#
# Usage:
#   ./infra/migrate_legacy_usage.sh dry-run
#   ./infra/migrate_legacy_usage.sh apply
#
# Run on the Hetzner host (needs `docker exec` against the three postgres
# containers: vera3-postgres, stepan-postgres, aibroker-postgres).

set -euo pipefail

MODE="${1:-dry-run}"
if [[ "$MODE" != "dry-run" && "$MODE" != "apply" ]]; then
    echo "Usage: $0 dry-run|apply" >&2
    exit 2
fi

declare -A SOURCES=(
    [vera]="vera3-postgres vera vera"
    [stepan]="stepan-postgres stepan stepan"
)

# Map source name → broker project_id
get_project_id() {
    docker exec aibroker-postgres psql -U aibroker -d aibroker -tA \
        -c "SELECT id FROM projects WHERE name='$1'"
}

# SELECT that reshapes legacy schema → broker.usage_log column order
# (project_id, api_key_id, lease_id, provider, model, capability, workflow,
#  tokens_in, tokens_out, cost_usd, latency_ms, status, error_kind,
#  http_status, created_at).
build_select() {
    local pid="$1" tag="$2"
    cat <<EOF
SELECT
    $pid::bigint                                            AS project_id,
    NULL::bigint                                            AS api_key_id,
    NULL::text                                              AS lease_id,
    provider,
    model,
    capability,
    'legacy:$tag' ||
        COALESCE(':' || workflow, '')                        AS workflow,
    tokens_in,
    tokens_out,
    cost_usd,
    latency_ms,
    CASE WHEN success THEN 'ok' ELSE 'error' END             AS status,
    ${LEGACY_ERROR_KIND:-NULL::text}                         AS error_kind,
    NULL::int                                                AS http_status,
    created_at
FROM usage_log
EOF
}

migrate_one() {
    local tag="$1" container="$2" user="$3" db="$4"
    local pid; pid=$(get_project_id "$tag")
    if [[ -z "$pid" ]]; then
        echo "!! no broker project named '$tag' — skipping" >&2
        return 1
    fi

    local before
    before=$(docker exec aibroker-postgres psql -U aibroker -d aibroker -tA \
        -c "SELECT COUNT(*), COALESCE(SUM(cost_usd),0)::numeric(10,4)
            FROM usage_log WHERE project_id=$pid")
    local source_stats
    source_stats=$(docker exec "$container" psql -U "$user" -d "$db" -tA \
        -c "SELECT COUNT(*), COALESCE(SUM(cost_usd),0)::numeric(10,4),
                    MIN(created_at)::date, MAX(created_at)::date FROM usage_log")

    echo "── $tag (project_id=$pid) ─────────────────────"
    echo "  source rows: $source_stats"
    echo "  broker rows before: $before"

    if [[ "$MODE" == "dry-run" ]]; then
        echo "  [dry-run] would DELETE legacy:* rows + COPY new ones"
        return 0
    fi

    # Detect whether source has error_kind column (vera does, stepan does not)
    local has_err
    has_err=$(docker exec "$container" psql -U "$user" -d "$db" -tA \
        -c "SELECT 1 FROM information_schema.columns
            WHERE table_name='usage_log' AND column_name='error_kind' LIMIT 1")
    if [[ -n "$has_err" ]]; then
        LEGACY_ERROR_KIND="error_kind"
    else
        LEGACY_ERROR_KIND="NULL::text"
    fi

    # 1. Wipe prior import for this project (idempotent)
    docker exec aibroker-postgres psql -U aibroker -d aibroker -c \
        "DELETE FROM usage_log
         WHERE project_id=$pid AND workflow LIKE 'legacy:%';" >/dev/null

    # 2. Stream rows via COPY
    local sel; sel=$(build_select "$pid" "$tag")
    local cols="project_id, api_key_id, lease_id, provider, model, capability,
                workflow, tokens_in, tokens_out, cost_usd, latency_ms, status,
                error_kind, http_status, created_at"
    docker exec "$container" psql -U "$user" -d "$db" -c \
        "COPY ($sel) TO STDOUT WITH (FORMAT csv, HEADER false)" | \
    docker exec -i aibroker-postgres psql -U aibroker -d aibroker -c \
        "COPY usage_log ($cols) FROM STDIN WITH (FORMAT csv, HEADER false)"

    local after
    after=$(docker exec aibroker-postgres psql -U aibroker -d aibroker -tA \
        -c "SELECT COUNT(*), COALESCE(SUM(cost_usd),0)::numeric(10,4)
            FROM usage_log WHERE project_id=$pid")
    echo "  broker rows after:  $after"
}

for tag in "${!SOURCES[@]}"; do
    read -r container user db <<< "${SOURCES[$tag]}"
    migrate_one "$tag" "$container" "$user" "$db"
done

echo
echo "Done (mode: $MODE)."
