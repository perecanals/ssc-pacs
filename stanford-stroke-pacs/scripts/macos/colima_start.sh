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
#   /Volumes/ThunderBay_RAID1   : read-write — cold archives + warmed imaging_data
#                                              (the container bind is still :ro)
#
# Idempotent: exits 0 if the VM is already running. Waits for the ThunderBay RAID
# to be mounted first — the bind-mount sources must exist before `colima start`.
#
# Run at boot via launchd/com.ssc.colima.plist, or manually:
#   scripts/macos/colima_start.sh
# Manage: `colima status` | `colima stop` | `colima restart` | `colima ssh`
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

REPO_MOUNT="/opt/ssc-pacs/ssc-pacs/stanford-stroke-pacs"
RAID_MOUNT="/Volumes/ThunderBay_RAID1"

# Orthanc is light on memory (~0.7 GB used of the limit) but OHIF/DICOMweb fires
# many parallel frame requests when a study loads, which wants vCPUs. 4 vCPU
# keeps the viewer responsive; 4 GB is ample (the guest barely touches it). vCPUs
# are a *cap*, not a hard reservation — the host reclaims them whenever Orthanc is
# idle, so this leaves plenty for the cold-storage warm extractions (zstd) and
# host Postgres. Override per-host with COLIMA_CPU / COLIMA_MEMORY.
CPU="${COLIMA_CPU:-4}"
MEMORY="${COLIMA_MEMORY:-4}"
DISK="${COLIMA_DISK:-100}"

# Already up? Make sure the docker CLI points at it and exit.
if colima status >/dev/null 2>&1; then
    echo "colima already running"
    docker context use colima >/dev/null 2>&1 || true
    exit 0
fi

# Wait for the external RAID (mount source for the cold archives / DICOM tree).
# External volumes can mount a little after boot/login.
for i in $(seq 1 60); do
    [[ -d "$RAID_MOUNT/ssc-pacs-data" ]] && break
    echo "waiting for $RAID_MOUNT to mount ($i/60)…"
    sleep 5
done
if [[ ! -d "$RAID_MOUNT/ssc-pacs-data" ]]; then
    echo "ERROR: $RAID_MOUNT not mounted; aborting colima start" >&2
    exit 1
fi

echo "starting colima (cpu=$CPU memory=${MEMORY}G disk=${DISK}G, virtiofs)…"
colima start \
    --cpu "$CPU" --memory "$MEMORY" --disk "$DISK" \
    --mount-type virtiofs \
    --mount "${REPO_MOUNT}:r" \
    --mount "${RAID_MOUNT}:w"

docker context use colima >/dev/null 2>&1 || true
echo "colima up — docker socket: $HOME/.colima/default/docker.sock"
