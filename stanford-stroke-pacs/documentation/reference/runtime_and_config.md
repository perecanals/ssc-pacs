# Runtime, packaging, and configuration

**Purpose:** How services run, which files hold config, ports, and operator-facing script inventory. For the map of *where every config value lives and what must stay in sync* see [`configuration_sources.md`](configuration_sources.md). For architecture narrative see [`architecture.md`](architecture.md). For install steps see [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md).

---

## Packaging summary

| Piece | How it runs | Ports |
|-------|-------------|-------|
| Orthanc | Docker (`ssc-orthanc`), host networking, custom image `ssc-orthanc:patched-indexer` | HTTP `8042`, DICOM `4242` |
| Web App | Native service, uvicorn — **launchd (`com.ssc.webapp`) on the current macOS production host; systemd (`ssc-web-app.service`) on Linux** | HTTP `8043` |
| PostgreSQL | Host server (not in this repo) | configured in `.env` |

Paths in this doc are relative to the **stack root** (`stanford-stroke-pacs/`,
where `config.toml`, `.env`, `orthanc.json`, and `docker-compose.yml` live)
unless a path is called out as relative to the git checkout root
(`/opt/ssc-pacs/ssc-pacs`, the Makefile home).

`docker-compose.yml` defines **Orthanc only**. The Web App is not a Compose service.

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
  they reach Orthanc via Web App's reverse proxy on `:8043`. Managed by
  `scripts/admin/manage_users.py`; not meant for manual editing, and should stay
  out of version control
- **`.env`**: PostgreSQL connection values passed into the container

### Enabled features (`orthanc.json`)

- HTTP server on `8042`
- DICOM server on `4242` (AET: `SSC`)
- remote access
- HTTP auth
- Folder Indexer with `ScanRoots: ["/dicom-data"]`, `Folders: []` (no
  continuous whole-tree scan), and `RemoveMissingFiles: false` so the index
  never drops files that temporarily disappear (required by `cold_path_cache`).
  New data is registered on demand per case via `POST /indexer/scan` from the
  ingestion executor — see [`image_ingestion_protocol.md`](image_ingestion_protocol.md)
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

The `/dicom-data` bind-mount **source** is not hardcoded in `docker-compose.yml`: it
comes from `config.toml` via `scripts/orthanc/dc.sh`, which exports
`[storage].dicom_data_root` (the uncompressed DICOM tree, used in both modes) as
`DICOM_MOUNT_SOURCE`. Bring the stack up through that wrapper.
On warm, compressed series archives are extracted back into the active tree — no
mount change is needed. See [`../cold_storage/runbook.md`](../cold_storage/runbook.md).

---

## Web App runtime

The web app runs natively on the host, managed by launchd (`com.ssc.webapp`)
in production on macOS, or by the `ssc-web-app.service` systemd unit on Linux:

- Python dependencies: the `ssc-pacs` conda env (or a venv); install from
  `web-app/requirements.txt`
- `uvicorn app:app --host 0.0.0.0 --port 8043`
- loads `.env` from the stack root via `python-dotenv` (the parent of `web-app/`)
- serves the React frontend from `web-app/dist/` (pre-built by Vite)
- non-secret tuning (storage paths, session length, `[backup]` settings) from stack-root **`config.toml`**
  via `web-app/config.py`

Frontend build:

- `cd web-app && npm install && npm run build`
- Node.js and npm are build-time only

Restart after code changes: `sudo launchctl kickstart -k system/com.ssc.webapp`
(macOS production) or `sudo systemctl restart ssc-web-app` (Linux). Platform
equivalents table: [`../guides/deployment_on_mac.md`](../guides/deployment_on_mac.md) §8.

---

## User and auth provisioning

`scripts/admin/manage_users.py` is the canonical tool for both user stores
(`users` in PostgreSQL + admin/service-account mirror in `orthanc_users.json`),
including `rotate-service-account` and `check-service-account`. The full auth
model, runtime split, and provisioning flow are canonical in
[`architecture.md`](architecture.md) §5.3.

