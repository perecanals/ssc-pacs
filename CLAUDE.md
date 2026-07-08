# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Never run `sudo` commands yourself** — there is no terminal to enter the password, so they always fail. Ask the user to run them (suggest the `! <command>` prefix so the output lands in the session).

## Versioning and commits

- **Commits**: `area: imperative summary` (e.g. `web-app: fix healthz version`,
  `ingestion: path-safety guards`). Group logical changes; no drive-by edits.
- **Small fixes** are commit + service restart, nothing more — production may
  legitimately run a few commits past the last tag (`git describe --tags`
  shows e.g. `v1.2-3-g<sha>`).
- **Cut a release** (tag `vX.Y` + 2–3 plain lines in root `CHANGELOG.md`) when
  a change is changelog-worthy: includes a DB migration, is a user-visible
  feature/behavior change, is a state you'd want to roll back to, or enough
  small fixes accumulated to name a checkpoint. Bump `X` only for scary
  upgrades (irreversible migration, storage-mode change); otherwise bump `Y`.
- Release = `git tag vX.Y && git push --tags` + changelog entry, done. No
  other tooling. Mention any included Alembic migration in the entry.

## Repository layout

```
ssc-pacs/                     # git checkout root (Makefile, CI, root scripts)
├── stanford-stroke-pacs/     # stack root — .env, config.toml, orthanc.json, docker-compose.yml live HERE
│   ├── web-app/              # FastAPI backend + React frontend (port 8043); alembic/ = migrations
│   ├── scripts/              # utility scripts (see scripts/README.md); _lib.sh = shared helpers
│   │   ├── admin/            # manage_users.py, rename_dataset_value.py, bulk_set_label_values.*, remove_label.*, teardown.sh
│   │   ├── backup/           # backup_pg_db.sh, backup_orthanc_storage.sh, check_backup_freshness.sh
│   │   ├── cold_storage/     # archive, cleanup, health, scoped_index, reindex_missing_series, prune_stale_index_paths, verify_and_repair
│   │   ├── connectivity/     # tunnel/{linux,macos,windows}/
│   │   ├── data_integrity/   # reconcile.py, dicom_path_sql_fs_audit.py, disk_vs_db_series_audit.py, detect_mixed_dirs.py
│   │   ├── dicom/            # dicom_to_nifti.py
│   │   ├── orthanc/          # dc.sh, enrich_orthanc.py, label_studies.py, check_status.sh
│   │   ├── linux/ · macos/   # install_systemd.sh · install_launchd.sh, colima + mount helpers
│   │   └── migration/        # reconcile_migration.py
│   ├── systemd/ · launchd/   # service unit templates (*.in), rendered by the installers
│   ├── documentation/        # Modular docs — start with documentation/context.md
│   ├── orthanc_users.json    # Service account + admin users only (managed by manage_users.py — never edit manually)
│   └── image_ingestion_protocols/  # Legacy SSC metadata ingestion (site-specific)
├── ssc-sql-db/               # upstream SQL table definitions + CSV import helpers
├── maintenance/              # gitignored local workspace (one-off scripts, benchmarks, attic, audit)
├── CHANGELOG.md              # release log (created at v1.0)
├── Makefile                  # test / lint / install-dev targets (run from here)
└── requirements.txt          # Python deps for the ssc-sql-db import helpers
```

## Services and ports

Production runs on **macOS via launchd** (`com.ssc.*` LaunchDaemons); the repo also ships Linux `systemd` unit templates (`systemd/*.in`) for portability.

| Service | How it runs (production) | Port |
|---------|--------------------------|------|
| Orthanc (`ssc-orthanc`) | Docker (`ssc-orthanc:patched-indexer`) | HTTP `8042`, DICOM `4242` |
| Web App | launchd `com.ssc.webapp`, uvicorn (Linux: `ssc-web-app.service`) | HTTP `8043` |
| PostgreSQL | host `com.ssc.postgres` (Homebrew) | from `.env` |

User-facing URLs (via SSH tunnel or localhost):
- `http://localhost:8042/ui/app/` — Orthanc Explorer 2
- `http://localhost:8042/ohif/` — OHIF viewer
- `http://localhost:8043/` — Web App landing page
- `http://localhost:8043/app/` — Web App app

## Common commands

Daily drivers below; the full day-2 sheet is `documentation/operations/commands.md`. Script paths are relative to the **stack root** (`stanford-stroke-pacs/`); `make` runs from the **checkout root** (`ssc-pacs/`).

