#!/usr/bin/env bash
# Provision — or audit — the host PostgreSQL cluster for the SSC-PACS stack.
#
# Usage:
#   provision_postgres.sh --check      # read-only audit of the existing cluster
#   provision_postgres.sh              # dry-run: print decision + planned actions
#   provision_postgres.sh --execute    # apply (run with sudo)
#
# The invariant this script enforces: the cluster's OS user is a dedicated
# SYSTEM account (uid < UID_MIN), never a login user. systemd-logind purges a
# login user's POSIX shared memory when their last session ends (RemoveIPC=yes
# is the default), which makes every NEW Postgres connection FATAL on an
# otherwise idle server. See docs/operations/postgres_provisioning.md.
#
# Decision tree (detect first, adopt rather than create, never clobber):
#   DB_HOST not local          -> out of scope; exit 0
#   server already reachable   -> adopt as-is; nothing to provision (use --check)
#   PGDATA exists, no server   -> binaries match PG_VERSION: install unit + start
#                                 mismatch: refuse (pg_upgrade is a manual step)
#   no PGDATA, binaries found  -> full provision: system user, initdb, unit
#   no binaries                -> refuse: install PostgreSQL >= 16 first
#
# Identity inputs (deploy.env at the stack root; all optional except where noted):
#   PG_OS_USER  cluster OS account (default: postgres; system-class asserted)
#   PG_BIN      dir containing initdb/pg_ctl (default: probed; refuses ambiguity)
#   PGDATA      data directory (required to provision a new cluster)
# The endpoint (DB_HOST/DB_PORT) comes from .env.
#
# Exit codes:
#   0 — ok / out of scope / clean audit
#   1 — usage error
#   2 — refused: unsafe or ambiguous situation that needs a human decision
#   3 — --check found problems (all findings are printed, then one exit)
set -euo pipefail

case "${1:-}" in
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    --check)   MODE=check ;;
    --execute) MODE=execute ;;
    "")        MODE=dryrun ;;
    *) echo "usage: $0 [--check|--execute]" >&2; exit 1 ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../_lib.sh
. "$SCRIPT_DIR/../_lib.sh"

ENV_FILE="$STACK_DIR/.env"
PG_FLOOR=16   # minimum supported major (CI tests 16 and 18; prod runs 18)

# --- resolve identity -------------------------------------------------------
DB_HOST=localhost DB_PORT=5432
if [[ -r "$ENV_FILE" ]]; then
    # Only the endpoint is needed; read the two keys rather than sourcing
    # the whole secret file into this shell.
    v="$(sed -n 's/^DB_HOST=//p' "$ENV_FILE" | tail -1 | tr -d '"'"'")"; [[ -n "$v" ]] && DB_HOST="$v"
    v="$(sed -n 's/^DB_PORT=//p' "$ENV_FILE" | tail -1 | tr -d '"'"'")"; [[ -n "$v" ]] && DB_PORT="$v"
fi

PG_OS_USER="${PG_OS_USER:-$(deploy_env_get PG_OS_USER)}"; PG_OS_USER="${PG_OS_USER:-postgres}"
PG_BIN="${PG_BIN:-$(deploy_env_get PG_BIN)}"
PGDATA="${PGDATA:-$(deploy_env_get PGDATA)}"

UID_MIN="$(awk '$1=="UID_MIN"{print $2}' /etc/login.defs 2>/dev/null | tail -1)"
UID_MIN="${UID_MIN:-1000}"

UNIT_NAME=ssc-postgres.service
UNIT_PATH="/etc/systemd/system/$UNIT_NAME"
TEMPLATE="$STACK_DIR/deploy/systemd/ssc-postgres.service.in"

is_local_host() {
    case "$1" in
        localhost|127.*|::1|"") return 0 ;;
        *) return 1 ;;
    esac
}

server_reachable() {
    if command -v pg_isready >/dev/null 2>&1; then
        pg_isready -h "$DB_HOST" -p "$DB_PORT" -t 3 >/dev/null 2>&1
    else
        (exec 3<>"/dev/tcp/$DB_HOST/$DB_PORT") 2>/dev/null && { exec 3>&- ; true; }
    fi
}

