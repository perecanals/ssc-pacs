# DICOM processing recipes

Copy-paste snippets for common operator tasks against the SSC PACS.
Most of these assume the `pacs` conda environment is active:

```bash
conda activate pacs
cd /home/perecanals/ssc-pacs/stanford-stroke-pacs
```

---

## DICOM → NIFTI

The Web App does **not** generate NIFTIs automatically in
`cold_path_cache` mode. Use `scripts/dicom/dicom_to_nifti.py` to produce them on
demand. It wraps `image_ingestion_protocols/utils.convert_dicom_to_nifti`
(SimpleITK + GDCM) and supports three input modes.

### 1. From a loose directory

If the DICOM files are sitting on disk at a path you know (e.g. a just-ingested
series before cleanup, or a sandbox copy):

```bash
python scripts/dicom/dicom_to_nifti.py \
    --dir /DATA2/pacs_imaging_data/4-0551/1.2.../AX_T2_FLAIR/1.2.../DICOM \
    --out /tmp/ax_t2_flair.nii.gz
```

If `--out` is omitted, the script writes to the canonical sibling location
`{dicom_dir.parent}/NIFTI/image.nii.gz`.

### 2. From a cold archive (no DB involvement)

If you only have the `.tar.zst` archive path — e.g. you're running this on
a backup host with no PostgreSQL or Orthanc available:

```bash
python scripts/dicom/dicom_to_nifti.py \
    --archive /DATA2/pacs_imaging_data_compressed/4-0551/1.2.../AX_T2_FLAIR/1.2.../DICOM.tar.zst \
    --out /tmp/ax_t2_flair.nii.gz
```

The script extracts the archive to a `TemporaryDirectory`, converts, and
cleans up the temp dir. `--out` is required here (no canonical sibling
location for a standalone archive).

### 3. By series UID (with optional automatic warming)

The most common case. Gives the series UID, lets the script find the files:

```bash
# Error if the study is currently cold
python scripts/dicom/dicom_to_nifti.py --series-uid 1.2.826.0.1.3680043.8.498.28617545145905959508444948339234956099

# Warm the study first (study-scoped; sibling series come along for the ride)
python scripts/dicom/dicom_to_nifti.py \
    --series-uid 1.2.826.0.1.3680043.8.498.28617545145905959508444948339234956099 \
    --warm-if-cold
```

Without `--out`, the NIFTI lands in the canonical
`{dicom_dir_parent}/NIFTI/image.nii.gz` location. Eviction of the warmed
study is the operator's responsibility — call `POST /api/studies/{uid}/evict`
or wait for the TTL-driven eviction loop.

---

## Inspect an archive without extracting

List the member files:

```bash
zstd -dc /DATA2/pacs_imaging_data_compressed/.../DICOM.tar.zst | tar -tvf - | head
```

Pull the DICOM metadata from the first instance without touching disk:

```bash
ARCHIVE=/DATA2/pacs_imaging_data_compressed/.../DICOM.tar.zst
FIRST_FILE=$(zstd -dc "$ARCHIVE" | tar -tf - | head -1)
zstd -dc "$ARCHIVE" | tar -xOf - "$FIRST_FILE" | python3 -c "
import sys, pydicom
ds = pydicom.dcmread(sys.stdin.buffer, stop_before_pixels=True)
print(ds)
"
```

Count the instances in an archive (useful for parity checks against the
loose dir):

```bash
zstd -dc archive.tar.zst | tar -tf - | wc -l
```

---

## Triage: series with loose files but no archive

`scripts/cold_storage/list_unarchived_series.py` prints `image_series` rows where
`dicom_archive_path IS NULL` but `dicom_dir_path IS NOT NULL`. That's
the set of series whose compression failed during ingestion (or never
ran).

```bash
# All such series (patient_id, studyinstanceuid, seriesinstanceuid, dicom_dir_path)
python scripts/cold_storage/list_unarchived_series.py

# Just the count
python scripts/cold_storage/list_unarchived_series.py --count

# Filter by patient
python scripts/cold_storage/list_unarchived_series.py --patient 4-0551

# Filter by import label (the batch tag from execute_image_ingestion_protocol.yaml)
python scripts/cold_storage/list_unarchived_series.py --import-label "2026-04-batch"
```

Fix: rerun the idempotent archiver against the affected patient(s):

```bash
python scripts/cold_storage/archive_all_series.py --patient 4-0551
```

`scripts/cold_storage/archive_all_series.py` filters by `dicom_dir_path IS NOT NULL` (not by
`dicom_archive_path IS NULL`), so it will re-attempt every series that has
loose files — safe to rerun even if most already have archives. It skips
existing archives.

---

## Triage: loose files on disk that are safe to remove

`scripts/cold_storage/cleanup_loose_dicoms.py` deletes loose `DICOM/` directories only
when the series' archive exists on disk, the archive's file count matches
the loose dir's file count, and the series is present in Orthanc's
`dicomidentifiers` index. Dry-run by default. With `--execute` it also
deletes the cleaned series' `series_cache_state` rows (absence reads as cold).
`--import-label <label>` (repeatable) scopes the pass to specific ingestion runs.

```bash
# See what would be removed (no mutation)
python scripts/cold_storage/cleanup_loose_dicoms.py

# Actually remove
python scripts/cold_storage/cleanup_loose_dicoms.py --execute

# Scope to one patient
python scripts/cold_storage/cleanup_loose_dicoms.py --execute --patient 4-0551

# Skip the per-archive file-count check (faster; relies on Orthanc presence alone)
python scripts/cold_storage/cleanup_loose_dicoms.py --execute --no-deep-verify
```

Suitable for cron:

```cron
*/15 * * * * cd /home/perecanals/ssc-pacs/stanford-stroke-pacs && \
  /opt/miniconda3/envs/pacs/bin/python scripts/cold_storage/cleanup_loose_dicoms.py --execute
```
