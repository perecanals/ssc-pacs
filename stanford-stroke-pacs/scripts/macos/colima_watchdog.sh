#!/usr/bin/env bash
# Long-lived watchdog that keeps the Colima Docker VM alive on this headless
# macOS server. Runs as the com.ssc.colima LaunchDaemon (KeepAlive=true).
#
# Why a watchdog instead of KeepAlive on colima_start.sh directly?
#   `colima start` is a ONE-SHOT: it returns 0 once the VM is up and then exits.
#   Under launchd KeepAlive=true that successful exit is treated as "process
#   finished" and relaunched immediately -> a busy loop of `colima start`.
#   So launchd supervises THIS script (which never exits), and this script
#   supervises the VM: every CHECK_INTERVAL seconds it verifies the VM is up and
#   calls colima_start.sh (idempotent) to bring it back if it crashed/stopped.
#
# Boot behaviour is preserved: the first loop iteration finds the VM down and
# runs colima_start.sh, which still waits for the ThunderBay RAID to mount
# before `colima start`.
#
# KeepAlive=true on the plist then only matters if THIS watchdog itself dies
# (e.g. killed) — launchd revives it after ThrottleInterval.
set -uo pipefail   # deliberately NOT -e: a failed health check must never kill the loop

export PATH="/opt/homebrew/bin:$PATH"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
START_SH="$HERE/colima_start.sh"
CHECK_INTERVAL="${COLIMA_CHECK_INTERVAL:-30}"

log() { echo "$(date '+%Y-%m-%dT%H:%M:%S') colima-watchdog: $*"; }

# Clean exit on launchd stop (bootout/kickstart send SIGTERM).
running=1
trap 'running=0; log "received stop signal, exiting"' TERM INT

log "starting (check interval=${CHECK_INTERVAL}s)"
while [[ "$running" -eq 1 ]]; do
    # `colima status` is a fast local state check (no daemon round-trip that can
    # hang), so it is safe to poll. If the VM is up we do nothing further.
    if colima status >/dev/null 2>&1; then
        docker context use colima >/dev/null 2>&1 || true
    else
        log "VM not running — invoking colima_start.sh"
        if "$START_SH"; then
            log "colima_start.sh succeeded; VM back up"
        else
            log "colima_start.sh failed (exit $?); will retry in ${CHECK_INTERVAL}s"
        fi
    fi
    # Interruptible sleep so a stop signal is honoured promptly.
    sleep "$CHECK_INTERVAL" &
    wait $! 2>/dev/null || true
done
