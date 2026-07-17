# PostgreSQL provisioning (Linux)

**Purpose:** how the host PostgreSQL cluster is provisioned, audited, and kept
safe on Linux — `scripts/linux/provision_postgres.sh`, the `ssc-postgres.service`
unit, the supported version range, and the migration procedure for a cluster
that runs as the wrong OS user. For the fresh-install sequence this slots into,
see [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md);
for macOS (Homebrew Postgres, no OS `postgres` user) see
[`../guides/deployment_on_mac.md`](../guides/deployment_on_mac.md).

---

## 1. The invariant: three identities that must never collapse

| # | Identity | Rule |
|---|----------|------|
| 1 | Operator / deploy user | Whoever runs the installers (`SUDO_USER`); owns the repo checkout and runs the web app |
| 2 | **Cluster OS user** | **Always a dedicated system account (uid < `UID_MIN`, conventionally `postgres`)** |
| 3 | DB role | `.env` `DB_USER` — an arbitrary per-deployment value; independent of #2 |

**Why #2 is load-bearing:** systemd-logind's default `RemoveIPC=yes` deletes a
*login* user's POSIX shared memory the moment their last session ends. System
users (uid < `UID_MIN`, per `logind.conf(5)`) are exempt. A cluster running as a
login account works flawlessly — until the day no session for that user is open,
at which point logind purges the postmaster's `/dev/shm` segments out from under
it: existing connections survive, but **every new connection dies with
`FATAL: could not open shared memory segment`**, and anything that builds a
connection pool (the web app) crash-loops. This exact incident took the stack
down on this deployment in July 2026; the trigger was as mundane as a long-lived
tmux session finally ending.

The name `postgres` is convention, not the requirement — `provision_postgres.sh`
defaults to it (override with `PG_OS_USER` in `deploy.env`) but **asserts the uid
class regardless of name**. A login-class account that happens to be called
`postgres` is refused: it would silently reintroduce the bug.

The OS user and the DB role are fully independent: keeping the bootstrap DB role
named after the original operator while the OS process runs as `postgres` is
normal and correct.

---

## 2. Supported PostgreSQL versions — a floor, not a pin

- **Minimum supported major: 16.** CI runs the backend suite against both
  PostgreSQL 16 and 18 (`.github/workflows/ci.yml`), so the floor and the
  production major are both continuously proven.
- `provision_postgres.sh` refuses to provision with binaries older than the
  floor, adopts any server ≥ 16 without questions, and **never installs or
  upgrades PostgreSQL itself** — installing the server (distro package, PGDG, or
  source build) is a host-preparation step that happens before the script runs.
- Binaries vs. an existing data directory is a different, harder rule: the
  majors must **match exactly**. On mismatch the script refuses and points at
  `pg_upgrade`; it never auto-upgrades a cluster.

---

## 3. `provision_postgres.sh`

```bash
scripts/linux/provision_postgres.sh --check      # read-only audit of the existing cluster
scripts/linux/provision_postgres.sh              # dry-run: print decision + planned actions
sudo scripts/linux/provision_postgres.sh --execute   # apply
```

Decision tree — detect first, adopt rather than create, refuse loudly, never
clobber:

| Situation | Action |
|---|---|
| `.env` `DB_HOST` is not local | Out of scope (remote/managed PG) — exit 0 |
| A server already answers on `DB_HOST:DB_PORT` | **Adopt.** Nothing to provision; `--check` audits it |
| `PGDATA` exists, no server running, majors match | Adopt: render + install `ssc-postgres.service`, start |
| `PGDATA` exists, majors mismatch | **Refuse** — `pg_upgrade` is a deliberate manual step |
| No `PGDATA`, binaries found | Provision: system user, `initdb`, unit, start |
| No binaries | **Refuse** — install PostgreSQL ≥ 16 first |

Safety properties:

- **Never runs `initdb` over an existing data directory** (that is data loss);
  a non-empty `PGDATA` without `PG_VERSION` is also refused.
- **Never overwrites a systemd unit it did not write**, and refuses to act while
  another unit file references the same `PGDATA` — one cluster, one unit.
- Creates the OS account only if missing, with `useradd --system`; an existing
  account is adopted but its uid class is asserted either way.
- Fresh clusters get `initdb --auth-local=peer --auth-host=scram-sha-256`, so
  `pg_hba.conf` is born without any `trust` entries (initdb's stock default
  includes passwordless `trust` replication lines — that is how any local user
  could `pg_basebackup` an entire cluster credential-free).
- Detects a **stale socket** in the socket directory before starting: `/tmp` is
  sticky, so only the socket's owner can unlink it — a `postgres`-owned
  postmaster cannot clear another user's leftover `/tmp/.s.PGSQL.<port>` and
  would fail to start (see §5).

`--check` identifies the cluster by **data directory and postmaster process,
not unit name**, so it audits clusters that predate this script. It reports
`OK:`/`WARN:` findings (exit 3 if any) covering: postmaster uid class, logind
`RemoveIPC` state (including whether a `RemoveIPC=no` mitigation lives in an
upgrade-safe drop-in or only in the `/etc/systemd/logind.conf` dpkg conffile),
`PGDATA` ownership, `pg_hba.conf` `trust` entries, and the version floor.

