#!/usr/bin/env bash
# Stop the whole SSC-PACS stack on macOS — non-destructive and reversible.
#
# This pauses the running services; it does NOT remove containers, volumes, or
# data. For the destructive path (removing the Orthanc container + volume + DB)
# see scripts/admin/teardown.sh — that is a different operation.
#
# All services are system-domain LaunchDaemons (KeepAlive=true), so the stop
# verb is `launchctl bootout system/<label>`. Two gotchas this script handles:
#   * com.ssc.colima runs a watchdog that restarts the VM within ~30s, so the
#     daemon MUST be booted out BEFORE `colima stop` or the VM comes right back.
#   * Postgres is stopped LAST (a dedicated com.ssc.postgres daemon) so the DB
#     stays available while the app layer shuts down.
#
#     sudo scripts/macos/stop_stack.sh            # stop (services return on reboot)
#     sudo scripts/macos/stop_stack.sh --retire   # + disable so they stay down after reboot
#     scripts/macos/stop_stack.sh --dry-run       # print the sequence, change nothing
#
# Bring it back with scripts/macos/start_stack.sh (or just reboot — every daemon
# is RunAtLoad, unless you used --retire).
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

RETIRE=no
DRY_RUN=no
for arg in "$@"; do
  case "$arg" in
    --retire)  RETIRE=yes ;;
    --dry-run) DRY_RUN=yes ;;
    -h|--help)
      sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (see --help)" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

if [[ "$DRY_RUN" == no && $EUID -ne 0 ]]; then
  echo "Run with sudo (or pass --dry-run to preview)." >&2
  exit 1
fi

# DEPLOY_USER owns the Colima VM + docker context; colima/docker must run as
# them, not root. Auto-derived; override in deploy.env (same key the installer uses).
DEPLOY_USER="${SUDO_USER:-$(id -un)}"
# shellcheck source=/dev/null
[[ -f "$STACK_DIR/deploy.env" ]] && source "$STACK_DIR/deploy.env"

# Canonical daemon order — mirror install_launchd.sh (single source of truth).
DAEMONS=(
  com.ssc.colima
  com.ssc.postgres
  com.ssc.webapp
  com.ssc.pg-backup-stanford-stroke
  com.ssc.pg-backup-orthanc
  com.ssc.orthanc-storage-backup
  com.ssc.pg-backup-freshness
  com.ssc.cold-storage-health
)
# The scheduled backup/health daemons (everything after colima/postgres/webapp).
NIGHTLY=("${DAEMONS[@]:3}")

# bootout tolerates a not-loaded daemon (exit 3 "No such process") so set -e
# never aborts a partial stack.
bootout_quiet() {
  echo "  + launchctl bootout system/$1"
  [[ "$DRY_RUN" == yes ]] && return 0
  launchctl bootout "system/$1" 2>/dev/null || true
}

# Run docker/colima as the VM owner; tolerate "already down".
as_user() {
  echo "  + sudo -u $DEPLOY_USER $*"
  [[ "$DRY_RUN" == yes ]] && return 0
  sudo -u "$DEPLOY_USER" "$@" 2>/dev/null || true
}

echo "==> Stopping scheduled backup/health daemons"
for d in "${NIGHTLY[@]}"; do bootout_quiet "$d"; done

echo "==> Stopping the web app"
bootout_quiet com.ssc.webapp

echo "==> Stopping the Orthanc container"
as_user docker stop ssc-orthanc

echo "==> Stopping Colima (watchdog daemon first, then the VM)"
bootout_quiet com.ssc.colima          # must precede `colima stop` or it restarts
as_user colima stop

echo "==> Stopping PostgreSQL (last, so the DB outlives the app layer)"
bootout_quiet com.ssc.postgres

if [[ "$RETIRE" == yes ]]; then
  echo "==> Retiring: disabling daemons so they do NOT return on reboot"
  for d in "${DAEMONS[@]}"; do
    echo "  + launchctl disable system/$d"
    [[ "$DRY_RUN" == yes ]] || launchctl disable "system/$d" 2>/dev/null || true
  done
fi

echo "==> Status"
if [[ "$DRY_RUN" == yes ]]; then
  echo "  (dry run — nothing changed)"
else
  sudo -u "$DEPLOY_USER" colima status 2>&1 | sed 's/^/  colima: /' || echo "  colima: not running"
  if nc -z localhost 8043 2>/dev/null; then echo "  web app :8043 STILL LISTENING"; else echo "  web app :8043 down"; fi
  if pg_isready -q 2>/dev/null; then echo "  postgres STILL accepting connections"; else echo "  postgres down"; fi
fi
echo "Done. Bring it back with scripts/macos/start_stack.sh (or reboot, unless --retire)."
