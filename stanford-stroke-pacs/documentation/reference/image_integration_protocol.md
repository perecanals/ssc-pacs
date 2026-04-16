# Image integration protocol

**Purpose:** How new imaging data gets into the PACS. Explains the protocol's
inputs, steps, outputs, and how it interacts with each storage mode. For the
cold storage design see [`../cold_storage/design.md`](../cold_storage/design.md).

Code lives under `stanford-stroke-pacs/image_integration_protocols/`. This
pipeline is **site-specific** to the Stanford Stroke Center DICOM layout and
metadata conventions. It is not part of a standard fresh deployment.

---

## What it does

Takes a directory of per-patient source DICOMs, and for each case:

1. Discovers DICOM series under the case directory
2. Groups series into studies
3. Validates studies against `lvo_clinical_data` (clinical DB table)
4. Copies DICOMs to the canonical layout under `legacy_dicom_root`
5. Optionally compresses each series to a `*.tar.zst` archive under `cold_archive_root`
6. Converts selected series to NIfTI alongside the DICOM tree
7. Upserts `image_study` and `image_series` rows
8. Optionally deletes the source directory after verifying the copy

All of this is wrapped in a per-case try/except so a failure in one case does
not stop the batch; errors are written to `logs/error_log_*.json`.

---

## Entry point

```bash
cd /home/perecanals/pacs/stanford-stroke-pacs/image_integration_protocols
conda activate pacs
python execute_image_integration_protocol.py [--config path/to/config.yaml]
```

By default it reads `execute_image_integration_protocol.yaml` next to the
script. The script walks `src_dir`, calls the `ImageIntegrationProtocol` class
for each patient subdirectory, and aggregates labelled-table sync at the end.

Logs land under `image_integration_protocols/logs/` with a timestamped name.
Both stdout and stderr are redirected through the logger.

---

## YAML config reference

```yaml
# Storage paths come from repo-root config.toml automatically — only override
# them here for one-off experiments. Standard runs need only:
database: stanford-stroke
src_dir: /path/to/new_cases_root            # one patient per subdirectory
overwrite_if_exists: false
anonymize_files: false
delete_originals_after_verification: false
import_label: "2026-04-batch"               # optional, tags all rows in this run
```

| Key | Purpose |
|---|---|
| `env_path` | Path to `.env` for DB credentials. Defaults to `<repo>/.env`. |
| `database` | PostgreSQL database name (usually `stanford-stroke`) |
| `src_dir` | Directory containing per-patient subdirectories to ingest |
| `overwrite_if_exists` | If true, re-integrate studies already in `image_study` (deletes the existing rows + DICOM tree for matching StudyInstanceUIDs first) |
| `anonymize_files` | Strip identifying DICOM headers during copy |
| `delete_originals_after_verification` | After verifying every file copied successfully, remove the source case directory |
| `import_label` | Free-text tag written to `import_label` column in both tables — useful for filtering a batch later |
| `cold_archive_root` | **Optional override.** Defaults to `[storage].cold_archive_root` from `config.toml` when `mode = "cold_path_cache"`, or `null` in legacy mode. The script warns if you override and the override differs from `config.toml`. |

`execute_image_integration_protocol.py` picks a single monotonic `import_id`
via `get_next_import_id()` (max existing + 1) and writes it into every row in
the batch, so you can later find everything that came in together.

### Config sources of truth

Storage paths and storage mode are read from `config.toml` by both
`ImageIntegrationProtocol` and the `execute_image_integration_protocol.py`
driver (via `companion/config.py`). This eliminates the previous hardcoded
`/DATA2/pacs_imaging_data` and the YAML's separate `cold_archive_root`. You
no longer need to keep multiple files in sync; the only path you typically
edit is `[storage]` in `config.toml`.

The driver validates the resolved configuration at startup:
- If `mode = "cold_path_cache"` but no `cold_archive_root` can be resolved,
  it raises immediately.
- If the YAML supplies a `cold_archive_root` that disagrees with `config.toml`,
  it logs a warning so the override is auditable.
