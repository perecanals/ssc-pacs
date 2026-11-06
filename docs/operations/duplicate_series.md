# Potentially duplicated series

Run the read-only screen across the PACS from the stack root, in the `ssc-pacs`
environment:

```bash
python scripts/data_integrity/duplicate_series_audit.py
python scripts/data_integrity/duplicate_series_audit.py --study <StudyInstanceUID> --verify-pixels
python scripts/data_integrity/duplicate_series_audit.py --dataset GUIDE
python scripts/data_integrity/duplicate_series_audit.py --scope subject
```

The default writes `report.json` and `series.csv` under
`maintenance/duplicate-series/<UTC timestamp>/` at the checkout root. Choose a
directory with `--output <directory>`; an existing directory's two report files
are replaced. Reports contain project identifiers and stored paths, so keep
them in this gitignored workspace. The CSV has one row per candidate series,
with a common group ID; the JSON includes the matching evidence, individual
series metadata, and any verification results. The printed summary reports
the actual coverage and candidate counts for that run.

## Metadata screening

The default compares different SeriesInstanceUIDs **within each study**, across
all studies in `image_series`. It reads the research DB in a read-only
transaction, closes that connection, and writes only the reports. It does not
query Orthanc or read imaging files. No schema change or service restart is
needed.

Candidates must share a nonblank description and modality, positive slice
count, and image dimensions. Description case and repeated whitespace are
normalized; meaningful suffixes are retained. A detailed fingerprint must also
match acquisition datetime/source, pixel spacing, thickness, axial coverage,
instance/position counts, and selected representative tags, including kernels,
image type, orientation, MR echo/diffusion/sequence parameters and intensity
scaling. Series numbers, export UIDs and filenames are not required to match.
The representative slice position is omitted because different exports can
select different representative instances.

`strong_metadata` requires a matching datetime explicitly sourced from acquisition,
orientation and pixel spacing. Other candidates are `possible_metadata`.
Unknown detailed fields can match other unknown values; missing basic fields
cause the series to be skipped and counted in the summary. Neither metadata
category establishes identical image content. Repeat acquisitions or derived
images with indistinguishable stored metadata can still be candidates.

`--scope subject` additionally compares studies of the same explicitly linked
person (`subject_id`), falling back to within-study comparison for unknown
subjects. It never uses PatientID as identity. `--dataset` selects studies
through `study_visibility`, including member studies, once each. Combining
`--study` with subject scope still limits the input to that one study.

## Image verification

`--verify-pixels` reads **every instance of each candidate series**. It streams
the recorded canonical `.tar.zst` archive without extracting or warming it;
legacy series without an archive path use their recorded loose directory.
Only one DICOM instance's payload and decoded samples need to be in memory at a
time. Filenames and export UIDs are ignored when comparing image content, but
every file's study/series UID must match its own database row.

Verification compares an order-independent multiset of SHA-256 fingerprints of
decoded pixel arrays and geometry, rescale, temporal and multiframe context.
Multiplicity is retained, byte order is normalized, and instance numbers are
used to preserve page order where no spatial position is available. It checks
the actual instance count against `n_instances_scanned` (falling back to
`number_of_slices`). This detects duplicate image content despite re-exported
UIDs or different filenames/transfer syntaxes, rather than requiring identical
DICOM file bytes.

Results:

- `confirmed_pixel_match`: all series in the group match in image content/context.
- `partial_pixel_match`: only some series match; `matching_sets` identifies them.
- `pixel_difference`: the candidate series differ in content/context.
- `incomplete`: at least one series could not be fully verified. Missing/offline
  archives, unreadable or non-image objects, count/UID mismatches, or missing
  compressed-pixel decoders are reported per series. A recorded archive is
  never silently replaced by a potentially incomplete warm copy.
- `not_requested` / `not_checked_limit`: content was not checked.

A fleet-wide verification can involve substantial archive I/O. Use
`--verify-pixels --max-verify-groups 20` to bound a run; remaining groups are
explicitly marked `not_checked_limit`. Start with a study or dataset when
reviewing results. Metadata passes return exit code 0 even with candidates;
verification returns 2 if any group is incomplete. Other runtime failures are
nonzero.

## Review and limitations

This is a conservative candidate screen, not an exhaustive content index.
Renamed descriptions, incomplete series, changed acquisition times, changed
representative tags, and unlinked cross-study re-exports may be missed. Series
present only in Orthanc are outside its scope; use
[`reconciliation.md`](reconciliation.md) for index coverage. Verification is
limited to metadata candidates; it does not hunt for arbitrary pixel matches.

Even confirmed matching images may carry different annotations or provenance.
Review the full series and annotation history before deciding whether anything
should be removed. This tool never chooses a survivor or deletes data. If
removal is separately authorized, use the supported
[`deletion workflow`](deleting_studies.md).
