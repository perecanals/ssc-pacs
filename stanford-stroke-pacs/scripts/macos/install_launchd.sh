#!/usr/bin/env bash
# Install the SSC-PACS macOS LaunchDaemons (headless boot persistence).
#
# This box has no console/GUI session, so per-user LaunchAgents won't load.
# We install everything as system LaunchDaemons (run as user `pere`) in
# /Library/LaunchDaemons. RUN WITH SUDO:
#
#     sudo scripts/macos/install_launchd.sh
#
# It performs a clean cutover from the manual bring-up:
#   1. stops the ephemeral web app (manual uvicorn) and Postgres (pg_ctl)
#   2. installs + bootstraps the daemons in dependency order
#   3. Orthanc is NOT a daemon — its `restart: unless-stopped` container comes
#      back automatically once Colima's Docker daemon is up.
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "Run with sudo." >&2; exit 1; }

REPO=/opt/ssc-pacs/ssc-pacs/stanford-stroke-pacs
SRC="$REPO/launchd"
DST=/Library/LaunchDaemons
PGDATA=/opt/homebrew/var/postgresql@16

# Order matters: colima (docker) → postgres → webapp → scheduled jobs.
DAEMONS=(
  com.ssc.colima
  com.ssc.postgres
  com.ssc.webapp
  com.ssc.pg-backup-stanford-stroke
  com.ssc.pg-backup-orthanc
  com.ssc.orthanc-storage-backup
  com.ssc.pg-backup-freshness
  com.ssc.reconciliation
  com.ssc.cold-storage-health
)

echo "==> Stopping ephemeral manual instances"
pkill -f 'uvicorn app:app' 2>/dev/null && echo "  stopped manual uvicorn" || echo "  no manual uvicorn"
sudo -u pere /opt/homebrew/opt/postgresql@16/bin/pg_ctl -D "$PGDATA" stop -m fast 2>/dev/null \
  && echo "  stopped manual postgres" || echo "  no manual postgres (or already a daemon)"

echo "==> Installing + bootstrapping daemons"
fails=()
for d in "${DAEMONS[@]}"; do
  cp "$SRC/$d.plist" "$DST/$d.plist"
  chown root:wheel "$DST/$d.plist"
  chmod 644 "$DST/$d.plist"
  launchctl bootout system "$DST/$d.plist" 2>/dev/null || true   # idempotent re-install
  # Don't let one daemon abort the whole install (was the cause of a half-install).
  if launchctl bootstrap system "$DST/$d.plist"; then
    launchctl enable "system/$d" || true
    echo "  bootstrapped $d"
  else
    echo "  !! bootstrap FAILED for $d (continuing)"; fails+=("$d")
  fi
  if [[ "$d" == com.ssc.postgres ]]; then
    ready=no
    for i in $(seq 1 30); do /opt/homebrew/bin/pg_isready -q && { ready=yes; break; }; sleep 1; done
    if [[ "$ready" == yes ]]; then echo "  postgres ready"; else
      echo "  !! postgres did NOT become ready — check ~/Library/Logs/com.ssc.postgres.err"; fails+=(postgres-not-ready)
    fi
  fi
done

if ((${#fails[@]})); then
  echo "==> WARNING: issues with: ${fails[*]}"
fi

echo "==> Status"
for d in "${DAEMONS[@]}"; do
  printf '  %-34s ' "$d"
  launchctl print "system/$d" 2>/dev/null | awk -F'= ' '/state =/{print $2; found=1} END{if(!found)print "not loaded"}'
done
echo "Done. Orthanc rides on Colima (restart: unless-stopped)."