- If `mode = "legacy"` but a `cold_archive_root` is set anyway, it logs a
  warning (the archives will be created but the running stack won't use them).

---

## Canonical on-disk layout

Each series ends up at:

```
{legacy_dicom_root}/
  {patient_id}/
    {studyinstanceuid}/
      {seriesdescription_normalized}/
        {seriesinstanceuid}/
          DICOM/              ← all instance files, flat
          NIFTI/image.nii.gz  ← only in legacy mode; skipped in cold_path_cache
```

`dicom_dir_path` in `image_series` points at the `DICOM/` directory. The
NIFTI sibling, when present, is at `.../<seriesUID>/NIFTI/image.nii.gz`.
In `cold_path_cache` mode this sibling is not produced by the protocol —
generate NIFTIs on demand via `scripts/dicom/dicom_to_nifti.py`.

In cold_path_cache mode the archive mirrors the same relative path under the
archive root, with `DICOM/` replaced by `DICOM.tar.zst`:

```
{cold_archive_root}/
  {patient_id}/
    {studyinstanceuid}/
      {seriesdescription_normalized}/
        {seriesinstanceuid}/
          DICOM.tar.zst
```

The archive format is **flat** — instance files are stored at the archive's
root, no `DICOM/` directory wrapper. This matches `scripts/cold_storage/archive_all_series.py`
and `cache_manager._compress_series_dir()`. Warm extraction reconstructs
the `DICOM/` directory from the tree position, not from a prefix inside
the tar.

---

## Protocol steps (inside `ImageIntegrationProtocol.execute_image_integration_protocol`)

| Step | Method | Notes |
|------|--------|-------|
| 1 | `create_series_table` | Recursively scans `case_dir`, reads DICOM headers, builds a pandas DataFrame of candidate series with their source paths |
| 2 | `create_study_table` | Groups series by StudyInstanceUID, computes per-study metadata, predicts `study_type` from `stroke_date` |
| 3 | `filter_existing_studies` | Skips studies already in `image_study` unless `overwrite_if_exists=true` (in which case it deletes existing rows + their DICOM directories first) |
| 4 | `load_clinical_data_table` | Reads `lvo_clinical_data` |
| 5 | `validate_studies_against_clinical_data` | Drops studies whose `patient_id` doesn't match a clinical row or whose `studydate` is outside the allowed stroke-date window |
| 6 | `assign_import_id` / `assign_import_label` | Tags all rows with the batch import_id/label |
| 7 | `add_paths_and_copy_dicom_files` | **Copies DICOMs** from source → `{legacy_dicom_root}/{patient_id}/{studyUID}/{seriesDesc}/{seriesUID}/DICOM/`. Optionally anonymizes. Sets `dicom_dir_path` on each row. |
| 8 | `compress_cold_archives` | **Only if `cold_archive_root` is set.** For each series, creates `{cold_archive_root}/.../DICOM.tar.zst`. Sets `dicom_archive_path` on each row. **Per-series strict, batch soft**: each archive is built to a `.tmp` sibling, member-count verified, and atomically renamed — so a published archive is always valid. A failure on one series does NOT abort the case; the loop continues and failures are collected. After the loop, a WARNING is printed summarizing `N/M` failed, and a JSON report is written to `image_integration_protocols/logs/compression_failures_<timestamp>.json` (includes seriesinstanceuid, studyinstanceuid, dicom_dir_path, error). Failed rows keep `dicom_archive_path = NULL` — retriable via `scripts/cold_storage/archive_all_series.py --patient <id>`. Idempotent: existing archives are re-verified rather than rebuilt; corrupted ones are detected and rebuilt. |
| 9 | `create_nifti_files` | In `legacy` mode: runs DICOM→NIFTI conversion for select series and writes `{seriesUID}/NIFTI/image.nii.gz`. In `cold_path_cache` mode: **skipped.** NIFTIs would accumulate orphaned once their sibling loose DICOMs are cleaned up. Generate on demand via `scripts/dicom/dicom_to_nifti.py` (see [`../recipes/dicom_processing.md`](../recipes/dicom_processing.md)). |
| 10 | `format_column_names` | Normalizes DataFrame column names, including adding `dicom_archive_path` to the set of columns to upsert |
| 11 | `_require_import_id_columns` / `_require_import_label_columns` / `_require_number_of_slices_column` / `_require_dicom_archive_path_column` | Auto-DDL: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for any columns the protocol writes that don't yet exist. Safe to run against a fresh DB. |
| 12 | `update_postgres_tables` | Upserts `image_study` and `image_series` |
| 13 | `verify_integrated_case` + `delete_original_case_dir` | **Only if `delete_originals_after_verification=true`.** Byte-compares each copied file against its source, then removes the source case directory. |

Return value: `{"studyinstanceuids": [...], "seriesinstanceuids": [...]}` —
used by the driver to sync per-level labelled mirror tables after the batch.

---

## Mode behavior

### Legacy mode

Set `cold_archive_root: null` (or omit) in the YAML.

```
source DICOMs
  → copy to /DATA2/pacs_imaging_data/... (loose files)
  → NIFTI alongside
  → upsert image_study / image_series (dicom_archive_path stays NULL)
```

Orthanc's Folder Indexer scans the legacy tree on its interval (60 s by
default) and indexes the new files automatically. No manual Orthanc action
is required.

