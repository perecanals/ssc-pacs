# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repository layout

```
pacs/
├── stanford-stroke-pacs/     # Main PACS stack (Orthanc + Web App)
│   ├── web-app/            # FastAPI backend + React frontend (port 8043)
│   ├── scripts/              # Organized utility scripts (see subdirectory layout below)
│   │   ├── admin/            # manage_users.py, remove_label.py/.sh, teardown.sh
│   │   ├── backup/           # backup_pg_db.sh, check_backup_freshness.sh
│   │   ├── cold_storage/     # archive, cleanup, health, mirror, unarchived, verify_and_repair
│   │   ├── connectivity/     # tunnel.sh
│   │   ├── data_integrity/   # reconcile.py, dicom_path_sql_fs_audit.py
│   │   ├── dicom/            # dicom_to_nifti.py
│   │   ├── one_off/          # backfill, holdout, path_availability_test
│   │   └── orthanc/          # enrich_orthanc.py, label_studies.py, check_status.sh
│   ├── documentation/        # Modular docs — start with documentation/context.md
│   ├── image_integration_protocols/  # Legacy SSC metadata ingestion (site-specific)
│   ├── .env                  # Secrets (DB creds, JWT, Orthanc admin password)
│   ├── config.toml           # Non-secret config (storage mode, paths, session TTL)
│   ├── docker-compose.yml    # Orthanc only (Web App is NOT a Compose service)
│   ├── orthanc.json          # Orthanc structural config
│   └── orthanc_users.json    # Service account + admin users only (managed by scripts/admin/manage_users.py — never edit manually)
├── ssc-sql-db/               # SQL table definitions and CSV import helpers
└── requirements.txt          # Python deps for root-level scripts
```

## Services and ports

| Service | How it runs | Port |
|---------|-------------|------|
| Orthanc (`ssc-orthanc`) | Docker | HTTP `8042`, DICOM `4242` |
| Web App | Native systemd (`ssc-web-app.service`), uvicorn | HTTP `8043` |
| PostgreSQL | Host (not in this repo) | from `.env` |

User-facing URLs (via SSH tunnel or localhost):
- `http://localhost:8042/ui/app/` — Orthanc Explorer 2
- `http://localhost:8042/ohif/` — OHIF viewer
- `http://localhost:8043/` — Web App landing page
- `http://localhost:8043/app/` — Web App app

## Common commands

Run from `stanford-stroke-pacs/` unless noted.

### Orthanc (Docker)
```bash
docker compose up -d
docker compose down
scripts/orthanc/check_status.sh   # status, API, plugins
```

### Web App (systemd)
```bash
sudo systemctl restart ssc-web-app
sudo systemctl status ssc-web-app
sudo journalctl -u ssc-web-app -f
```

### Web App development (hot-reload)
```bash
# Terminal 1 — FastAPI backend
conda activate pacs
cd web-app && uvicorn app:app --port 8043 --reload

# Terminal 2 — Vite dev server (proxies /api to :8043)
cd web-app && npm run dev
# Browse at http://localhost:5173
```

### Testing and linting (run from repo root)
```bash
make install-dev               # one-time: install Python+Node dev deps + pre-commit hooks
make test                      # run all tests (backend + frontend)
make test-backend              # pytest only (needs local Postgres)
make test-frontend             # vitest only
make lint                      # ruff check on web-app/
```

### Rebuild frontend
```bash
cd web-app && npm run build
sudo systemctl restart ssc-web-app
```

### User management
```bash
python scripts/admin/manage_users.py list
python scripts/admin/manage_users.py add <user> [--admin]   # --admin also mirrors into orthanc_users.json
python scripts/admin/manage_users.py passwd <user>
python scripts/admin/manage_users.py remove <user>
python scripts/admin/manage_users.py rotate-service-account # rotates Orthanc service account (.env + orthanc_users.json)
# Restart Orthanc only when orthanc_users.json was touched (admin user changes or service-account rotation):
docker restart ssc-orthanc
```