---

## Deployment order (high level)

1. Host prerequisites: Docker, PostgreSQL, Python, Node/npm for builds, DICOM tree
2. Create `.env`; set storage mode + paths in `config.toml`
3. `python3 -m pip install -r requirements.txt` (root-level script deps; the
   `requirements.txt` lives at the git checkout root, one level above the stack root)
4. `./init_orthanc_db.sh`
5. `python scripts/admin/manage_users.py add <user> --admin` + `rotate-service-account`
6. `scripts/orthanc/dc.sh up -d` (Orthanc — wrapper resolves the DICOM mount from
   `config.toml` and applies the macOS override automatically)
7. `pip install -r web-app/requirements.txt`, `cd web-app && npm install && npm run build`
8. Install the service units from templates: `sudo scripts/linux/install_systemd.sh`
   (macOS: `sudo scripts/macos/install_launchd.sh`) — enables the web app + timers
9. Wait for Orthanc indexing; optionally `scripts/orthanc/enrich_orthanc.py` / `scripts/orthanc/label_studies.py`
10. Validate (see [`../guides/installation_and_deployment.md`](../guides/installation_and_deployment.md))

---

## Helper scripts

### Reusable operational scripts

| File | Purpose |
|------|---------|
| `scripts/admin/manage_users.py` | Manage Web App users in PostgreSQL; mirror admin entries into `orthanc_users.json`; rotate **and verify** the Orthanc service account |
| `scripts/orthanc/dc.sh` | `docker compose` wrapper: resolves the DICOM mount from `config.toml`, selects the macOS override; use instead of bare `docker compose` |
| `scripts/linux/install_systemd.sh` | Render `systemd/*.in` templates for this host (auto-derived identity; `deploy.env` overrides) and install/enable the units |
| `scripts/macos/install_launchd.sh` | Same, for the macOS `launchd/*.plist.in` daemons |
| `init_orthanc_db.sh` | Create the Orthanc PostgreSQL role and database; idempotent; sources `.env` |
| `scripts/orthanc/check_status.sh` | Orthanc-focused status check for container, REST API, and plugin endpoints |
| `scripts/connectivity/tunnel/{linux,macos,windows}/` | Per-platform SSH tunnel helpers for remote access (`tunnel.sh` / `tunnel.command` / `tunnel.cmd`) |

### Data- and installation-specific scripts

| File | Purpose | Portability |
|------|---------|-------------|
| `scripts/orthanc/enrich_orthanc.py` | Mutates Orthanc's PostgreSQL index so OE2 shows identifiers from source metadata | Optional; skip if DICOM headers are already usable |
| `scripts/orthanc/label_studies.py` | Seeds Orthanc study labels from `study_type` + `modality` | Portable if columns exist |
| `web-app/labelled_table_sync.py` | Helpers for maintaining per-level labelled mirror tables | Imported by `web-app/routes/labels.py` and `scripts/admin/remove_label.py` |
| `scripts/admin/remove_label.py` | Remove a label definition + annotation rows from DB | |
| `scripts/admin/bulk_set_label_values.py` | Bulk-set annotation values from a CSV/Excel table; creates the label on demand | Requires `openpyxl` for `.xlsx` |
| `image_ingestion_protocols/` | Legacy metadata pipeline | Not part of standard fresh deploy |

---

## Known caveats

- **`scripts/admin/teardown.sh`** is destructive — full caveat (incl. its now-corrected `.env` source) is canonical in [`architecture.md`](architecture.md) §8.
- **`docker-compose.yml`** uses a relative `env_file: .env` which resolves to `stanford-stroke-pacs/.env`. That file must exist.
- **Custom Orthanc image** — the stack references `ssc-orthanc:patched-indexer` which is not on a registry. Fresh deployments must build it locally first (`cd orthanc-indexer-patched && docker build -t ssc-orthanc:patched-indexer .`) before `docker compose up`.

