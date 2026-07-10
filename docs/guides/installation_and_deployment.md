# Installation and Deployment

**Purpose:** Fresh-server runbook only. For the full map of *where every config value lives and what must stay in sync* see [`../reference/configuration_sources.md`](../reference/configuration_sources.md). For packaging facts and config file roles see [`../reference/runtime_and_config.md`](../reference/runtime_and_config.md). For architecture see [`../reference/architecture.md`](../reference/architecture.md).

> **Platform:** this runbook targets **Linux** (systemd, host networking).
> Production currently runs on **macOS** — for that platform read this document
> for the sequence, then apply the deltas in
> [`deployment_on_mac.md`](deployment_on_mac.md) (Colima, launchd, Homebrew
> Postgres).

> **Directory terms used below.** The git checkout root (`ssc-pacs/`) holds
> the `Makefile` and CI. The **stack root** is
> `stanford-stroke-pacs/` inside it — `.env`, `config.toml`,
> `docker-compose.yml`, `scripts/`, `web-app/`, and the stack
> `requirements.txt` all live there. Commands below run from the **stack
> root** unless stated otherwise.

> **Three files, two installers.** A fresh deploy edits only `.env` (secrets),
> `config.toml` (non-secret ops), and optionally `deploy.env` (per-host identity),
> then runs `scripts/orthanc/dc.sh up -d` and the unit installer for the platform.
> Compose, service units, and `orthanc.json` are not hand-edited — see the
> [config sources map](../reference/configuration_sources.md).

This document is the operator runbook for deploying the PACS stack on a fresh
server.

It assumes the target deployment wants the same overall architecture:

- Orthanc + OE2 + OHIF in Docker (**Orthanc only** in `docker-compose.yml`)
- Web App app as a **native systemd service** on port `8043` (not Docker)
- PostgreSQL on the host (both databases — see §3)
- DICOM files kept on disk and indexed read-only (or hot cache when using cold storage)

---

## 0. Two install paths — pick one

| Path | Use when | Follow |
|---|---|---|
| **Fresh install** | New server with **no existing PACS data** — you create the databases, roles, and schema from scratch and ingest data afterwards | This document (all sections) |
| **Migration install** | You are **porting an existing deployment** to a new host (its PostgreSQL data, the Orthanc index, and the DICOM archives already exist and must travel **without reindexing**) | [`../operations/cluster_migration.md`](../operations/cluster_migration.md) §§1–4, then return to §5 here (Steps 5–8: bring-up, web app, service units) + §6 validation |

The two paths share the host prerequisites (§2), the config files (§3), and the
service bring-up (§5 Steps 5–8). They differ only in **how the databases get
their contents**: a fresh install creates empty databases and the schema (§5
Step 3); a migration `pg_restore`s both databases from the source host instead.
If unsure, you are doing a fresh install.

---

## 1. Schema vs. data — what this runbook does and doesn't create

For a **fresh install**, this runbook creates the databases, roles, and **table
schema** for you (§3, via Alembic) — you do **not** need a pre-existing
metadata layer. What you must supply is the *content*: real rows in the upstream
`patient` / `image_study` / `image_series` tables (loaded from your source
metadata or the ingestion pipeline — §5 Step 3d) and the matching DICOM tree on
disk. `image_ingestion_protocols/` is legacy/site-specific pipeline code, not the
normal bootstrap path for a fresh install.

---

## 2. Host prerequisites

The host should satisfy all of the following:

- Linux host compatible with Docker host networking
- Docker daemon running
- Docker Compose plugin available
- PostgreSQL **server** installed and running (a single server hosts both the
  `stanford-stroke` and `orthanc_db` databases)
- PostgreSQL admin access to bootstrap the databases — either `sudo -u postgres
  psql`, or a role with `CREATEDB`/`CREATEROLE` you can connect as (§3)
- free host ports:
  - `8042` for Orthanc HTTP
  - `4242` for Orthanc DICOM
  - `8043` for the web app app
