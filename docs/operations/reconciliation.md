# Two-DB reconciliation

The PACS has two PostgreSQL databases that must stay in sync but have no
enforced referential integrity:

- **`orthanc_db`** — owned by Orthanc; indexes DICOM files on disk.
- **`stanford-stroke`** — owned by Web App; `image_series` tracks research
  metadata including `dicom_dir_path` and `dicom_archive_path`.

The reconciliation job diffs the two sources and surfaces drift.  It is a
**read-only observer** — it never mutates either database or the filesystem.

Orthanc-side series UIDs are read directly from `orthanc_db` (one read-only
query on `maindicomtags`, seconds on a 100k-series index) — the one sanctioned
read of that database outside Orthanc itself.  If that connection fails, the
job falls back to REST enumeration (`GET /series/{id}` per series), which is
functionally identical but takes tens of minutes at scale.

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

> **Fail-fast:** importing the reconciliation module (via the `db` import
> chain) raises immediately if `ORTHANC_ADMIN_USER` / `ORTHANC_ADMIN_PASSWORD`
> are unset in `.env`. If `reconcile.py` errors out before doing any work,
> check those credentials first.

---

## On-demand only (no scheduled job)

Reconciliation is **not scheduled** — run it by hand with the commands above
when you want a fresh report (typically right after an ingestion run or a
storage migration). The fresh-deploy installers
(`scripts/macos/install_launchd.sh`, `scripts/linux/install_systemd.sh`) do
**not** install any reconciliation timer or LaunchDaemon, and there are no
`deploy/systemd/reconciliation.*` / `deploy/launchd/com.ssc.reconciliation.plist.in`
templates.

It used to run every 6 hours, but that was removed: unattended, it provided no
value because nobody read the reports. It is also the kind of job you want to
run deliberately, not while an ingestion is in flight: the read pass is careful
to release its `image_series` lock before the disk scan (so it cannot block
ingestion), but a concurrent run still competes for disk I/O. (The run is much
cheaper than it used to be — series UIDs now come from one `orthanc_db` query
instead of ~100k REST calls, and the per-series loose-file `stat()` check was
removed — but the archive-existence pass still stats every archive on cold
storage.)

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
- A per-case scoped registration (`POST /indexer/scan`) failed or was
  interrupted (e.g. Orthanc OOM-killed mid-scan), so the series was never
  registered. There is **no continuous Folder Indexer scan** (`Folders: []`),
  so it will not self-heal — it must be re-registered explicitly.
- Loose DICOMs were deleted before the series was ever registered.
- In `cold_path_cache` mode: series is cold (files evicted) and the patched
  Folder Indexer removed the index entry.  This should not happen with
  `RemoveMissingFiles: false` — investigate the Orthanc config.

**Remediation.** Warming does **not** fix this — warm/evict is index-neutral
(with `RemoveMissingFiles: false`, extracting files adds no index rows). The
fix is to **re-register** the series with the patched indexer's scoped scan,
in bounded passes:

```bash
# Confirm the archive/dir the series expects
psql -d stanford-stroke -c \
  "SELECT dicom_dir_path, dicom_archive_path FROM image_series
   WHERE seriesinstanceuid = '<uid>';"

# Backfill every missing series (bounded passes; safe, idempotent)
python scripts/cold_storage/reindex_missing_series.py --execute
#   or a targeted list:
python scripts/cold_storage/scoped_index.py --series <uid1,uid2>

# Re-check — in_db_not_in_orthanc should drop
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

### `dicom_dir_missing` (removed)

Earlier versions also checked that `dicom_dir_path` exists on disk. The check
was removed: in `cold_path_cache` mode evicted series legitimately have no
loose files (the archive is canonical), so it flagged every cold series as a
mismatch while costing one `stat()` per series on the data disk. To spot-check
whether a specific series has loose files, cross-reference
`series_cache_state` (`cold` = no loose files expected).

### `dicom_archive_missing`

The `dicom_archive_path` column points to an archive file that does not exist
on disk.

**Common causes:**
- Archive was accidentally deleted or moved.
- Compression failed partway during ingestion.  The ingest pipeline
  (`image_ingestion_protocols/`) is the biggest source of NULL or broken
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

### `orphaned_annotations`

An `annotations` row whose entity no longer exists in the patient / study /
series spine — i.e. a `patient`, `image_study`, or `image_series` key that was
deleted (or never ingested) out from under a label. Detected by
`reconcile.py` (`orphaned_annotations`) and **included in `total_mismatches`**,
so a non-zero count here fails a "clean" reconciliation the same as an index
drift.

**Common causes:**
- Upstream re-ingestion deleted and recreated rows with different keys.
- An annotation was written against an entity that was later removed.

**Investigation:**
```bash
# The JSON report lists the offending annotation_id / entity_id under
# mismatches.orphaned_annotations:
python scripts/data_integrity/reconcile.py --json
jq '.mismatches.orphaned_annotations' \
  maintenance/reconciliation-reports/<latest>.json
```

Resolve by re-ingesting the missing entity (if it should exist) or deleting
the stale annotation via the API / a manual `DELETE` (if the entity is gone
for good).

---

## Prometheus metrics

The reconciliation run updates these gauges:

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `reconciliation_mismatches_total` | gauge | `category` (incl. `orphaned_annotations`) | Count per mismatch category |
| `reconciliation_last_run_timestamp` | gauge | — | Unix epoch of last run |
| `reconciliation_duration_seconds` | gauge | — | Duration of last run |

> **These are CLI-process-local.** `reconcile.py` calls
> `update_reconciliation_metrics` inside its **own** process registry and
> then exits, so the values die with the process. The web app runs in a
> **separate process** and never calls it — the web app `/metrics` endpoint
> therefore shows these gauges as **0 / absent**, not the real last-run
> values. To surface them in Prometheus you would need to wire the web app to
> read the latest JSON report at scrape time (not currently implemented).

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
