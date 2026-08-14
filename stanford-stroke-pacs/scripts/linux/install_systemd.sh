#!/usr/bin/env bash
# Render + install the SSC-PACS systemd units from templates (portable).
#
# The repo ships only templates (deploy/systemd/*.in) with __TOKENS__ for the per-host
# bits (user, repo path, conda python). This script resolves those automatically
# — override any value in `deploy.env` at the stack root — fills the templates,
# and installs the concrete units into /etc/systemd/system. Run with sudo:
#
#     sudo scripts/linux/install_systemd.sh            # render + install + enable
#     scripts/linux/install_systemd.sh --dry-run       # render to a temp dir only
#
# See docs/reference/configuration_sources.md and
# docs/guides/installation_and_deployment.md.
set -euo pipefail

DRY_RUN=no
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=yes

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SRC="$STACK_DIR/deploy/systemd"
DST=/etc/systemd/system

# _lib.sh provides config_get (config.toml is the authoritative home for the
# web-app port; deploy.env's WEBAPP_PORT overrides it per host).
# shellcheck source=../_lib.sh
. "$SCRIPT_DIR/../_lib.sh"

if [[ "$DRY_RUN" == no && $EUID -ne 0 ]]; then
  echo "Run with sudo (or pass --dry-run to preview)." >&2
  exit 1
fi

# --- per-host identity (auto-derived; override in deploy.env) ---
DEPLOY_USER="${SUDO_USER:-$(id -un)}"
# shellcheck source=/dev/null
[[ -f "$STACK_DIR/deploy.env" ]] && source "$STACK_DIR/deploy.env"

REPO_ROOT="${REPO_ROOT:-$STACK_DIR}"
DOCS_ROOT="${DOCS_ROOT:-$(cd "$STACK_DIR/.." && pwd)/docs}"
DEPLOY_GROUP="${DEPLOY_GROUP:-$(id -gn "$DEPLOY_USER" 2>/dev/null || echo "$DEPLOY_USER")}"

if [[ -z "${PYTHON_BIN:-}" ]]; then
  if command -v getent >/dev/null 2>&1; then
    home="$(getent passwd "$DEPLOY_USER" | cut -d: -f6)"
  else
    home="$(eval echo "~$DEPLOY_USER")"   # fallback (e.g. previewing on macOS)
  fi
  for cand in \
    "$home/miniconda3/envs/ssc-pacs/bin/python" \
    "$home/anaconda3/envs/ssc-pacs/bin/python" \
    "$home/miniforge3/envs/ssc-pacs/bin/python" \
    "$home/miniconda3/envs/pacs/bin/python" \
    "$home/anaconda3/envs/pacs/bin/python" \
    "$home/miniforge3/envs/pacs/bin/python"; do
    [[ -x "$cand" ]] && { PYTHON_BIN="$cand"; break; }
  done
  PYTHON_BIN="${PYTHON_BIN:-$(command -v python3 || true)}"
fi
UVICORN_BIN="${UVICORN_BIN:-$(dirname "$PYTHON_BIN")/uvicorn}"
WEBAPP_PORT="${WEBAPP_PORT:-$(config_get web-app port 8043)}"

# --- data-mount ordering -------------------------------------------------
# The data filesystems mount late (fstab `nofail`, LUKS unlocked from a
# keyfile), i.e. after local-fs.target — so anything that starts early can
# capture the empty stub directory underneath the mountpoint. Docker is the
# worst case because it resolves a bind mount once, at container start: a
# too-early ssc-orthanc keeps an empty /dicom-data for its whole lifetime and
# serves HTTP 500 for every frame. RequiresMountsFor= orders the units after
# the right .mount units; see deploy/systemd/docker-ssc-data-mounts.conf.in.
#
# The mountpoints are derived from config.toml (the SSOT for the data roots),
# never hardcoded. Override in deploy.env when the disks are not mounted at
# install time — the paths cannot be resolved then:
#   DATA_MOUNTS="/media/pacs-ssd-1"
#   BACKUP_MOUNTS="/DATA2"
mount_point_of() {
  local p="$1" m=""
  [[ -n "$p" ]] || return 0
  # Walk up to the nearest existing ancestor so a fresh install (tree not
  # created yet) still resolves to the filesystem that will hold it.
  while [[ -n "$p" && "$p" != "/" && ! -e "$p" ]]; do p="$(dirname "$p")"; done
  # `stat -c` is GNU coreutils; on a macOS --dry-run preview this yields
  # nothing and the directive is simply omitted.
  m="$(stat -c %m "$p" 2>/dev/null || true)"
  # "/" never needs a dependency — it is mounted before any unit runs.
  [[ -n "$m" && "$m" != "/" ]] && printf '%s' "$m"
}

