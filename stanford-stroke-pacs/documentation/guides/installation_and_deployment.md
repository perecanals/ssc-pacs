# Installation and Deployment

**Purpose:** Fresh-server runbook only. For the full map of *where every config value lives and what must stay in sync* see [`../reference/configuration_sources.md`](../reference/configuration_sources.md). For packaging facts and config file roles see [`../reference/runtime_and_config.md`](../reference/runtime_and_config.md). For architecture see [`../reference/architecture.md`](../reference/architecture.md).

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

## 1. What must already exist

For a **fresh install**, this runbook creates the databases, roles, and schema
for you (§3) — you do **not** need a pre-existing metadata layer. What you must
supply is the *content*: real rows in the upstream `patient` / `image_study` /
`image_series` tables and the matching DICOM tree on disk.

Minimum required inputs:

- a Linux host
- Docker Engine with `docker compose`
- a PostgreSQL **server** reachable from the host (installed; the databases
  themselves are created in §3)
- a superuser or a role with `CREATEDB`/`CREATEROLE` to bootstrap the databases
- `python3` and `pip`
- Node.js and npm (for building the web app frontend)
- a DICOM directory tree on disk
- the **data** to populate `image_series` / `image_study` / `patient` (the
  schema is created in §3; loading rows is the ingestion step, not part of the
  service bootstrap)

Important:

- this repo creates the upstream **table schema** (§3, from `ssc-sql-db/`) but
  does **not** generate the upstream **data** — that comes from your source
  metadata or the ingestion pipeline
- `image_integration_protocols/` is legacy/site-specific pipeline code, not the
  normal bootstrap path for a fresh PACS install

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

For the helper scripts, install Python packages from the repo root
`requirements.txt`.

---

## 3. Required environment configuration

Create a local `.env` file in the repo root before first start.

Variables expected by the current codebase:

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
repo-root **`config.toml`** (loaded by `web-app/config.py`). `config.toml` is
**required** — the web app fails fast at startup if it is missing — and ships in
the repo; edit it in place for this host. Per-host service-unit identity (OS user,
repo path, conda python) is auto-derived by the installers and overridable in
`deploy.env` (`cp deploy.env.example deploy.env`). See the
[config sources map](../reference/configuration_sources.md).

---

## 4. Expected source metadata tables

The upstream table **schema** is created in §5 Step 3c (from `ssc-sql-db/`). This
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

How the repo uses these tables:

- web app browsing reads from them
- `scripts/data_integrity/reconcile.py` compares `image_series` against Orthanc's index
- `scripts/orthanc/label_studies.py` reads `study_type` from `image_study` and `modality` from
  `image_series`
- `scripts/orthanc/enrich_orthanc.py` uses them for display enrichment

If the new environment does not have equivalent tables yet, the PACS service
layer can still be deployed, but the web app and metadata-driven scripts will
not function as documented.

---

## 5. First-time bootstrap sequence

Use this order for a new deployment.

### Step 1. Install root Python dependencies

From the repo root:

```bash
python3 -m pip install -r requirements.txt
```

This installs the dependencies needed by root-level helper scripts such as:

- `scripts/admin/manage_users.py`
- `scripts/orthanc/enrich_orthanc.py`
- `scripts/orthanc/label_studies.py`

### Step 2. Set the DICOM path in `config.toml` (not in compose)

The Orthanc `/dicom-data` bind mount is **not** hardcoded in `docker-compose.yml`.
Its source comes from `config.toml`: `scripts/orthanc/dc.sh` reads `[storage].mode`
and exports the matching path (`legacy_dicom_root` in `legacy` mode,
`hot_cache_dir` in `cold_path_cache` mode) as `DICOM_MOUNT_SOURCE`. So on a new
server you set the path **once**, in `config.toml`:

```toml
[storage]
mode = "legacy"                       # or "cold_path_cache"
legacy_dicom_root = "/your/dicom/path"
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
  the web-app-owned tables (`users`, `annotations`, `label_definitions`,
  `user_preferences`, snapshots).
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

**3c. Create the upstream table schema in `stanford-stroke`.** The DDL lives in
`ssc-sql-db/` (table definitions only — no data, each script `\connect`s to the
DB):

```bash
# Run from stanford-stroke-pacs/ (ssc-sql-db/ lives at the repo root, one level up):
psql -d stanford-stroke -f ../ssc-sql-db/create_patient.sql
psql -d stanford-stroke -f ../ssc-sql-db/create_image_study.sql
psql -d stanford-stroke -f ../ssc-sql-db/create_image_series.sql
# Optional, site-specific clinical side-table (load via ssc-sql-db/import_lvo_table_to_psql.sh):
#   creates public.lvo_clinical_data
```

> The **web-app-owned** tables are **not** created here — `web-app/app.py` runs
> Alembic `upgrade head` automatically at first startup (Step 8) and creates
> them, including a `CREATE TABLE IF NOT EXISTS patient` safety net (revision
> `0006`). Running 3c first is still recommended so the upstream spine exists
> before you load data.

**3d. Load the upstream data.** Creating the tables does **not** populate them.
Load your real `patient` / `image_study` / `image_series` rows from your source
(CSV import via `ssc-sql-db/import_csv_to_postgres.py`, a dump from an existing
system, or the site-specific `image_integration_protocols/` pipeline). The web
app browses these tables, so it will be empty until they are populated.

### Step 4. Create the first admin user and the Orthanc service account

End users authenticate to Web App (which proxies OHIF/DICOMweb to Orthanc).
Admins additionally need a direct Orthanc login so they can reach Orthanc
Explorer 2 on `:8042`. The service account is the credential Web App uses
internally when proxying.

```bash
# 1. Create the first admin (DB + orthanc_users.json):
python scripts/admin/manage_users.py add <username> --admin

# 2. Set the Orthanc service-account password (.env + orthanc_users.json):
python scripts/admin/manage_users.py rotate-service-account