# Probe for exactly one PostgreSQL bin dir when PG_BIN is not pinned.
probe_pg_bin() {
    local found=()
    local d
    for d in /opt/pgsql*/bin /usr/local/pgsql/bin /usr/lib/postgresql/*/bin; do
        [[ -x "$d/initdb" ]] && found+=("$d")
    done
    if [[ ${#found[@]} -eq 0 ]] && command -v initdb >/dev/null 2>&1; then
        found+=("$(dirname "$(command -v initdb)")")
    fi
    if [[ ${#found[@]} -gt 1 ]]; then
        echo "AMBIGUOUS:${found[*]}"
    elif [[ ${#found[@]} -eq 1 ]]; then
        echo "${found[0]}"
    fi
}

pg_major_of_bin() { "$1/pg_ctl" --version | grep -oE '[0-9]+' | head -1; }

# Query the server over the local socket with peer auth, never prompting.
# Ambient PGHOST/PGPORT (e.g. from a user's bashrc) would silently redirect to
# TCP + password auth, so they are stripped; the socket dir is probed because
# source builds default to /tmp while distro builds use /var/run/postgresql.
local_psql() {
    local q="$1" d out
    for d in /tmp /var/run/postgresql; do
        if out="$(env -u PGHOST -u PGPORT -u PGUSER -u PGDATABASE \
                  psql -w -h "$d" -p "$DB_PORT" -d postgres -tAc "$q" 2>/dev/null)"; then
            printf '%s' "$out"
            return 0
        fi
    done
    return 1
}

# --- audit (--check) --------------------------------------------------------
FINDINGS=0
ok()   { echo "OK:    $*"; }
warn() { echo "WARN:  $*"; FINDINGS=$((FINDINGS + 1)); }

run_check() {
    echo "== SSC-PACS PostgreSQL audit ($DB_HOST:$DB_PORT) =="

    if ! is_local_host "$DB_HOST"; then
        ok "DB_HOST '$DB_HOST' is remote — cluster provisioning is out of scope here"
        exit 0
    fi
    if ! server_reachable; then
        warn "no PostgreSQL server answering on $DB_HOST:$DB_PORT"
        exit 3
    fi
    ok "server answers on $DB_HOST:$DB_PORT"

    # Identify the cluster by data directory + postmaster process, not by unit
    # name (the unit may predate this script). Prefer asking the server; fall
    # back to deploy.env PGDATA.
    local data_dir="" pm_pid="" pm_user="" pm_uid=""
    # Peer auth works when the invoking OS user has a matching superuser role;
    # degrades to the deploy.env PGDATA value otherwise.
    data_dir="$(local_psql 'show data_directory' || true)"
    data_dir="${data_dir:-$PGDATA}"
    if [[ -z "$data_dir" ]]; then
        warn "cannot determine PGDATA (no psql access; set PGDATA in deploy.env) — ownership checks skipped"
    else
        ok "data directory: $data_dir"
        if [[ -r "$data_dir/postmaster.pid" ]]; then
            pm_pid="$(head -1 "$data_dir/postmaster.pid")"
        else
            pm_pid="$(pgrep -o -f "[p]ostgres.*-D *$data_dir" 2>/dev/null || true)"
        fi
    fi

    if [[ -n "$pm_pid" ]] && kill -0 "$pm_pid" 2>/dev/null; then
        pm_user="$(ps -o user= -p "$pm_pid" | tr -d ' ')"
        pm_uid="$(id -u "$pm_user" 2>/dev/null || echo '?')"
        if [[ "$pm_uid" != '?' && "$pm_uid" -lt "$UID_MIN" ]]; then
            ok "postmaster runs as '$pm_user' (uid $pm_uid < UID_MIN $UID_MIN — system account)"
        else
            warn "postmaster runs as '$pm_user' (uid $pm_uid >= UID_MIN $UID_MIN — LOGIN account;" \
                 "logind RemoveIPC can kill its shared memory when the user's last session ends)"
            # Only relevant while the cluster user is login-class:
            local ripc
            ripc="$(systemctl show systemd-logind -p RemoveIPC --value 2>/dev/null || true)"
            if [[ "$ripc" == no ]]; then
                if ls /etc/systemd/logind.conf.d/*.conf >/dev/null 2>&1 \
                   && grep -qsi 'RemoveIPC=no' /etc/systemd/logind.conf.d/*.conf; then
                    ok "logind RemoveIPC=no via drop-in (upgrade-safe mitigation)"
                else
                    warn "logind RemoveIPC=no but only in /etc/systemd/logind.conf (dpkg conffile —" \
                         "a systemd upgrade can revert it; use a /etc/systemd/logind.conf.d/ drop-in)"
                fi
            else
                warn "logind RemoveIPC=$ripc — the incident condition is LIVE for this cluster"
            fi
        fi
    else
        warn "could not identify the postmaster process — user-class check skipped"
    fi

    # The unit that manages it (informational; --check never edits units).
    if [[ -n "$pm_pid" ]]; then
        local unit
        unit="$(ps -o unit= -p "$pm_pid" 2>/dev/null | tr -d ' ')"
        [[ -n "$unit" && "$unit" != "-" ]] && ok "managed by unit: $unit" \
            || warn "postmaster does not run under a systemd unit"
    fi

    # PGDATA ownership must match the postmaster user.
    if [[ -n "$data_dir" && -e "$data_dir" && -n "$pm_user" ]]; then
        local owner
        owner="$(stat -c '%U' "$data_dir")"
        [[ "$owner" == "$pm_user" ]] \
            && ok "PGDATA owned by '$owner' (matches postmaster user)" \
            || warn "PGDATA owned by '$owner' but postmaster runs as '$pm_user'"
    fi

    # pg_hba: no 'trust' lines — with the shipped trust replication lines any
    # local user could pg_basebackup the whole cluster, credential-free.
    if [[ -n "$data_dir" && -r "$data_dir/pg_hba.conf" ]]; then
        local trust_lines
        trust_lines="$(grep -cE '^[^#]*\btrust\b' "$data_dir/pg_hba.conf" || true)"
        [[ "$trust_lines" -eq 0 ]] \
            && ok "pg_hba.conf has no 'trust' entries" \
            || warn "pg_hba.conf has $trust_lines 'trust' entr(ies) — passwordless access; use peer/scram-sha-256"
    elif [[ -n "$data_dir" ]]; then
        warn "pg_hba.conf not readable as $(id -un) — re-run with sudo for the auth audit"
    fi

    # Version floor.
    local ver
    ver="$(local_psql 'show server_version' | cut -d. -f1 || true)"
    [[ -z "$ver" && -n "$data_dir" && -r "$data_dir/PG_VERSION" ]] && ver="$(cat "$data_dir/PG_VERSION")"
    if [[ -n "$ver" ]]; then
        [[ "$ver" -ge "$PG_FLOOR" ]] \
            && ok "server major version $ver (floor: $PG_FLOOR)" \
            || warn "server major version $ver is below the supported floor $PG_FLOOR"
    else
        warn "could not determine the server version"
    fi

    echo "== $FINDINGS finding(s) =="
    [[ "$FINDINGS" -eq 0 ]] && exit 0 || exit 3
}

if [[ "$MODE" == check ]]; then run_check; fi

# --- provisioning decision tree (dry-run / execute) --------------------------
doit() {
    # In dry-run, print the command; in execute, print then run it.
    echo "  + $*"
    [[ "$MODE" == execute ]] && "$@"
    return 0
}

refuse() { echo "REFUSED: $*" >&2; exit 2; }

if ! is_local_host "$DB_HOST"; then
    echo "DB_HOST '$DB_HOST' is not local — remote/managed PostgreSQL is out of scope. Nothing to do."
    exit 0
fi

if server_reachable; then
    echo "A PostgreSQL server already answers on $DB_HOST:$DB_PORT — adopting it as-is."
    echo "Nothing to provision. Run '$0 --check' to audit it against the invariants."
    exit 0
fi

if [[ "$MODE" == execute && $EUID -ne 0 ]]; then
    refuse "run with sudo for --execute (dry-run needs no root)"
fi

# Resolve binaries.
if [[ -z "$PG_BIN" ]]; then
    PG_BIN="$(probe_pg_bin)"
    case "$PG_BIN" in
        AMBIGUOUS:*)
            refuse "multiple PostgreSQL installations found (${PG_BIN#AMBIGUOUS:}) — pin PG_BIN in deploy.env" ;;
        "")
            refuse "no PostgreSQL binaries found — install PostgreSQL >= $PG_FLOOR, then re-run" ;;
    esac
fi
[[ -x "$PG_BIN/initdb" && -x "$PG_BIN/pg_ctl" ]] || refuse "PG_BIN=$PG_BIN lacks initdb/pg_ctl"
BIN_MAJOR="$(pg_major_of_bin "$PG_BIN")"
[[ "$BIN_MAJOR" -ge "$PG_FLOOR" ]] || refuse "PostgreSQL $BIN_MAJOR at $PG_BIN is below the supported floor $PG_FLOOR"

[[ -n "$PGDATA" ]] || refuse "PGDATA is not set — add it to deploy.env (e.g. PGDATA=/srv/ssc-pacs/pgdata)"

# Never allow another unit to keep claiming this PGDATA.
other_unit="$(grep -ls "$PGDATA" /etc/systemd/system/*.service 2>/dev/null | grep -v "$UNIT_NAME" || true)"
[[ -n "$other_unit" ]] && refuse "another unit references $PGDATA ($other_unit) — stop it, disable it, and remove the unit file first"

# Stale-socket trap: under sticky /tmp only the socket's OWNER can unlink it.
# A postgres-owned postmaster cannot clear a stale socket left by another user,
# so it would fail to start — surface that now, not at 2am.
for s in "/tmp/.s.PGSQL.$DB_PORT" "/tmp/.s.PGSQL.$DB_PORT.lock"; do
    if [[ -e "$s" ]]; then
        sock_owner="$(stat -c '%U' "$s")"
        [[ "$sock_owner" == "$PG_OS_USER" ]] \
            || refuse "stale $s owned by '$sock_owner' — remove it as that user before starting the cluster"
    fi
done

echo "== Plan (mode: $MODE) =="
echo "  PG_BIN=$PG_BIN (major $BIN_MAJOR)  PGDATA=$PGDATA  PG_OS_USER=$PG_OS_USER"

# System account: adopt if present (but ASSERT its uid class — a login-class
# account named 'postgres' would silently reintroduce the incident), create
# otherwise.
if getent passwd "$PG_OS_USER" >/dev/null; then
    pg_uid="$(id -u "$PG_OS_USER")"
    [[ "$pg_uid" -lt "$UID_MIN" ]] \
        || refuse "OS user '$PG_OS_USER' exists with uid $pg_uid >= UID_MIN $UID_MIN (login class) — that is the incident bug; pick another PG_OS_USER or fix the account"
    echo "  OS user '$PG_OS_USER' exists (uid $pg_uid, system class) — adopting"
else
    nologin="$(command -v nologin || echo /usr/sbin/nologin)"
    doit useradd --system --user-group --no-create-home --home-dir "$PGDATA" --shell "$nologin" "$PG_OS_USER"
fi
PG_OS_GROUP="$(id -gn "$PG_OS_USER" 2>/dev/null || echo "$PG_OS_USER")"

if [[ -e "$PGDATA/PG_VERSION" ]]; then
    # Existing cluster, no server running: adopt. NEVER initdb here — that is
    # data loss. Binaries must match the on-disk major exactly.
    DATA_MAJOR="$(cat "$PGDATA/PG_VERSION")"
    [[ "$DATA_MAJOR" == "$BIN_MAJOR" ]] \
        || refuse "PGDATA is major $DATA_MAJOR but PG_BIN is major $BIN_MAJOR — run pg_upgrade manually (never automated here)"
    owner="$(stat -c '%U' "$PGDATA")"
    [[ "$owner" == "$PG_OS_USER" ]] \
        || refuse "PGDATA owned by '$owner', expected '$PG_OS_USER' — chown it first (clean-stop the old server, verify /tmp/.s.PGSQL.$DB_PORT is gone, then: chown -R $PG_OS_USER:$PG_OS_GROUP $PGDATA)"
    echo "  existing cluster (major $DATA_MAJOR, owner OK) — will install unit + start"
elif [[ -e "$PGDATA" && -n "$(ls -A "$PGDATA" 2>/dev/null)" ]]; then
    refuse "$PGDATA exists, is not empty, and has no PG_VERSION — refusing to touch it"
else
    doit install -d -m 700 -o "$PG_OS_USER" -g "$PG_OS_GROUP" "$PGDATA"
    # -A flags bake sane auth in from the first byte: peer for local sockets,
    # scram for TCP — and, crucially, NO 'trust' replication lines (initdb's
    # default pg_hba would allow credential-free pg_basebackup to any local user).
    doit sudo -u "$PG_OS_USER" "$PG_BIN/initdb" -D "$PGDATA" \
        --auth-local=peer --auth-host=scram-sha-256
fi

# Render + install the unit. Refuse to clobber a unit we did not write.
if [[ -e "$UNIT_PATH" ]] && ! grep -q 'PostgreSQL (SSC PACS)' "$UNIT_PATH"; then
    refuse "$UNIT_PATH exists and was not written by this script — refusing to overwrite"
fi
echo "  + render $TEMPLATE -> $UNIT_PATH"
if [[ "$MODE" == execute ]]; then
    DOCS_ROOT="$(cd "$STACK_DIR/.." && pwd)/docs"
    sed -e "s|__PG_OS_USER__|$PG_OS_USER|g" \
        -e "s|__PG_OS_GROUP__|$PG_OS_GROUP|g" \
        -e "s|__PG_BIN__|$PG_BIN|g" \
        -e "s|__PGDATA__|$PGDATA|g" \
        -e "s|__DOCS_ROOT__|$DOCS_ROOT|g" \
        "$TEMPLATE" > "$UNIT_PATH"
    if grep -q '__[A-Z_]*__' "$UNIT_PATH"; then
        rm -f "$UNIT_PATH"
        refuse "unsubstituted tokens after render — template/token mismatch"
    fi
    chmod 644 "$UNIT_PATH"
fi
doit systemctl daemon-reload
doit systemctl enable --now "$UNIT_NAME"

if [[ "$MODE" == execute ]]; then
    for _ in $(seq 1 15); do
        server_reachable && { echo "OK: PostgreSQL is up on $DB_HOST:$DB_PORT"; exit 0; }
        sleep 2
    done
    echo "server did not come up within 30s — check: journalctl -u $UNIT_NAME" >&2
    exit 2
fi
echo "== Dry run only — re-run with --execute (under sudo) to apply =="
