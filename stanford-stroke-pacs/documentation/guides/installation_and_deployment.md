# Installation and Deployment

**Purpose:** Fresh-server runbook only. For packaging facts and config file roles see [`../reference/runtime_and_config.md`](../reference/runtime_and_config.md). For architecture see [`../reference/architecture.md`](../reference/architecture.md).

This document is the operator runbook for deploying the PACS stack on a fresh
server.

It assumes the target deployment wants the same overall architecture:

- Orthanc + OE2 + OHIF in Docker (**Orthanc only** in `docker-compose.yml`)
- Web App app as a **native systemd service** on port `8043` (not Docker)
- PostgreSQL on the host
- DICOM files kept on disk and indexed read-only (or hot cache when using cold storage)

---

## 1. What must already exist

Before using this repo on another server, decide whether the new environment
already has the source metadata layer that the web app app expects.

Minimum required inputs:

- a Linux host
- Docker Engine with `docker compose`
- PostgreSQL reachable from the host
- `python3` and `pip`
- Node.js and npm (for building the web app frontend)
- a DICOM directory tree on disk
- a metadata table equivalent to `image_series` (and `image_study` for study
  metadata) if you want to use the web app app and the metadata-driven helper
  scripts

Important:

- this repo does **not** build `image_series` / `image_study` as part of
  standard PACS deployment
- `image_integration_protocols/` is legacy/site-specific pipeline code, not the
  normal bootstrap path for a fresh PACS install

---

## 2. Host prerequisites

The host should satisfy all of the following:

- Linux host compatible with Docker host networking
- Docker daemon running
- Docker Compose plugin available
- PostgreSQL installed and running
- ability to execute `sudo -u postgres psql` for `init_orthanc_db.sh`
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

Non-secret web app tuning (storage paths, storage mode, session length) lives in repo-root `config.toml` (loaded by `web-app/config.py`).

---

## 4. Expected source metadata tables

If you want the full web app workflow, the source database should provide
tables compatible with `image_series` and `image_study`.

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

### Step 2. Confirm the DICOM mount path and `env_file` path

The current `docker-compose.yml` mounts this host path into Orthanc:

```text
/DATA2/pacs_imaging_data:/dicom-data:ro
```

On a new server you will likely need to edit `docker-compose.yml` so the left
side matches the real DICOM path on that machine.

Additionally, `docker-compose.yml` contains a hardcoded absolute `env_file` path
(currently `/home/perecanals/pacs/.env`). Update it to point to the `.env` file
in the repo root or the correct location for your deployment.

Requirements:

- the DICOM path must exist
- Orthanc only needs read access
- the dataset should already be organized on disk before startup

For cold storage / hot cache, follow [`../cold_storage/runbook.md`](../cold_storage/runbook.md) instead of the default legacy mount once you are ready.

### Step 3. Create the Orthanc PostgreSQL database and role

Run:

```bash
./init_orthanc_db.sh
```

What it does:

- sources the repo `.env`
- creates `PG_ORTHANC_USER` if missing
- creates `PG_ORTHANC_DB` if missing
- grants privileges on the Orthanc index database

Important caveat:

- the script assumes local PostgreSQL administration via `sudo -u postgres psql`
- it is not a generic remote-database provisioning script

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
docker compose up -d
```

This starts **`ssc-orthanc`** only. `docker-compose.yml` does not define a Web App container.

### Step 6. Install Web App Python dependencies

From the repo root (use your preferred env, e.g. conda `pacs`):

```bash
python3 -m pip install -r web-app/requirements.txt
```

### Step 7. Build the web app frontend

```bash
cd web-app && npm ci && npm run build
```

### Step 8. Install and start the web app (systemd)

```bash
sudo cp ssc-web-app.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ssc-web-app
```

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

Enable scheduled backups (one-time — do this on any real deployment). The stack
ships nightly jobs for **both** PostgreSQL databases **and** the Orthanc storage
volume — the latter holds OHIF-authored SR annotations (the **only copy**) plus
the Folder Indexer DB, so it is not optional:

```bash
sudo cp systemd/pg-backup-stanford-stroke.service systemd/pg-backup-stanford-stroke.timer \
        systemd/pg-backup-orthanc.service systemd/pg-backup-orthanc.timer \
        systemd/orthanc-storage-backup.service systemd/orthanc-storage-backup.timer \
        systemd/pg-backup-freshness.service systemd/pg-backup-freshness.timer \
        /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now \
    pg-backup-stanford-stroke.timer pg-backup-orthanc.timer \
    orthanc-storage-backup.timer pg-backup-freshness.timer
systemctl list-timers 'pg-backup-*' 'orthanc-storage-backup*'
```

Backups land in `/DATA2/pg_backups/`. Full mechanism, retention, and recovery:
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
- `docker-compose.yml` uses an absolute `env_file` path that must be updated
  for a fresh deployment on a different host
- `scripts/orthanc/check_status.sh` uses the `ssc-orthanc` container name and reads Orthanc
  credentials from repo-root `.env`

Treat these as current repo caveats, not as recommended design patterns.