### Indexing / enrichment
```bash
python scripts/orthanc/enrich_orthanc.py   # rewrite patient/study/series labels in OE2
python scripts/orthanc/label_studies.py    # seed Orthanc study labels from study_type + modality
```

### Two-DB reconciliation
```bash
python scripts/data_integrity/reconcile.py               # human-readable summary (image_series vs Orthanc)
python scripts/data_integrity/reconcile.py --json        # write JSON report to maintenance/reconciliation-reports/
python scripts/data_integrity/reconcile.py --json --quiet # JSON only, no stdout (cron/timer mode)
```

### Schema migrations (`stanford-stroke` DB only)
```bash
cd web-app && conda activate pacs
alembic current                  # show current revision
alembic upgrade head             # apply pending revisions (also runs at app startup)
alembic revision -m "<message>"  # scaffold a new revision
```
Workflow + production-stamp procedure: `documentation/operations/schema_migrations.md`. Do **not** run Alembic against `orthanc_db`.

### Cold storage (cold_path_cache mode)
```bash
# Check archiving progress
python scripts/cold_storage/archive_all_series.py --dry-run

# Run archiver (parallel, pick worker count to taste)
conda activate pacs
python scripts/cold_storage/archive_all_series.py --workers 4

# Check archive coverage in DB
psql -d stanford-stroke -c "
  SELECT COUNT(*) FILTER (WHERE dicom_archive_path IS NOT NULL) AS archived,
         COUNT(*) AS total FROM image_series;"

# Manually warm a study (Python — no CLI yet)
conda activate pacs
cd web-app
python3 -c "from cache_manager import warm_study; print(warm_study('<uid>'))"

# Check cache status
curl http://localhost:8043/api/studies/<uid>/cache-status

# Warm via API — returns 202 immediately; extraction runs in the
# bounded app.state.warm_executor pool (max_workers from [storage].warm_workers).
# Poll cache-status until status == "hot" to know when files are restored.
curl -X POST -b cookies.txt http://localhost:8043/api/studies/<uid>/warm

# Manually evict a study (CLI or API)
python3 -c "from cache_manager import evict_study; print(evict_study('<uid>'))"
curl -X POST -b cookies.txt http://localhost:8043/api/studies/<uid>/evict

# Clean up loose DICOMs that are safe to remove (archive exists + Orthanc has indexed)
python scripts/cold_storage/cleanup_loose_dicoms.py                  # dry-run
python scripts/cold_storage/cleanup_loose_dicoms.py --execute        # apply
python scripts/cold_storage/cleanup_loose_dicoms.py --patient 4-0551 # limit to one patient

# Triage series with loose files but no archive (compression failures)
python scripts/cold_storage/list_unarchived_series.py --count
python scripts/cold_storage/list_unarchived_series.py --patient 4-0551
python scripts/cold_storage/archive_all_series.py --patient 4-0551   # idempotent retry

# On-demand DICOM → NIFTI (cold_path_cache mode skips auto-NIFTI generation)
python scripts/dicom/dicom_to_nifti.py --dir /path/to/DICOM
python scripts/dicom/dicom_to_nifti.py --archive /path/to/DICOM.tar.zst --out /tmp/x.nii.gz
python scripts/dicom/dicom_to_nifti.py --series-uid <uid> --warm-if-cold
```

### Patched Orthanc image (cold storage)

Cold storage depends on a custom Orthanc image with a patched Folder Indexer
plugin. Source: `/home/perecanals/pacs/orthanc-indexer-patched/`.

```bash
# Rebuild after editing the patch
cd /home/perecanals/pacs/orthanc-indexer-patched
docker build -t ssc-orthanc:patched-indexer .

# Deploy (swap into docker-compose.yml and restart Orthanc)
cd /home/perecanals/pacs/stanford-stroke-pacs
docker compose down && docker compose up -d
docker logs ssc-orthanc | grep RemoveMissingFiles   # should print the patch's startup banner
```

## Architecture

The stack has two services and two databases.