# 3. (optional) confirm .env and orthanc_users.json agree:
python scripts/admin/manage_users.py check-service-account
```

What this step accomplishes:

- ensures `users` exists in the research/app DB
- inserts the admin user with a bcrypt password hash
- mirrors the admin entry into `orthanc_users.json`
- writes the service-account credential into `orthanc_users.json` and `.env`

Why this matters before first start:

- Orthanc auth is enabled and depends on `orthanc_users.json` existing with at
  least the service-account entry — without it, Web App's proxy and host-local
  scripts will get 401s from Orthanc.

### Step 5. Start Orthanc (Docker)

Run:

```bash
scripts/orthanc/dc.sh up -d
```

Use the `dc.sh` wrapper, not bare `docker compose`: it resolves the DICOM mount
from `config.toml` and (on macOS) applies the override. This starts
**`ssc-orthanc`** only. `docker-compose.yml` does not define a Web App container.

### Step 6. Install Web App Python dependencies

From the repo root (use your preferred env, e.g. conda `pacs`):

```bash
python3 -m pip install -r web-app/requirements.txt
```

### Step 7. Build the web app frontend

```bash
cd web-app && npm ci && npm run build
```

### Step 8. Install and start the web app + timers (systemd)

The units ship as **templates** (`systemd/*.in`) with `__TOKENS__` for the
per-host bits. The installer resolves user/repo/python automatically (override in
`deploy.env`), renders the templates into `/etc/systemd/system/`, and enables the
web app plus the backup/reconciliation/health timers:

```bash
scripts/linux/install_systemd.sh --dry-run    # preview the rendered units
sudo scripts/linux/install_systemd.sh         # render + install + enable
```

This replaces the old `sudo cp systemd/ssc-web-app.service …` step (and the
separate backup-timer copy in §9) — one command installs everything. The dormant
`cold-archive-mirror.timer` is left disabled by default.

### Step 9. Wait for Orthanc indexing

Orthanc's Folder Indexer scans `/dicom-data` on startup and then periodically.

Depending on dataset size, this may take significant time.

Useful checks while indexing:

```bash
docker compose logs -f orthanc
curl -s -u <user>:<pass> http://localhost:8042/statistics | python3 -m json.tool
```

### Step 10. Run post-index tasks as needed

There are two optional-but-useful post-index tasks.

#### Option A. Display enrichment in Orthanc

Run:

```bash
python scripts/orthanc/enrich_orthanc.py
```

Use this when:

- the DICOM headers are anonymized or not operator-friendly
- you want OE2 to display `patient_id` (mapped to Patient ID/Name),
  `seriesdescription` (mapped to Series Description), and `studydescription`
  from `image_study` (mapped to Study Description) in the source metadata

Skip this when:

- the DICOM headers already contain acceptable values for Orthanc/OE2
- you do not want to mutate Orthanc's PostgreSQL index tables
- you only need indexing, OHIF, or the web app workflow

#### Option B. Pre-seed Orthanc study labels

Run:

```bash
python scripts/orthanc/label_studies.py
```

Use this when:

- `image_study` provides `study_type` and `image_series` provides `modality`
- you want those values available immediately as Orthanc study labels in OE2

This script is idempotent and safe to re-run after new studies are indexed.

---

## 6. Validation checklist

After startup, validate each layer separately.

### 6.1 Container and API checks

Basic checks:

```bash
docker compose ps
docker compose logs -f orthanc
```

Orthanc system and statistics:

```bash
curl -s -u <user>:<pass> http://localhost:8042/system | python3 -m json.tool
curl -s -u <user>:<pass> http://localhost:8042/statistics | python3 -m json.tool
```

Web App service:

```bash
sudo systemctl status ssc-web-app
```

Web App read APIs:

```bash
curl -s http://localhost:8043/api/labels | python3 -m json.tool
curl -s 'http://localhost:8043/api/series?per_page=5' | python3 -m json.tool
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
- `scripts/data_integrity/reconcile.py` uses `ORTHANC_ADMIN_USER` /
  `ORTHANC_ADMIN_PASSWORD`

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

If the source metadata table is present, run:

```bash
python scripts/data_integrity/reconcile.py
```

This compares `SeriesInstanceUID` values between:

- `image_series`
- Orthanc's indexed series reported via REST API

It is the most useful repo-provided verification that indexing actually matches
the expected metadata inventory.

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

The repo's `scripts/connectivity/tunnel.sh` forwards all three ports (`8042`, `8043`, `4242`).

---

## 8. Developer setup

For contributors who want to run the test suite and linters locally.

### Prerequisites

- Python 3.12+ with the `pacs` conda environment
- Node.js 20+ and npm
- A local PostgreSQL instance (the test suite creates a scratch DB)

### One-time setup

From the repo root:

```bash
conda activate pacs
make install-dev
```

This installs all Python deps (runtime + dev), runs `npm ci` for the frontend,
and installs pre-commit hooks so linting runs automatically before each commit.

### Running tests

```bash
make test          # backend + frontend
make test-backend  # pytest only (requires Postgres)
make test-frontend # vitest only (no Postgres needed)
```

### Running linters

```bash
make lint          # ruff check
```

### CI

Every push to `main` and every PR triggers GitHub Actions CI (`.github/workflows/ci.yml`). Required jobs: lint, backend-tests, frontend-tests, frontend-build. The mypy job is advisory (non-blocking).

Pre-commit hooks run locally before each commit if installed via `make install-dev` (or `pre-commit install`).

---

## 9. Ongoing operations

Common actions after deployment:

Add a user:

```bash
python scripts/admin/manage_users.py add <username>
docker restart ssc-orthanc
```

Change a user password:

```bash
python scripts/admin/manage_users.py passwd <username>
docker restart ssc-orthanc
```

If the changed user is the Orthanc service account used by the web app:

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

`image_integration_protocols/`

- legacy Stanford Stroke Center–specific metadata ingestion and correction
  pipeline
- not required just to deploy PACS services
- only relevant if recreating the same upstream metadata-generation workflow

---

## 11. Known repo caveats

These are current implementation mismatches worth remembering during deployment:

- `scripts/admin/teardown.sh` is destructive and should not be used casually; it does not
  stop the web app systemd service
- `scripts/admin/teardown.sh` sources `.env` from two levels above the repo root (`../../.env`),
  **not** the repo-root `.env` used by web app and helper scripts
- bring the stack up with `scripts/orthanc/dc.sh`, not bare `docker compose` — the
  DICOM mount source comes from `config.toml` via the wrapper, so bare
  `docker compose up` errors that `DICOM_MOUNT_SOURCE` is unset
- `scripts/orthanc/check_status.sh` uses the `ssc-orthanc` container name and reads Orthanc
  credentials from repo-root `.env`

Treat these as current repo caveats, not as recommended design patterns.

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
   columns across the schema to the new prefixes, and reset `cache_state` to cold.
5. **Bring-up + web app + service units** — return here for §5 Steps 5–8
   (`scripts/orthanc/dc.sh up -d`, web app deps/build, the unit installer).
6. **Verify** — `cluster_migration.md` §4 (`reconcile_migration.py`, then
   `reconcile.py`) instead of, or in addition to, §6 here.

The service layer (Orthanc container, web app, systemd/launchd units, the
`dc.sh` wrapper) is **identical** to a fresh install — only the database
provisioning differs (restore vs. create-and-ingest).
