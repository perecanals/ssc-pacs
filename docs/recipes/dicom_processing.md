# DICOM processing recipes

Copy-paste snippets for common operator tasks against the SSC PACS.
Most of these assume the `ssc-pacs` conda environment is active and that you are
in the stack root (`stanford-stroke-pacs/`):

```bash
conda activate ssc-pacs
# cd into the stack root
```

Paths shown as `<dicom_data_root>` / `<cold_archive_root>` are the
`[storage]` values from `config.toml`.

---

## Web-app downloads

In Navigator, admins and staff see **Download DICOM as zip** and **NIfTI**
buttons beside each series, including expanded child rows. Staff must also have
access to that series' patient dataset. Normal users receive 403; out-of-scope
series receive 404 before files or conversion tools are accessed. These actions
are independent of the optional Data Exports table module.

`GET /api/series/{uid}/dicom-zip` streams the series ZIP.
`GET /api/series/{uid}/nifti` returns a compressed `.nii.gz` volume using the
existing ingestion SimpleITK/GDCM conversion function via
`scripts/dicom/dicom_to_nifti.py`. The web route explicitly selects the requested
DICOM SeriesInstanceUID. It runs the converter as a subprocess with an argument
list (no shell interpolation), a five-minute timeout and two ITK threads.
Unconvertible/non-image series return 422; timeouts return 504, with ZIP suggested
as a fallback. Conversion preserves the source geometry; it does not resample,
skull-strip or perform clinical processing.

Both routes prefer an existing cold archive, extracting it into a private
request directory; otherwise they read the loose DICOM directory. They do not
warm Orthanc, change cache state or write NIfTI alongside canonical source files.
Two imaging downloads may run at once; further requests return 429. Temporary
files and concurrency slots are released after completion, client disconnects,
or preparation/conversion failures.
Reserve enough host temporary disk for two extracted series and conversion
outputs. These limits are independent of Data Exports' CSV/XLSX worker limits.

Download names sanitize path separators/control characters and use an ASCII
HTTP-header fallback plus encoded UTF-8 `filename*`. The frontend prefers the
encoded name, supporting Unicode descriptions without header-encoding failures.
Imaging downloads use authenticated fetch/blob delivery and thus need browser
memory for the downloaded file. Requests appear in normal HTTP logs; the Data
Exports report/configuration audit applies to table exports, not imaging files.

## DICOM → NIFTI

NIFTIs are **not** generated during ingestion in `cold_path_cache` mode — the
ingestion protocol's NIfTI-conversion step is a dormant-by-design path, kept for
future use but skipped in this mode. Use `scripts/dicom/dicom_to_nifti.py` to
produce them on demand. It wraps
`image_ingestion_protocols/utils.convert_dicom_to_nifti` (SimpleITK + GDCM) and
supports three input modes.

### 1. From a loose directory

If the DICOM files are sitting on disk at a path you know (e.g. a just-ingested
series before cleanup, or a sandbox copy):

```bash
python scripts/dicom/dicom_to_nifti.py \
    --dir <dicom_data_root>/<patient-id>/1.2.../AX_T2_FLAIR/1.2.../DICOM \
    --out /tmp/ax_t2_flair.nii.gz
```

For a directory containing multiple DICOM series, pass `--dicom-series-uid <UID>`
to select one. Without it, ambiguous or empty directories fail explicitly.

If `--out` is omitted, the script writes to the canonical sibling location
`{dicom_dir.parent}/NIFTI/image.nii.gz`.

### 2. From a cold archive (no DB involvement)

If you only have the `.tar.zst` archive path — e.g. you're running this on
a backup host with no PostgreSQL or Orthanc available:

```bash
python scripts/dicom/dicom_to_nifti.py \
    --archive <cold_archive_root>/<patient-id>/1.2.../AX_T2_FLAIR/1.2.../DICOM.tar.zst \
    --out /tmp/ax_t2_flair.nii.gz
```

The script extracts the archive to a `TemporaryDirectory`, converts, and
cleans up the temp dir. `--out` is required here (no canonical sibling
location for a standalone archive).

### 3. By series UID (with optional automatic warming)

The most common case. Gives the series UID, lets the script find the files:

```bash
# Error if the study is currently cold
python scripts/dicom/dicom_to_nifti.py --series-uid <series-uid>

# Warm the study first (study-scoped; sibling series come along for the ride)
python scripts/dicom/dicom_to_nifti.py \
    --series-uid <series-uid> \
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
zstd -dc <cold_archive_root>/.../DICOM.tar.zst | tar -tvf - | head
```

Pull the DICOM metadata from the first instance without touching disk:

```bash
ARCHIVE=<cold_archive_root>/.../DICOM.tar.zst
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
python scripts/cold_storage/list_unarchived_series.py --patient <patient-id>

# Filter by import label (the batch tag from execute_image_ingestion_protocol.yaml)
python scripts/cold_storage/list_unarchived_series.py --import-label "2026-04-batch"
```

Fix: rerun the idempotent archiver against the affected patient(s) (dry-run is
the default — add `--execute` to actually compress):

```bash
python scripts/cold_storage/archive_all_series.py --execute --patient <patient-id>
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
python scripts/cold_storage/cleanup_loose_dicoms.py --execute --patient <patient-id>

# Skip the per-archive file-count check (faster; relies on Orthanc presence alone)
python scripts/cold_storage/cleanup_loose_dicoms.py --execute --no-deep-verify
```

Suitable for scheduling — it runs from the platform service units (a systemd
timer on Linux; a launchd job on macOS, see
[`../guides/deployment_on_mac.md`](../guides/deployment_on_mac.md)). The generic
form, from the stack root, is:

```bash
conda run -n ssc-pacs python scripts/cold_storage/cleanup_loose_dicoms.py --execute
```