**Orthanc** indexes the on-disk DICOM tree (read-only bind mount) into `orthanc_db`. It serves Orthanc Explorer 2, OHIF, and DICOMweb. It does not own the DICOM files. The deployment runs a **custom Orthanc image (`ssc-orthanc:patched-indexer`)** with a patched Folder Indexer plugin that honours `RemoveMissingFiles: false` — required for cold_path_cache. Source at `/home/perecanals/pacs/orthanc-indexer-patched/`.

**Web App** is a FastAPI app that reads research metadata from the `stanford-stroke` PostgreSQL database and stores multi-level annotations. It serves a React frontend built with Vite + Tailwind CSS. In production, a single uvicorn process on `:8043` serves both the API and the pre-built `web-app/dist/`. Node.js is only needed at build time.

**Web App backend modules** (under `web-app/`):
- `app.py` — entry point: lifespan (pool + migrations), middleware (sliding JWT, request-ID/metrics), rate limiter, router registration (~230 lines).
- `db.py` — **single source of truth** for `DB_CONFIG` and the `ThreadedConnectionPool`. All modules import `get_conn` from here. Also exposes `audit_user_var` (contextvar) — when set by middleware, `get_conn()` auto-sets `SET LOCAL app.audit_user` so the annotation audit trigger can attribute changes.
- `auth.py` — JWT utilities (`create_jwt`, `decode_jwt`, `get_current_user`).
- `orthanc_client.py` — thin wrappers around Orthanc REST calls.
- `common.py` — shared SQL builders (`build_label_filter_sql`), annotation helpers, constants.
- `config.py` — loads `config.toml` (storage, web app settings).
- `cache_manager.py` — cold-storage warm/evict logic.
- `reconciliation.py` — two-DB reconciliation: compares `image_series` vs Orthanc index + disk path checks.
- `routes/` — `APIRouter` submodules: `auth`, `preferences`, `studies`, `cold_storage`, `annotations`, `labels`, `admin`, `static`.

