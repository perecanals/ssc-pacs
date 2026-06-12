#!/usr/bin/env bash
# Start the Colima VM that backs Docker on this headless macOS server.
#
# This box has NO Docker Desktop (it is a GUI app). Colima provides the Linux VM
# — via Apple's Virtualization.framework — that the Docker daemon runs inside.
# The Orthanc container, its named storage volume, and every `docker compose`
# operation talk to that daemon. Socket: ~/.colima/default/docker.sock.
#
# Mounts (Colima only shares $HOME by default, so these are explicit):
#   <repo>/stanford-stroke-pacs : read-only  — orthanc.json / orthanc_users.json
#                                              bind-mounts for the container
#   data mount                  : read-write — common parent of config.toml's
#                                              [storage].dicom_data_root and
#                                              cold_archive_root (override with
#                                              COLIMA_DATA_MOUNT); the container
#                                              bind is still :ro
#
# Idempotent: exits 0 if the VM is already running. Waits for the storage roots
# to be mounted first — the bind-mount sources must exist before `colima start`.
#
# Run at boot via launchd/com.ssc.colima.plist, or manually:
#   scripts/macos/colima_start.sh
# Manage: `colima status` | `colima stop` | `colima restart` | `colima ssh`
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# _lib.sh resolves STACK_DIR from its own location and provides config_get.
# shellcheck source=../_lib.sh
source "$SCRIPT_DIR/../_lib.sh"

REPO_MOUNT="$STACK_DIR"
# The VM must see the storage roots from config.toml. Mount their common
# parent read-write; hard-fail if config.toml is unreadable (an empty value
# here would otherwise become a wrong mount at boot).
DICOM_ROOT="$(config_get storage dicom_data_root "")"
COLD_ROOT="$(config_get storage cold_archive_root "")"
if [[ -z "$DICOM_ROOT" || -z "$COLD_ROOT" ]]; then
    echo "ERROR: could not read [storage] roots from $CONFIG_TOML" >&2
    exit 1
fi
DATA_MOUNT="${COLIMA_DATA_MOUNT:-$(python3 -c 'import os,sys;print(os.path.commonpath(sys.argv[1:]))' "$DICOM_ROOT" "$COLD_ROOT")}"

# OHIF/DICOMweb fires many parallel frame requests when a study loads, which
# wants vCPUs, so 4 keeps the viewer responsive. vCPUs are a *cap*, not a hard
# reservation — the host reclaims them whenever Orthanc is idle, so this leaves
# plenty for the cold-storage warm extractions and host Postgres. 8 GB gives
# Orthanc headroom for indexing/caching under load. Override per-host with
# COLIMA_CPU / COLIMA_MEMORY.
CPU="${COLIMA_CPU:-4}"
MEMORY="${COLIMA_MEMORY:-8}"
DISK="${COLIMA_DISK:-100}"

# Already up? Make sure the docker CLI points at it and exit.
if colima status >/dev/null 2>&1; then
    echo "colima already running"
    docker context use colima >/dev/null 2>&1 || true
    exit 0
fi

# Wait for the external volume holding the storage roots (cold archives /
# DICOM tree). External volumes can mount a little after boot/login.
for i in $(seq 1 60); do
    [[ -d "$DICOM_ROOT" && -d "$COLD_ROOT" ]] && break
    echo "waiting for $DATA_MOUNT to mount ($i/60)…"
    sleep 5
done
if [[ ! -d "$DICOM_ROOT" || ! -d "$COLD_ROOT" ]]; then
    echo "ERROR: $DATA_MOUNT not mounted (missing $DICOM_ROOT or $COLD_ROOT); aborting colima start" >&2
    exit 1
fi

echo "starting colima (cpu=$CPU memory=${MEMORY}G disk=${DISK}G, virtiofs)…"
colima start \
    --cpu "$CPU" --memory "$MEMORY" --disk "$DISK" \
    --mount-type virtiofs \
    --mount "${REPO_MOUNT}:r" \
    --mount "${DATA_MOUNT}:w"

docker context use colima >/dev/null 2>&1 || true
echo "colima up — docker socket: $HOME/.colima/default/docker.sock"