### Orthanc (Docker)
```bash
scripts/orthanc/dc.sh up -d       # wrapper: resolves the DICOM mount + macOS override.
scripts/orthanc/dc.sh down        # Use instead of bare `docker compose`, which errors on
                                  # the unset DICOM_MOUNT_SOURCE guard.
scripts/orthanc/check_status.sh   # status, API, plugins
```

### Web App (launchd — production)
```bash
sudo launchctl kickstart -k system/com.ssc.webapp   # restart (Linux: sudo systemctl restart ssc-web-app)
tail -f ~/Library/Logs/ssc-web-app.log              # logs (.err = stderr)
```

### Web App development (hot-reload)
```bash
conda activate ssc-pacs
cd web-app && uvicorn app:app --port 8043 --reload   # FastAPI backend
cd web-app && npm run dev                             # Vite dev server (proxies /api → :8043); browse :5173
```

### Testing and linting (from the checkout root, inside the ssc-pacs env)
```bash
make install-dev   # one-time: Python + Node dev deps + pre-commit hooks
make test          # backend (pytest) + frontend (vitest) + ingestion (pytest, DB-free)
make test-backend  # pytest only (needs local Postgres)
make lint          # ruff (web-app + scripts + ingestion) + eslint (npm run lint)
```
Rebuild frontend: `cd web-app && npm run build`, then restart the web app.

### User management
```bash
python scripts/admin/manage_users.py list                    # incl. dataset grants
python scripts/admin/manage_users.py add <user> [--admin] [--datasets 'PRECISE,CRISP2/LVO']
python scripts/admin/manage_users.py passwd <user>
python scripts/admin/manage_users.py set-datasets <user> <csv|--all|--none>
python scripts/admin/manage_users.py rotate-service-account  # + check-service-account verifies .env ↔ JSON
# Restart Orthanc only when orthanc_users.json changed (admin edits / rotation): docker restart ssc-orthanc
```

### Reconciliation & schema migrations
```bash
python scripts/data_integrity/reconcile.py            # image_series vs Orthanc summary (--json writes a report)
cd web-app && alembic current && alembic upgrade head # migrations (also run at app startup; stanford-stroke DB only)
```

## Architecture

Two services and two databases. Full topology + request/ingest flows: `documentation/reference/system_overview.md`; dual-DB rationale + auth model: `documentation/reference/architecture.md`.

**Orthanc** indexes the on-disk DICOM tree (read-only bind mount) into `orthanc_db` and serves Orthanc Explorer 2, OHIF, and DICOMweb. It does not own the DICOM files. The deployment runs a **custom `ssc-orthanc:patched-indexer` image** whose Folder Indexer honours `RemoveMissingFiles: false` (required for cold_path_cache; source in `orthanc-indexer-patched/`). Steady-state `orthanc.json` has `Folders: []` — indexing is per-case `POST /indexer/scan`, not a continuous tree scan.

**Web App** is a FastAPI app that reads research metadata from the `stanford-stroke` PostgreSQL DB and stores multi-level annotations, serving a Vite + Tailwind React frontend. In production one uvicorn process on `:8043` serves both the API and the pre-built `web-app/dist/`; Node is build-time only.

**Backend modules** (`web-app/`): `app.py` (lifespan pool+migrations, middleware, router registration); `db.py` (SSOT for `DB_CONFIG` + pool + `audit_user_var`); `auth.py` (JWT); `orthanc_client.py`; `common.py` (SQL builders, annotation helpers); `config.py` (config.toml); `cache_manager.py` (cold-storage warm/evict); `reconciliation.py`; `rate_limit.py`; `dataset_access.py`; `labelled_table_sync.py`; `metrics.py`; `routes/` (auth, preferences, studies, cold_storage, annotations, labels, admin, static, proxy). Frontend detail: `documentation/reference/web_app_frontend.md`.

**Two-database model:**
- `orthanc_db` — Orthanc's internal index; do not query/mutate except explicit enrichment. (Sanctioned exception: reconciliation bulk-reads series UIDs read-only via `PG_ORTHANC_*` creds — one query instead of ~100k REST calls.)
- `stanford-stroke` — upstream read-only tables (`patient`, `image_study`, `image_series`, clinical side-table `lvo_clinical_data`) plus web-app-owned tables (`annotations`, `annotations_history`, `label_definitions`, `label_value_options`, `users`, `user_preferences`, `series_cache_state`, `*_labelled` mirrors). Connection from `.env` (`DB_HOST/PORT/NAME/USER/PASSWORD`); web-app-owned tables are Alembic-migrated at startup.
- `patient` is the **patient-level spine** (one row per patient, ingest-populated). `lvo_clinical_data` is retired as a roster — joined only to prefer its clinical `stroke_date` via `COALESCE(c.stroke_date, p.stroke_date)`, never otherwise queried.

