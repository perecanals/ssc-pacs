#!/usr/bin/env python3
"""Read-only duplicate-series screening, with optional full pixel verification.

Default: compare series within every study using stored metadata only.
--scope subject also compares different studies of explicitly linked people.
Never merges, deletes, warms, extracts, or changes the Orthanc index.
See docs/operations/duplicate_series.md for criteria and limitations.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import tarfile
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path

STACK_ROOT = Path(__file__).resolve().parents[2]

# Reconstruction/sequence differences must not disappear behind a shared name.
# No patient names, private tags, UIDs, or representative slice position in the
# fingerprint: re-exports change identifiers, and representatives may differ.
TAG_FIELDS = (
    "ConvolutionKernel", "ImageType", "ImageOrientationPatient", "Rows", "Columns",
    "PixelSpacing", "SliceThickness", "SpacingBetweenSlices", "EchoTime", "EchoNumbers",
    "RepetitionTime", "InversionTime", "FlipAngle", "DiffusionBValue",
    "DiffusionGradientOrientation", "ScanningSequence", "SequenceVariant",
    "ScanOptions", "SequenceName", "MRAcquisitionType", "BitsAllocated", "BitsStored",
    "PixelRepresentation", "SamplesPerPixel", "PhotometricInterpretation",
    "RescaleSlope", "RescaleIntercept", "NumberOfFrames",
)
DETAIL_FIELDS = (
    "acquisitiondatetime", "acquisitiondatetime_source", "pixelspacing", "slicethickness",
    "scanaxialcoverage_mm", "n_positions", "same_position_count", "n_instances_scanned",
    "distinct_kernels", "distinct_image_types",
)
PIXEL_CONTEXT = (
    "ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing", "SliceThickness",
    "SpacingBetweenSlices", "RescaleSlope", "RescaleIntercept", "PhotometricInterpretation",
    "TemporalPositionIdentifier", "TriggerTime", "FrameTime", "FrameTimeVector",
    "AcquisitionDateTime", "AcquisitionDate", "AcquisitionTime", "EchoTime", "EchoNumbers",
    "DiffusionBValue", "DiffusionGradientOrientation", "ImageType", "ConvolutionKernel",
    "SharedFunctionalGroupsSequence", "PerFrameFunctionalGroupsSequence",
)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def normalized(value) -> str:
    return " ".join(str(value or "").split()).lower()


def base_key(row: dict, scope: str = "study") -> tuple | None:
    """Require a named series, modality, positive count and image dimensions."""
    shape = row.get("imageshape") or []
    count = row.get("number_of_slices")
    study = row.get("studyinstanceuid")
    if (not study or not normalized(row.get("seriesdescription"))
            or not normalized(row.get("modality")) or not count or count <= 0
            or len(shape) < 2 or any(v is None or v <= 0 for v in shape)):
        return None
    # Unknown subjects never become one global bucket. PatientID is not identity.
    identity = ("subject", row["subject_id"]) if scope == "subject" and row.get("subject_id") else ("study", study)
    return (identity, normalized(row["seriesdescription"]), normalized(row["modality"]), count, tuple(shape))


def find_candidates(rows: list[dict], scope: str = "study") -> tuple[list[dict], dict]:
    buckets = defaultdict(list)
    skipped = 0
    for row in rows:
        key = base_key(row, scope)
        if key is None:
            skipped += 1
            continue
        evidence = {field: row.get(field) for field in DETAIL_FIELDS}
        evidence["tags"] = {field: (row.get("tags") or {}).get(field) for field in TAG_FIELDS}
        buckets[(key, canonical(evidence))].append(row)
    groups = []
    for (_, signature), members in buckets.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda r: r["seriesinstanceuid"])
        first = members[0]
        strong = (
            first.get("acquisitiondatetime") is not None
            and first.get("acquisitiondatetime_source") == "acquisition"
            and bool((first.get("tags") or {}).get("ImageOrientationPatient"))
            and bool(first.get("pixelspacing") or (first.get("tags") or {}).get("PixelSpacing"))
        )
        groups.append({
            "group_id": hashlib.sha256(canonical([r["seriesinstanceuid"] for r in members]).encode()).hexdigest()[:16],
            "match": "strong_metadata" if strong else "possible_metadata",
            "seriesdescription": first["seriesdescription"],
            "modality": first["modality"],
            "number_of_slices": first["number_of_slices"],
            "imageshape": first["imageshape"],
            "evidence": json.loads(signature),
            "series": members,
            "verification": {"status": "not_requested"},
        })
    groups.sort(key=lambda g: (g["series"][0]["studyinstanceuid"], normalized(g["seriesdescription"]), g["group_id"]))
    return groups, {"series_scanned": len(rows), "skipped_missing_basic_metadata": skipped,
                    "candidate_groups": len(groups), "candidate_series": sum(len(g["series"]) for g in groups),
                    "candidate_studies": len({r["studyinstanceuid"] for g in groups for r in g["series"]})}


def fetch_rows(study: str | None, dataset: str | None, scope: str = "study") -> list[dict]:
    """One read-only snapshot; close it before any archive I/O."""
    sys.path.insert(0, str(STACK_ROOT / "web-app"))
    import psycopg2
    from db import DB_CONFIG
    from psycopg2.extras import RealDictCursor

    # Coarse buckets avoid transferring representative tags for unique series.
    # The Python pass applies the same basic key and the detailed fingerprint.
    identity = "COALESCE(s.subject_id, s.studyinstanceuid)" if scope == "subject" else "s.studyinstanceuid"
    conditions = []
    params = []
    if study:
        conditions.append("s.studyinstanceuid = %s")
        params.append(study)
    if dataset:
        conditions.append("""EXISTS (SELECT 1 FROM study_visibility sv
            JOIN patient p USING (patient_key)
            WHERE sv.studyinstanceuid = s.studyinstanceuid AND p.dataset = %s)""")
        params.append(dataset)
    where = " AND ".join(conditions) or "TRUE"
    query = f"""
        WITH base AS (
            SELECT s.seriesinstanceuid, s.studyinstanceuid, s.patient_key, s.subject_id,
                   s.seriesdescription, s.modality, s.number_of_slices, s.imageshape,
                   count(*) OVER (PARTITION BY {identity},
                       lower(trim(regexp_replace(s.seriesdescription, '\\s+', ' ', 'g'))),
                       lower(trim(regexp_replace(s.modality, '\\s+', ' ', 'g'))),
                       s.number_of_slices, s.imageshape) AS bucket_size
            FROM image_series s WHERE {where}
        )
        SELECT b.*, s.acquisitiondatetime, s.acquisitiondatetime_source,
               s.pixelspacing, s.slicethickness, s.scanaxialcoverage_mm,
               s.seriesnumber, s.import_label, s.dicom_archive_path, s.dicom_dir_path,
               t.n_positions, t.same_position_count, t.n_instances_scanned,
               t.distinct_kernels, t.distinct_image_types,
               CASE WHEN b.bucket_size > 1 THEN
                   (SELECT jsonb_object_agg(k,v) FROM jsonb_each(t.tags) kv(k,v) WHERE k = ANY(%s))
               END AS tags
        FROM base b JOIN image_series s USING (seriesinstanceuid)
        LEFT JOIN series_dicom_tags t USING (seriesinstanceuid)
    """
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(query, (*params, list(TAG_FIELDS)))
            return [dict(row) for row in cur]
    finally:
        conn.close()


def instance_digest(ds) -> str:
    """Decoded samples + geometry/intensity/time context, excluding export IDs."""
    import numpy as np

    pixels = ds.pixel_array
    # Canonical little-endian representation handles different transfer syntaxes.
    pixels = np.ascontiguousarray(pixels.astype(pixels.dtype.newbyteorder("<"), copy=False))
    context = {name: ds.data_element(name).to_json_dict(None, 1024)
               for name in PIXEL_CONTEXT if name in ds}
    if "ImagePositionPatient" not in ds and "PerFrameFunctionalGroupsSequence" not in ds:
        context["InstanceNumber"] = str(getattr(ds, "InstanceNumber", ""))
    digest = hashlib.sha256(canonical([pixels.shape, pixels.dtype.str, context]).encode())
    digest.update(pixels.tobytes())
    return digest.hexdigest()


def fingerprint_series(row: dict) -> dict:
    """Stream canonical archives (or legacy loose files), keeping one image in RAM.

    Any wrong UID, missing pixel data, count mismatch or decode error prevents
    verification. Never silently drop unreadable files or fall back to a stale
    warm copy when a canonical archive is recorded.
    """
    import pydicom
    import zstandard

    counts = Counter()

    def read_instance(fileobj):
        ds = pydicom.dcmread(fileobj)
        if (str(getattr(ds, "SeriesInstanceUID", "")) != row["seriesinstanceuid"]
                or str(getattr(ds, "StudyInstanceUID", "")) != row["studyinstanceuid"]):
            raise ValueError("file's study/series UID differs from the database row")
        counts[instance_digest(ds)] += 1

    archive = row.get("dicom_archive_path")
    source = archive or row.get("dicom_dir_path")
    try:
        if archive:
            with Path(archive).open("rb") as raw, zstandard.ZstdDecompressor().stream_reader(raw) as stream:
                with tarfile.open(fileobj=stream, mode="r|") as tar:
                    for member in tar:
                        if member.issym() or member.islnk():
                            raise ValueError("archive contains a link")
                        if member.isfile() and not Path(member.name).name.startswith("."):
                            with tar.extractfile(member) as fileobj:
                                # pydicom seeks while parsing; the zstd/tar stream
                                # cannot rewind. Buffer only this one instance.
                                read_instance(io.BytesIO(fileobj.read()))
        elif source:
            files = sorted(p for p in Path(source).rglob("*") if p.is_file() and not p.name.startswith("."))
            for path in files:
                read_instance(path)
        else:
            raise ValueError("no stored archive or loose DICOM path")
        actual = sum(counts.values())
        expected = row.get("n_instances_scanned") or row.get("number_of_slices")
        if not actual or actual != expected:
            raise ValueError(f"instance count {actual} differs from expected {expected}")
        return {"status": "ok", "sha256": hashlib.sha256(canonical(sorted(counts.items())).encode()).hexdigest(),
                "instances": actual, "source": source}
    except Exception as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}", "source": source}


def verify_group(group: dict) -> dict:
    by_digest = defaultdict(list)
    results = {}
    for row in group["series"]:
        uid = row["seriesinstanceuid"]
        result = fingerprint_series(row)
        results[uid] = result
        if result["status"] == "ok":
            by_digest[result["sha256"]].append(uid)
    matches = sorted(sorted(uids) for uids in by_digest.values() if len(uids) > 1)
    if len(results) != sum(len(uids) for uids in by_digest.values()):
        status = "incomplete"
    elif len(by_digest) == 1:
        status = "confirmed_pixel_match"
    elif matches:
        status = "partial_pixel_match"
    else:
        status = "pixel_difference"
    return {"status": status, "matching_sets": matches, "series": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", help="Limit input to one StudyInstanceUID")
    parser.add_argument("--dataset", help="Limit to studies visible to this dataset (includes memberships)")
    parser.add_argument("--scope", choices=("study", "subject"), default="study",
                        help="Compare within studies (default) or across studies of linked people")
    parser.add_argument("--verify-pixels", action="store_true", help="Read/decode every candidate instance; potentially slow")
    parser.add_argument("--max-verify-groups", type=int, help="Verify only the first N groups; explicitly mark the remainder")
    parser.add_argument("--output", type=Path, help="Report directory (default: maintenance/duplicate-series/<UTC timestamp>)")
    args = parser.parse_args()
    if args.max_verify_groups is not None and (not args.verify_pixels or args.max_verify_groups < 1):
        parser.error("--max-verify-groups requires --verify-pixels and a positive number")
    started = time.monotonic()
    print("Reading series metadata (read-only)...", file=sys.stderr, flush=True)
    rows = fetch_rows(args.study, args.dataset, args.scope)
    groups, summary = find_candidates(rows, args.scope)
    del rows
    if args.verify_pixels:
        for index, group in enumerate(groups):
            if args.max_verify_groups is not None and index >= args.max_verify_groups:
                group["verification"] = {"status": "not_checked_limit"}
                continue
            print(f"Verifying group {index + 1}/{len(groups)}: {group['seriesdescription']}", file=sys.stderr, flush=True)
            group["verification"] = verify_group(group)
    summary["matches"] = dict(Counter(g["match"] for g in groups))
    summary["verification"] = dict(Counter(g["verification"]["status"] for g in groups))
    report = {"schema_version": 1, "timestamp": datetime.now(UTC).isoformat(),
              "duration_seconds": round(time.monotonic() - started, 2),
              "scope": args.scope, "study": args.study, "dataset": args.dataset,
              "summary": summary, "groups": groups}
    output = args.output or (STACK_ROOT.parent / "maintenance" / "duplicate-series"
                             / datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ"))
    output.mkdir(parents=True, exist_ok=True)
    path = output / "report.json"
    path.write_text(json.dumps(report, indent=2, default=str) + "\n")
    # One row per candidate series, so reviewers can sort/filter without JSON tooling.
    with (output / "series.csv").open("w", newline="") as fileobj:
        writer = csv.writer(fileobj)
        writer.writerow(["group_id", "match", "verification", "studyinstanceuid", "seriesinstanceuid",
                         "patient_key", "seriesdescription", "modality", "number_of_slices", "import_label"])
        for group in groups:
            for row in group["series"]:
                writer.writerow([group["group_id"], group["match"], group["verification"]["status"],
                                 *[row.get(k) for k in ("studyinstanceuid", "seriesinstanceuid", "patient_key",
                                                       "seriesdescription", "modality", "number_of_slices", "import_label")]])
    print(json.dumps(summary, indent=2))
    print(f"Report: {path}\nCSV: {output / 'series.csv'}")
    return 2 if summary["verification"].get("incomplete") else 0


if __name__ == "__main__":
    raise SystemExit(main())