- writable checkout of this repository
- filesystem path for the DICOM repository chosen and available

For the helper scripts, install Python packages from the **stack root**
`stanford-stroke-pacs/requirements.txt` (§5 Step 1). One more prerequisite the
compose file assumes: the custom Orthanc image **`ssc-orthanc:patched-indexer`**
is not on any registry — it must be **built on the host** before bring-up (§5
Step 5).

---

## 3. Required environment configuration

Create a local `.env` file in the **stack root** (`stanford-stroke-pacs/.env` —
`docker-compose.yml` loads it via a relative `env_file: .env`) before first
start. Start from the tracked template:

```bash
cp .env.example .env    # then fill in real values
```

Variables expected by the current codebase (same key set as `.env.example`):

| Variable | Purpose |
|----------|---------|
| `DB_HOST` | Host for the research/app PostgreSQL database |
| `DB_PORT` | Port for the research/app PostgreSQL database |
| `DB_NAME` | Database name used by web app and helper scripts, typically `stanford-stroke` |
| `DB_USER` | Database user for the research/app database |
| `DB_PASSWORD` | Password for the research/app database |
| `PG_ORTHANC_DB` | Orthanc index database name |
| `PG_ORTHANC_USER` | Orthanc index database user |
| `PG_ORTHANC_PASSWORD` | Orthanc index database password |
| `ORTHANC_URL` | Base URL used by Web App's reverse proxy and host-local scripts, typically `http://localhost:8042` |
| `ORTHANC_ADMIN_USER` | Orthanc service account; Web App attaches it on every proxied OHIF/DICOMweb call. Also used by `scripts/data_integrity/reconcile.py` and other host-local scripts. |
| `ORTHANC_ADMIN_PASSWORD` | Password for the Orthanc service account |
| `JWT_SECRET` | Secret used to sign web app JWT cookies |

Optional: `ORTHANC_HTTP_PORT` / `ORTHANC_DICOM_PORT` change the Orthanc ports in
one place (default `8042` / `4242` if unset).

Non-secret web app tuning (storage paths, storage mode, session length) lives in
the stack-root **`config.toml`** (loaded by `web-app/config.py`). `config.toml` is
**required** — the web app fails fast at startup if it is missing — and ships in
the repo; edit it in place for this host. Per-host service-unit identity (OS user,
repo path, conda python) is auto-derived by the installers and overridable in
`deploy.env` (`cp deploy.env.example deploy.env`). See the
[config sources map](../reference/configuration_sources.md).

---

## 4. Expected source metadata tables

The upstream table **schema** is created in §5 Step 3c (by Alembic). This
section documents the **columns the code actually reads**, so you can map your
source data onto them when loading rows (§5 Step 3d). For the full web app
workflow `image_series` and `image_study` must be populated.

At minimum, the current scripts rely on these columns:

**image_series:**

- `patient_id`
- `studyinstanceuid`
- `seriesinstanceuid`
- `seriesdescription`
- `dicom_dir_path`
- `modality`

**image_study** (for study-level metadata):

- `study_type` (used by `scripts/orthanc/label_studies.py`)
- `studydescription` (used by `scripts/orthanc/enrich_orthanc.py`)

Beyond the web app (which browses both tables),
`scripts/data_integrity/reconcile.py` compares `image_series` against Orthanc's
index. Without equivalent tables the service layer still deploys, but the web
app and metadata-driven scripts will not function as documented.

---

## 5. First-time bootstrap sequence

Use this order for a new deployment.

### Step 1. Install the stack's script dependencies

From the stack root (`stanford-stroke-pacs/`):

```bash
python3 -m pip install -r requirements.txt
```

This installs the dependencies needed by the stack's helper scripts, e.g.:

- `scripts/admin/manage_users.py` (needs `bcrypt`)
- `scripts/orthanc/enrich_orthanc.py`
- `scripts/orthanc/label_studies.py`

