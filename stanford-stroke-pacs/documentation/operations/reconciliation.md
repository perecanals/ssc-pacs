# Two-DB reconciliation

The PACS has two PostgreSQL databases that must stay in sync but have no
enforced referential integrity:

- **`orthanc_db`** — owned by Orthanc; indexes DICOM files on disk.
- **`stanford-stroke`** — owned by Web App; `image_series` tracks research
  metadata including `dicom_dir_path` and `dicom_archive_path`.

The reconciliation job diffs the two sources and surfaces drift.  It is a
**read-only observer** — it never mutates either database or the filesystem.

---

## Running manually

```bash
cd stanford-stroke-pacs

# Human-readable summary
python scripts/data_integrity/reconcile.py

# JSON report (written to maintenance/reconciliation-reports/)
python scripts/data_integrity/reconcile.py --json

# JSON, no stdout (quiet mode)
python scripts/data_integrity/reconcile.py --json --quiet
```

---

## On-demand only (no scheduled job)

Reconciliation is **not scheduled** — run it by hand with the commands above
when you want a fresh report (typically right after an ingestion run or a
storage migration). The fresh-deploy installers
(`scripts/macos/install_launchd.sh`, `scripts/linux/install_systemd.sh`) do
**not** install any reconciliation timer or LaunchDaemon, and there are no
`systemd/reconciliation.*` / `launchd/com.ssc.reconciliation.plist.in`
templates.

It used to run every 6 hours, but that was removed: on the full dataset the run
is a 30–60 min storm of `stat()` calls over every series on cold storage, and
— unattended — it provided no value because nobody read the reports. It is also
the kind of job you want to run deliberately, not while an ingestion is in
flight: the read pass is now careful to release its `image_series` lock before
the disk scan (so it can no longer block ingestion), but a concurrent run still
competes for disk I/O.

---

## Admin API endpoint

```
GET /api/admin/reconciliation/latest
```

Returns the most recent JSON report.  Requires an authenticated admin user
(verified against `users.is_admin`).  Non-admin users receive 403.

Example:

```bash
curl -b cookies.txt http://localhost:8043/api/admin/reconciliation/latest | jq .summary
```

---

## Mismatch categories

### `in_db_not_in_orthanc`

A series exists in `image_series` but is not indexed by Orthanc.

**Common causes:**
- Loose DICOMs were deleted before Orthanc indexed them.
- Ingest pipeline (`image_integration_protocols/`) completed the DB insert
  but Orthanc's Folder Indexer hasn't scanned yet (wait for `Interval`
  seconds and re-check).
- In `cold_path_cache` mode: series is cold (files evicted) and the patched
  Folder Indexer removed the index entry.  This should not happen with
  `RemoveMissingFiles: false` — investigate the Orthanc config.

**Investigation:**
```bash
# Check if the archive exists
psql -d stanford-stroke -c \
  "SELECT dicom_dir_path, dicom_archive_path FROM image_series
   WHERE seriesinstanceuid = '<uid>';"

# If archive exists, warm the study
curl -X POST -b cookies.txt http://localhost:8043/api/studies/<study_uid>/warm

# Re-check after Orthanc re-scans
python scripts/data_integrity/reconcile.py | grep '<uid>'
```

### `in_orthanc_not_in_db`

A series is indexed by Orthanc but has no row in `image_series`.

**Common causes:**
- Manual DICOM upload via Orthanc Explorer that bypassed the ingest pipeline.
- The ingest pipeline inserted into Orthanc but the DB insert failed.

**Investigation:**
```bash
# Look up the series in Orthanc
curl -u admin http://localhost:8042/tools/lookup -d '<uid>' | jq .

# If the series should exist in the DB, create the row via the ingest
# pipeline or a manual INSERT.
```

### `dicom_dir_missing`

The `dicom_dir_path` column points to a directory that does not exist on disk.

**Common causes:**
- In `cold_path_cache` mode this is **expected** for cold (evicted) series.
  Cross-reference `series_cache_state` — if the row is `cold`, this is normal.
- Loose DICOMs were moved or deleted outside the application.

**Investigation:**
```bash
# Check series_cache_state
psql -d stanford-stroke -c \
  "SELECT cs.status, cs.last_accessed_at
   FROM image_series s
   LEFT JOIN series_cache_state cs ON cs.seriesinstanceuid = s.seriesinstanceuid
   WHERE s.seriesinstanceuid = '<uid>';"
```

In `cold_path_cache` mode, filter out cold series when interpreting this
category.  The reconciliation report includes the raw count; the operator
should subtract known-cold rows.

### `dicom_archive_missing`

The `dicom_archive_path` column points to an archive file that does not exist
on disk.

**Common causes:**
- Archive was accidentally deleted or moved.
- Compression failed partway during ingestion.  The ingest pipeline
  (`image_integration_protocols/`) is the biggest source of NULL or broken
  archive paths.  Retry with:
  ```bash
  python scripts/cold_storage/archive_all_series.py --patient <patient_id>
  ```

**Investigation:**
```bash
# List all unarchived series
python scripts/cold_storage/list_unarchived_series.py

# Retry archiving for a specific patient
python scripts/cold_storage/archive_all_series.py --patient <patient_id>
```

---

## Prometheus metrics

The reconciliation run updates these gauges (added in WS 06):

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `reconciliation_mismatches_total` | gauge | `category` | Count per mismatch category |
| `reconciliation_last_run_timestamp` | gauge | — | Unix epoch of last run |
| `reconciliation_duration_seconds` | gauge | — | Duration of last run |

These are refreshed by the CLI script on every run.  The web app
`/metrics` endpoint exposes them alongside the other application metrics.

---

## Report rotation

The CLI keeps the most recent 30 JSON reports in
`maintenance/reconciliation-reports/` and deletes older ones automatically.

---

## Related scripts

- `scripts/cold_storage/list_unarchived_series.py` — lists series with no archive
  (one dimension of the reconciliation check).
- `scripts/cold_storage/archive_all_series.py` — retries compression for specific
  patients.
- `scripts/cold_storage/cold_storage_health.py` — health probe for cold-storage
  subsystem (stuck warming, orphan dirs, disk free).
