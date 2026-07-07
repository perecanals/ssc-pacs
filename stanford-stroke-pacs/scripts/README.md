# Scripts

Operational and diagnostic scripts for the SSC PACS stack. Run from
`stanford-stroke-pacs/` unless noted otherwise.

All Python scripts expect the `pacs` conda environment and a valid `.env` file
at the repo root.

---

## Directory layout

| Directory | Purpose | Key scripts |
|---|---|---|
| `admin/` | User provisioning, label/dataset ops, teardown | `manage_users.py`, `rename_dataset_value.py`, `backfill_annotation_history.py`, `teardown.sh` |
| `backup/` | PostgreSQL dump and freshness monitoring | `backup_pg_db.sh`, `check_backup_freshness.sh` |
| `cold_storage/` | Archive, cleanup, health, index repair | `archive_all_series.py`, `cleanup_loose_dicoms.py`, `reindex_missing_series.py`, `cold_storage_health.py` |
| `connectivity/` | SSH tunnel helper | `tunnel.sh` |
| `data_integrity/` | Cross-store audits (see matrix below) | `reconcile.py`, `dicom_path_sql_fs_audit.py`, `disk_vs_db_series_audit.py`, `detect_mixed_dirs.py` |
| `dicom/` | DICOM conversion utilities | `dicom_to_nifti.py` |
| `migration/` | Port the stack to a new host (e.g. Linux→Mac) | `reconcile_migration.py` |
| `orthanc/` | Orthanc enrichment, labelling, status check | `enrich_orthanc.py`, `label_studies.py`, `check_status.sh` |

Site-specific one-off scripts (past incidents/migrations, hardcoded paths) are
**not** part of the repo — they live in the gitignored `maintenance/scripts/`
at the workspace root.

## Data-integrity audit matrix

Four complementary tools — different directions and cost classes, deliberately
**not** merged (e.g. folding the FS→DB walk into `reconcile.py` would drag an
hours-long tree scan into the cron/JSON-report path):

| Tool | Direction | Cost | Question it answers |
|---|---|---|---|
| `data_integrity/reconcile.py` | DB ↔ Orthanc (+ DB-recorded paths exist) | cheap; cron-able, feeds admin endpoint/metrics | Does everything in `image_series` exist in Orthanc's index, and vice versa? |
| `data_integrity/dicom_path_sql_fs_audit.py` | SQL → FS (sampled) | cheap | Do the paths recorded in SQL exist / look right on disk? |
| `data_integrity/disk_vs_db_series_audit.py` | FS → DB (full walk, first file per dir) | expensive (hours) | Is there imaging on disk that `image_series` doesn't know about, or with drifted slice counts? |
| `data_integrity/detect_mixed_dirs.py` | FS deep (every file header) | very expensive, targeted | Does one physical DICOM dir hold more than one true series? |

---

## Quick reference

```bash
# User management
python scripts/admin/manage_users.py list
python scripts/admin/manage_users.py add <user> [--admin]

# Two-DB reconciliation
python scripts/data_integrity/reconcile.py
python scripts/data_integrity/reconcile.py --json

# Post-migration reconciliation (run on the target host after a port)
python scripts/migration/reconcile_migration.py

# Cold storage
python scripts/cold_storage/archive_all_series.py --dry-run
python scripts/cold_storage/cleanup_loose_dicoms.py
python scripts/cold_storage/cold_storage_health.py --json

# DICOM processing
python scripts/dicom/dicom_to_nifti.py --series-uid <uid> --warm-if-cold

# Orthanc enrichment
python scripts/orthanc/enrich_orthanc.py
python scripts/orthanc/label_studies.py

# Backup
./scripts/backup/backup_pg_db.sh stanford-stroke
./scripts/backup/check_backup_freshness.sh
```
