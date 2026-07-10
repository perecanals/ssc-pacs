#!/usr/bin/env bash
# Start the whole SSC-PACS stack on Linux — inverse of stop_stack.sh.
#
# Brings Orthanc up first (so the web app's DICOMweb proxy has a target), then
# the web app, then the scheduled timers. dockerd and the shared host
# PostgreSQL are assumed already running (they are not stopped by stop_stack.sh).
#
#     sudo scripts/linux/start_stack.sh            # start the units
#     sudo scripts/linux/start_stack.sh --enable   # start AND enable (autostart on boot)
#     scripts/linux/start_stack.sh --dry-run       # print the sequence, change nothing
#
# Use --enable to undo a previous `stop_stack.sh --retire`.
set -euo pipefail

ENABLE=no
DRY_RUN=no
for arg in "$@"; do
  case "$arg" in
    --enable)  ENABLE=yes ;;
    --dry-run) DRY_RUN=yes ;;
    -h|--help)
      sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown argument: $arg (see --help)" >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC="$STACK_DIR/deploy/systemd"

if [[ "$DRY_RUN" == no && $EUID -ne 0 ]]; then
  echo "Run with sudo (or pass --dry-run to preview)." >&2
  exit 1
fi

# Enumerate timers from the unit templates (single source of truth). Never start
# cold-archive-mirror — it is dormant by default (Tier 2; enable manually).
TIMERS=()
for t in "$SRC"/*.timer.in; do
  [[ -e "$t" ]] || continue
  name="$(basename "${t%.in}")"
  [[ "$name" == cold-archive-mirror.timer ]] && continue
  TIMERS+=("$name")
done

run() {
  echo "  + $*"
  [[ "$DRY_RUN" == yes ]] && return 0
  "$@"
}

start_verb=(systemctl start)
[[ "$ENABLE" == yes ]] && start_verb=(systemctl enable --now)

echo "==> Starting Orthanc (docker compose up via dc.sh)"
run "$STACK_DIR/scripts/orthanc/dc.sh" up -d

echo "==> Starting the web app"
run "${start_verb[@]}" ssc-web-app.service

echo "==> Starting scheduled timers"
for t in "${TIMERS[@]}"; do run "${start_verb[@]}" "$t"; done

echo "==> Status"
if [[ "$DRY_RUN" == yes ]]; then
  echo "  (dry run — nothing changed)"
else
  systemctl is-active ssc-web-app.service | sed 's/^/  ssc-web-app: /' || true
  "$STACK_DIR/scripts/orthanc/dc.sh" ps 2>/dev/null | sed 's/^/  /' || true
fi
echo "Done."
