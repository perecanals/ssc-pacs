#!/usr/bin/env bash
# Nightly, zero-downtime backup of the Orthanc storage Docker volume.
#
# The volume holds the only copy of OHIF-authored DICOM SR annotations plus the
# Folder Indexer's SQLite DB (indexer-plugin.db). orthanc_db (the PG index) is
# backed up separately; this protects the payloads it points at.
#
# A throwaway container mounts the volume READ-ONLY and streams a consistent
# gzip tar to stdout (see scripts/backup/orthanc_storage_snapshot.py). Orthanc
# is never paused or written to.
#
# Usage: backup_orthanc_storage.sh
#
# Optional env overrides:
#   BACKUP_ENV_FILE          (default: <stack>/.env resolved from the script location; sourced if present)
#   BACKUP_ROOT              (default: config.toml [backup].backup_root, else /DATA2/pg_backups)
#   RETENTION_DAYS           (default: config.toml [backup].retention_days, else 60)
#   ORTHANC_STORAGE_VOLUME   (default stanford-stroke-pacs_ssc-orthanc-storage)
#   BACKUP_HELPER_IMAGE      (default python:3.12-slim)
#
# Output layout:
#   $BACKUP_ROOT/orthanc_storage/<utc-timestamp>.tar.gz
#   $BACKUP_ROOT/orthanc_storage/<utc-timestamp>.tar.gz.sha256
#   $BACKUP_ROOT/orthanc_storage/latest.tar.gz  -> newest archive
#
# Exit codes:
#   0 — full backup written and verified
#   2 — env file explicitly set but unreadable
#   3 — docker/snapshot failed (no usable archive produced)
#   4 — checksum or symlink update failed
#   5 — archive written but the indexer DB snapshot was omitted/degraded
#       (SR files ARE backed up; the index is rebuildable) — flagged for alerting

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAP_PY="$SCRIPT_DIR/orthanc_storage_snapshot.py"
# _lib.sh defines STACK_DIR (repo-relative) and config_get (reads config.toml),
# so paths/settings are deployable anywhere without editing this script.
# shellcheck source=_lib.sh
. "$SCRIPT_DIR/_lib.sh"

BACKUP_ENV_FILE="${BACKUP_ENV_FILE:-$STACK_DIR/.env}"
BACKUP_ROOT="${BACKUP_ROOT:-$(config_get backup backup_root /DATA2/pg_backups)}"
RETENTION_DAYS="${RETENTION_DAYS:-$(config_get backup retention_days 60)}"
ORTHANC_STORAGE_VOLUME="${ORTHANC_STORAGE_VOLUME:-stanford-stroke-pacs_ssc-orthanc-storage}"
BACKUP_HELPER_IMAGE="${BACKUP_HELPER_IMAGE:-python:3.12-slim}"
TARGET="orthanc_storage"

# Env file is optional here (no DB creds needed). Source it only to stay aligned
# with the pg-backup jobs; tolerate absence, but fail if it was set unreadable.
if [[ -e "$BACKUP_ENV_FILE" ]]; then
    if [[ ! -r "$BACKUP_ENV_FILE" ]]; then
        echo "env file not readable: $BACKUP_ENV_FILE" >&2
        exit 2
    fi
    set -a
    # shellcheck disable=SC1090
    . "$BACKUP_ENV_FILE"
    set +a
fi

if [[ ! -r "$SNAP_PY" ]]; then
    echo "snapshot helper not found: $SNAP_PY" >&2
    exit 3
fi

dest_dir="$BACKUP_ROOT/$TARGET"
mkdir -p "$dest_dir"
chmod 0700 "$dest_dir" 2>/dev/null || true

ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$dest_dir/${ts}.tar.gz"
tmp="${out}.partial"

echo "[$(date -u +%FT%TZ)] backing up volume $ORTHANC_STORAGE_VOLUME -> $out"

# Stream the consistent gzip tar out of the helper container (volume :ro).
# stdout → host file (owned by the invoking user); the container runs as root
# so it can read root-owned volume files. --rm discards the ephemeral DB stage.
rc=0
docker run --rm \
    -v "${ORTHANC_STORAGE_VOLUME}:/vol:ro" \
    -v "${SNAP_PY}:/snap.py:ro" \
    "${BACKUP_HELPER_IMAGE}" \
    python /snap.py > "$tmp" || rc=$?

if (( rc != 0 && rc != 5 )); then
    rm -f "$tmp"
    echo "snapshot failed for $ORTHANC_STORAGE_VOLUME (rc=$rc)" >&2
    exit 3
fi
if (( rc == 5 )); then
    echo "WARNING: indexer DB snapshot omitted/degraded; SR files ARE backed up" >&2
fi

mv "$tmp" "$out"
chmod 0600 "$out"

if ! sha256sum "$out" > "${out}.sha256"; then
    echo "sha256sum failed for $out" >&2
    exit 4
fi

ln -sfn "$(basename "$out")" "$dest_dir/latest.tar.gz"
ln -sfn "$(basename "$out").sha256" "$dest_dir/latest.tar.gz.sha256"

# Retention: prune archives older than RETENTION_DAYS, but always keep ≥1.
mapfile -t old < <(find "$dest_dir" -maxdepth 1 -name '*.tar.gz' -type f -mtime "+${RETENTION_DAYS}" | sort)
keep_count=$(find "$dest_dir" -maxdepth 1 -name '*.tar.gz' -type f | wc -l)
for f in "${old[@]:-}"; do
    [[ -z "$f" ]] && continue
    if (( keep_count <= 1 )); then
        break
    fi
    rm -f "$f" "${f}.sha256"
    keep_count=$((keep_count - 1))
done

size_h="$(du -h "$out" | cut -f1)"
echo "[$(date -u +%FT%TZ)] OK $TARGET ($size_h)"
exit "$rc"
