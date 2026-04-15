#!/usr/bin/env python3
"""
Evaluate cold storage strategy on a sample of patients:
1) Storage savings (loose DICOM vs per-series tar.zst under compressed root)
2) rsync comparison (uncompressed patient tree vs compressed mirror)
3) Decompression + Orthanc ingest time (one study per patient)

Does not delete source DICOMs. Writes archives under COLD_ARCHIVE_ROOT unless
--skip-compression is set (then Phase 1 only measures loose DICOMs and reads
sizes of existing tar.zst files; rsync and ingest still use those paths).
Loads DB and Orthanc credentials from stanford-stroke-pacs/.env (parent of benchmarks/).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

import psycopg2
import psycopg2.extras
import requests
import zstandard as zstd
from dotenv import load_dotenv

# Repo root: stanford-stroke-pacs/
REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

DEFAULT_LEGACY_ROOT = Path("/DATA2/pacs_imaging_data")
DEFAULT_COMPRESSED_ROOT = Path("/DATA2/pacs_imaging_data_compressed")
DEFAULT_HOT_CACHE = Path("/DATA2/pacs_hot_cache")

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "stanford-stroke"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)

ORTHANC_URL = os.getenv("ORTHANC_URL", "http://localhost:8042").rstrip("/")
ORTHANC_USER = os.getenv("ORTHANC_ADMIN_USER")
ORTHANC_PASS = os.getenv("ORTHANC_ADMIN_PASSWORD")


def iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def tree_stats(root: Path) -> tuple[int, int]:
    total, n = 0, 0
    for p in iter_files(root):
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        n += 1
    return total, n


def ensure_empty_dir(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)


def tar_zst_dir(src: Path, out_path: Path, level: int) -> float:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    t0 = time.perf_counter()
    cctx = zstd.ZstdCompressor(level=level, threads=-1)
    with out_path.open("wb") as f_out:
        with cctx.stream_writer(f_out) as z_out:
            with tarfile.open(fileobj=z_out, mode="w|") as tf:
                for f in iter_files(src):
                    tf.add(f, arcname=str(f.relative_to(src)))
    return time.perf_counter() - t0


def untar_zst_to(in_path: Path, dst: Path) -> float:
    ensure_empty_dir(dst)
    t0 = time.perf_counter()
    dctx = zstd.ZstdDecompressor()
    with in_path.open("rb") as f_in:
        with dctx.stream_reader(f_in) as z_in:
            with tarfile.open(fileobj=z_in, mode="r|") as tf:
                tf.extractall(dst)
    return time.perf_counter() - t0


def archive_path_for_series_dir(
    dicom_dir: Path, legacy_root: Path, compressed_root: Path
) -> Path:
    dicom_dir = dicom_dir.resolve()
    legacy_root = legacy_root.resolve()
    rel = dicom_dir.relative_to(legacy_root)
    return compressed_root / rel.parent / f"{rel.name}.tar.zst"


def patient_dir_on_disk(legacy_root: Path, patient_id: str) -> Path:
    return legacy_root / patient_id


def compressed_patient_dir(compressed_root: Path, patient_id: str) -> Path:
    return compressed_root / patient_id


def select_patients(conn, count: int, explicit: list[str] | None) -> list[str]:
    if explicit:
        return explicit
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT patient_id,
                   COALESCE(SUM(number_of_slices), 0)::bigint AS total_slices
            FROM image_series
            WHERE patient_id IS NOT NULL AND patient_id != ''
            GROUP BY patient_id
            ORDER BY total_slices DESC NULLS LAST
            """
        )
        rows = cur.fetchall()
    if not rows:
        return []
    n = len(rows)
    if n <= count:
        return [r[0] for r in rows]
    # Evenly spaced indices across the sorted-by-size list (~top/middle/bottom spread)
    picks: list[str] = []
    seen: set[str] = set()
    for j in range(count):
        idx = min(n - 1, int((j + 0.5) * n / count))
        pid = rows[idx][0]
        if pid in seen:
            continue
        seen.add(pid)
        picks.append(pid)
    k = 0
    while len(picks) < count and k < n:
        pid = rows[k][0]
        if pid not in seen:
            picks.append(pid)
            seen.add(pid)
        k += 1
    return picks[:count]