**Annotation model:** three levels (`patient`/`study`/`series`); annotations are shared (one value per entity+label; `created_by` = last editor); parent-level values inherit downward; cross-level filtering is supported; every write is captured in `annotations_history` by a PL/pgSQL trigger (`documentation/operations/annotation_history.md`).

**Storage modes** (`config.toml [storage].mode`):
- `legacy` — the Folder Indexer reads loose DICOMs from `dicom_data_root`.
- `cold_path_cache` (production) — canonical series are `*.tar.zst` archives under `cold_archive_root`. Warm extracts them back to the original `dicom_dir_path`; the patched index keeps pointing there even when files are absent, so OHIF works the moment files return. Evict deletes the extracted files; the index stays. **Requires the patched image + `"RemoveMissingFiles": false`.** See `documentation/cold_storage/`.

**Auth:**
- End users live in the PostgreSQL `users` table (bcrypt, SSOT); login returns an HttpOnly JWT cookie.
- **Per-user dataset access**: `users.allowed_datasets text[]` gates the `patient.dataset` cohorts a non-admin may see (deny-by-default: empty = no data; admins bypass). Enforced on every patient-data endpoint + the DICOMweb proxy (`web-app/dataset_access.py`). See architecture.md §5.4.
- The Web App reverse-proxies `/ohif/*` and `/dicom-web/*` to Orthanc (`routes/proxy.py`, async `httpx`) with the service-account Basic auth from `.env`; end users never present Orthanc credentials.
- `orthanc_users.json` holds only the service account + admin users; managed by `scripts/admin/manage_users.py` (`rotate-service-account` rewrites `.env` + JSON atomically).

## Image ingestion protocol

New imaging data is ingested via `stanford-stroke-pacs/image_ingestion_protocols/` (site-specific to SSC layout/metadata; not part of a fresh deployment):
```bash
cd stanford-stroke-pacs/image_ingestion_protocols
cp execute_image_ingestion_protocol.example.yaml execute_image_ingestion_protocol.yaml  # then edit (gitignored)
conda activate ssc-pacs
python execute_image_ingestion_protocol.py [--config path/to/config.yaml]
```
YAML keys, ingestion steps, and per-mode behavior: `documentation/reference/image_ingestion_protocol.md`. `src_dir` is required; storage roots come from `config.toml` (leave `cold_archive_root` unset in the YAML). The NIfTI-conversion and study-type-prediction steps are kept but dormant by design.

## Cold storage

Production runs `cold_path_cache` (migration from `legacy` complete, validated in OHIF). Cache state is keyed **per series** in `series_cache_state` (study/patient status is a derived aggregate; warm/evict are index-neutral). Verify live coverage rather than trusting a figure here:
```bash
psql -d stanford-stroke -c "SELECT count(*) FILTER (WHERE dicom_archive_path IS NOT NULL) archived, count(*) total FROM image_series;"
```
Depends on the `ssc-orthanc:patched-indexer` image + `"RemoveMissingFiles": false`. Design, runbook, and current status: `documentation/cold_storage/`.

## Key caveats

- `docker-compose.yml` uses `env_file: .env` (relative to the compose file); `stanford-stroke-pacs/.env` must exist.
- The stack depends on the custom `ssc-orthanc:patched-indexer` image; build it on the host before `dc.sh up`.
- `scripts/admin/teardown.sh` is destructive; it resolves `.env` and the compose dir from the stack root (`$SCRIPT_DIR/../..`) and is confirmation-guarded — use with care.
- `orthanc_users.json` must never be edited manually; always use `scripts/admin/manage_users.py`.
- `image_ingestion_protocols/` is the legacy SSC-specific ingestion pipeline; not part of a standard fresh deployment.

## Documentation index

All docs live under `stanford-stroke-pacs/documentation/`; start with `documentation/context.md` for the topic map. Most-used:

- `reference/system_overview.md` — end-to-end depiction of the whole stack
- `reference/architecture.md` — topology, dual-DB rationale, auth model
- `reference/data_stores.md` — table schemas and query patterns
- `reference/image_ingestion_protocol.md` — ingesting new data, YAML config, per-mode behavior
- `operations/commands.md` — day-2 operations cheat sheet
- `operations/schema_migrations.md` — Alembic workflow + production-stamp procedure
- `operations/reconciliation.md` — two-DB reconciliation, mismatch categories, admin endpoint
- `cold_storage/` — cold-storage design + runbook
