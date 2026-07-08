#!/usr/bin/env bash
# Render + install the SSC-PACS macOS LaunchDaemons from templates (headless boot
# persistence).
#
# This box has no console/GUI session, so per-user LaunchAgents won't load. We
# install everything as system LaunchDaemons (run as DEPLOY_USER) in
# /Library/LaunchDaemons. The repo ships only templates (launchd/*.plist.in) with
# __TOKENS__ for the per-host bits (user, home, Homebrew prefix, conda env, repo
# path). This script resolves those automatically — override any value in
# `deploy.env` at the stack root — fills the templates, and bootstraps the
# daemons. RUN WITH SUDO:
#
#     sudo scripts/macos/install_launchd.sh           # render + install + bootstrap
#     scripts/macos/install_launchd.sh --dry-run      # render to a temp dir only
#
# It performs a clean cutover from the manual bring-up:
#   1. stops the ephemeral web app (manual uvicorn) and Postgres (pg_ctl)
#   2. installs + bootstraps the daemons in dependency order
#   3. Orthanc is NOT a daemon — its `restart: unless-stopped` container comes
#      back automatically once Colima's Docker daemon is up.
#
# IMPORTANT: if your data is on an EXTERNAL volume, the daemons ALSO need Full Disk
# Access granted once in the GUI (this script cannot do it) or warm/backups fail with
# "Operation not permitted". See documentation/guides/deployment_on_mac.md §6,
# "Full Disk Access". Restart the daemons after granting.
set -euo pipefail

DRY_RUN=no
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=yes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC="$STACK_DIR/launchd"
DST=/Library/LaunchDaemons

if [[ "$DRY_RUN" == no && $EUID -ne 0 ]]; then
  echo "Run with sudo (or pass --dry-run to preview)." >&2
  exit 1
fi

# --- per-host identity (auto-derived; override in deploy.env) ---
DEPLOY_USER="${SUDO_USER:-$(id -un)}"
# shellcheck source=/dev/null
[[ -f "$STACK_DIR/deploy.env" ]] && source "$STACK_DIR/deploy.env"

REPO_ROOT="${REPO_ROOT:-$STACK_DIR}"
if [[ -z "${HOME_DIR:-}" ]]; then
  HOME_DIR="$(dscl . -read "/Users/$DEPLOY_USER" NFSHomeDirectory 2>/dev/null | awk '{print $2}')"
  HOME_DIR="${HOME_DIR:-/Users/$DEPLOY_USER}"
fi
if [[ -z "${BREW_PREFIX:-}" ]]; then
  BREW_PREFIX="$(sudo -u "$DEPLOY_USER" brew --prefix 2>/dev/null || true)"
  BREW_PREFIX="${BREW_PREFIX:-/opt/homebrew}"
fi
CONDA_ENV_BIN="${CONDA_ENV_BIN:-$BREW_PREFIX/Caskroom/miniconda/base/envs/ssc-pacs/bin}"
PGDATA="${PGDATA:-$BREW_PREFIX/var/postgresql@16}"

echo "==> Resolved deployment identity"
printf '  %-14s %s\n' REPO_ROOT "$REPO_ROOT" DEPLOY_USER "$DEPLOY_USER" \
  HOME_DIR "$HOME_DIR" BREW_PREFIX "$BREW_PREFIX" CONDA_ENV_BIN "$CONDA_ENV_BIN" PGDATA "$PGDATA"
[[ -x "$CONDA_ENV_BIN/uvicorn" ]] || echo "  !! $CONDA_ENV_BIN/uvicorn not executable here" >&2

render() {
  sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
      -e "s|__DEPLOY_USER__|$DEPLOY_USER|g" \
      -e "s|__HOME_DIR__|$HOME_DIR|g" \
      -e "s|__BREW_PREFIX__|$BREW_PREFIX|g" \
      -e "s|__CONDA_ENV_BIN__|$CONDA_ENV_BIN|g" \
      "$1"
}

# Order matters: colima (docker) → postgres → webapp → scheduled jobs.
# Note: systemd's dormant cold-archive-mirror timer has NO launchd counterpart
# by design (Tier-2, never enabled); everything else is mirrored 1:1.
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

if [[ "$DRY_RUN" == yes ]]; then
  out="$(mktemp -d)"
  for d in "${DAEMONS[@]}"; do render "$SRC/$d.plist.in" > "$out/$d.plist"; done
  echo "==> Rendered ${#DAEMONS[@]} plists into $out"
  echo "--- com.ssc.webapp.plist ---"; cat "$out/com.ssc.webapp.plist"
  # Match only the tokens render() substitutes — '__[A-Z_]*__' also hits the
  # templates' "Do not edit __TOKENS__" header comment (false positive).
  tokens='__(REPO_ROOT|DEPLOY_USER|HOME_DIR|BREW_PREFIX|CONDA_ENV_BIN)__'
  if grep -rlE "$tokens" "$out" >/dev/null 2>&1; then
    echo "  !! unsubstituted tokens remain:" >&2; grep -rnE "$tokens" "$out" >&2
    exit 1
  fi
  # Validate plist syntax if plutil is available.
  if command -v plutil >/dev/null 2>&1; then
    for p in "$out"/*.plist; do plutil -lint "$p" >/dev/null; done
    echo "  plutil -lint: all plists OK"
  fi
  echo "==> Dry run OK (no system changes). Remove --dry-run to install."
  exit 0
fi

echo "==> Stopping ephemeral manual instances"
pkill -f 'uvicorn app:app' 2>/dev/null && echo "  stopped manual uvicorn" || echo "  no manual uvicorn"
sudo -u "$DEPLOY_USER" "$BREW_PREFIX/opt/postgresql@16/bin/pg_ctl" -D "$PGDATA" stop -m fast 2>/dev/null \
  && echo "  stopped manual postgres" || echo "  no manual postgres (or already a daemon)"

echo "==> Installing + bootstrapping daemons"
fails=()
for d in "${DAEMONS[@]}"; do
  render "$SRC/$d.plist.in" > "$DST/$d.plist"
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
    for _ in $(seq 1 30); do "$BREW_PREFIX/bin/pg_isready" -q && { ready=yes; break; }; sleep 1; done
    if [[ "$ready" == yes ]]; then echo "  postgres ready"; else
      echo "  !! postgres did NOT become ready — check $HOME_DIR/Library/Logs/com.ssc.postgres.err"; fails+=(postgres-not-ready)
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
