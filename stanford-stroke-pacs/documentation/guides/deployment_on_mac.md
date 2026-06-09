# Deployment on macOS

**Purpose:** macOS-specific delta on top of the canonical
[`installation_and_deployment.md`](installation_and_deployment.md). It does
**not** repeat the full runbook — read that first for the overall sequence
(env vars, DB bootstrap, user provisioning, post-index tasks). This document
covers only what changes on a Mac and how to make the stack survive reboots.
For moving an existing deployment onto a Mac, see
[`../operations/cluster_migration.md`](../operations/cluster_migration.md).

The stack ports cleanly in principle — Orthanc in Docker, Web App as a
native process, PostgreSQL on the host — but four assumptions in the Linux
setup must be adapted.

---

## 1. The four structural differences

| # | Linux assumption | Why it breaks on macOS | Fix (section) |
|---|---|---|---|
| 1 | `network_mode: host` in `docker-compose.yml` | Docker (Colima) runs Linux in a VM; host networking does **not** publish container ports to the Mac, and the container can't reach `localhost` Postgres | §4 |
| 2 | Image is Linux/amd64 | Apple Silicon is arm64; `orthancteam/orthanc` may be amd64-only | §3 |
| 3 | Web App runs under **systemd** (`ssc-web-app.service`) | macOS has no systemd | §6 |
| 4 | `/DATA2/...` paths, `sudo -u postgres` | Those paths don't exist; Homebrew Postgres has no `postgres` system user (your Mac user is the superuser) | §5 |

On an **Intel Mac**, difference #2 disappears.

---

## 2. Prerequisites (Homebrew + Colima)

This server runs **headless** (no GUI), so it uses **Colima** — a CLI-only Docker
engine on a Lima VM (Apple's Virtualization.framework) — instead of Docker Desktop.
No GUI, no root, no Docker-Desktop licensing.

```bash
xcode-select --install                       # clang/make (skip if already present)
brew install colima docker docker-compose    # headless engine + docker CLI + compose v2
brew install node postgresql@16 bash         # bash 5.x — see §7
brew install --cask miniconda                # or reuse an existing conda
```

Start the VM with the host paths the stack bind-mounts. Colima shares only `$HOME`
by default, so the repo dir (under `/opt`) and the external `/Volumes` DICOM drive
must be added explicitly. VirtioFS (default on vz) keeps the continuous DICOM-tree
scan fast. The exact invocation is captured in
[`scripts/macos/colima_start.sh`](../../scripts/macos/colima_start.sh) — idempotent,
and it waits for the RAID to mount before starting:

```bash
scripts/macos/colima_start.sh
# equivalent to:
#   colima start --cpu 4 --memory 8 --disk 100 --mount-type virtiofs \
#     --mount /opt/ssc-pacs/ssc-pacs/stanford-stroke-pacs:r \
#     --mount /Volumes/ThunderBay_RAID1:w
```

`docker` / `docker compose` then talk to Colima automatically (socket
`~/.colima/default/docker.sock`, context `colima`). There is no Docker Desktop
file-sharing dialog — the `--mount` flags replace it. Verify the mounts are visible
inside the VM with `colima ssh -- ls /opt/ssc-pacs/ssc-pacs/stanford-stroke-pacs`.

---

## 3. Build the patched Orthanc image (Apple Silicon note)

Cold storage requires the custom `ssc-orthanc:patched-indexer` image (see
[`orthanc-indexer-patched/README.md`](../../../orthanc-indexer-patched/README.md)).
First check whether the base supports your architecture:

```bash
docker manifest inspect orthancteam/orthanc | grep -E 'architecture'
```

- **Intel Mac** — build normally: `docker build -t ssc-orthanc:patched-indexer .`
- **Apple Silicon, base has arm64** — same command; builds native.
- **Apple Silicon, amd64 only** — build (and run) under Rosetta emulation. The
  Dockerfile's two stages must agree on architecture, so force amd64 for both:

  ```bash
  cd orthanc-indexer-patched
  docker build --platform linux/amd64 -t ssc-orthanc:patched-indexer .
  ```

  Compiling the plugin under emulation takes a few minutes (one-time). If you
  emulate the build, also add `platform: linux/amd64` to the `orthanc:` service
  in `docker-compose.yml` so the runtime container matches.

---

## 4. Docker networking changes (via an override file)

The base `docker-compose.yml` stays **unchanged** — it keeps `network_mode: host`
for Linux. Do not edit it. macOS needs explicit port publishing and must reach
the host's Postgres via `host.docker.internal` (the DNS name **Colima** — and
Docker Desktop — resolve to the Mac host; it is **not** `localhost` from inside the
container). Under Colima this address NATs to the host **loopback**, so Postgres
needs **no `pg_hba`/`listen_addresses` change** — see §5. Isolate this divergence in
a `docker-compose.override.yml` next to the base file — Docker Compose merges it
automatically on `docker compose up`:

```yaml
# docker-compose.override.yml — macOS only. Auto-merged over docker-compose.yml.
services:
  orthanc:
    # platform: linux/amd64          # only if you emulated the build (§3)
    network_mode: !reset null        # drop the base file's `network_mode: host`
    ports:
      - "8042:8042"
      - "4242:4242"
    environment:
      ORTHANC__POSTGRESQL__HOST: "host.docker.internal"   # overrides ${DB_HOST}
    volumes:
      - ~/pacs/imaging_data:/dicom-data:ro                # your Mac DICOM path
```

The `!reset` tag (Compose ≥ v2.24) is required: a plain `network_mode: null`
is treated as "no value" and the base's `host` wins, which would silently keep
host networking and ignore your `ports:`. With `!reset null`, Orthanc falls
back to the default bridge network and the ports take effect — verify the
merged result with `docker compose config` before `up`.

Merge behavior, confirmed via `docker compose config`: `ports:` is additive,
and `volumes:` is keyed by **target** (container path) — so the override's
`…:/dicom-data` *replaces* the base bind mount at `/dicom-data` while the named
volume and the `orthanc.json`/`orthanc_users.json` mounts from the base are kept.

Keep this override file Mac-local (e.g. `.gitignore` it, or name it
`docker-compose.mac.yml` and pass `-f docker-compose.yml -f docker-compose.mac.yml`)
so it never lands on the Linux host.

Leave `DB_HOST=localhost` in `.env`: that value is still correct for the
**native** Web App process and host-local scripts. Only the container needs
`host.docker.internal`, which is why it is overridden literally here rather
than via `${DB_HOST}`. `ORTHANC_URL` stays `http://localhost:8042` — the
published ports make Orthanc reachable from the Mac host.

> The container mount point stays `/dicom-data`. Orthanc only ever sees that
> path, never the host path — keep this in mind for migration (§see
> `cluster_migration.md`).

---

## 5. PostgreSQL on the host

```bash
brew services start postgresql@16
createdb stanford-stroke          # your Mac user is the PG superuser
```

Two Mac-specific adaptations to the documented bootstrap:

- **`init_orthanc_db.sh`** resolves `.env` relative to itself, so no path edit
  is needed (override with `ENV_FILE=… ./init_orthanc_db.sh` if your `.env`
  lives elsewhere). It connects via TCP as `DB_USER` (needs
  `CREATEDB`/`CREATEROLE`); on Homebrew Postgres your Mac user is superuser by
  default, so use it for `DB_USER` or grant those roles. There is no
  `sudo -u postgres` step on a Mac.
- **Let the Orthanc container reach Postgres.** Connections from
  `host.docker.internal` arrive over TCP from the Docker VM's subnet, which the
  default Homebrew config rejects. In `$(brew --prefix)/var/postgresql@16`:
  - `postgresql.conf`: `listen_addresses = '*'`
  - `pg_hba.conf`: add `host all all 192.168.0.0/16 scram-sha-256`
  - then `brew services restart postgresql@16`

> **Simpler alternative for an eval/dev box:** run Postgres as a Docker service
> in the same compose file instead. Orthanc then reaches it by service name and
> Web App via a published port — no `host.docker.internal`, no `pg_hba`
> editing. Use the host-Postgres path for anything resembling production.

---

## 6. Web App as a launchd service (start on boot)

There is no systemd; `ssc-web-app.service` does not apply. Create a **launchd
agent** so Web App starts at login and restarts on crash. Write
`~/Library/LaunchAgents/com.ssc.webapp.plist` (adjust the conda path and
username):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>            <string>com.ssc.webapp</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/you/miniconda3/envs/pacs/bin/uvicorn</string>
    <string>app:app</string>
    <string>--host</string><string>0.0.0.0</string>
    <string>--port</string><string>8043</string>
  </array>
  <key>WorkingDirectory</key> <string>/Users/you/ssc-pacs/stanford-stroke-pacs/web-app</string>
  <key>RunAtLoad</key>        <true/>
  <key>KeepAlive</key>        <true/>
  <key>StandardOutPath</key>  <string>/Users/you/Library/Logs/ssc-web-app.log</string>
  <key>StandardErrorPath</key><string>/Users/you/Library/Logs/ssc-web-app.err</string>
