#!/usr/bin/env bash
# Stop the whole SSC-PACS stack on Linux — non-destructive and reversible.
#
# This pauses the running services; it does NOT remove containers, volumes, or
# data. For the destructive path (removing the Orthanc container + volume + DB)
# see scripts/admin/teardown.sh — that is a different operation.
#
# Sequence: stop the scheduled timers (so no backup/health job fires mid-stop),
# stop the web app, then `dc.sh down` for Orthanc. dockerd and the shared host
# PostgreSQL (ssc-postgres.service) are LEFT RUNNING by design — other things on
# the box may use them. A commented opt-in below stops Postgres too if you are
# shutting down the whole machine.
#
#     sudo scripts/linux/stop_stack.sh            # stop (units still autostart on boot)
#     sudo scripts/linux/stop_stack.sh --retire   # + disable so they stay down after reboot
#     scripts/linux/stop_stack.sh --dry-run       # print the sequence, change nothing
#
# Bring it back with scripts/linux/start_stack.sh.
set -euo pipefail

RETIRE=no
DRY_RUN=no
for arg in "$@"; do
  case "$arg" in
    --retire)  RETIRE=yes ;;
    --dry-run) DRY_RUN=yes ;;
    -h|--help)
      sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

# Enumerate timers from the unit templates (single source of truth — matches
# install_systemd.sh). cold-archive-mirror is dormant by default (Tier 2), so
# only stop it if it is actually active.
TIMERS=()
for t in "$SRC"/*.timer.in; do
  [[ -e "$t" ]] || continue
  name="$(basename "${t%.in}")"
  if [[ "$name" == cold-archive-mirror.timer ]]; then
    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet "$name"; then
      TIMERS+=("$name")
    fi
    continue
  fi
  TIMERS+=("$name")
done

run() {
  echo "  + $*"
  [[ "$DRY_RUN" == yes ]] && return 0
  "$@"
}

echo "==> Stopping scheduled timers"
for t in "${TIMERS[@]}"; do run systemctl stop "$t"; done

echo "==> Stopping the web app"
run systemctl stop ssc-web-app.service

echo "==> Stopping Orthanc (docker compose down via dc.sh)"
run "$STACK_DIR/scripts/orthanc/dc.sh" down

echo "==> Leaving host PostgreSQL and dockerd running (shared services)."
echo "    To stop Postgres too (only when shutting down the whole box):"
echo "      sudo systemctl stop ssc-postgres.service"

if [[ "$RETIRE" == yes ]]; then
  echo "==> Retiring: disabling units so they do NOT autostart on boot"
  run systemctl disable ssc-web-app.service
  for t in "${TIMERS[@]}"; do run systemctl disable "$t"; done
fi

echo "==> Status"
if [[ "$DRY_RUN" == yes ]]; then
  echo "  (dry run — nothing changed)"
else
  systemctl is-active ssc-web-app.service | sed 's/^/  ssc-web-app: /' || true
  "$STACK_DIR/scripts/orthanc/dc.sh" ps 2>/dev/null | sed 's/^/  /' || true
fi
echo "Done. Bring it back with scripts/linux/start_stack.sh."
