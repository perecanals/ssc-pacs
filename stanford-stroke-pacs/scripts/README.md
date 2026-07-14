# Scripts

Operational and diagnostic scripts for the SSC PACS stack. Run from
`stanford-stroke-pacs/` unless noted otherwise.

All Python scripts expect the `ssc-pacs` conda environment and a valid `.env`
file at the stack root (`stanford-stroke-pacs/.env`).

**CLI polarity convention:** anything that mutates DB/disk/index is a dry-run
by default and applies only with `--execute`; interactive prompts have a
`--yes` bypass; report-only tools take no polarity flag. Deliberately exempt
(idempotent/additive by design, stated in each docstring):
`backfill_storage_sizes.py`, `manage_users.py` (interactive),
`dicom_to_nifti.py` (writes only its output).

---

## Directory layout

| Directory | Purpose | Key scripts |
|---|---|---|
| `admin/` | User provisioning, credential rotation, label/dataset ops, series classification, teardown | `manage_users.py`, `rotate_service_account.py`, `rotate_db_password.py`, `bulk_set_label_values.py`, `remove_label.py`, `rename_dataset_value.py`, `reclassify_series_types.py`, `teardown.sh` |
| `backup/` | PostgreSQL dump, Orthanc volume snapshot, freshness monitoring | `backup_pg_db.sh`, `backup_orthanc_storage.sh` (+ in-container `orthanc_storage_snapshot.py`), `check_backup_freshness.sh` |
| `cold_storage/` | Archive, cleanup, health, cache state, index repair | `archive_all_series.py`, `cleanup_loose_dicoms.py`, `scoped_index.py`, `reindex_missing_series.py`, `prune_stale_index_paths.py`, `rebuild_cache_state.py`, `cold_storage_health.py`, `backfill_storage_sizes.py`, `list_unarchived_series.py`, `verify_and_repair_archives.py`, `mirror_cold_archive.sh` |
| `connectivity/` | Sanitized SSH tunnel templates for end users (per OS) | `tunnel/{linux,macos,windows}/tunnel.*` |
| `data_integrity/` | Cross-store audits + repairs (see matrix below) | `reconcile.py`, `dicom_path_sql_fs_audit.py`, `disk_vs_db_series_audit.py`, `detect_mixed_dirs.py`, `repair_dicomweb_metadata_cache.py` |
| `dicom/` | DICOM conversion utilities | `dicom_to_nifti.py` |
| `linux/` | Linux deploy path (systemd units) + whole-stack control | `install_systemd.sh`, `stop_stack.sh`, `start_stack.sh` |
| `macos/` | macOS host tooling (Colima, launchd, disks) + whole-stack control | `colima_start.sh`, `colima_watchdog.sh`, `install_launchd.sh`, `stop_stack.sh`, `start_stack.sh` |
| `migration/` | Port the stack to a new host | `repoint_host_paths.py`, `reconcile_migration.py` |
| `orthanc/` | Compose wrapper, status check | `dc.sh`, `check_status.sh` |

`_lib.sh` holds the shared shell helpers (`STACK_DIR`, `config_get`,
`deploy_env_get`, `resolve_python`) sourced by the shell scripts.

Site-specific one-offs (past incidents/migrations, hardcoded paths) are **not**
part of the repo — they live in the gitignored `maintenance/scripts/` at the
workspace root (see its `README.md` index; `attic/` holds retired tools
superseded by current ones).

## Data-integrity audit matrix

Four complementary tools — different directions and cost classes, deliberately
**not** merged (e.g. folding the FS→DB walk into `reconcile.py` would drag an
hours-long tree scan into the cron/JSON-report path):

| Tool | Direction | Cost | Question it answers |
|---|---|---|---|
| `data_integrity/reconcile.py` | DB ↔ Orthanc (+ DB-recorded paths exist, + annotations vs spine tables) | cheap; cron-able, feeds admin endpoint/metrics | Does everything in `image_series` exist in Orthanc's index (and vice versa), and does every annotation still have its entity? |
| `data_integrity/dicom_path_sql_fs_audit.py` | SQL → FS (sampled) | cheap | Do the paths recorded in SQL exist / look right on disk? |
| `data_integrity/disk_vs_db_series_audit.py` | FS → DB (full walk, first file per dir) | expensive (hours) | Is there imaging on disk that `image_series` doesn't know about, or with drifted slice counts? |
| `data_integrity/detect_mixed_dirs.py` | FS deep (every file header) | very expensive, targeted | Does one physical DICOM dir hold more than one true series? |
| `data_integrity/repair_dicomweb_metadata_cache.py` | Orthanc index → Orthanc (repairs, not just reports) | cheap to report; `--execute` re-extracts each affected series | Which series did Orthanc cache empty WADO-RS metadata for (OHIF spins forever, HTTP 400)? Rebuilds them: warm → rebuild → evict. See `docs/cold_storage/runbook.md`. |

---

## Quick reference

```bash
# User management
python scripts/admin/manage_users.py list
python scripts/admin/manage_users.py add <user> [--admin] [--datasets 'A,B']

# Credential rotation (prompts for the new secret; --generate mints + prints one)
python scripts/admin/rotate_service_account.py rotate   # Orthanc svc acct: .env + orthanc_users.json
python scripts/admin/rotate_service_account.py check    # verify the two agree
python scripts/admin/rotate_db_password.py rotate       # DB_PASSWORD: ALTER ROLE + .env
python scripts/admin/rotate_db_password.py check        # verify .env authenticates

# Rename a dataset cohort tag everywhere (patient + user grants + mirror)
python scripts/admin/rename_dataset_value.py --from-value old --to-value new [--execute]

# Bulk-set label values from CSV/Excel (dry-run by default)
python scripts/admin/bulk_set_label_values.py --file x.csv --level series \
    --id-column seriesinstanceuid --value-column v --label mylabel [--execute]

# Recompute machine series_type / study_type from series_dicom_tags (dry-run by
# default; prints a confusion report + the unresolved residue). Reads the tag
# table, not the archives — safe to re-run whenever the lexicons change.
python scripts/admin/reclassify_series_types.py [--label sir_batch1] [--execute]

# Two-DB reconciliation
python scripts/data_integrity/reconcile.py
python scripts/data_integrity/reconcile.py --json

# Repoint host paths after a port (dry-run by default; --apply to commit)
python scripts/migration/repoint_host_paths.py
python scripts/migration/repoint_host_paths.py --apply

# Post-migration reconciliation (run on the target host after a port)
python scripts/migration/reconcile_migration.py

# Cold storage (mutators are dry-run by default; add --execute to apply)
python scripts/cold_storage/archive_all_series.py            # preview; --execute to archive
python scripts/cold_storage/cleanup_loose_dicoms.py           # preview; --execute to delete
python scripts/cold_storage/rebuild_cache_state.py            # preview; --execute to write
python scripts/cold_storage/cold_storage_health.py --json     # read-only probe

# DICOM processing
python scripts/dicom/dicom_to_nifti.py --series-uid <uid> --warm-if-cold

# Backup
./scripts/backup/backup_pg_db.sh stanford-stroke
./scripts/backup/check_backup_freshness.sh
```