---

## Diagnostic and testing scripts (`scripts/`)

| File | Purpose |
|------|---------|
| `scripts/cold_storage/archive_all_series.py` | Offline archiver: compress all series to `tar.zst` for cold storage |
| `scripts/data_integrity/dicom_path_sql_fs_audit.py` | Read-only audit comparing `image_series.dicom_dir_path` to actual filesystem |
| `maintenance/scripts/orthanc_path_availability_test.py` (checkout-root `maintenance/`, gitignored) | Verify Orthanc can serve instances when their files are present/absent on disk |
| `maintenance/scripts/orthanc_holdout_case.py` (checkout-root `maintenance/`, gitignored) | Temporarily hide/restore cases from the DICOM tree for manual OHIF testing |

(`maintenance/` is a gitignored workspace at the git checkout root, one level
above the stack root — not under `stanford-stroke-pacs/`.)

---

## Repository layout (documentation-relevant)

```text
stanford-stroke-pacs/
├── .env                          # Local secrets and connection settings
├── .env.example                  # Template for .env (secrets only)
├── config.toml                   # Non-secret paths, storage mode, session length, backup settings (REQUIRED)
├── deploy.env.example            # Per-host service-unit identity overrides (copy → deploy.env, gitignored)
├── orthanc_users.json            # Service account + admin users only (managed by manage_users.py)
├── docker-compose.yml            # Orthanc only (Linux base; DICOM mount via ${DICOM_MOUNT_SOURCE})
├── docker-compose.override.macos.yml  # macOS deltas, selected by scripts/orthanc/dc.sh
├── orthanc.json                  # Orthanc structural config
├── web-app/
│   ├── app.py                    # FastAPI backend
│   ├── cache_manager.py          # Cold storage warm/eviction
│   ├── labelled_table_sync.py    # Per-level labelled mirror table helpers
│   ├── reconciliation.py         # image_series vs Orthanc index reconciliation
│   ├── dataset_access.py         # Per-user dataset scopes + TTL caches (proxy guard)
│   ├── rate_limit.py             # Request rate limiter
│   ├── config.py                 # Loads config.toml
│   ├── alembic/                  # Schema migrations (versions/) — run at startup
│   ├── requirements.txt
│   ├── build.sh                  # npm run build helper
│   └── src/                      # React frontend
├── init_orthanc_db.sh
├── scripts/                      # Organized into subdirectories
│   ├── admin/                    # manage_users, rename_dataset_value, backfill_annotation_history, teardown
│   ├── backup/                   # backup_pg_db, check_backup_freshness
│   ├── cold_storage/             # archive, cleanup, scoped_index, reindex_missing_series, health, mirror
│   ├── connectivity/             # tunnel
│   ├── data_integrity/           # reconcile, dicom_path_sql_fs_audit, disk_vs_db_series_audit, detect_mixed_dirs
│   ├── dicom/                    # dicom_to_nifti
│   ├── linux/                    # install_systemd.sh (renders systemd/*.in)
│   ├── macos/                    # colima_*, install_launchd.sh (renders launchd/*.plist.in)
│   └── orthanc/                  # enrich_orthanc, label_studies, check_status, dc.sh
├── systemd/                      # systemd unit TEMPLATES (*.in) — rendered by scripts/linux/install_systemd.sh
├── launchd/                      # macOS LaunchDaemon TEMPLATES (*.plist.in) — rendered by scripts/macos/install_launchd.sh
├── image_ingestion_protocols/  # Legacy metadata pipeline
├── documentation/
│   ├── context.md
│   ├── reference/
│   ├── guides/
│   ├── operations/
│   ├── cold_storage/
│   └── history/
└── requirements.txt
```

For a fuller file tree of the web app frontend, see [`web_app.md`](web_app.md) §8.
