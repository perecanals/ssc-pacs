#!/usr/bin/env bash
# Backup freshness monitor.
#
# Exits nonzero if any monitored target is missing or stale (older than
# MAX_AGE_HOURS, default 36). Designed to be wired into a systemd unit
# so OnFailure= can fire an alert hook.
#
# By default checks PostgreSQL backups for orthanc_db and stanford-stroke,
# plus the Orthanc storage-volume backup (orthanc_storage), under BACKUP_ROOT
# (default /DATA2/pg_backups).
#
# Pass --include-cold-archive to additionally check the cold-archive
# mirror destination. On the dev host this flag is NOT passed (Tier 2
# is dormant); production cutover enables it.
#
# Env overrides:
#   BACKUP_ROOT       (default /DATA2/pg_backups)
#   MAX_AGE_HOURS     (default 36)
#   COLD_MIRROR_DEST  (required if --include-cold-archive)
#   COLD_MIRROR_MAX_AGE_HOURS (default 36)
#
# Exit codes:
#   0 — all checked targets are fresh
#   1 — usage error
#   2 — at least one target is stale or missing

set -euo pipefail

INCLUDE_COLD=0
for arg in "$@"; do
    case "$arg" in
        --include-cold-archive) INCLUDE_COLD=1 ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

BACKUP_ROOT="${BACKUP_ROOT:-/DATA2/pg_backups}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"
DBS=("orthanc_db" "stanford-stroke")

now_epoch=$(date +%s)
max_age_sec=$(( MAX_AGE_HOURS * 3600 ))
fail=0

check_path_age() {
    local label="$1" path="$2" max_sec="$3"
    if [[ ! -e "$path" ]]; then
        echo "STALE: $label missing ($path)"
        fail=1
        return
    fi
    local mtime age
    mtime=$(stat -c %Y "$path")
    age=$(( now_epoch - mtime ))
    if (( age > max_sec )); then
        local age_h=$(( age / 3600 ))
        echo "STALE: $label is ${age_h}h old (max $((max_sec/3600))h) — $path"
        fail=1
    else
        local age_h=$(( age / 3600 ))
        echo "OK:    $label ${age_h}h old — $path"
    fi
}

for db in "${DBS[@]}"; do
    check_path_age "pg_backup[$db]" "$BACKUP_ROOT/$db/latest.dump" "$max_age_sec"
done

# Orthanc storage volume backup (OHIF SR annotations + indexer DB). Runs nightly
# on dev like the pg dumps, so it's checked unconditionally.
check_path_age "orthanc_storage" "$BACKUP_ROOT/orthanc_storage/latest.tar.gz" "$max_age_sec"

if (( INCLUDE_COLD == 1 )); then
    : "${COLD_MIRROR_DEST:?--include-cold-archive set but COLD_MIRROR_DEST not exported}"
    cold_max=$(( ${COLD_MIRROR_MAX_AGE_HOURS:-36} * 3600 ))
    check_path_age "cold_mirror" "$COLD_MIRROR_DEST" "$cold_max"
fi

if (( fail != 0 )); then
    exit 2
fi
exit 0
