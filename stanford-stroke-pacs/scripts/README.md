# Scripts

Operational and diagnostic scripts for the SSC PACS stack. Run from
`stanford-stroke-pacs/` unless noted otherwise.

All Python scripts expect the `pacs` conda environment and a valid `.env` file
at the repo root.

---

## Directory layout

| Directory | Purpose | Key scripts |
|---|---|---|
| `admin/` | User provisioning, label removal, teardown | `manage_users.py`, `remove_label.py`, `teardown.sh` |
| `backup/` | PostgreSQL dump and freshness monitoring | `backup_pg_db.sh`, `check_backup_freshness.sh` |
| `cold_storage/` | Archive, cleanup, health, mirror | `archive_all_series.py`, `cleanup_loose_dicoms.py`, `cold_storage_health.py` |
| `connectivity/` | SSH tunnel helper | `tunnel.sh` |
| `data_integrity/` | Two-DB reconciliation, filesystem audit | `reconcile.py`, `dicom_path_sql_fs_audit.py` |
| `dicom/` | DICOM conversion utilities | `dicom_to_nifti.py` |
| `one_off/` | Migration helpers, one-time test utilities | `backfill_annotation_history.py`, `orthanc_holdout_case.py` |
| `orthanc/` | Orthanc enrichment, labelling, status check | `enrich_orthanc.py`, `label_studies.py`, `check_status.sh` |

---

## Quick reference

```bash
# User management
python scripts/admin/manage_users.py list
python scripts/admin/manage_users.py add <user> [--admin]

# Two-DB reconciliation
python scripts/data_integrity/reconcile.py
python scripts/data_integrity/reconcile.py --json

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