### cold_path_cache mode

Set `cold_archive_root: /DATA2/pacs_imaging_data_compressed` in the YAML.

```
source DICOMs
  → copy to /DATA2/pacs_imaging_data/... (loose files)
  → compress to /DATA2/pacs_imaging_data_compressed/.../DICOM.tar.zst
  → (NIFTI generation skipped — produce on demand via scripts/dicom/dicom_to_nifti.py)
  → upsert image_study / image_series (dicom_archive_path populated)
```

At the end of the run, for each successfully compressed series:
- Loose DICOMs exist at `dicom_dir_path`
- Compressed archive exists at `dicom_archive_path`
- DB row references both

For any series whose compression failed, `dicom_archive_path` stays NULL.
The case does not abort — other series complete normally. See the failure
log at `image_integration_protocols/logs/compression_failures_*.json` and
retry via `scripts/cold_storage/archive_all_series.py --patient <id>` (idempotent).
Use `scripts/cold_storage/list_unarchived_series.py` to list NULL-archive rows.

The **patched Folder Indexer** (`ssc-orthanc:patched-indexer`) picks up the
loose DICOMs on its next scan and adds them to Orthanc's main DB. Because
`RemoveMissingFiles: false`, those entries will persist even after the loose
files are removed.

### Cleanup of loose DICOMs after integration (cold_path_cache only)

The integration protocol does **not** delete loose DICOMs after compression.
This is intentional: they must stay on disk long enough for Orthanc's Folder
Indexer to see them. Once Orthanc has indexed the new series, the loose
files are redundant and can be removed.

Use **`scripts/cold_storage/cleanup_loose_dicoms.py`** to do this safely. The script:

1. Pulls every series from `image_series` that has a populated
   `dicom_archive_path`
2. Pulls every `SeriesInstanceUID` Orthanc currently knows about by querying
   `orthanc_db.dicomidentifiers` directly (one fast SQL query)
3. For each candidate series, verifies:
   - the archive file exists and is non-empty
   - the archive's regular-file count matches the loose dir's count
     (skippable with `--no-deep-verify` for faster runs)
   - the series' `SeriesInstanceUID` is present in Orthanc's index
4. Only when all checks pass does it delete the loose `DICOM/` directory.
   The NIFTI sibling (`.../<seriesUID>/NIFTI/`) is preserved.

```bash
cd /home/perecanals/pacs/stanford-stroke-pacs
conda activate pacs

# Dry-run by default — see what would be cleaned
python scripts/cold_storage/cleanup_loose_dicoms.py

# Limit to one patient
python scripts/cold_storage/cleanup_loose_dicoms.py --patient 4-0551

# Actually delete
python scripts/cold_storage/cleanup_loose_dicoms.py --execute

# Faster (skips opening each tar.zst to count members)
python scripts/cold_storage/cleanup_loose_dicoms.py --execute --no-deep-verify
```

