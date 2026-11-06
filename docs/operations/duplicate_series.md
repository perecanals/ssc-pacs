# Potentially duplicated series

Run the read-only screen across the PACS from the stack root, in the `ssc-pacs`
environment:

```bash
python scripts/data_integrity/duplicate_series_audit.py
python scripts/data_integrity/duplicate_series_audit.py --study <StudyInstanceUID> --verify-pixels
python scripts/data_integrity/duplicate_series_audit.py --dataset GUIDE
python scripts/data_integrity/duplicate_series_audit.py --scope subject

# Verify all candidates, saving resumable per-group evidence; then prepare a review.
python scripts/data_integrity/duplicate_series_audit.py --verify-pixels --workers 2 --output ../maintenance/duplicate-series/verified
python scripts/data_integrity/duplicate_series_review.py --report ../maintenance/duplicate-series/verified/report.json --output ../maintenance/duplicate-series/verified/review
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

For JPEG/JPEG lossless instances, install the optional pinned decoders in the
environment used to run the audit, then resume unavailable groups:

```bash
python -m pip install -r scripts/data_integrity/requirements-pixels.txt
python scripts/data_integrity/duplicate_series_audit.py --verify-pixels --workers 2 --resume --output ../maintenance/duplicate-series/verified
```

This is optional for metadata screening and uncompressed images. Other transfer
syntaxes may need their own decoder. A decode failure never counts as a match.

Each completed group is saved atomically under the output directory's
`verification/` folder. If interrupted, repeat the same command with `--resume`.
Successful checkpoints are reused only when the verifier version, series/study
identity, expected counts, stored source path and source file identity/size/mtime
still match. Unavailable or changed sources are rechecked. A source that changes
during verification is reported unavailable. This is a local file-change check,
not permission to delete later without revalidation. `--workers N` runs a bounded
number of verification processes; the default is one. Two is a reasonable way to
start a deliberate fleet run. The check records the PatientIDs actually found in
all instances, separately from the content digest.

## Approval-only removal review

`duplicate_series_review.py` consumes a completed report from the current
verifier and reads current ownership, study visibility and manual annotations
in a new read-only transaction. It writes `review.json` and `review.csv`, plus
`removals.csv` listing only proposed removal/keeper pairs and `conflicts.csv`
listing conflicting values with their source annotations. It has
**no execute option**, changes no annotations and sets no application labels.
Flags are in these review artifacts. They are proposals awaiting approval.

Only a fully verified matching subset can produce a proposal. The source files
must still match the verification signatures, and the series' identity/ownership
must still match the audit. The rules are:

- Different PatientIDs anywhere in the series rows, parent studies, visible
  enrollments (including members), or the actual DICOM headers protect the
  matching set: no removal or label merge is proposed. Missing identities also
  block proposals. Equal patient IDs never override enrollment boundaries.
- Within the same study, prefer the copy with the most direct manual annotation
  rows. Machine series types/ranks are not manual labels. Inherited patient/study
  labels already have the same parent and remain unchanged.
- Complementary labels are proposed for copying to the retained series. Equal
  values are retained once. Distinct nonempty notes for an equal value are
  combined with their source series and last editor recorded. The detailed
  proposal includes the original annotation IDs, values, notes and attribution;
  no original annotation/history row is rewritten by the review.
- Different values for the same label block the whole matching subset for
  explicit resolution. Null, empty and nonempty values are not silently treated
  as interchangeable.
- Ties use a repeatable random choice among copies with the most annotations,
  so the proposed survivor does not change on each review.
- Cross-study matches require a separate study-level review even for the same
  PatientID. Matching individual series do not prove that entire studies and
  their inherited annotations are redundant. No whole study is proposed for
  deletion by this tool.

`awaiting_approval` and `awaiting_approval_with_label_merge` identify proposals;
all `blocked_*`, `protected_different_patient_ids`, and
`cross_study_review_required` decisions retain every copy. The CSV identifies
the proposed survivor/removals and a stable decision ID for each matching subset;
the JSON contains label changes, conflicts,
live state snapshots and a state digest. A group's partial/incomplete check can
still contain a fully checked matching subset; only those explicitly matching
series may enter the proposal, leaving all other series untouched.

Specific series removal and label-copy/notes-merge actions need separate approval.
Before any later approved execution, revalidate pixels/source signatures,
ownership, visibility, annotations and label edit permissions (there is no admin
bypass). Apply and audit approved label changes before removing anything, retain
their source attribution, and use the supported deletion workflow. A stale
proposal is never sufficient authorization to remove a different or changed
series. This release supplies the review, not a bulk deletion executor.

## Review and limitations

This is a conservative candidate screen, not an exhaustive content index.
Renamed descriptions, incomplete series, changed acquisition times, changed
representative tags, and unlinked cross-study re-exports may be missed. Series
present only in Orthanc are outside its scope; use
[`reconciliation.md`](reconciliation.md) for index coverage. Verification is
limited to metadata candidates; it does not hunt for arbitrary pixel matches.

Even confirmed matching images may carry different annotations or provenance.
Review the proposal and annotation history before deciding whether anything
should be removed. These tools never delete data or apply label merges. If
removal is separately authorized, use the supported
[`deletion workflow`](deleting_studies.md).
