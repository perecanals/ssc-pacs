#!/usr/bin/env bash
# docker compose wrapper — single source of truth for the Orthanc DICOM mount.
#
# Bare `docker compose up` cannot know which host directory to bind to
# /dicom-data: that depends on [storage].mode in config.toml (the loose tree in
# `legacy` mode, the warm cache in `cold_path_cache` mode). This wrapper reads
# config.toml, exports DICOM_MOUNT_SOURCE for the compose `${DICOM_MOUNT_SOURCE}`
# interpolation, selects the right per-platform override, and execs docker
# compose from the stack dir. Use it instead of bare `docker compose`:
#
#     scripts/orthanc/dc.sh up -d
#     scripts/orthanc/dc.sh config        # render the effective compose
#     scripts/orthanc/dc.sh down
#
# Anything after the script name is passed straight through to docker compose.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# _lib.sh resolves STACK_DIR / CONFIG_TOML from its own location and provides
# config_get <section> <key> <fallback>; reuse it so paths stay portable.
# shellcheck source=../backup/_lib.sh
source "$SCRIPT_DIR/../backup/_lib.sh"

mode="$(config_get storage mode legacy)"
case "$mode" in
  cold_path_cache) DICOM_MOUNT_SOURCE="$(config_get storage hot_cache_dir "")" ;;
  legacy)          DICOM_MOUNT_SOURCE="$(config_get storage legacy_dicom_root "")" ;;
  *)
    echo "dc.sh: unknown [storage].mode '$mode' in $CONFIG_TOML" >&2
    exit 1
    ;;
esac

if [[ -z "$DICOM_MOUNT_SOURCE" ]]; then
  echo "dc.sh: could not resolve a DICOM mount path for mode '$mode' from $CONFIG_TOML" >&2
  exit 1
fi
export DICOM_MOUNT_SOURCE

files=(-f "$STACK_DIR/docker-compose.yml")
# macOS/Colima needs the explicit-ports + host.docker.internal override. The
# base file is Linux (host networking); the override is selected only here, so a
# stray docker-compose.override.yml never silently breaks a Linux host.
if [[ "$(uname -s)" == "Darwin" ]]; then
  files+=(-f "$STACK_DIR/docker-compose.override.macos.yml")
fi

echo "dc.sh: storage.mode=$mode  DICOM_MOUNT_SOURCE=$DICOM_MOUNT_SOURCE" >&2
exec docker compose --project-directory "$STACK_DIR" "${files[@]}" "$@"
