#!/usr/bin/env bash
# Start the whole SSC-PACS stack on macOS — inverse of stop_stack.sh.
#
# Bootstraps every LaunchDaemon in dependency order (colima first — its watchdog
# starts the Docker VM; then postgres, the web app, and the nightly jobs). The
# Orthanc container is NOT a daemon: its `restart: unless-stopped` policy brings
# it back on its own once Colima's Docker daemon is up.
#
#     sudo scripts/macos/start_stack.sh            # enable + bootstrap all daemons
#     scripts/macos/start_stack.sh --dry-run       # print the sequence, change nothing
#
# A plain reboot also revives the stack — every daemon is RunAtLoad. Use this
# only after a manual stop_stack.sh (or after stop_stack.sh --retire, which this
# re-enables). Requires the plists to be installed already (install_launchd.sh).
set -euo pipefail

export PATH="/opt/homebrew/bin:$PATH"

DRY_RUN=no
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=yes ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (see --help)" >&2; exit 1 ;;
  esac
done

if [[ "$DRY_RUN" == no && $EUID -ne 0 ]]; then
  echo "Run with sudo (or pass --dry-run to preview)." >&2
  exit 1
fi

DST=/Library/LaunchDaemons

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

echo "==> Enabling + bootstrapping daemons (dependency order)"
for d in "${DAEMONS[@]}"; do
  plist="$DST/$d.plist"
  if [[ ! -f "$plist" ]]; then
    echo "  !! $plist not installed — run scripts/macos/install_launchd.sh first; skipping $d"
    continue
  fi
  echo "  + launchctl enable system/$d && bootstrap system $plist"
  [[ "$DRY_RUN" == yes ]] && continue
  launchctl enable "system/$d" 2>/dev/null || true
  launchctl bootout system "$plist" 2>/dev/null || true    # idempotent re-bootstrap
  if launchctl bootstrap system "$plist"; then
    echo "    bootstrapped $d"
  else
    echo "  !! bootstrap FAILED for $d (continuing)"
  fi
done

echo "==> Status"
if [[ "$DRY_RUN" == yes ]]; then
  echo "  (dry run — nothing changed)"
else
  for d in "${DAEMONS[@]}"; do
    printf '  %-34s ' "$d"
    launchctl print "system/$d" 2>/dev/null | awk -F'= ' '/state =/{print $2; f=1} END{if(!f)print "not loaded"}'
  done
fi
echo "Done. Orthanc rides on Colima (restart: unless-stopped)."