### Step 2. Set the DICOM path in `config.toml` (not in compose)

The Orthanc `/dicom-data` bind mount is **not** hardcoded in `docker-compose.yml`.
Its source comes from `config.toml`: `scripts/orthanc/dc.sh` reads
`[storage].dicom_data_root` (the uncompressed DICOM tree — the loose tree in
`legacy` mode, the warm cache that archives extract into in `cold_path_cache`
mode) and exports it as `DICOM_MOUNT_SOURCE`. So on a new server you set the path
**once**, in `config.toml`:

```toml
[storage]
mode = "legacy"                       # or "cold_path_cache"
dicom_data_root = "/your/dicom/path"
```

Always bring Orthanc up through the wrapper (`scripts/orthanc/dc.sh up -d`) — bare
`docker compose up` errors that `DICOM_MOUNT_SOURCE` is unset.

> **This runbook targets Linux** (host networking). On **macOS** the wrapper also
> applies `docker-compose.override.macos.yml` automatically (drops host networking,
> publishes ports, points Postgres at `host.docker.internal`). Follow
> [`deployment_on_mac.md`](deployment_on_mac.md) §4 for the rest of the macOS deltas.

`docker-compose.yml` loads secrets via `env_file: .env`, a path relative to the
compose file. Ensure `stanford-stroke-pacs/.env` exists before bring-up.

Requirements:

- the DICOM path must exist
- Orthanc only needs read access
- the dataset should already be organized on disk before startup

For cold storage / hot cache, follow [`../cold_storage/runbook.md`](../cold_storage/runbook.md) instead of the default legacy mount once you are ready.

### Step 3. Create the PostgreSQL databases, roles, and upstream tables

The stack uses **two databases on one PostgreSQL server** (see
[`../reference/configuration_sources.md`](../reference/configuration_sources.md)):

- **`stanford-stroke`** — the research/app DB. Holds the upstream metadata
  (`patient`, `image_study`, `image_series`, optional `lvo_clinical_data`) **and**
  the web-app-owned tables (`users`, `annotations`, `annotations_history`,
  `label_definitions`, `user_preferences`, `series_cache_state`, and the
  `*_labelled` mirrors).
- **`orthanc_db`** — Orthanc's internal index.

> **Migration install:** skip the *create + schema* sub-steps below — you
> `pg_restore` both databases from the source host instead. Follow
> [`../operations/cluster_migration.md`](../operations/cluster_migration.md) §1,
> then continue at Step 4.

**3a. Create the `stanford-stroke` database and app role.** Use the credentials
you put in `.env` (`DB_NAME`, `DB_USER`, `DB_PASSWORD`). As a PostgreSQL admin
(e.g. `sudo -u postgres psql`):

```sql
-- CREATEDB/CREATEROLE let DB_USER bootstrap orthanc_db in 3b; revoke afterwards
-- if you prefer a least-privilege runtime role (the app itself needs neither).
CREATE ROLE "<DB_USER>" WITH LOGIN CREATEDB CREATEROLE PASSWORD '<DB_PASSWORD>';
CREATE DATABASE "stanford-stroke" OWNER "<DB_USER>";
```

(On a single-user dev box where your OS user is already the PG superuser, a plain
`createdb stanford-stroke` is enough — that role then doubles as `DB_USER`.)

**3b. Create the Orthanc database and role.** This one is scripted and idempotent
— it reads `.env` and connects via TCP as `DB_USER` (which is why 3a grants it
`CREATEDB`/`CREATEROLE`; alternatively run it as the superuser):

```bash
./init_orthanc_db.sh        # creates PG_ORTHANC_USER + PG_ORTHANC_DB, grants privileges
```

Optionally tighten the runtime role afterwards: `ALTER ROLE "<DB_USER>" NOCREATEDB NOCREATEROLE;`