# Emit a single deduplicated `RequiresMountsFor=` directive, or nothing at all
# when every path already lives on the root filesystem.
mounts_line() {
  local p m
  local -a found=()
  for p in "$@"; do
    m="$(mount_point_of "$p")"
    [[ -n "$m" ]] && found+=("$m")
  done
  if ((${#found[@]} == 0)); then return 0; fi
  printf 'RequiresMountsFor=%s' "$(printf '%s\n' "${found[@]}" | sort -u | paste -sd' ' -)"
}

if [[ -n "${DATA_MOUNTS:-}" ]]; then
  DATA_MOUNTS_LINE="RequiresMountsFor=$DATA_MOUNTS"
else
  DATA_MOUNTS_LINE="$(mounts_line \
    "$(config_get storage dicom_data_root '')" \
    "$(config_get storage cold_archive_root '')")"
fi
if [[ -n "${BACKUP_MOUNTS:-}" ]]; then
  BACKUP_MOUNTS_LINE="RequiresMountsFor=$BACKUP_MOUNTS"
else
  BACKUP_MOUNTS_LINE="$(mounts_line "$(config_get backup backup_root '')")"
fi

echo "==> Resolved deployment identity"
printf '  %-13s %s\n' REPO_ROOT "$REPO_ROOT" DEPLOY_USER "$DEPLOY_USER" \
  DEPLOY_GROUP "$DEPLOY_GROUP" PYTHON_BIN "$PYTHON_BIN" UVICORN_BIN "$UVICORN_BIN" \
  WEBAPP_PORT "$WEBAPP_PORT" \
  DATA_MOUNTS "${DATA_MOUNTS_LINE:-(none — data roots are on /)}" \
  BACKUP_MOUNTS "${BACKUP_MOUNTS_LINE:-(none — backup root is on /)}"

# Warn (don't fail) on missing binaries so --dry-run still works off-host.
[[ -x "$UVICORN_BIN" ]] || echo "  !! UVICORN_BIN not executable here: $UVICORN_BIN" >&2
[[ -d "$REPO_ROOT/web-app" ]] || echo "  !! $REPO_ROOT/web-app not found" >&2

render() {
  sed -e "s|__REPO_ROOT__|$REPO_ROOT|g" \
      -e "s|__DOCS_ROOT__|$DOCS_ROOT|g" \
      -e "s|__DEPLOY_USER__|$DEPLOY_USER|g" \
      -e "s|__DEPLOY_GROUP__|$DEPLOY_GROUP|g" \
      -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
      -e "s|__UVICORN_BIN__|$UVICORN_BIN|g" \
      -e "s|__WEBAPP_PORT__|$WEBAPP_PORT|g" \
      -e "s|__DATA_MOUNTS_LINE__|$DATA_MOUNTS_LINE|g" \
      -e "s|__BACKUP_MOUNTS_LINE__|$BACKUP_MOUNTS_LINE|g" \
      "$1"
}

# The Docker drop-in is not a unit: it installs into a .d directory rather than
# $DST, so the flat loops below skip it and handle it separately.
DOCKER_DROPIN_SRC="$SRC/docker-ssc-data-mounts.conf.in"
DOCKER_DROPIN_DIR="$DST/docker.service.d"
DOCKER_DROPIN_DST="$DOCKER_DROPIN_DIR/10-ssc-data-mounts.conf"

# ssc-postgres.service.in is provisioned separately: its tokens (__PG_BIN__,
# __PGDATA__, __PG_OS_USER__…) are cluster identity, not deploy identity, and
# are resolved by scripts/linux/provision_postgres.sh against the actual
# cluster. Skipping it here also keeps the token guard below honest.
skip_template() {
  local b; b="$(basename "$1")"
  [[ "$b" == ssc-postgres.service.in || "$b" == docker-ssc-data-mounts.conf.in ]]
}

if [[ "$DRY_RUN" == yes ]]; then
  out="$(mktemp -d)"
  for f in "$SRC"/*.in; do
    skip_template "$f" && continue
    render "$f" > "$out/$(basename "${f%.in}")"
  done
  render "$DOCKER_DROPIN_SRC" > "$out/10-ssc-data-mounts.conf"
  echo "==> Rendered $(ls -1 "$out" | wc -l | tr -d ' ') files into $out"
  echo "--- ssc-web-app.service ---"; cat "$out/ssc-web-app.service"
  echo "--- docker.service.d/10-ssc-data-mounts.conf ---"; cat "$out/10-ssc-data-mounts.conf"
  # Surface any token that survived substitution.
  if grep -rl '__[A-Z_]*__' "$out" >/dev/null 2>&1; then
    echo "  !! unsubstituted tokens remain:" >&2; grep -rn '__[A-Z_]*__' "$out" >&2
    exit 1
  fi
  echo "==> Dry run OK (no system changes). Remove --dry-run to install."
  exit 0
fi

echo "==> Installing units into $DST"
for f in "$SRC"/*.in; do
  skip_template "$f" && { echo "  skipping $(basename "$f") (installed by provision_postgres.sh)"; continue; }
  name="$(basename "${f%.in}")"
  render "$f" > "$DST/$name"
  chmod 644 "$DST/$name"
done

# Docker's bind mounts must resolve after the data filesystems are mounted, or
# ssc-orthanc serves 500s for every frame until it is restarted by hand.
if [[ -n "$DATA_MOUNTS_LINE" ]]; then
  install -d -m 755 "$DOCKER_DROPIN_DIR"
  render "$DOCKER_DROPIN_SRC" > "$DOCKER_DROPIN_DST"
  chmod 644 "$DOCKER_DROPIN_DST"
  echo "  installed $DOCKER_DROPIN_DST ($DATA_MOUNTS_LINE)"
else
  # Data roots live on / — there is no late mount to wait for. Drop any
  # stale copy so a re-run after moving the data off a separate disk is clean.
  rm -f "$DOCKER_DROPIN_DST"
  echo "  skipping the Docker mount drop-in (data roots are on /)"
fi

if grep -rl '__[A-Z_]*__' "$DST"/ssc-*.service "$DST"/*-*.service "$DST"/*.timer \
     "$DOCKER_DROPIN_DST" >/dev/null 2>&1; then
  echo "  !! unsubstituted tokens remain in installed units — aborting" >&2
  exit 1
fi

systemctl daemon-reload

echo "==> Enabling units"
systemctl enable --now ssc-web-app.service
for t in "$SRC"/*.timer.in; do
  timer="$(basename "${t%.in}")"
  # cold-archive-mirror is dormant by default (Tier 2; needs /etc/default/pacs-cold-mirror).
  if [[ "$timer" == cold-archive-mirror.timer ]]; then
    echo "  skipping $timer (dormant — enable manually in production)"
    continue
  fi
  systemctl enable --now "$timer"
  echo "  enabled $timer"
done

echo "==> Status"
systemctl --no-pager --plain status ssc-web-app.service | head -n 3 || true
systemctl list-timers --all --no-pager | grep -E 'pacs|cold|backup|freshness' || true
echo "Done."