Inputs (all in `deploy.env`, see `deploy.env.example`): `PG_OS_USER` (default
`postgres`), `PG_BIN` (default: probed; required when several majors are
installed), `PGDATA` (required to provision). The endpoint comes from `.env`
(`DB_HOST`/`DB_PORT`).

The unit template is `deploy/systemd/ssc-postgres.service.in`. It is rendered by
`provision_postgres.sh`, **not** by `install_systemd.sh` (which skips it): its
tokens are cluster identity, and `User=` is always a literal system account,
never `__DEPLOY_USER__`. `ssc-web-app.service` orders itself
`After=ssc-postgres.service`; on hosts where Postgres is remote or distro-managed
that reference is silently ignored by systemd and the web app's
`Restart=on-failure` covers the startup race.

**Scope: the physical cluster only.** The logical bootstrap — roles, databases,
schema — stays where it always was: install-guide §5 Step 3 (`CREATE ROLE`/
`CREATE DATABASE`, `./init_orthanc_db.sh`, Alembic at web-app startup). Those
steps run over TCP as `DB_USER` and are deliberately portable to macOS and
remote servers, where no `postgres` OS user exists.

---

## 4. Defense in depth: `RemoveIPC=no` drop-in

With the cluster on a system uid, logind's purge no longer applies to it. Still
disable `RemoveIPC` host-wide, in an upgrade-safe drop-in rather than the dpkg
conffile (`/etc/systemd/logind.conf` can be reverted by a systemd package
upgrade):

```bash
sudo install -d /etc/systemd/logind.conf.d
printf '[Login]\nRemoveIPC=no\n' | sudo tee /etc/systemd/logind.conf.d/10-removeipc.conf
sudo systemctl restart systemd-logind    # existing sessions are unaffected
systemctl show systemd-logind -p RemoveIPC   # expect RemoveIPC=no
```

---

## 5. Migrating a cluster off a login user

For an existing cluster whose postmaster runs as a login account
(`--check` warns about it). Order is **load-bearing** because of the sticky
socket directory: under `/tmp` (mode `1777`), only the socket's **owner** can
unlink `.s.PGSQL.<port>` — once the unit runs as `postgres`, a stale socket left
by the old user is un-removable by the new postmaster and the cluster will fail
to start. Clean-stop and verify the socket is gone **before** switching the unit.

Preconditions: a maintenance window (the web app and Orthanc lose their DB for
the duration), and `PG_OS_USER`/`PG_BIN`/`PGDATA` set in `deploy.env`.

```bash
# 0. Audit the starting state
scripts/linux/provision_postgres.sh --check

# 1. Stop consumers, then the cluster — a clean stop removes the socket
sudo systemctl stop ssc-web-app
docker stop ssc-orthanc
sudo systemctl stop <old-postgres-unit>          # e.g. postgresql18.service

# 2. VERIFY the socket and lock are gone before proceeding
ls /tmp/.s.PGSQL.*        # must print nothing; if not, remove as the OLD user

# 3. Retire the old unit — one cluster, one unit
sudo systemctl disable <old-postgres-unit>
sudo rm /etc/systemd/system/<old-postgres-unit>
sudo systemctl daemon-reload

# 4. Hand the data directory to the system account
sudo chown -R postgres:postgres "<PGDATA>"
# parents of PGDATA must be traversable (x) by the postgres user — verify:
sudo -u postgres test -r "<PGDATA>/PG_VERSION" && echo ok

# 5. Install + start ssc-postgres.service (adopt path: sees PGDATA, no server)
scripts/linux/provision_postgres.sh              # dry-run first — read the plan
sudo scripts/linux/provision_postgres.sh --execute

# 6. Restart consumers, re-render the web-app unit (picks up the new After=)
sudo scripts/linux/install_systemd.sh
docker start ssc-orthanc
curl -s http://localhost:8043/healthz

# 7. Re-audit — the uid-class warning must be gone
scripts/linux/provision_postgres.sh --check
```

Notes:

- `peer` authentication keys on the *connecting* OS user, so operator socket
  logins (`psql` as a role matching your username) keep working after the
  ownership switch; the web app connects over TCP with `scram-sha-256` and is
  equally unaffected. Administrative socket access as the bootstrap superuser
  becomes `sudo -u postgres psql` where applicable.
- The chown is metadata-only and fast, but it also carries
  `pg_hba.conf`/`postgresql.conf` along — nothing else to move.
- Logs: the unit writes `server.log` inside `PGDATA` (now owned by `postgres`).

---

## 6. Hardening an existing `pg_hba.conf`

Fresh clusters are born clean (§3). For an existing cluster with `trust` lines
(initdb's stock default ships three passwordless replication entries), edit
`<PGDATA>/pg_hba.conf` — `local` lines to `peer`, `host` lines to
`scram-sha-256`:

```
local   replication  all                  peer
host    replication  all  127.0.0.1/32    scram-sha-256
host    replication  all  ::1/128         scram-sha-256
```

then reload (no restart needed): `psql -d postgres -c 'SELECT pg_reload_conf()'`
(or `systemctl reload ssc-postgres`). Nothing in this stack uses replication
connections — backups are `pg_dump` — so the change is consumer-invisible;
`--check` verifies the result.