**3c. Create the schema in `stanford-stroke`.** Alembic is the single source of
truth for the DDL — one `upgrade head` creates the upstream `patient` /
`image_study` / `image_series` tables, the `lvo_clinical_data` side-table, and
the web-app-owned tables. `web-app/app.py` runs `alembic upgrade head`
automatically at first startup (Step 8), so you can skip ahead. Run it manually
now only if you want the upstream spine to exist before loading data in Step 3d:

```bash
# From the stack root stanford-stroke-pacs/, with .env present (env.py reads DB_* from it):
conda activate ssc-pacs
alembic upgrade head
```

> `alembic upgrade head` is idempotent, so the automatic run at first startup
> (Step 8) is a no-op if you ran it here.

**3d. Load the upstream data.** Creating the tables does **not** populate them.
Load your real `patient` / `image_study` / `image_series` rows from your source
(a dump from an existing system, your own CSV loader, or the site-specific
`image_ingestion_protocols/` pipeline). The web app browses these tables, so it
will be empty until they are populated.

### Step 4. Create the first admin user and the Orthanc service account

End users authenticate to Web App (which proxies OHIF/DICOMweb to Orthanc).
Admins additionally need a direct Orthanc login so they can reach Orthanc
Explorer 2 on `:8042`. The service account is the credential Web App uses
internally when proxying.

```bash
# 1. Create the first admin (DB + orthanc_users.json):
#    Admins bypass dataset access control. Non-admin users are DENY-BY-DEFAULT:
#    created without --datasets they see NO data — see §9.
python scripts/admin/manage_users.py add <username> --admin

# 2. Set the Orthanc service-account password (.env + orthanc_users.json):
python scripts/admin/manage_users.py rotate-service-account

# 3. (optional) confirm .env and orthanc_users.json agree:
python scripts/admin/manage_users.py check-service-account
```

This ensures `users` exists in the research/app DB, inserts the admin (bcrypt
hash) and mirrors it into `orthanc_users.json`, and writes the service-account
credential into both `orthanc_users.json` and `.env`. It matters **before first
start** because Orthanc auth depends on `orthanc_users.json` carrying at least
the service-account entry — without it, the Web App proxy and host-local scripts
get 401s from Orthanc.

### Step 5. Build the patched Orthanc image, then start Orthanc (Docker)

`docker-compose.yml` references the custom image
**`ssc-orthanc:patched-indexer`** — a fork of the Folder Indexer plugin with the
`RemoveMissingFiles` flag and the scoped `/indexer/scan` endpoint (required for
`cold_path_cache`, and the image the stack runs in every mode). It is **not on
any registry**: build it once on the host or bring-up fails with "image not
found". From the checkout root:

```bash
cd orthanc-indexer-patched
docker build -t ssc-orthanc:patched-indexer .    # see its README.md for platform notes
```

Then, from the stack root:

```bash
scripts/orthanc/dc.sh up -d
```

Use the `dc.sh` wrapper, not bare `docker compose`: it resolves the DICOM mount
from `config.toml` and (on macOS) applies the override. This starts
**`ssc-orthanc`** only. `docker-compose.yml` does not define a Web App container.

### Step 6. Install Web App Python dependencies

From the stack root (use your preferred env, e.g. conda `ssc-pacs`):

```bash
python3 -m pip install -r web-app/requirements.txt
```

### Step 7. Build the web app frontend

```bash
cd web-app && npm ci && npm run build
```

### Step 8. Install and start the web app + timers (systemd)

The units ship as **templates** (`deploy/systemd/*.in`) with `__TOKENS__` for the
per-host bits. The installer resolves user/repo/python automatically (override in
`deploy.env`), renders the templates into `/etc/systemd/system/`, and enables the
web app plus the backup/health timers (reconciliation is on-demand only — no
timer; see [`operations/reconciliation.md`](../operations/reconciliation.md)):

```bash
scripts/linux/install_systemd.sh --dry-run    # preview the rendered units
sudo scripts/linux/install_systemd.sh         # render + install + enable
```