**Two-database model:**
- `orthanc_db` — Orthanc's internal index; do not query or mutate unless doing explicit Orthanc enrichment work.
- `stanford-stroke` — upstream read-only tables (`patient`, `image_study`, `image_series`, and the clinical side-table `lvo_clinical_data`) plus web-app-owned tables (`annotations`, `annotations_history`, `label_definitions`, `users`, `user_preferences`, snapshot tables). Connection from `.env`: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`.
  - `patient` is the **patient-level spine** (one row per patient, comprehensive, populated by the ingest pipeline). `lvo_clinical_data` is no longer the patient roster — it's joined only to prefer its clinical `stroke_date` when a patient is matched (`COALESCE(c.stroke_date, p.stroke_date)`). Patient-level upstream DDL lives in `ssc-sql-db/create_patient.sql`; a `CREATE TABLE IF NOT EXISTS` bootstrap is also in Alembic revision `0006`.

**web-app-owned tables** are created/migrated by `web-app/app.py` on startup (via Alembic).

**Annotation model:**
- Three levels: `patient`, `study`, `series`.
- Annotations are shared (one value per entity+label across all users; `created_by` tracks last editor).
- Parent-level annotations inherit downward to child rows in API responses.
- Cross-level filtering: e.g., filter patients by a series-level label.
- **Audit trail:** every INSERT/UPDATE/DELETE on `annotations` is captured in `annotations_history` by a PL/pgSQL trigger. See `documentation/operations/annotation_history.md`.

**Storage modes** (set in `config.toml` `[storage].mode`):
- `legacy` — Orthanc Folder Indexer reads loose DICOM files from `legacy_dicom_root`.
- `cold_path_cache` — canonical series are `*.tar.zst` archives under `cold_archive_root`. On warm, archives are extracted back to the **original** `dicom_dir_path` recorded in `image_series`. Orthanc's index (via the patched indexer) keeps pointing at those paths even when files are absent, so OHIF works immediately once files are restored — no re-ingestion. Eviction deletes the extracted files; the index stays intact. **Requires `ssc-orthanc:patched-indexer` image and `"RemoveMissingFiles": false` in `orthanc.json`.** See `documentation/cold_storage/`.

**Auth:**
- End users live in the PostgreSQL `users` table (bcrypt) — single source of truth. Web App login returns an HttpOnly JWT cookie.
- Web App reverse-proxies `/ohif/*` and `/dicom-web/*` to Orthanc (`web-app/routes/proxy.py`, async `httpx`) and attaches the service-account Basic auth from `.env` on every upstream call. End users never present credentials to Orthanc.
- `orthanc_users.json` holds only the service account + admin users (`users.is_admin=True`). Admins can reach `:8042/ui/app/` and `:8042/ohif/` directly as themselves.
- `scripts/admin/manage_users.py` is the single provisioning tool: regular `add`/`passwd`/`remove` touch the DB (and mirror admin entries into the JSON); `rotate-service-account` rewrites `.env`'s `ORTHANC_ADMIN_PASSWORD` and the JSON service-account entry atomically.

## Web App frontend layout

```
web-app/src/
  App.jsx                  React Router (/ and /app)
  api/client.js            Fetch wrapper with 401 handling
  context/                 AuthContext
  pages/                   Landing, Web App (page-level layout + preview state)
  utils/
    colors.js              Shared color palette (NOTION_COLORS, hashStr, valueColor)
    table.js               Table constants (LEVEL_CONFIG), formatters, filter helpers
  components/
    DataTable/             Core: hierarchical patient→study→series table (split)
      index.jsx            Orchestrator — wires hooks, renders main body
      ChildRows.jsx        Child + grandchild expandable row rendering
      TableHeader.jsx      Column headers, sort carets, filter inputs
      SelectFilterControl.jsx  Dropdown filter for select-type columns
      useTableData.js      Data fetch hook (infinite-scroll accumulation, label filters)
      usePreferencePersistence.js  Debounced server-side pref save
      useDragColumns.js    Column drag-and-drop reorder
      actions.js           DICOM download, OHIF link, refresh actions
    PreviewPane.jsx        Embedded OHIF iframe
    TopBar.jsx             Level switcher, column selector, Reset View, auth
    Sidebar.jsx            Search, label filters, modality quick filters
    InlineEdit.jsx         In-table annotation editing
    ColumnSelector.jsx     Column visibility/order control
    LabelDefModal.jsx      Label definition creation UI
```

The `DataTable` is one generic component used at all three hierarchy levels. Table preferences (column visibility, order, sort, filters, frozen column) are persisted per user and per level in `user_preferences` (server-side JSONB). All components use `prop-types` for runtime prop validation.

## Image integration protocol

New imaging data is ingested via `stanford-stroke-pacs/image_integration_protocols/`:

```bash
cd stanford-stroke-pacs/image_integration_protocols
# Edit execute_image_integration_protocol.yaml, then:
conda activate pacs
python execute_image_integration_protocol.py [--config path/to/config.yaml]
```

**YAML config keys** (`execute_image_integration_protocol.yaml`):
- `src_dir` — directory of patient subdirectories to ingest
- `import_label` — tag all rows in this run with a label
- `dataset` — optional cohort tag recorded on the `patient` table only (`dataset text[]`, union-accumulated across batches)
- `overwrite_if_exists` — re-integrate studies already in the DB
- `anonymize_files` — strip patient identifiers from DICOM headers before copying
- `delete_originals_after_verification` — remove the source `case_dir` after copy verification
- `cold_archive_root` — if set, compress each series DICOM dir to `*.tar.zst` at this root after copying (supports `cold_path_cache` mode). Loose files are **not deleted** — they remain for the Orthanc Folder Indexer to pick up.

**Integration steps** (in order):
1. Scan source dirs for readable DICOM series
2. Group into studies, validate against `lvo_clinical_data`
3. Copy DICOMs to `legacy_dicom_root/{patient_id}/{StudyUID}/{SeriesDesc}/{SeriesUID}/DICOM/`
4. If `cold_archive_root` set: compress each series dir to `cold_archive_root/.../DICOM.tar.zst`, record path in `image_series.dicom_archive_path`
5. Convert select series to NIfTI
6. Upsert into `image_series`, `image_study`, and `patient` (one transaction). The `patient` upsert recomputes `stroke_date = MIN(image_study.acquisitiondatetime)`, preserves origin `import_id`/`import_label`, and unions the `dataset` tag.

`image_integration_protocols/` is site-specific (SSC directory layout and metadata conventions). It is not part of a standard fresh deployment.

## Cold storage status (as of 2026-04-10)

Migration from `legacy` to `cold_path_cache` is **complete and validated end-to-end in OHIF**.

**Current state:**
- `config.toml` mode is `cold_path_cache`
- Archive coverage: 13,801 / 13,801 series have `dicom_archive_path` populated
- Orthanc index: 13,861 series in `ssc-orthanc:patched-indexer` with `"RemoveMissingFiles": false`
- Loose DICOMs moved to `/DATA2/pacs_imaging_data_loose_backup` (not yet deleted — kept as safety net)
- `/DATA2/pacs_imaging_data/` is empty; warm extracts archives back on demand
- Warm/evict/re-warm cycle works transparently in the UI (click a row → "Warming imaging cache…" spinner → OHIF loads)

**Why a custom Orthanc image was required:** the upstream Folder Indexer removes
missing files from Orthanc's index on every scan — incompatible with a cold
storage workflow where files legitimately come and go. The fork adds a
`RemoveMissingFiles` config flag that skips the removal step. See
[orthanc-indexer-patched/README.md](orthanc-indexer-patched/README.md).

**Remaining task:**
- Once satisfied with production behavior, delete the backup: `rm -rf /DATA2/pacs_imaging_data_loose_backup`
- Set `eviction_ttl_hours` in `config.toml` to a production value (e.g. 24) and restart web app

**Archive format:** files are stored flat at the archive root (matching `scripts/cold_storage/archive_all_series.py` convention — no `DICOM/` subdirectory wrapper).

**Post-ingestion workflow:** after `image_integration_protocol` runs, loose DICOMs for the new studies are present at their `dicom_dir_path`. The patched Folder Indexer picks them up on its next scan (within `Interval` seconds) and keeps them in the index indefinitely even after they're moved/compressed. No Orthanc restart required for routine ingestion.

## Key caveats

- `docker-compose.yml` uses `env_file: .env` (relative to the compose file). `stanford-stroke-pacs/.env` must exist.
- The stack depends on the custom `ssc-orthanc:patched-indexer` image; it must be built on the host before `docker compose up`.
- `scripts/admin/teardown.sh` is destructive and sources `.env` from two levels above the repo root (`../../.env`), not the repo-root `.env` — use with care.
- `orthanc_users.json` must not be edited manually; always use `scripts/admin/manage_users.py`.
- `image_integration_protocols/` is the legacy SSC-specific metadata ingestion pipeline; it is not part of a standard fresh deployment.

## Documentation index

All canonical docs are under `stanford-stroke-pacs/documentation/`. Start with `documentation/context.md` for a topic map. Key references:

- `documentation/reference/system_overview.md` — end-to-end depiction of the whole stack (Web App + Orthanc + OHIF + cold storage + the two PostgreSQL DBs)
- `documentation/reference/architecture.md` — full topology, data flow, auth model
- `documentation/reference/data_stores.md` — all table schemas and query patterns
- `documentation/reference/web_app.md` — Web App product rationale and features
- `documentation/reference/web_app_frontend.md` — React component detail
- `documentation/reference/image_integration_protocol.md` — ingesting new data, YAML config, per-mode behavior
- `documentation/operations/commands.md` — day-2 operations cheat sheet
- `documentation/operations/reconciliation.md` — two-DB reconciliation (image_series vs Orthanc), mismatch categories, admin endpoint
- `documentation/operations/annotation_history.md` — annotation audit trail: trigger, session-variable coupling, history API, backfill, retention
- `documentation/cold_storage/` — cold storage design and runbook
- `documentation/recipes/dicom_processing.md` — DICOM → NIFTI, archive inspection, cleanup scripts
