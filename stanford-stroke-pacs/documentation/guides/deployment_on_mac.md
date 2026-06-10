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

> **Plus, if the box is headless and data lives on an external volume:** background
> LaunchDaemons are denied access to that volume until granted **Full Disk Access** —
> warm and backups fail with `Operation not permitted` otherwise. This is a manual,
> GUI-only one-time step; see §6, *"Full Disk Access"*.

---

## 2. Prerequisites (Homebrew + Colima)

This server runs **headless** (no GUI), so it uses **Colima** — a CLI-only Docker
engine on a Lima VM (Apple's Virtualization.framework) — instead of Docker Desktop.
No GUI, no root, no Docker-Desktop licensing.

```bash
xcode-select --install                       # clang/make (skip if already present)
brew install colima docker docker-compose    # headless engine + docker CLI + compose v2
brew install node postgresql@16 bash coreutils   # bash 5.x (§8); coreutils = GNU sha256sum/stat the backup jobs need
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

The VM is sized at 4 vCPU / 8 GB. vCPUs matter when a study loads in OHIF, which
fires many parallel DICOMweb frame requests; 2 vCPU made the viewer sluggish.
vCPUs are a *cap*, not a hard reservation, so the host still reclaims them for
cold-storage warm extractions and Postgres whenever Orthanc is idle. Tune
per-host via `COLIMA_CPU` / `COLIMA_MEMORY`; resizing takes effect on the next
`colima stop && colima start` (or the watchdog's next restart).

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

Start Postgres for the bootstrap. On a **headless** box `brew services` can't load
its `gui/$UID` agent (*"Domain does not support specified action"*), so start it
directly; **boot persistence is handled later by the `com.ssc.postgres` LaunchDaemon**
(§6 / `install_launchd.sh`), not `brew services`:

```bash
pg_ctl -D "$(brew --prefix)/var/postgresql@16" start   # headless; (`brew services start postgresql@16` only on a GUI Mac)
createdb stanford-stroke          # your Mac user is the PG superuser
```

Two Mac-specific adaptations to the documented bootstrap:

- **`init_orthanc_db.sh`** resolves `.env` relative to itself, so no path edit
  is needed (override with `ENV_FILE=… ./init_orthanc_db.sh` if your `.env`
  lives elsewhere). It connects via TCP as `DB_USER` (needs
  `CREATEDB`/`CREATEROLE`); on Homebrew Postgres your Mac user is superuser by
  default, so use it for `DB_USER` or grant those roles. There is no
  `sudo -u postgres` step on a Mac.
- **Orthanc container → Postgres: no config change under Colima.** Colima NATs
  `host.docker.internal` to the host **loopback**, so Postgres sees the container
  connection as `127.0.0.1` and the default Homebrew `host 127.0.0.1/32 trust` rule
  accepts it — **no `listen_addresses` / `pg_hba` edit needed** (verified: a
  `postgres:16-alpine` container read `orthanc_db` over `host.docker.internal` with
  no extra config). Postgres stays bound to loopback, not the LAN. *(Docker Desktop
  would instead arrive from a `192.168.65/24` subnet needing a `scram` rule — that
  does **not** apply to Colima.)*

> **Simpler alternative for an eval/dev box:** run Postgres as a Docker service
> in the same compose file instead. Orthanc then reaches it by service name and
> Web App via a published port — no `host.docker.internal`, no `pg_hba`
> editing. Use the host-Postgres path for anything resembling production.

---

## 6. Web App as a launchd service (start on boot)

There is no systemd; `ssc-web-app.service` does not apply. The repo ships
ready-made plists in [`launchd/`](../../launchd/) — `com.ssc.colima`,
`com.ssc.postgres`, `com.ssc.webapp`, and the nightly `com.ssc.pg-backup-*`,
`com.ssc.orthanc-storage-backup`, `com.ssc.reconciliation`,
`com.ssc.cold-storage-health` jobs — so you don't hand-write them. The web-app plist
runs `…/envs/ssc-pacs/bin/uvicorn app:app --host 0.0.0.0 --port 8043` with
`RunAtLoad` + `KeepAlive`.

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

### Full Disk Access — REQUIRED when data lives on an external volume

macOS blocks **background LaunchDaemons** from reading/writing **external/removable
volumes** (e.g. the ThunderBay RAID) — a process in a login/SSH session is allowed, a
daemon is **not**. Installing the daemons is not enough; without this grant the
failures are silent and misleading:

- Web app warm fails with `extraction_produced_no_warm_series` — look for
  `Operation not permitted` / EPERM under `/Volumes/...` in `~/Library/Logs/ssc-web-app.err`.
- The nightly backups can't write to the backup volume.

There is **no CLI way** to grant this on a non-MDM Mac — do it once in the GUI
(Screen Sharing / VNC is fine; enable it over SSH if needed). In **System Settings →
Privacy & Security → Full Disk Access**, click **`+`** and add these binaries. The
picker hides `/opt`, so either run `open -R "<path>"` in Terminal and **drag** the
revealed binary in, or press **⌘⇧G** in the picker and paste the full path:

| Binary | For | Note |
|---|---|---|
| `<conda base>/envs/ssc-pacs/bin/python3.12` | web app warm/evict | stable across brew upgrades |
| `/opt/homebrew/Cellar/bash/<ver>/bin/bash` | backup scripts (`>` redirects) | **re-add after `brew upgrade bash`** |
| `/opt/homebrew/Cellar/postgresql@16/<ver>/bin/pg_dump` | `pg_dump` writes the `.dump` | **re-add after `brew upgrade postgresql@16`** |

`/opt/homebrew/bin/{bash,pg_dump}` are symlinks — grant the **resolved** Cellar path
(`readlink -f /opt/homebrew/bin/bash`). The Cellar paths carry the version number, so
they change on upgrade; the conda `python3.12` does not.

**TCC only applies to a freshly-launched process — restart the daemons after
granting** (an already-running daemon keeps failing against its old, un-granted
process):

```bash
sudo launchctl kickstart -k system/com.ssc.webapp                      # picks up FDA for warm
sudo launchctl kickstart -k system/com.ssc.pg-backup-stanford-stroke   # verify the backup path too
```

Verify: warm a study in the UI (files appear under `legacy_dicom_root`), and
`tail ~/Library/Logs/com.ssc.pg-backup-stanford-stroke.log` should show `OK …`.

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