The script aborts immediately if `STORAGE_MODE != "cold_path_cache"` to
prevent accidental data loss in `legacy` mode. It is safe to run
repeatedly and is suitable for cron — for example, to clean up loose
DICOMs from any new ingestion within 5 minutes:

```cron
*/5 * * * * cd /home/perecanals/pacs/stanford-stroke-pacs && \
  /home/perecanals/miniconda3/envs/pacs/bin/python scripts/cold_storage/cleanup_loose_dicoms.py \
  --execute --no-deep-verify --quiet >> logs/cleanup_loose_dicoms.log 2>&1
```

Run a deep-verify pass periodically (weekly or so) to catch any archive
corruption that the fast path would miss.

---

## Configuration coupling with the rest of the stack

The integration protocol reads `[storage].legacy_dicom_root`, `[storage].cold_archive_root`,
and `[storage].mode` from `config.toml` via `companion/config.py`. There are
no hardcoded paths to keep in sync.

**Checklist before running integration in `cold_path_cache` mode:**
1. `config.toml` has `mode = "cold_path_cache"` with the right
   `legacy_dicom_root` and `cold_archive_root`
2. The custom `ssc-orthanc:patched-indexer` image is deployed and running
3. `orthanc.json` has `"RemoveMissingFiles": false`

That's it — the protocol picks up the same values automatically.

---

## Error handling

- **Per-case:** `execute_image_integration_protocol.py` wraps each patient
  directory in a try/except. Exceptions are logged, the error is appended
  to `logs/error_log_*.json`, and the loop continues.
- **Per-step inside a case:** individual steps (compression, NIFTI
  conversion, verification) catch exceptions per series where possible and
  log them without aborting the whole case.
- **Database writes:** `update_postgres_tables` runs inside a transaction.
  If it raises, the upsert is rolled back, but the files on disk from the
  copy step remain (they'll be cleaned up when you rerun the protocol with
  `overwrite_if_exists=true` for the affected studies).

---

## Running from scratch — typical flow

```bash
# 1. Drop new per-patient directories into src_dir
ls /path/to/new_cases_root/
#   patient-0001/  patient-0002/  patient-0003/

# 2. Edit execute_image_integration_protocol.yaml
#    - src_dir: /path/to/new_cases_root
#    - cold_archive_root: /DATA2/pacs_imaging_data_compressed   # in cold_path_cache
#    - import_label: "2026-04-research-batch"

# 3. Run
cd /home/perecanals/pacs/stanford-stroke-pacs/image_integration_protocols
conda activate pacs
python execute_image_integration_protocol.py

# 4. Watch the log
tail -f logs/execute_image_integration_protocol_*.log

# 5. After completion, check the summary — it prints total/processed/skipped/failed

# 6. Verify Orthanc picked up the new data (within ~60 s of writing loose files)
source ../.env
psql -d stanford-stroke -c "
  SELECT COUNT(*) FROM image_series WHERE import_label = '2026-04-research-batch';"
curl -s -u "${ORTHANC_ADMIN_USER}:${ORTHANC_ADMIN_PASSWORD}" http://localhost:8042/statistics | \
  python3 -c "import sys,json;d=json.load(sys.stdin);print(f'series={d[\"CountSeries\"]}')"

# 7. (cold_path_cache only) Once verified, you can reclaim disk space by
#    deleting the loose DICOM subdirectories for the batch.
```

---

## What it does NOT do

- It does **not** talk to Orthanc directly. Indexing is delegated entirely
  to the Folder Indexer plugin.
- It does **not** populate `cache_state`. New series start implicitly cold
  if their files are ever removed; the warm flow creates the row on first
  click.
- It does **not** clean up loose DICOMs after compression. That's a separate
  step via `scripts/cold_storage/cleanup_loose_dicoms.py` (runnable manually or via cron).
- It does **not** run the labelled-table sync by itself — that's done once
  per batch in `execute_image_integration_protocol.py` after all cases are
  processed (via `labelled_table_sync.sync_labelled_rows`).