This replaces the old `sudo cp deploy/systemd/ssc-web-app.service …` step (and the
separate backup-timer copy in §9) — one command installs everything. The dormant
`cold-archive-mirror.timer` is left disabled by default.

### Step 9. Index the DICOM tree into Orthanc

**The shipped `orthanc.json` does NOT scan continuously**: its `Indexer` block
has `"Folders": []` (plus `"ScanRoots": ["/dicom-data"]`), so the monitor is
idle and a fresh deploy indexes **nothing** until you trigger it. Pick one:

- **Scoped scans (how this deployment runs):** register data on demand via the
  patched plugin's `POST /indexer/scan` endpoint — the ingestion pipeline does
  this per case, and `scripts/cold_storage/scoped_index.py` /
  `scripts/cold_storage/reindex_missing_series.py` do it in bulk (bounded
  passes — large unbounded registrations can OOM the container; see
  `docs/cold_storage/`).
- **Continuous scan (upstream behavior):** set
  `"Folders": ["/dicom-data"]` in `orthanc.json` and restart Orthanc; the
  indexer then scans on startup and every `Interval` seconds. Suitable for a
  simple `legacy`-mode install; do **not** combine with `cold_path_cache`
  bulk loads.

Depending on dataset size, indexing may take significant time. Useful checks:

```bash
scripts/orthanc/dc.sh logs -f orthanc      # dc.sh, not bare docker compose (mount var)
curl -s -u <user>:<pass> http://localhost:8042/statistics | python3 -m json.tool
```

### Step 10. Run post-index tasks as needed

Two optional-but-useful post-index tasks:

- **Display enrichment** — `python scripts/orthanc/enrich_orthanc.py`. Run it
  only when the DICOM headers are anonymized or not operator-friendly: it maps
  `patient_id`, `seriesdescription`, and `studydescription` (from `image_study`)
  into OE2's Patient/Series/Study Description fields, mutating Orthanc's
  PostgreSQL index tables. Skip it if the headers already display acceptably or
  you only need indexing/OHIF/the web app.
- **Pre-seed study labels** — `python scripts/orthanc/label_studies.py`. Turns
  `image_study.study_type` + `image_series.modality` into Orthanc study labels
  in OE2. Idempotent and safe to re-run after new studies are indexed.

---

## 6. Validation checklist

After startup, validate each layer separately.

### 6.1 Container and API checks

Basic checks (always via the `dc.sh` wrapper — bare `docker compose` errors
that `DICOM_MOUNT_SOURCE` is unset):

```bash
scripts/orthanc/dc.sh ps
scripts/orthanc/dc.sh logs -f orthanc
```

Orthanc system and statistics:

```bash
curl -s -u <user>:<pass> http://localhost:8042/system | python3 -m json.tool
curl -s -u <user>:<pass> http://localhost:8042/statistics | python3 -m json.tool
```

Web App service:

```bash
sudo systemctl status ssc-web-app
curl -s http://localhost:8043/healthz | python3 -m json.tool   # unauthenticated liveness + version
```

Web App read APIs require a login cookie (unauthenticated calls return **401**):

```bash
curl -s -c cookies.txt -X POST http://localhost:8043/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "<user>", "password": "<pass>"}'
curl -s -b cookies.txt http://localhost:8043/api/labels | python3 -m json.tool
curl -s -b cookies.txt 'http://localhost:8043/api/series?per_page=5' | python3 -m json.tool
```

### 6.2 Provided helper checks

The repo includes:

```bash
./scripts/orthanc/check_status.sh
python scripts/data_integrity/reconcile.py
```

Current caveats:

- `scripts/orthanc/check_status.sh` reads Orthanc credentials from `.env` (no hardcoded values)
- `scripts/orthanc/check_status.sh` validates the `ssc-orthanc` container and Orthanc only, not
  the web app