def fetch_series_for_patient(conn, patient_id: str) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT seriesinstanceuid, studyinstanceuid, dicom_dir_path
            FROM image_series
            WHERE patient_id = %s AND dicom_dir_path IS NOT NULL
            """,
            (patient_id,),
        )
        return list(cur.fetchall())


def _parse_rsync_stats(stdout: str, stderr: str) -> dict[str, Any]:
    out = (stdout or "") + "\n" + (stderr or "")
    stats: dict[str, Any] = {}
    for line in out.splitlines():
        m = re.match(r"Number of files: ([\d,]+)(?: \((?:reg: )?([\d,]+)\))?", line)
        if m:
            stats["number_of_files"] = m.group(1).replace(",", "")
            if m.group(2):
                stats["number_of_regular_files"] = m.group(2).replace(",", "")
        m = re.match(r"Total file size: ([\d,]+) bytes", line)
        if m:
            stats["total_file_size_bytes"] = int(m.group(1).replace(",", ""))
        m = re.match(r"Total transferred file size: ([\d,]+) bytes", line)
        if m:
            stats["total_transferred_file_size_bytes"] = int(m.group(1).replace(",", ""))
    return stats


def _run_rsync(src: Path, dst: Path) -> dict[str, Any]:
    t0 = time.perf_counter()
    proc = subprocess.run(
        ["rsync", "-a", "--stats", f"{str(src).rstrip('/')}/", f"{str(dst).rstrip('/')}/"],
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - t0
    result: dict[str, Any] = {
        "exit_code": proc.returncode,
        "elapsed_s": elapsed,
        "stdout_tail": (proc.stdout or "")[-4000:],
    }
    result.update(_parse_rsync_stats(proc.stdout or "", proc.stderr or ""))
    return result


def run_rsync_stats(src: Path, dst: Path) -> dict[str, Any]:
    """rsync -a --stats to an empty destination (cold transfer)."""
    ensure_empty_dir(dst)
    return _run_rsync(src, dst)


def run_rsync_noop(src: Path, dst: Path) -> dict[str, Any]:
    """rsync -a --stats when destination already matches (no-op).

    Measures pure file-indexing overhead: rsync still walks every file on both
    sides to compare timestamps and sizes, but transfers nothing.  This cost
    scales with the number of files, which is the key metric for large DICOM
    trees vs a handful of tar.zst archives.
    """
    dst.mkdir(parents=True, exist_ok=True)
    return _run_rsync(src, dst)


def orthanc_auth() -> tuple[str, str] | None:
    if ORTHANC_USER and ORTHANC_PASS:
        return (ORTHANC_USER, ORTHANC_PASS)
    return None


def orthanc_lookup_study_id(study_uid: str) -> str | None:
    auth = orthanc_auth()
    if not auth:
        return None
    r = requests.post(
        f"{ORTHANC_URL}/tools/lookup",
        data=study_uid,
        auth=auth,
        timeout=60,
    )
    if r.status_code != 200:
        return None
    for entry in r.json():
        if entry.get("Type") == "Study":
            return entry.get("ID")
    return None


def orthanc_delete_study(study_orthanc_id: str) -> bool:
    auth = orthanc_auth()
    if not auth or not study_orthanc_id:
        return False
    r = requests.delete(
        f"{ORTHANC_URL}/studies/{study_orthanc_id}",
        auth=auth,
        timeout=120,
    )
    return r.status_code in (200, 204)


def ingest_dicom_files(files: list[Path]) -> tuple[float, int]:
    auth = orthanc_auth()
    if not auth:
        raise RuntimeError("ORTHANC_ADMIN_USER/PASSWORD not set in .env")
    t0 = time.perf_counter()
    ok = 0
    for fp in files:
        with fp.open("rb") as f:
            r = requests.post(
                f"{ORTHANC_URL}/instances",
                data=f.read(),
                headers={"Content-Type": "application/dicom"},
                auth=auth,
                timeout=300,
            )
        if r.status_code in (200, 201):
            ok += 1
    return time.perf_counter() - t0, ok


@dataclass
class PatientStorageResult:
    patient_id: str
    original_bytes: int
    original_files: int
    compressed_bytes: int
    compressed_archives: int
    ratio: float | None
    series_compressed: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PatientRsyncResult:
    patient_id: str
    uncompressed: dict[str, Any]
    compressed: dict[str, Any]
    uncompressed_noop: dict[str, Any] = field(default_factory=dict)
    compressed_noop: dict[str, Any] = field(default_factory=dict)


@dataclass
class PatientIngestResult:
    patient_id: str
    studyinstanceuid: str
    extract_s: float
    ingest_s: float
    files_ingested: int
    orthanc_study_id: str | None
    skipped_reason: str | None = None


def main() -> int:
    ap = argparse.ArgumentParser(description="Cold storage evaluation benchmark")
    ap.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY_ROOT)
    ap.add_argument("--compressed-root", type=Path, default=DEFAULT_COMPRESSED_ROOT)
    ap.add_argument("--hot-cache", type=Path, default=DEFAULT_HOT_CACHE)
    ap.add_argument("--eval-temp", type=Path, default=Path("/tmp/cold_storage_eval_rsync"))
    ap.add_argument("--patients", nargs="*", help="Explicit patient_id list (overrides auto sample)")
    ap.add_argument("--sample-size", type=int, default=5)
    ap.add_argument("--zstd-level", type=int, default=6)
    ap.add_argument("--skip-rsync", action="store_true")
    ap.add_argument("--skip-ingest", action="store_true", help="Skip Orthanc decompression+ingest phase")
    ap.add_argument("--skip-compression", action="store_true", help="Do not create tar.zst files; use existing archives under --compressed-root only",
    )
    ap.add_argument("--out-json", type=Path, default=REPO_ROOT / "benchmarks" / "cold_storage_evaluation_results.json",
    )
    args = ap.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set in .env", file=sys.stderr)
        return 1

    legacy_root: Path = args.legacy_root.resolve()
    compressed_root: Path = args.compressed_root.resolve()
    hot_cache: Path = args.hot_cache.resolve()
    eval_temp: Path = args.eval_temp.resolve()

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        patient_ids = select_patients(
            conn, args.sample_size, list(args.patients) if args.patients else None
        )
    finally:
        conn.close()

    if not patient_ids:
        print("No patients selected.", file=sys.stderr)
        return 1

    print(f"\nSelected {len(patient_ids)} patients: {patient_ids}")
    print(f"  Legacy root:     {legacy_root}")
    print(f"  Compressed root: {compressed_root}")
    print(f"  Hot cache:       {hot_cache}")
    print(f"  zstd level:      {args.zstd_level}")
    print(f"  Skip rsync:       {args.skip_rsync}")
    print(f"  Skip ingest:      {args.skip_ingest}")
    print(f"  Skip compression: {args.skip_compression}")

    try:
        compressed_root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Cannot create compressed root {compressed_root}: {e}", file=sys.stderr)
        return 1

    storage_results: list[PatientStorageResult] = []
    rsync_results: list[PatientRsyncResult] = []
    ingest_results: list[PatientIngestResult] = []

    for i, pid in enumerate(patient_ids, 1):
        print(f"\n{'='*60}")
        print(f"[{i}/{len(patient_ids)}] Patient {pid}")
        print(f"{'='*60}")
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            series_rows = fetch_series_for_patient(conn, pid)
        finally:
            conn.close()

        print(f"  Found {len(series_rows)} series in DB")

        # --- Phase 1: Compress each series to tar.zst (or reuse existing) ---
        if args.skip_compression:
            print(f"  [STORAGE] Using existing tar.zst only (--skip-compression)...")
        else:
            print(f"  [STORAGE] Compressing series to tar.zst (zstd level {args.zstd_level})...")
        orig_bytes = orig_files = 0
        comp_bytes = 0
        n_archives = 0
        per_series: list[dict[str, Any]] = []

        for j, row in enumerate(series_rows, 1):
            dpath = row.get("dicom_dir_path")
            if not dpath:
                continue
            src = Path(dpath)
            if not src.is_dir():
                print(f"    [{j}/{len(series_rows)}] SKIP (dir missing): {src}")
                per_series.append(
                    {
                        "seriesinstanceuid": row["seriesinstanceuid"],
                        "error": "missing_dir",
                        "path": str(src),
                    }
                )
                continue
            b, fc = tree_stats(src)
            orig_bytes += b
            orig_files += fc
            arch = archive_path_for_series_dir(src, legacy_root, compressed_root)
            if args.skip_compression:
                if arch.is_file() and arch.stat().st_size > 0:
                    sz = arch.stat().st_size
                    create_s = 0.0
                    comp_bytes += sz
                    n_archives += 1
                    ratio_s = sz / b if b else 0
                    print(
                        f"    [{j}/{len(series_rows)}] existing archive: {fc} files loose, "
                        f"{b / (1024*1024):.1f} MiB -> {sz / (1024*1024):.1f} MiB "
                        f"({ratio_s:.2%}), create_s=0s"
                    )
                    per_series.append(
                        {
                            "seriesinstanceuid": row["seriesinstanceuid"],
                            "studyinstanceuid": row["studyinstanceuid"],
                            "dicom_dir_path": str(src),
                            "archive_path": str(arch),
                            "archive_bytes": sz,
                            "tar_zst_create_s": create_s,
                            "compression_skipped": True,
                        }
                    )
                else:
                    print(f"    [{j}/{len(series_rows)}] SKIP (no existing archive): {arch}")
                    per_series.append(
                        {
                            "seriesinstanceuid": row["seriesinstanceuid"],
                            "studyinstanceuid": row["studyinstanceuid"],
                            "dicom_dir_path": str(src),
                            "error": "no_existing_archive",
                            "expected_archive": str(arch),
                        }
                    )
                continue

            create_s = tar_zst_dir(src, arch, level=args.zstd_level)
            sz = arch.stat().st_size
            comp_bytes += sz
            n_archives += 1
            ratio_s = sz / b if b else 0
            print(
                f"    [{j}/{len(series_rows)}] {fc} files, "
                f"{b / (1024*1024):.1f} MiB -> {sz / (1024*1024):.1f} MiB "
                f"({ratio_s:.2%}) in {create_s:.2f}s"
            )
            per_series.append(
                {
                    "seriesinstanceuid": row["seriesinstanceuid"],
                    "studyinstanceuid": row["studyinstanceuid"],
                    "dicom_dir_path": str(src),
                    "archive_path": str(arch),
                    "archive_bytes": sz,
                    "tar_zst_create_s": create_s,
                }
            )

        ratio = (comp_bytes / orig_bytes) if orig_bytes else None
        print(
            f"  [STORAGE] Patient total: {orig_files} files, "
            f"{orig_bytes / (1024*1024):.1f} MiB -> {comp_bytes / (1024*1024):.1f} MiB "
            f"({ratio:.2%} of original)" if ratio else
            f"  [STORAGE] Patient total: no data"
        )
        storage_results.append(
            PatientStorageResult(
                patient_id=pid,
                original_bytes=orig_bytes,
                original_files=orig_files,
                compressed_bytes=comp_bytes,
                compressed_archives=n_archives,
                ratio=ratio,
                series_compressed=per_series,
            )
        )

        # --- Phase 2: rsync comparison ---
        if not args.skip_rsync:
            print(f"  [RSYNC] Comparing rsync of uncompressed vs compressed patient tree...")
            pu = patient_dir_on_disk(legacy_root, pid)
            pc = compressed_patient_dir(compressed_root, pid)
            if pu.is_dir():
                dst_u = eval_temp / "uncompressed" / pid
                dst_c = eval_temp / "compressed" / pid

                # Run 1: cold rsync (destination empty -> full transfer)
                print(f"    --- Run 1: cold rsync (full transfer) ---")
                print(f"    Uncompressed: rsync {pu}/ -> {dst_u}/")
                ru = run_rsync_stats(pu, dst_u)
                print(f"      {ru.get('elapsed_s', 0):.2f}s, files: {ru.get('number_of_files', '?')}")

                rc = {"skipped": True, "reason": "compressed_patient_dir_missing"}
                if pc.is_dir():
                    print(f"    Compressed:   rsync {pc}/ -> {dst_c}/")
                    rc = run_rsync_stats(pc, dst_c)
                    print(f"      {rc.get('elapsed_s', 0):.2f}s, files: {rc.get('number_of_files', '?')}")
                else:
                    print(f"    Compressed:   SKIP (no compressed dir yet at {pc})")

                # Run 2: no-op rsync (destination already matches -> only index/checksum)
                # This measures the pure file-indexing overhead that scales with file count.
                print(f"    --- Run 2: no-op rsync (already synced, index-only) ---")
                print(f"    Uncompressed: rsync {pu}/ -> {dst_u}/ (no-op)")
                ru_noop = run_rsync_noop(pu, dst_u)
                print(f"      {ru_noop.get('elapsed_s', 0):.2f}s (nothing to transfer, pure index cost)")

                rc_noop: dict[str, Any] = {"skipped": True, "reason": "compressed_patient_dir_missing"}
                if pc.is_dir():
                    print(f"    Compressed:   rsync {pc}/ -> {dst_c}/ (no-op)")
                    rc_noop = run_rsync_noop(pc, dst_c)
                    print(f"      {rc_noop.get('elapsed_s', 0):.2f}s (nothing to transfer, pure index cost)")

                rsync_results.append(
                    PatientRsyncResult(
                        patient_id=pid,
                        uncompressed=ru,
                        compressed=rc,
                        uncompressed_noop=ru_noop,
                        compressed_noop=rc_noop,
                    )
                )
            else:
                print(f"    SKIP (legacy dir missing: {pu})")
                rsync_results.append(
                    PatientRsyncResult(
                        patient_id=pid,
                        uncompressed={"skipped": True, "reason": "legacy_patient_dir_missing"},
                        compressed={"skipped": True},
                    )
                )
        else:
            print(f"  [RSYNC] Skipped (--skip-rsync)")

        # --- Phase 3: Decompress + Orthanc ingest ---
        if args.skip_ingest or not orthanc_auth():
            reason = "skip_ingest_flag" if args.skip_ingest else "no_orthanc_auth"
            print(f"  [INGEST] Skipped ({reason})")
            ingest_results.append(
                PatientIngestResult(
                    patient_id=pid,
                    studyinstanceuid="",
                    extract_s=0.0,
                    ingest_s=0.0,
                    files_ingested=0,
                    orthanc_study_id=None,
                    skipped_reason=reason,
                )
            )
            continue

        studies: dict[str, list[dict]] = {}
        for s in per_series:
            if "archive_path" not in s:
                continue
            suid = s.get("studyinstanceuid") or ""
            studies.setdefault(suid, []).append(s)

        if not studies:
            print(f"  [INGEST] SKIP (no archives for this patient)")
            ingest_results.append(
                PatientIngestResult(
                    patient_id=pid,
                    studyinstanceuid="",
                    extract_s=0.0,
                    ingest_s=0.0,
                    files_ingested=0,
                    orthanc_study_id=None,
                    skipped_reason="no_archives_for_patient",
                )
            )
            continue

        best_study = max(studies.items(), key=lambda x: len(x[1]))
        study_uid, arch_rows = best_study
        if not study_uid:
            print(f"  [INGEST] SKIP (empty study UID)")
            ingest_results.append(
                PatientIngestResult(
                    patient_id=pid,
                    studyinstanceuid="",
                    extract_s=0.0,
                    ingest_s=0.0,
                    files_ingested=0,
                    orthanc_study_id=None,
                    skipped_reason="empty_study_uid",
                )
            )
            continue

        print(
            f"  [INGEST] Testing study {study_uid[:30]}... "
            f"({len(arch_rows)} series)"
        )

        study_cache = hot_cache / "eval" / study_uid
        if study_cache.exists():
            shutil.rmtree(study_cache)
        study_cache.mkdir(parents=True, exist_ok=True)

        pre_id = orthanc_lookup_study_id(study_uid)
        if pre_id:
            print(f"    Cleaning pre-existing Orthanc copy: {pre_id}")
            orthanc_delete_study(pre_id)

        print(f"    Extracting {len(arch_rows)} archives to {study_cache}...")
        t_extract = 0.0
        dicom_files: list[Path] = []
        for s in arch_rows:
            arch_p = Path(s["archive_path"])
            sdir = study_cache / s["seriesinstanceuid"]
            t_extract += untar_zst_to(arch_p, sdir)
            dicom_files.extend(iter_files(sdir))
        print(f"    Extracted {len(dicom_files)} files in {t_extract:.2f}s")

        print(f"    POSTing {len(dicom_files)} files to Orthanc {ORTHANC_URL}/instances...")
        try:
            ingest_s, n_ok = ingest_dicom_files(dicom_files)
        except Exception as e:
            print(f"    ERROR during ingest: {e}")
            ingest_results.append(
                PatientIngestResult(
                    patient_id=pid,
                    studyinstanceuid=study_uid,
                    extract_s=t_extract,
                    ingest_s=0.0,
                    files_ingested=0,
                    orthanc_study_id=None,
                    skipped_reason=str(e),
                )
            )
            if study_cache.exists():
                shutil.rmtree(study_cache)
            continue

        print(f"    Ingested {n_ok}/{len(dicom_files)} files in {ingest_s:.2f}s")
        print(f"    Total warm-up: {t_extract + ingest_s:.2f}s (extract {t_extract:.2f}s + ingest {ingest_s:.2f}s)")

        orthanc_id = orthanc_lookup_study_id(study_uid)
        if orthanc_id:
            print(f"    Cleaning up Orthanc study {orthanc_id}...")
            orthanc_delete_study(orthanc_id)
        if study_cache.exists():
            shutil.rmtree(study_cache)
        print(f"    Cleanup done.")

        ingest_results.append(
            PatientIngestResult(
                patient_id=pid,
                studyinstanceuid=study_uid,
                extract_s=t_extract,
                ingest_s=ingest_s,
                files_ingested=n_ok,
                orthanc_study_id=orthanc_id,
            )
        )

    total_orig = sum(r.original_bytes for r in storage_results)
    total_comp = sum(r.compressed_bytes for r in storage_results)
    payload = {
        "host": os.uname().nodename,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "legacy_root": str(legacy_root),
        "compressed_root": str(compressed_root),
        "zstd_level": args.zstd_level,
        "skip_compression": args.skip_compression,
        "patient_ids": patient_ids,
        "aggregate": {
            "total_original_bytes": total_orig,
            "total_compressed_bytes": total_comp,
            "overall_ratio": (total_comp / total_orig) if total_orig else None,
        },
        "storage": [asdict(r) for r in storage_results],
        "rsync": [asdict(r) for r in rsync_results],
        "ingest": [asdict(r) for r in ingest_results],
    }

    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, indent=2))

    def fmt_mib(b: int) -> str:
        return f"{b / (1024 * 1024):.1f}"

    # --- Summary ---
    print(f"\n{'#'*60}")
    print(f"  RESULTS SUMMARY  ({len(patient_ids)} patients)")
    print(f"{'#'*60}")

    storage_title = (
        "--- 1. Storage savings (existing tar.zst only; --skip-compression) ---"
        if args.skip_compression
        else "--- 1. Storage savings (loose DICOM -> tar.zst) ---"
    )
    print(f"\n{storage_title}")
    print(f"{'patient_id':<16} {'orig_MiB':>10} {'files':>7} {'comp_MiB':>10} {'archives':>9} {'ratio':>7}")
    for r in storage_results:
        rat = f"{r.ratio:.3f}" if r.ratio is not None else "n/a"
        print(
            f"{r.patient_id:<16} {fmt_mib(r.original_bytes):>10} "
            f"{r.original_files:>7} {fmt_mib(r.compressed_bytes):>10} "
            f"{r.compressed_archives:>9} {rat:>7}"
        )
    overall_ratio = (total_comp / total_orig) if total_orig else None
    rat_s = f"{overall_ratio:.3f}" if overall_ratio else "n/a"
    total_files = sum(r.original_files for r in storage_results)
    total_archives = sum(r.compressed_archives for r in storage_results)
    print(f"{'TOTAL':<16} {fmt_mib(total_orig):>10} {total_files:>7} {fmt_mib(total_comp):>10} {total_archives:>9} {rat_s:>7}")
    if overall_ratio:
        saved_pct = (1 - overall_ratio) * 100
        print(f"  -> {saved_pct:.1f}% storage saved, {total_files} files reduced to {total_archives} archives")

    if rsync_results:
        print(f"\n--- 2a. rsync cold transfer (empty destination) ---")
        print(f"{'patient_id':<16} {'uncomp_s':>10} {'comp_s':>10} {'speedup':>9}")
        for r in rsync_results:
            u = r.uncompressed.get("elapsed_s")
            c = r.compressed.get("elapsed_s")
            u_s = f"{u:.2f}" if u is not None else "skip"
            c_s = f"{c:.2f}" if c is not None else "skip"
            sp = f"{u / c:.1f}x" if u and c else "n/a"
            print(f"{r.patient_id:<16} {u_s:>10} {c_s:>10} {sp:>9}")

        print(f"\n--- 2b. rsync no-op (already synced — pure file-indexing overhead) ---")
        print(f"  rsync still walks every file on both sides to compare timestamps/sizes.")
        print(f"  This is the cost that scales with file count in large DICOM trees.")
        print(f"{'patient_id':<16} {'uncomp_s':>10} {'comp_s':>10} {'speedup':>9}")
        for r in rsync_results:
            u2 = r.uncompressed_noop.get("elapsed_s") if r.uncompressed_noop else None
            c2 = r.compressed_noop.get("elapsed_s") if r.compressed_noop else None
            u2_s = f"{u2:.2f}" if u2 is not None else "skip"
            c2_s = f"{c2:.2f}" if c2 is not None else "skip"
            sp2 = f"{u2 / c2:.1f}x" if u2 and c2 else "n/a"
            print(f"{r.patient_id:<16} {u2_s:>10} {c2_s:>10} {sp2:>9}")

    print(f"\n--- 3. Decompress + Orthanc ingest (overhead per study) ---")
    print(f"{'patient_id':<16} {'extract_s':>10} {'ingest_s':>10} {'total_s':>10} {'files':>7} {'note'}")
    for r in ingest_results:
        note = r.skipped_reason or ""
        total = r.extract_s + r.ingest_s
        print(
            f"{r.patient_id:<16} {r.extract_s:>10.2f} {r.ingest_s:>10.2f} "
            f"{total:>10.2f} {r.files_ingested:>7} {note}"
        )
    active = [r for r in ingest_results if not r.skipped_reason]
    if active:
        avg_total = sum(r.extract_s + r.ingest_s for r in active) / len(active)
        avg_extract = sum(r.extract_s for r in active) / len(active)
        avg_ingest = sum(r.ingest_s for r in active) / len(active)
        print(f"  -> Average warm-up: {avg_total:.2f}s (extract {avg_extract:.2f}s + ingest {avg_ingest:.2f}s)")

    print(f"\nFull results: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
