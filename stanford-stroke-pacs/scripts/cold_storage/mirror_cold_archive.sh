#!/usr/bin/env bash
# Tier 2 — cold-archive replication. DORMANT on the dev host.
#
# Mirrors every archive volume (config.toml [storage.archive_volumes]) to
# $COLD_MIRROR_DEST/<volume>/ using rsync; an offline volume (no sentinel) is
# skipped and the run exits 1. COLD_MIRROR_DEST defaults to config.toml
# [backup].cold_mirror_dest; an empty destination keeps the mirror dormant.
#
# Usage:
#   mirror_cold_archive.sh           # real run (rsync writes)
#   mirror_cold_archive.sh --dry-run # simulate; rsync prints what would change
#
# Activation (production cutover):
#   1. Provision a destination disk or remote (e.g. /DATA3/cold_mirror,
#      or a borg/restic repo).
#   2. Set config.toml [backup].cold_mirror_dest = "/path/to/mirror"
#      (SOURCE_DIR only for a one-off single-tree mirror into the dest root)
#   3. systemctl enable --now cold-archive-mirror.timer
#
# If COLD_MIRROR_DEST is unset, this script exits 0 with a notice — so
# accidentally enabling the timer on a host without configuration is a
# no-op, not a failure spam.
#
# Env:
#   SOURCE_DIR        (default: every archive volume; set = one tree -> dest root)
#   COLD_MIRROR_DEST  (default: [backup].cold_mirror_dest; empty => no-op)
#   RSYNC_EXTRA_ARGS  (default: [backup].cold_mirror_rsync_args)

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

COLD_MIRROR_DEST="${COLD_MIRROR_DEST:-$(config_get backup cold_mirror_dest "")}"
RSYNC_EXTRA_ARGS="${RSYNC_EXTRA_ARGS:-$(config_get backup cold_mirror_rsync_args "")}"
if [[ -z "$COLD_MIRROR_DEST" ]]; then
    echo "[mirror_cold_archive] COLD_MIRROR_DEST not set; cold mirror is dormant. Exiting 0."
    exit 0
fi

dry_flag=""
if (( DRY_RUN == 1 )); then
    dry_flag="--dry-run"
    echo "[mirror_cold_archive] DRY RUN — no changes will be written"
fi

# One source per archive volume -> <dest>/<volume>/. SOURCE_DIR (one-off
# override) mirrors a single tree straight into <dest>/ instead.
declare -a NAMES=() SOURCES=()
if [[ -n "${SOURCE_DIR:-}" ]]; then
    NAMES=("")
    SOURCES=("$SOURCE_DIR")
else
    while IFS=$'\t' read -r name root; do
        NAMES+=("$name")
        SOURCES+=("$root")
    done < <(archive_volume_roots)
fi
if ((${#SOURCES[@]} == 0)); then
    echo "[mirror_cold_archive] no archive volumes resolved from config.toml" >&2
    exit 1
fi

failed=0
for i in "${!SOURCES[@]}"; do
    name="${NAMES[$i]}" src="${SOURCES[$i]}"
    dest="$COLD_MIRROR_DEST${name:+/$name}"
    # --delete below would wipe the mirror if an unmounted disk left an empty
    # mount point: a volume is mirrored only while its sentinel names it.
    if [[ -n "$name" ]] && ! grep -q "\"name\": \"$name\"" "$src/.ssc-volume" 2>/dev/null; then
        echo "[mirror_cold_archive] SKIP volume $name: $src is offline (sentinel missing or wrong)" >&2
        failed=1
        continue
    fi
    if [[ ! -d "$src" ]]; then
        echo "[mirror_cold_archive] source does not exist: $src" >&2
        failed=1
        continue
    fi
    mkdir -p "$dest"
    echo "[$(date -u +%FT%TZ)] mirror $src -> $dest"
    # -a: archive (perms, times, symlinks); --delete: keep mirror in lockstep;
    # --partial: resumable; --info=stats2 for end-of-run summary in journald.
    # shellcheck disable=SC2086
    rsync -a --delete --partial --info=stats2 $dry_flag ${RSYNC_EXTRA_ARGS:-} \
        "$src"/ "$dest"/ || failed=1
done

# check_backup_freshness.sh ages the destination root itself; per-volume runs
# only touch <dest>/<volume>/, so mark a fully successful pass explicitly.
if (( failed == 0 && DRY_RUN == 0 )); then
    touch "$COLD_MIRROR_DEST"
fi
echo "[$(date -u +%FT%TZ)] mirror complete"
exit "$failed"
