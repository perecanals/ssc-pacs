# Image ingestion protocol

**Purpose:** How new imaging data gets into the PACS. Explains the protocol's
inputs, steps, outputs, and how it interacts with each storage mode. For the
cold storage design see [`../cold_storage/design.md`](../cold_storage/design.md).

Code lives under `stanford-stroke-pacs/image_ingestion_protocols/`. This
pipeline is **site-specific** to the Stanford Stroke Center DICOM layout and
metadata conventions. It is not part of a standard fresh deployment.

---

## What it does

Takes a directory of per-patient source DICOMs, and for each case:

1. Discovers series by grouping every readable file under the case by its `SeriesInstanceUID` (not by folder)
2. Groups series into studies
3. Validates studies against `lvo_clinical_data` (clinical DB table)
4. Copies DICOMs to the canonical layout under `dicom_data_root`
5. Optionally compresses each series to a `*.tar.zst` archive under `cold_archive_root`
6. Converts selected series to NIfTI alongside the DICOM tree
7. Upserts `image_study`, `image_series`, and `patient` rows (one transaction)
8. Optionally deletes the source directory after verifying the copy

All of this is wrapped in a per-case try/except so a failure in one case does
not stop the batch; errors are written to `logs/error_log_*.json`.

---

## Entry point

```bash
cd /home/perecanals/ssc-pacs/stanford-stroke-pacs/image_ingestion_protocols
conda activate pacs
python execute_image_ingestion_protocol.py [--config path/to/config.yaml]
```

By default it reads `execute_image_ingestion_protocol.yaml` next to the
script. That file is **gitignored** (it holds site/run-specific paths) —
create it by copying the tracked template
`execute_image_ingestion_protocol.example.yaml`. The script walks
`src_dir`, calls the `ImageIngestionProtocol` class for each patient
subdirectory, and aggregates labelled-table sync at the end.

Logs land under `image_ingestion_protocols/logs/` with a timestamped name.
Both stdout and stderr are redirected through the logger.

### Resuming an interrupted run

A large backlog runs for days and will be interrupted repeatedly. Each case is
processed in deterministic `sorted()` order and commits atomically at the end
(one transaction), so the driver can resume **success-based** without
re-scanning completed cases off slow disk:

- On startup it parses **every** prior log whose `Source directory:` header
  matches the current `src_dir` (`determine_resume_skip_set`) and skips exactly
  the cases with a `Successfully completed processing case` marker (union
  across all matching logs). Only proven successes are skipped: cases that
  **failed** (per-case errors don't stop a run — e.g. every case after the disk
  fills) or were **interrupted** mid-case lack the marker and are re-processed
  (idempotent — `filter_existing_studies` skips studies already in the DB, and
  a case already fully committed takes the resume-boundary re-index path).
- Resume is **on by default** (`resume: true` in the YAML). Pass `--no-resume` to
  process every case from the top regardless. Switching to a new `src_dir` (new
  batch) starts fresh automatically, since the log header won't match.