- `scripts/data_integrity/reconcile.py` imports the web app's modules (`db`,
  `reconciliation`, `metrics`), so it needs `web-app/requirements.txt`
  installed (Step 6), and it fails fast unless `.env` provides both
  `ORTHANC_ADMIN_USER`/`ORTHANC_ADMIN_PASSWORD` **and** the `PG_ORTHANC_*`
  credentials (it bulk-reads `orthanc_db` read-only)

### 6.3 Browser checks

Verify these URLs:

- `http://localhost:8042/ui/app/`
- `http://localhost:8042/ohif/`
- `http://localhost:8043/`
- `http://localhost:8043/app/`

Expected outcomes:

- Orthanc Explorer 2 loads at `/ui/app/`
- OHIF opens at `/ohif/`
- the landing page shows links to Orthanc Explorer and OHIF
- the web app app loads its series browser

### 6.4 Index coverage check

`reconcile.py` (§6.2) is the most useful coverage check: it compares
`SeriesInstanceUID` values between `image_series` and Orthanc's indexed series
(via REST), so it verifies that indexing actually matches the expected metadata
inventory. Run it once the source metadata tables are populated.

---

## 7. SSH tunnel notes

The documented tunnel for interactive use should forward:

- `8042` for Orthanc/OE2/OHIF
- `8043` for the web app app
- optionally `4242` if DICOM port forwarding is needed

Example:

```bash
ssh -N \
  -L 8042:localhost:8042 \
  -L 8043:localhost:8043 \
  -L 4242:localhost:4242 \
  -o ServerAliveInterval=60 \
  -o ServerAliveCountMax=3 \
  <user>@<server>
```

The repo ships per-platform tunnel helpers that forward all three ports
(`8042`, `8043`, `4242`): `scripts/connectivity/tunnel/linux/tunnel.sh`,
`.../macos/tunnel.command`, and `.../windows/tunnel.cmd`.

---

## 8. Developer setup

For contributors who want to run the test suite and linters locally.

### Prerequisites

- Python 3.12+ with the `ssc-pacs` conda environment
- Node.js 20+ and npm
- A local PostgreSQL instance (the test suite creates a scratch DB)

### One-time setup

From the checkout root (where the `Makefile` lives):

```bash
conda activate ssc-pacs
make install-dev
```

This installs all Python deps (runtime + dev), runs `npm ci` for the frontend,
and installs pre-commit hooks so linting runs automatically before each commit.

### Running tests

```bash
make test            # backend + frontend + ingestion
make test-backend    # pytest only (requires Postgres)
make test-frontend   # vitest only (no Postgres needed)
make test-ingestion  # ingestion-protocol suite (no Postgres needed)
```

### Running linters

```bash
make lint          # ruff (web-app, scripts, ingestion) + eslint
```

### CI

Every push to `main` and every PR triggers GitHub Actions CI (`.github/workflows/ci.yml`). Required jobs: lint, backend-tests, ingestion-tests, frontend-tests, frontend-build. The mypy job is advisory (non-blocking).

Pre-commit hooks run locally before each commit if installed via `make install-dev` (or `pre-commit install`).

---

## 9. Ongoing operations

Common actions after deployment:

Add a user. **Dataset grants are deny-by-default**: a non-admin created
without `--datasets` sees **no data at all** — grant the `patient.dataset`
cohorts the user may see at creation time (or later with `set-datasets`):

```bash
python scripts/admin/manage_users.py add <username> --datasets 'PRECISE,CRISP2/LVO'
python scripts/admin/manage_users.py set-datasets <username> --all   # or a csv, or --none
```

Change a user password:

```bash
python scripts/admin/manage_users.py passwd <username>
```

Restart Orthanc **only** when `orthanc_users.json` changed — i.e. after adding
or changing an **admin** user (`--admin`, mirrored into the JSON) or rotating
the service account. Regular-user changes live in PostgreSQL only and need no
restart:

```bash
docker restart ssc-orthanc
```

