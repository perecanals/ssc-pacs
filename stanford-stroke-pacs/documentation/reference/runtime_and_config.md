# Runtime, packaging, and configuration

**Purpose:** How services run, which files hold config, ports, and operator-facing script inventory. For architecture narrative see [`architecture.md`](architecture.md). For install steps see [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md).

---

## Packaging summary

| Piece | How it runs | Ports |
|-------|-------------|-------|
| Orthanc | Docker (`ssc-orthanc`), host networking, custom image `ssc-orthanc:patched-indexer` | HTTP `8042`, DICOM `4242` |
| Companion | Native **systemd** (`ssc-companion.service`), uvicorn | HTTP `8043` |
| PostgreSQL | Host server (not in this repo) | configured in `.env` |

`docker-compose.yml` defines **Orthanc only**. The Companion is not a Compose service.

The Orthanc image is built locally from [`../../../orthanc-indexer-patched/`](../../../orthanc-indexer-patched/) —
a fork of the upstream Folder Indexer plugin with a `RemoveMissingFiles` config
flag required by `cold_path_cache`. See [`../cold_storage/design.md`](../cold_storage/design.md)
for the why, and [`../cold_storage/runbook.md`](../cold_storage/runbook.md#build-the-patched-orthanc-image)
for the build/deploy commands.

---

## Orthanc configuration

Orthanc configuration is split between versioned structural config and local
secret-bearing files:

- **`orthanc.json`**: structural runtime settings, plugin enablement, ports, and
  Folder Indexer config
- **`orthanc_users.json`**: deploy-time `RegisteredUsers` file holding only the
  Orthanc service account plus admin users. End users do not have entries here —
  they reach Orthanc via Companion's reverse proxy on `:8043`. Managed by
  `scripts/admin/manage_users.py`; not meant for manual editing, and should stay
  out of version control
- **`.env`**: PostgreSQL connection values passed into the container

### Enabled features (`orthanc.json`)

- HTTP server on `8042`
- DICOM server on `4242` (AET: `SSC`)
- remote access
- HTTP auth
- Folder Indexer scanning `/dicom-data` every 60 seconds, with
  `RemoveMissingFiles: false` so the scan never removes indexed files that
  temporarily disappear (required by `cold_path_cache`)
- DICOMweb at `/dicom-web/`
- OHIF at `/ohif`
- Orthanc Explorer 2 as the default UI at `/ui/app/`

Orthanc Explorer 2 is configured with study list, label editing, label counts,
and OHIF launch from the study list.

### Storage model

Orthanc is used as an indexer and viewer over an existing DICOM tree, not as the
primary owner of the DICOM files.

- source DICOM files remain on the host filesystem
- the mount into the container is read-only (for legacy layout)
- Orthanc keeps its internal bookkeeping in `orthanc_db`
- the named Docker volume `ssc-orthanc-storage` is still present because Orthanc
  uses `/var/lib/orthanc/db`, but image duplication is disabled (`ENABLE_STORAGE=false`)

In `cold_path_cache` mode the Docker mount stays on `/DATA2/pacs_imaging_data` (same as legacy).
On warm, compressed series archives are extracted back to their original `dicom_dir_path` — no
change to the mount is needed. See [`../cold_storage/runbook.md`](../cold_storage/runbook.md).

---

## Companion runtime

The companion runs natively on the host, managed by **`ssc-companion.service`**:

- Python dependencies: typically a conda env named `pacs` or a venv; install from
  `companion/requirements.txt`
- `uvicorn app:app --host 0.0.0.0 --port 8043`
- loads `.env` from the repo root via `python-dotenv` (the parent of `companion/`)
- serves the React frontend from `companion/dist/` (pre-built by Vite)
- non-secret tuning (storage paths, session length) from repo-root **`config.toml`**
  via `companion/config.py`

Frontend build:

- `cd companion && npm install && npm run build`
- Node.js and npm are build-time only

Restart after code changes: `sudo systemctl restart ssc-companion`.

---

## User and auth provisioning

**`scripts/admin/manage_users.py`** is the canonical tool for user management.

It:

- ensures `users` exists
- adds, updates, or removes bcrypt-backed users in PostgreSQL
- when the affected user has `is_admin=True`, also writes their plaintext
  credential into `orthanc_users.json` so admins can reach Orthanc directly
- provides a separate `rotate-service-account` subcommand that rotates the
  service-account password in both `.env` (`ORTHANC_ADMIN_PASSWORD`) and the
  matching entry in `orthanc_users.json`. It does not touch the DB.

Runtime split:

- End users authenticate to Companion (`:8043`) and reach OHIF and DICOMweb
  through Companion's reverse proxy (`/ohif/*`, `/dicom-web/*`). They have no
  direct credentials at Orthanc.
- Orthanc (`:8042`) is reachable directly only to admins (with their own
  `orthanc_users.json` entry) and to host-local scripts using the service
  account from `.env`.

---

## Deployment order (high level)

1. Host prerequisites: Docker, PostgreSQL, Python, Node/npm for builds, DICOM tree
2. Create `.env`
3. `python3 -m pip install -r requirements.txt`
4. Adjust `docker-compose.yml` DICOM bind mount if needed
5. `./init_orthanc_db.sh`
6. `python scripts/admin/manage_users.py add <user> --admin`
7. `docker compose up -d` (Orthanc)
8. `pip install -r companion/requirements.txt`, `cd companion && npm install && npm run build`
9. Install and enable **`ssc-companion.service`**
10. Wait for Orthanc indexing; optionally `scripts/orthanc/enrich_orthanc.py` / `scripts/orthanc/label_studies.py`
11. Validate (see [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md))

---

## Helper scripts

### Reusable operational scripts

| File | Purpose |
|------|---------|
| `scripts/admin/manage_users.py` | Manage Companion users in PostgreSQL; mirror admin entries into `orthanc_users.json`; rotate the Orthanc service account |
| `init_orthanc_db.sh` | Create the Orthanc PostgreSQL role and database; idempotent; sources `.env` |
| `scripts/orthanc/check_status.sh` | Orthanc-focused status check for container, REST API, and plugin endpoints |
| `scripts/connectivity/tunnel.sh` | SSH tunnel helper for remote access |

### Data- and installation-specific scripts

| File | Purpose | Portability |
|------|---------|-------------|
| `scripts/orthanc/enrich_orthanc.py` | Mutates Orthanc's PostgreSQL index so OE2 shows identifiers from source metadata | Optional; skip if DICOM headers are already usable |
| `scripts/orthanc/label_studies.py` | Seeds Orthanc study labels from `study_type` + `modality` | Portable if columns exist |
| `companion/labelled_table_sync.py` | Helpers for maintaining per-level labelled mirror tables | Imported by `companion/routes/labels.py` and `scripts/admin/remove_label.py` |
| `scripts/admin/remove_label.py` | Remove a label definition + annotation rows from DB | |
| `scripts/admin/bulk_set_label_values.py` | Bulk-set annotation values from a CSV/Excel table; creates the label on demand | Requires `openpyxl` for `.xlsx` |
| `image_integration_protocols/` | Legacy metadata pipeline | Not part of standard fresh deploy |

---

## Known caveats

- **`scripts/admin/teardown.sh`** is destructive (stops stack, removes volumes, drops Orthanc DB/role, edits `.env`). It does **not** stop the companion systemd service. It sources `.env` from two levels above the repo root (`../../.env`), **not** the repo-root `.env` used by everything else — use with care.
- **`docker-compose.yml`** uses a relative `env_file: .env` which resolves to `stanford-stroke-pacs/.env`. That file must exist.
- **Custom Orthanc image** — the stack references `ssc-orthanc:patched-indexer` which is not on a registry. Fresh deployments must build it locally first (`cd orthanc-indexer-patched && docker build -t ssc-orthanc:patched-indexer .`) before `docker compose up`.

---

## Diagnostic and testing scripts (`scripts/`)

| File | Purpose |
|------|---------|
| `scripts/cold_storage/archive_all_series.py` | Offline archiver: compress all series to `tar.zst` for cold storage |
| `scripts/data_integrity/dicom_path_sql_fs_audit.py` | Read-only audit comparing `image_series.dicom_dir_path` to actual filesystem |
| `scripts/one_off/orthanc_path_availability_test.py` | Verify Orthanc can serve instances when their files are present/absent on disk |
| `scripts/one_off/orthanc_holdout_case.py` | Temporarily hide/restore cases from the DICOM tree for manual OHIF testing |

---

## Repository layout (documentation-relevant)

```text
stanford-stroke-pacs/
├── .env                          # Local secrets and connection settings
├── .env.example                  # Template for .env (secrets only)
├── config.toml                   # Non-secret paths, storage mode, session length
├── orthanc_users.json            # Service account + admin users only (managed by manage_users.py)
├── docker-compose.yml            # Orthanc only
├── orthanc.json                  # Orthanc structural config
├── ssc-companion.service         # systemd unit for the companion
├── companion/
│   ├── app.py                    # FastAPI backend
│   ├── cache_manager.py          # Cold storage warm/eviction
│   ├── labelled_table_sync.py    # Per-level labelled mirror table helpers
│   ├── config.py                 # Loads config.toml
│   ├── requirements.txt
│   ├── build.sh                  # npm run build helper
│   └── src/                      # React frontend
├── init_orthanc_db.sh
├── scripts/                      # Organized into subdirectories
│   ├── admin/                    # manage_users, remove_label, teardown
│   ├── backup/                   # backup_pg_db, check_backup_freshness
│   ├── cold_storage/             # archive, cleanup, list_unarchived, health, mirror
│   ├── connectivity/             # tunnel
│   ├── data_integrity/           # reconcile, dicom_path_sql_fs_audit
│   ├── dicom/                    # dicom_to_nifti
│   ├── one_off/                  # backfill_annotation_history, orthanc_holdout_case, etc.
│   └── orthanc/                  # enrich_orthanc, label_studies, check_status
├── benchmarks/                   # Cold storage benchmarks
├── image_integration_protocols/  # Legacy metadata pipeline
├── documentation/
│   ├── context.md
│   ├── reference/
│   ├── guides/
│   ├── operations/
│   ├── cold_storage/
│   └── history/
└── requirements.txt
```

For a fuller file tree of the Companion frontend, see [`companion.md`](companion.md) §8.