- The run summary reports `Skipped (resume): N`. Failed cases remain recorded
  in `logs/error_log_*.json` and are retried automatically on the next run of
  the same batch (they carry no success marker).

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
dataset: "crisp2"                           # optional, cohort tag recorded on the patient table
```

| Key | Purpose |
|---|---|
| `env_path` | Path to `.env` for DB credentials. Defaults to `<repo>/.env`. |
| `database` | PostgreSQL database name (usually `stanford-stroke`) |
| `src_dir` | Directory containing per-patient subdirectories to ingest |
| `overwrite_if_exists` | Controls behavior when a `StudyInstanceUID` is already in `image_study`. **`false` (default — append + drift detect):** keep the existing `image_study` row untouched; for each series, append it if `SeriesInstanceUID` is new, skip it if `(SeriesUID, number_of_slices)` matches the DB, and **re-ingest** it if the SeriesUID is in DB but the on-disk slice count differs from `image_series.number_of_slices` (wiping the stale `dicom_dir_path` and `dicom_archive_path` for that series first). Series whose DB `number_of_slices` is NULL emit a warning and are skipped — set this flag to `true` to force re-ingest. **`true` (full overwrite):** for each matching StudyInstanceUID, delete the existing rows in `image_study`, `image_series`, `image_study_labelled`, `image_series_labelled`, and the on-disk DICOM tree + cold archives, then re-ingest from the new scan. Any series previously in DB but not in the new scan does NOT survive. |
| `anonymize_files` | Strip identifying DICOM headers during copy |
| `delete_originals_after_verification` | After verifying every file copied successfully, remove the source case directory |
| `import_label` | Free-text tag written to `import_label` column in both tables — useful for filtering a batch later |
| `dataset` | Optional cohort/dataset tag. Recorded only on the `patient` table (`dataset text[]`, union-accumulated across batches); not written to `image_study`/`image_series`. |
| `cold_archive_root` | **Optional override.** Defaults to `[storage].cold_archive_root` from `config.toml` when `mode = "cold_path_cache"`, or `null` in legacy mode. The script warns if you override and the override differs from `config.toml`. |
| `cleanup_loose_after_indexing` | `cold_path_cache` only (ignored with a warning in legacy mode). Default `true`: after each case's Orthanc indexing verifies, delete its series' loose `DICOM/` dirs (same safety checks as `cleanup_loose_dicoms.py`; NIFTI siblings preserved). Set `false` to keep loose files until a manual cleanup pass. See "Cleanup of loose DICOMs after ingestion". |
| `resume` | Default `true`. Skip cases that prior logs for this `src_dir` prove were successfully completed; failed/interrupted cases re-run (see "Resume"). CLI `--no-resume` overrides. |
| `compress_workers` | Concurrent per-series archive compressions within a case (thread pool; zstd releases the GIL). Default `4`; `1` = serial. |
| `pipeline_indexing` | Default `true`: each case's Orthanc indexing + loose cleanup + cache stamping runs on a background worker while the next case ingests — per-case wall ≈ max(ingest, index) instead of their sum. The completion marker (resume contract) is logged only after the worker finishes, so interrupts remain safe. Auto-disabled (with a warning) when `overwrite_if_exists` is `true`. `false` = strictly serial per case, same code path. |

`execute_image_ingestion_protocol.py` picks a single monotonic `import_id`
via `get_next_import_id()` (max existing + 1) and writes it into every row in
the batch, so you can later find everything that came in together.

### Config sources of truth

Storage paths and storage mode are read from `config.toml` by both
`ImageIngestionProtocol` and the `execute_image_ingestion_protocol.py`
driver (via `web-app/config.py`). This eliminates the previous hardcoded
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
{dicom_data_root}/
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

## Protocol steps (inside `ImageIngestionProtocol.execute_image_ingestion_protocol`)

| Step | Method | Notes |
|------|--------|-------|
| 1 | `create_series_table` | Recursively walks `case_dir`, reads each file's DICOM header, and **buckets files by `SeriesInstanceUID`** — one DataFrame row per real series, with the aggregated list of source file paths. A series is defined by its UID, not its folder: same-UID files spread across folders are **merged** into one row; a "mixed" folder holding several UIDs is **split** into its true series; `number_of_slices` = count of files carrying that UID. Files under one UID that disagree on `SeriesNumber`/`StudyInstanceUID` (a standard violation) trigger a **loud WARNING** and are kept merged — a suspected true UID collision to inspect at source (no split, no UID re-mint). This guarantees the upsert conflict key is unique within the batch. See [How series are identified](#how-series-are-identified). |
| 2 | `create_study_table` | Groups series by StudyInstanceUID, computes per-study metadata, predicts `study_type` from `stroke_date` |
| 3 | `filter_existing_studies` | Decides per study/series what to do given the current DB state. Always loads both `image_study` and `image_series` for the scanned `StudyInstanceUID`s. **Append mode (`overwrite_if_exists=false`):** for studies already in DB, drops the study row from the working set so the persisted `import_id` / `import_label` / `study_path` are preserved; then per series, drops the series row if `(SeriesUID, number_of_slices)` matches DB, keeps it for re-ingest if the slice count drifted (and wipes the stale `dicom_dir_path` and `dicom_archive_path` from disk before re-copy), keeps it if the SeriesUID is new, or warns-and-skips if DB `number_of_slices` is NULL. **Overwrite mode (`overwrite_if_exists=true`):** calls `overwrite_existing_study()`, which deletes the on-disk DICOM directories, stale cold archives, and the rows in `image_study`, `image_series`, `image_study_labelled`, and `image_series_labelled` for that study, all in one transaction — orphan rows from series that no longer exist on disk cannot survive. |
| 4 | `load_clinical_data_table` | Reads `lvo_clinical_data` |
| 5 | `validate_studies_against_clinical_data` | Drops studies whose `patient_id` doesn't match a clinical row or whose `studydate` is outside the allowed stroke-date window |
| 6 | `assign_import_id` / `assign_import_label` | Tags all rows with the batch import_id/label |
| 7 | `add_paths_and_copy_dicom_files` | **Copies DICOMs** from source → `{dicom_data_root}/{patient_id}/{studyUID}/{seriesDesc}/{seriesUID}/DICOM/`. Copies the series' aggregated file list (which may span several source folders); on a destination basename collision it **renames** the file (`…__dupN`) so nothing is overwritten. Optionally anonymizes. Sets `dicom_dir_path` and records the source→dest pairs for verification. |
| 8 | `compress_cold_archives` | **Only if `cold_archive_root` is set.** For each series, creates `{cold_archive_root}/.../DICOM.tar.zst`. Sets `dicom_archive_path` on each row. **Per-series strict, batch soft**: each archive is built to a `.tmp` sibling, member-count verified, and atomically renamed — so a published archive is always valid. A failure on one series does NOT abort the case; the loop continues and failures are collected. After the loop, a WARNING is printed summarizing `N/M` failed, and a JSON report is written to `image_ingestion_protocols/logs/compression_failures_<timestamp>.json` (includes seriesinstanceuid, studyinstanceuid, dicom_dir_path, error). Failed rows keep `dicom_archive_path = NULL` — retriable via `scripts/cold_storage/archive_all_series.py --patient <id>`. Idempotent: existing archives are re-verified rather than rebuilt; corrupted ones are detected and rebuilt. |
| 9 | `create_nifti_files` | In `legacy` mode: runs DICOM→NIFTI conversion for select series and writes `{seriesUID}/NIFTI/image.nii.gz`. In `cold_path_cache` mode: **skipped.** NIFTIs would accumulate orphaned once their sibling loose DICOMs are cleaned up. Generate on demand via `scripts/dicom/dicom_to_nifti.py` (see [`../recipes/dicom_processing.md`](../recipes/dicom_processing.md)). |
| 10 | `format_column_names` | Normalizes DataFrame column names, including adding `dicom_archive_path` to the set of columns to upsert |
| 11 | `_require_import_id_columns` / `_require_import_label_columns` / `_require_number_of_slices_column` / `_require_dicom_archive_path_column` | Auto-DDL: `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for any columns the protocol writes that don't yet exist. Safe to run against a fresh DB. |
| 12 | `update_postgres_tables` | Upserts `image_series`, `image_study`, then `patient` — all in one transaction. `_upsert_patient` registers one row per patient: `stroke_date = MIN(image_study.acquisitiondatetime)` (recomputed across all of the patient's studies), `import_id`/`import_label` keep the **origin** (first-seen, preserved on conflict), and `dataset` is the deduped union of the `dataset` config across batches. As a belt-and-suspenders guard, `_upsert_dataframe` drops any rows duplicated on the conflict key (keep-last, with a WARNING) before the INSERT — so a stray duplicate can never again roll back a whole case via `CardinalityViolation` (`ON CONFLICT DO UPDATE` cannot touch the same target twice). |
| 13 | `verify_ingested_case` + `delete_original_case_dir` | **Only if `delete_originals_after_verification=true`.** Iterates the recorded source→dest pairs (so it survives the collision-rename case), byte-compares each copied file against its source, then removes the source case directory. |

Return value: `{"studyinstanceuids": [...], "seriesinstanceuids": [...]}` —
used by the driver to sync per-level labelled mirror tables after the batch.

### How series are identified

The protocol enforces the DICOM identity rule directly, independent of how the
source files are laid out on disk:

- **Same `SeriesInstanceUID` = same series → merge.** All files carrying a given
  UID become one row, even if they are scattered across several source folders
  (e.g. a stray instance mis-filed into a different folder).
- **Different `SeriesInstanceUID` = different series → split.** A single folder
  that contains files from several series (a "mixed" / localizer folder) is split
  into one row per UID — nothing is dropped.

This replaced an earlier one-row-per-directory scan that used the first file in
each folder as the series. That model silently conflated mixed folders and, when
two folders resolved to the same UID, emitted a duplicate conflict key that
aborted the whole case with a `CardinalityViolation`. Grouping by UID is lossless
and keeps the conflict key unique by construction.

Regression coverage: `image_ingestion_protocols/test_image_ingestion_grouping.py`
(mixed-folder split, cross-folder merge, collision warning, copy-collision rename).

### How `series_type` is detected

During `create_series_table`, each series is classified geometry-first into
`CTP`, `PWI`, `DWI`, or `NULL` (`utils.identify_series_type`). The discriminating
signal is **`same_position_count`** — the largest number of frames in the series
that share one `ImagePositionPatient` (`utils.max_same_position_count`, computed
from the headers already in memory). Static scans visit each slice location once
(~1); dynamic acquisitions cycle through time/b-values at each location, so the
count equals the number of timepoints. Combined with `Modality` and a small
`SeriesDescription` exclusion list:

| series_type | Modality | same-position count | excluded descriptions |
|-------------|----------|---------------------|-----------------------|
| `CTP` | CT | ≥ 15 | — |
| `PWI` | MR | ≥ 15 | asl, fmri, qsm, swi |
| `DWI` | MR | 2–14 | asl, fmri, qsm, swi |

Anything else resolves to `NULL`. The legacy keyword `CTA`/`NCCT` detection is
**no longer** wired into `series_type` (those lists were never tuned for this
dataset); `is_cta_series`/`is_ncct_series` remain only for `identify_study_type`'s
BASELINE/FOLLOW_UP study classification. Enhanced-multiframe series whose
per-frame geometry can't be decoded degrade to `NULL` rather than misclassifying.

Thresholds live as constants in `utils.py` (`PERFUSION_MIN_FRAMES`,
`DWI_FRAME_RANGE`, `MR_DYNAMIC_EXCLUDE`). A local maintenance helper to recompute
`series_type` for existing rows from their on-disk or archived headers lives
under the gitignored `hidden/ingestion_dev/backfill_series_type.py` (dry-run by
default; `--execute` to write; `--patient` / `--import-label` / `--only-missing`
filters), alongside the local unit tests for the detectors and resume logic.

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
log at `image_ingestion_protocols/logs/compression_failures_*.json` and
retry via `scripts/cold_storage/archive_all_series.py --patient <id>` (idempotent).
Use `scripts/cold_storage/list_unarchived_series.py` to list NULL-archive rows.

**Orthanc indexing happens per case** (always on in `cold_path_cache`).
Immediately after each case's DB commit, `index_case_into_orthanc` →
`scripts/cold_storage/scoped_index.py` issues a `POST /indexer/scan` to the
patched indexer's on-demand endpoint (see `orthanc-indexer-patched/PATCHES.md`)
scoped to just that case's study folders — no config edits, no restarts, cost
O(case). Registrations are verified per series via `/tools/lookup`. Because
`RemoveMissingFiles: false`, index entries persist even after the loose files
are evicted.

With `pipeline_indexing: true` (default) the indexing + loose cleanup + cache
stamping of case N run on a single background worker thread while the main
thread ingests case N+1 (bounded queue: at most ~2 cases outstanding, so
un-cleaned loose data on disk stays capped). The patched indexer runs one scan
at a time (a second scan request gets 409 and the client waits), so one worker
matches the server exactly.

This per-case ordering has three properties:

- **Interruptible**: the resume marker "Successfully completed processing case
  X" means *ingested + indexed + verified + cleaned* — it is logged by the
  worker only after all of those finish; killing a run loses at most the
  in-flight case(s), which carry no marker. On resume they are re-analyzed
  and — if their series were already committed — re-indexed (idempotent, fast
  over already-registered files; an orphaned in-flight scan is absorbed by the
  409 busy-wait).
- **OOM-safe**: a single huge uninterrupted scan can push Orthanc core past the
  VM memory ceiling (the plugin's DICOM cache plateaus at ~0.35 GiB, but core's
  working set grows with sustained registration). Case-sized scans stay near
  baseline; an oversized case (> ~40k instances) is automatically split into
  bounded passes with a settle between them
  (`scoped_index.register_in_bounded_passes`, also used by
  `scripts/cold_storage/reindex_missing_series.py`).
- **Viewable immediately**: a case shows up in OHIF while the batch is still
  running, instead of after an end-of-run mega-scan.

An indexing failure (e.g. Orthanc down) is **non-fatal**: the case's data is
safe on disk + in the DB, the error is recorded in the run's error log
(`<case>#indexing`), and the run continues. At the end of the run a **sanity
pass** verifies every series ingested this run against Orthanc's index,
re-registers any that are missing (bounded passes, `Force=true` to clear
orphaned rows from truncated scans), and logs a final verdict — "Orthanc index
clean: N/N series verified" or the list of still-missing series. Anything
still missing after that: backfill with
`scripts/cold_storage/reindex_missing_series.py`.

### Cleanup of loose DICOMs after ingestion (cold_path_cache only)

Once a series is compressed **and** indexed into Orthanc, the loose copy is
redundant — the archive is canonical and the index survives eviction.

By default (`cleanup_loose_after_indexing: true`) the protocol reclaims the
space during the run itself: after each case's indexing verifies, its series'
loose `DICOM/` dirs are deleted with the same safety checks as
`cleanup_loose_dicoms.py` below (archive present + file-count match + series
verified in the index; NIFTI siblings preserved; series that fail a check are
skipped and logged, and nothing is deleted for a case whose indexing failed).
Freshly ingested studies then read as *cold* and warm on demand like the rest
of the corpus.

Set `cleanup_loose_after_indexing: false` in the YAML to keep loose files on
disk ("hot", instantly viewable) until a manual `cleanup_loose_dicoms.py` pass.

Either way, `series_cache_state` is stamped to match the outcome: cleaned
series get their row deleted (reads cold); with cleanup disabled, archived
series keeping loose files are upserted `hot` (visible as hot in the UI and
TTL-evictable). Archive-suspect series stay row-less so eviction can never
delete their only copy. See "What it does NOT do" below.

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
cd /home/perecanals/ssc-pacs/stanford-stroke-pacs
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
*/5 * * * * cd /home/perecanals/ssc-pacs/stanford-stroke-pacs && \
  /home/perecanals/miniconda3/envs/pacs/bin/python scripts/cold_storage/cleanup_loose_dicoms.py \
  --execute --no-deep-verify --quiet >> logs/cleanup_loose_dicoms.log 2>&1
```

Run a deep-verify pass periodically (weekly or so) to catch any archive
corruption that the fast path would miss.

---

## Configuration coupling with the rest of the stack

The ingestion protocol reads `[storage].dicom_data_root`, `[storage].cold_archive_root`,
and `[storage].mode` from `config.toml` via `web-app/config.py`. There are
no hardcoded paths to keep in sync.

**Checklist before running ingestion in `cold_path_cache` mode:**
1. `config.toml` has `mode = "cold_path_cache"` with the right
   `dicom_data_root` and `cold_archive_root`
2. The custom `ssc-orthanc:patched-indexer` image is deployed and running
3. `orthanc.json` has `"RemoveMissingFiles": false`

That's it — the protocol picks up the same values automatically.

---

## Error handling

- **Per-case:** `execute_image_ingestion_protocol.py` wraps each patient
  directory in a try/except. Exceptions are logged, the error is appended
  to `logs/error_log_*.json`, and the loop continues.
- **Per-step inside a case:** individual steps (compression, NIFTI
  conversion, verification) catch exceptions per series where possible and
  log them without aborting the whole case.
- **Database writes:** `update_postgres_tables` runs inside a transaction.
  If it raises, the upsert is rolled back but the files on disk from the
  copy step remain. Simply re-running the protocol with default settings
  is enough to recover: `filter_existing_studies` will see the series UIDs
  as new (the upsert never committed) and re-ingest them, overwriting the
  stale on-disk files in place. Use `overwrite_if_exists=true` only if you
  also want to wipe an existing study's DB rows on top of that.

---

## Running from scratch — typical flow

```bash
# 1. Drop new per-patient directories into src_dir
ls /path/to/new_cases_root/
#   patient-0001/  patient-0002/  patient-0003/

# 2. Edit execute_image_ingestion_protocol.yaml
#    (first run: copy execute_image_ingestion_protocol.example.yaml)
#    - src_dir: /path/to/new_cases_root
#    - cold_archive_root: /DATA2/pacs_imaging_data_compressed   # in cold_path_cache
#    - import_label: "2026-04-research-batch"

# 3. Run
cd /home/perecanals/ssc-pacs/stanford-stroke-pacs/image_ingestion_protocols
conda activate pacs
python execute_image_ingestion_protocol.py

# 4. Watch the log
tail -f logs/execute_image_ingestion_protocol_*.log

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

- The `ImageIngestionProtocol` class itself does **not** talk to Orthanc;
  indexing is driven by the executor
  (`execute_image_ingestion_protocol.py`), per case, via the patched
  indexer's `POST /indexer/scan` endpoint (see "Mode behavior" above).
- The executor **does** stamp `series_cache_state` per case, after Orthanc
  indexing verifies: series whose loose dirs were cleaned get their row
  deleted (absence reads as cold); with cleanup disabled, archived series
  whose loose dirs remain are upserted `hot` with `last_accessed_at` set so
  the TTL eviction sweep can reclaim them later. Series that are
  archive-suspect (compression failed, count mismatch) or whose indexing
  failed are left row-less on purpose: row-less reads cold and the TTL sweep
  never touches row-less series, so their loose files — possibly the only
  copy — can't be evicted before the archive/reindex retry. Stamping
  failures are non-fatal — reconcile with
  `scripts/cold_storage/rebuild_cache_state.py`.
- It cleans up loose DICOMs after compression by default
  (`cleanup_loose_after_indexing: true`); with `false` that's a separate step via
  `scripts/cold_storage/cleanup_loose_dicoms.py` (runnable manually or via cron).
- It does **not** run the labelled-table sync by itself — that's done once
  per batch in `execute_image_ingestion_protocol.py` after all cases are
  processed (via `labelled_table_sync.sync_labelled_rows`).