If the changed credential is the Orthanc service account used by the web app
(`rotate-service-account`), also:

```bash
sudo systemctl restart ssc-web-app
```

Rebuild the web app frontend after code changes:

```bash
cd web-app && npm run build
sudo systemctl restart ssc-web-app
```

Scheduled backups are installed **automatically** by
`scripts/linux/install_systemd.sh` (§8) — the nightly jobs for **both** PostgreSQL
databases **and** the Orthanc storage volume (the latter holds OHIF-authored SR
annotations, the **only copy**, plus the Folder Indexer DB). Confirm they are
active:

```bash
systemctl list-timers 'pg-backup-*' 'orthanc-storage-backup*'
```

Backups land under `[backup].backup_root` from `config.toml`. Full mechanism, retention, and recovery:
[`../operations/backup_strategy.md`](../operations/backup_strategy.md) and
[`../operations/restore_runbook.md`](../operations/restore_runbook.md).

---

## 10. What is not part of standard redeployment

Do not treat these as mandatory steps unless the new environment truly needs
them.

`scripts/orthanc/enrich_orthanc.py`

- optional display-enrichment step
- specific to deployments where Orthanc's displayed identifiers need replacing
- directly mutates Orthanc PostgreSQL tables

`image_ingestion_protocols/`

- legacy Stanford Stroke Center–specific metadata ingestion and correction
  pipeline
- not required just to deploy PACS services
- only relevant if recreating the same upstream metadata-generation workflow

---

## 11. Known repo caveats

- `scripts/admin/teardown.sh` is destructive; it sources the stack-root `.env`
  (resolved relative to its own location) and does **not** stop the web app
  service — use with care.
- Always bring the stack up with `scripts/orthanc/dc.sh`, not bare `docker
  compose` (which errors that `DICOM_MOUNT_SOURCE` is unset — the wrapper
  exports it from `config.toml`).
- `scripts/orthanc/check_status.sh` validates the `ssc-orthanc` container only
  (not the web app) and reads Orthanc credentials from `.env`.

---

## 12. Migration install (porting an existing deployment)

If you are **not** starting from zero but moving a live deployment to a new host,
do **not** create empty databases and re-ingest. The canonical procedure is
[`../operations/cluster_migration.md`](../operations/cluster_migration.md); it is
designed so the Orthanc index, OHIF SR annotations, and the DICOM archives travel
**without reindexing** (Orthanc only ever references the container path
`/dicom-data`, so host-path changes are invisible to its index).

How it slots into this runbook:

1. **§2 host prerequisites + §3 config files** — same as a fresh install. Set
   `config.toml` `[storage]` to the **new** host's paths; create `.env`.
2. **Databases** — instead of §5 Step 3, follow `cluster_migration.md` §1:
   create the empty databases (`createdb stanford-stroke`, `./init_orthanc_db.sh`)
   then `pg_restore` **both** dumps from the source host. Restoring
   `stanford-stroke` brings `alembic_version` along, so the web app sees the
   schema already at head and does not re-migrate.
3. **Orthanc index volume + archive tree** — `cluster_migration.md` §2: migrate
   the `<project>_ssc-orthanc-storage` Docker volume (indexer state + the only
   copy of OHIF SR annotations) and rsync `cold_archive_root`.
4. **Host-path backfill** — `cluster_migration.md` §3: rewrite the host-path
   columns across the schema to the new prefixes, and reset `series_cache_state` to cold.
5. **Bring-up + web app + service units** — return here for §5 Steps 5–8
   (`scripts/orthanc/dc.sh up -d`, web app deps/build, the unit installer).
6. **Verify** — `cluster_migration.md` §4 (`reconcile_migration.py`, then
   `reconcile.py`) instead of, or in addition to, §6 here.

The service layer (Orthanc container, web app, systemd/launchd units, the
`dc.sh` wrapper) is **identical** to a fresh install — only the database
provisioning differs (restore vs. create-and-ingest).
