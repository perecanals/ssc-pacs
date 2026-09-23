#!/usr/bin/env bash
# Backup freshness monitor.
#
# Exits nonzero if any monitored target is missing or stale (older than
# MAX_AGE_HOURS, default 36). Designed to be wired into a systemd unit
# so OnFailure= can fire an alert hook.
#
# By default checks PostgreSQL backups for orthanc_db and stanford-stroke,
# plus the Orthanc storage-volume backup (orthanc_storage), under BACKUP_ROOT
# (config.toml [backup].backup_root).
#
# A configured [backup].cold_mirror_dest is monitored automatically.
# --include-cold-archive additionally makes a missing destination an error.
#
# Env overrides:
#   BACKUP_ROOT       (default: config.toml [backup].backup_root — required, no built-in fallback)
#   MAX_AGE_HOURS     (default: config.toml [backup].max_age_hours, else 36)
#   BACKUP_ENV_FILE  (default: stack .env; database names only)
#   COLD_MIRROR_DEST  (default: config.toml [backup].cold_mirror_dest)
#   COLD_MIRROR_MAX_AGE_HOURS (default MAX_AGE_HOURS)
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

# _lib.sh defines config_get (reads config.toml) so defaults stay in one place.
# shellcheck source=../_lib.sh
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../_lib.sh"

# backup_root is installation-specific: no hardcoded fallback, configure it or
# pass BACKUP_ROOT=... explicitly for a one-off run.
BACKUP_ROOT="${BACKUP_ROOT:-$(config_get backup backup_root "")}"
if [[ -z "$BACKUP_ROOT" ]]; then
    echo "ERROR: [backup].backup_root is not configured in config.toml" \
         "(copy config.example.toml and set it, or pass BACKUP_ROOT=... for a one-off run)" >&2
    exit 2
fi
MAX_AGE_HOURS="${MAX_AGE_HOURS:-$(config_get backup max_age_hours 36)}"
COLD_MIRROR_DEST="${COLD_MIRROR_DEST:-$(config_get backup cold_mirror_dest "")}"
[[ -n "$COLD_MIRROR_DEST" ]] && INCLUDE_COLD=1
# Database identity is shared with the producers and remote consumer.
BACKUP_ENV_FILE="${BACKUP_ENV_FILE:-$STACK_DIR/.env}"
if [[ ! -r "$BACKUP_ENV_FILE" ]]; then
    echo "env file not readable: $BACKUP_ENV_FILE" >&2
    exit 2
fi
set -a
# shellcheck disable=SC1090
. "$BACKUP_ENV_FILE"
set +a
DBS=("${PG_ORTHANC_DB:?PG_ORTHANC_DB not set}" "${DB_NAME:?DB_NAME not set}")

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
    # GNU stat (Linux) vs BSD stat (macOS)
    mtime=$(stat -c %Y "$path" 2>/dev/null || stat -f %m "$path")
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
    cold_max=$(( ${COLD_MIRROR_MAX_AGE_HOURS:-$MAX_AGE_HOURS} * 3600 ))
    check_path_age "cold_mirror" "$COLD_MIRROR_DEST" "$cold_max"
fi

if (( fail != 0 )); then
    exit 2
fi
exit 0
