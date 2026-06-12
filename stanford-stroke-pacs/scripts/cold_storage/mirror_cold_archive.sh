#!/usr/bin/env bash
# Tier 2 — cold-archive replication. DORMANT on the dev host.
#
# Mirrors $SOURCE_DIR (the cold archive root) to $COLD_MIRROR_DEST using
# rsync. SOURCE_DIR defaults to config.toml [storage].cold_archive_root;
# COLD_MIRROR_DEST is env-only so the dev host can ship the script without
# a destination configured.
#
# Usage:
#   mirror_cold_archive.sh           # real run (rsync writes)
#   mirror_cold_archive.sh --dry-run # simulate; rsync prints what would change
#
# Activation (production cutover):
#   1. Provision a destination disk or remote (e.g. /DATA3/cold_mirror,
#      or a borg/restic repo).
#   2. Create /etc/default/pacs-cold-mirror with:
#        COLD_MIRROR_DEST=/path/to/mirror
#      (SOURCE_DIR only if it must differ from [storage].cold_archive_root)
#   3. systemctl enable --now cold-archive-mirror.timer
#
# If COLD_MIRROR_DEST is unset, this script exits 0 with a notice — so
# accidentally enabling the timer on a host without configuration is a
# no-op, not a failure spam.
#
# Env:
#   SOURCE_DIR        (default: config.toml [storage].cold_archive_root)
#   COLD_MIRROR_DEST  (required to actually run; absent => no-op)
#   RSYNC_EXTRA_ARGS  (optional, e.g. "--bwlimit=50000")

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib.sh
source "$SCRIPT_DIR/../_lib.sh"

DRY_RUN=0
for arg in "$@"; do
    case "$arg" in
        --dry-run|-n) DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,29p' "$0"
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 1
            ;;
    esac
done

if [[ -z "${COLD_MIRROR_DEST:-}" ]]; then
    echo "[mirror_cold_archive] COLD_MIRROR_DEST not set; cold mirror is dormant. Exiting 0."
    exit 0
fi

SOURCE_DIR="${SOURCE_DIR:-$(config_get storage cold_archive_root "")}"
: "${SOURCE_DIR:?SOURCE_DIR not set and [storage].cold_archive_root unreadable}"

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "[mirror_cold_archive] SOURCE_DIR does not exist: $SOURCE_DIR" >&2
    exit 1
fi

dry_flag=""
if (( DRY_RUN == 1 )); then
    dry_flag="--dry-run"
    echo "[mirror_cold_archive] DRY RUN — no changes will be written"
fi

mkdir -p "$COLD_MIRROR_DEST"

echo "[$(date -u +%FT%TZ)] mirror $SOURCE_DIR -> $COLD_MIRROR_DEST"

# -a: archive (perms, times, symlinks); --delete: keep mirror in lockstep;
# --partial: resumable; --info=stats2 for end-of-run summary in journald.
# shellcheck disable=SC2086
rsync -a --delete --partial --info=stats2 $dry_flag ${RSYNC_EXTRA_ARGS:-} \
    "$SOURCE_DIR"/ "$COLD_MIRROR_DEST"/

echo "[$(date -u +%FT%TZ)] mirror complete"