</dict>
</plist>
```

**Headless servers (no console login): use LaunchDaemons, not LaunchAgents.**
A per-user LaunchAgent only loads inside a GUI login session — on a headless box
accessed over SSH, `launchctl bootstrap gui/$UID …` (and `brew services`) fail with
*"Domain does not support specified action"*. Install as **system LaunchDaemons**
instead. This repo ships ready-made daemon plists in
[`launchd/`](../../launchd/) (run as `pere`) and an installer that does the whole
cutover — Colima, Postgres, the Web App, **and** the nightly backup/reconciliation/
health jobs:

```bash
sudo scripts/macos/install_launchd.sh        # stops the manual instances, then
                                             # bootstraps every com.ssc.* daemon
# manage individual daemons (system domain):
sudo launchctl kickstart -k system/com.ssc.webapp     # restart  (≈ systemctl restart)
sudo launchctl print        system/com.ssc.webapp     # status
tail -f ~/Library/Logs/ssc-web-app.log                 # logs
```

Orthanc is **not** a daemon — its `restart: unless-stopped` container returns
automatically once Colima's Docker engine is up. The `com.ssc.colima` daemon runs
a **watchdog** ([`scripts/macos/colima_watchdog.sh`](../../scripts/macos/colima_watchdog.sh))
that brings the VM up at boot and **restarts it within ~30s if it ever crashes or
stops**, so Orthanc recovers on its own from a VM crash, not just a clean reboot.

---

## 7. Make the whole stack survive reboots

| Component | Boot persistence on macOS |
|---|---|
| Orthanc (Docker) | `restart: unless-stopped` in compose **plus** the Colima VM supervised by the `com.ssc.colima` LaunchDaemon ([`launchd/com.ssc.colima.plist`](../../launchd/com.ssc.colima.plist), `KeepAlive` + `ThrottleInterval=30`). It runs a watchdog ([`scripts/macos/colima_watchdog.sh`](../../scripts/macos/colima_watchdog.sh)) that starts the VM at boot via the idempotent [`scripts/macos/colima_start.sh`](../../scripts/macos/colima_start.sh) and restarts it within ~30s on crash/stop. The container will not come back without the Colima VM running. (Do **not** point the daemon at `colima_start.sh` directly — it exits 0 once the VM is up, which `KeepAlive` would busy-loop.) |
| PostgreSQL | `com.ssc.postgres` LaunchDaemon (installed by `install_launchd.sh`). On a headless box `brew services` can't load a `gui/$UID` agent, so a system daemon running `postgres -D <datadir>` as `pere` is used instead. |
| Web App | `com.ssc.webapp` LaunchDaemon (§6, `RunAtLoad` + `KeepAlive`). |

After a reboot, verify all three with the day-2 commands below.

---

## 8. Day-2 commands (Linux → macOS equivalents)

| Task | Linux | macOS |
|---|---|---|
| Restart Web App | `sudo systemctl restart ssc-web-app` | `sudo launchctl kickstart -k system/com.ssc.webapp` |
| Web App status | `systemctl status ssc-web-app` | `sudo launchctl print system/com.ssc.webapp` |
| Web App logs | `journalctl -u ssc-web-app -f` | `tail -f ~/Library/Logs/ssc-web-app.log` |
| Docker engine | (systemd `docker.service`) | `colima status` / `colima start` (or `scripts/macos/colima_start.sh`) / `colima stop` |
| Orthanc up/down | `docker compose up -d` / `down` | identical (once Colima is up) |
| Orthanc status | `scripts/orthanc/check_status.sh` | identical (works as-is) |
| Postgres restart | `sudo systemctl restart postgresql` | `brew services restart postgresql@16` |

**Rebuild the frontend after code changes:**

```bash
cd web-app && npm run build
launchctl kickstart -k gui/$(id -u)/com.ssc.webapp
```

**bash 3.2 caveat:** macOS ships an ancient `/bin/bash`. A few ops scripts use
bash 4+ features — e.g. `scripts/backup/backup_pg_db.sh` and
`scripts/backup/backup_orthanc_storage.sh` use `mapfile` and will fail under it.
That is why §2 installs Homebrew `bash`; invoke those scripts with it
(`/opt/homebrew/bin/bash scripts/backup/backup_pg_db.sh ...`) or put it first on
`PATH`. There is no `systemd` on macOS, so the nightly backups must be wired
through `launchd` (or `cron`) the same way the web app uses a launchd job —
the backup **scripts** themselves are platform-agnostic.

---

## 9. Validation

No SSH tunnel is needed on a local Mac — browse directly. `cookie_secure` is
already `false` in `config.toml` (with a note that Safari rejects Secure
cookies on `http://localhost`), so login works over loopback.

```bash
docker compose ps
scripts/orthanc/check_status.sh
launchctl print gui/$(id -u)/com.ssc.webapp | grep state
```

Browser checks: `http://localhost:8042/ui/app/`, `/ohif/`, and
`http://localhost:8043/app/`.
