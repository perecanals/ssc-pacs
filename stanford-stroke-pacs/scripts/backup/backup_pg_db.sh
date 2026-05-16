#!/usr/bin/env bash
# Nightly logical backup of one PostgreSQL database via pg_dump -Fc.
#
# Usage: backup_pg_db.sh <db-name>
#
# Reads connection details from BACKUP_ENV_FILE (default:
# /home/perecanals/pacs/stanford-stroke-pacs/.env). Required keys:
#   DB_HOST, DB_PORT, DB_USER, DB_PASSWORD
# Optional override: BACKUP_ROOT (default: /DATA2/pg_backups).
# Optional override: RETENTION_DAYS (default: 60).
#
# Output layout:
#   $BACKUP_ROOT/<db>/<utc-timestamp>.dump      # pg_dump custom format
#   $BACKUP_ROOT/<db>/<utc-timestamp>.dump.sha256
#   $BACKUP_ROOT/<db>/latest.dump  -> symlink to newest dump
#
# Exit codes:
#   0 — backup written and verified
#   1 — usage error
#   2 — env file missing or required key unset
#   3 — pg_dump failed
#   4 — checksum or symlink update failed

set -euo pipefail

TARGET_DB="${1:-}"
if [[ -z "$TARGET_DB" ]]; then
    echo "usage: $0 <db-name>" >&2
    exit 1
fi

BACKUP_ENV_FILE="${BACKUP_ENV_FILE:-/home/perecanals/pacs/stanford-stroke-pacs/.env}"
BACKUP_ROOT="${BACKUP_ROOT:-/DATA2/pg_backups}"
RETENTION_DAYS="${RETENTION_DAYS:-60}"

if [[ ! -r "$BACKUP_ENV_FILE" ]]; then
    echo "env file not readable: $BACKUP_ENV_FILE" >&2
    exit 2
fi

set -a
# shellcheck disable=SC1090
. "$BACKUP_ENV_FILE"
set +a

: "${DB_HOST:?DB_HOST not set in $BACKUP_ENV_FILE}"
: "${DB_PORT:?DB_PORT not set in $BACKUP_ENV_FILE}"
: "${DB_USER:?DB_USER not set in $BACKUP_ENV_FILE}"
: "${DB_PASSWORD:?DB_PASSWORD not set in $BACKUP_ENV_FILE}"

dest_dir="$BACKUP_ROOT/$TARGET_DB"
mkdir -p "$dest_dir"
chmod 0700 "$dest_dir" 2>/dev/null || true

ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$dest_dir/${ts}.dump"
tmp="${out}.partial"

export PGPASSWORD="$DB_PASSWORD"

echo "[$(date -u +%FT%TZ)] backing up $TARGET_DB -> $out"

if ! pg_dump \
        --host="$DB_HOST" \
        --port="$DB_PORT" \
        --username="$DB_USER" \
        --format=custom \
        --compress=6 \
        --no-owner \
        --no-privileges \
        --file="$tmp" \
        "$TARGET_DB"; then
    rm -f "$tmp"
    echo "pg_dump failed for $TARGET_DB" >&2
    exit 3
fi

mv "$tmp" "$out"
chmod 0600 "$out"

if ! sha256sum "$out" > "${out}.sha256"; then
    echo "sha256sum failed for $out" >&2
    exit 4
fi

ln -sfn "$(basename "$out")" "$dest_dir/latest.dump"
ln -sfn "$(basename "$out").sha256" "$dest_dir/latest.dump.sha256"

# Retention: prune dumps older than RETENTION_DAYS, but always keep at least one.
mapfile -t old < <(find "$dest_dir" -maxdepth 1 -name '*.dump' -type f -mtime "+${RETENTION_DAYS}" | sort)
keep_count=$(find "$dest_dir" -maxdepth 1 -name '*.dump' -type f | wc -l)
for f in "${old[@]:-}"; do
    [[ -z "$f" ]] && continue
    if (( keep_count <= 1 )); then
        break
    fi
    rm -f "$f" "${f}.sha256"
    keep_count=$((keep_count - 1))
done

size_h="$(du -h "$out" | cut -f1)"
echo "[$(date -u +%FT%TZ)] OK $TARGET_DB ($size_h)"
