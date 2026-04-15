#!/usr/bin/env python3
"""
Cold storage benchmark report.

Three measurement phases, one report:

  1. Storage footprint (all patients on disk):
       loose bytes + file count vs. tar.zst bytes + archive count
  2. Backup speed via rsync (first 30 patient_ids, alphabetical):
       full cold copy + no-op (metadata-only) walk, for loose and compressed trees
  3. Decompression cost (10 evenly-spaced patients):
       per-series and per-study wall-clock timings; scatter plots vs. size

Outputs:
  - cold_storage_report_results.json  (raw data)
  - figures/*.png                     (matplotlib)
  - cold_storage_report.md            (human-readable report)

Reuses helpers from cold_storage_evaluation.py (tree_stats, untar_zst_to,
_run_rsync, iter_files) so both benchmarks stay consistent.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import psycopg2
from dotenv import load_dotenv

# Reuse primitives from the existing benchmark
from cold_storage_evaluation import (
    _run_rsync,
    ensure_empty_dir,
    iter_files,
    tree_stats,
    untar_zst_to,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = Path(__file__).resolve().parent
FIGURES_DIR = BENCH_DIR / "figures"

load_dotenv(REPO_ROOT / ".env")

DEFAULT_LOOSE_ROOT = Path("/DATA2/pacs_imaging_data_loose_backup")
DEFAULT_COMPRESSED_ROOT = Path("/DATA2/pacs_imaging_data_compressed")
# /tmp is on root fs and nearly full; use /DATA2 scratch by default.
DEFAULT_RSYNC_DEST = Path("/DATA2/pacs_hot_cache/cold_storage_report_rsync")
DEFAULT_DECOMPRESS_SCRATCH = Path("/DATA2/pacs_hot_cache/cold_storage_report_decompress")

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "stanford-stroke"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class PatientStorage:
    patient_id: str
    loose_bytes: int
    loose_files: int
    compressed_bytes: int
    compressed_archives: int
    ratio: float | None  # compressed / loose


@dataclass
class RsyncRun:
    elapsed_s: float
    exit_code: int
    number_of_files: str | None
    total_file_size_bytes: int | None
    total_transferred_file_size_bytes: int | None


@dataclass
class PatientRsync:
    patient_id: str
    loose_full: RsyncRun | None
    compressed_full: RsyncRun | None
    loose_noop: RsyncRun | None
    compressed_noop: RsyncRun | None


@dataclass
class SeriesDecompress:
    patient_id: str
    study_uid: str
    series_uid: str
    archive_bytes: int
    extracted_bytes: int
    extracted_files: int
    elapsed_s: float


@dataclass
class StudyDecompress:
    patient_id: str
    study_uid: str
    n_series: int
    archive_bytes: int
    extracted_bytes: int
    extracted_files: int
    elapsed_s: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def patient_ids_on_disk(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def archive_bytes_for_patient(compressed_patient_dir: Path) -> tuple[int, int]:
    total = 0
    n = 0
    for p in compressed_patient_dir.rglob("*.tar.zst"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                continue
            n += 1
    return total, n


def _rsync_to_run(raw: dict[str, Any]) -> RsyncRun:
    nf = raw.get("number_of_files")
    return RsyncRun(
        elapsed_s=float(raw.get("elapsed_s", 0.0)),
        exit_code=int(raw.get("exit_code", -1)),
        number_of_files=str(nf) if nf is not None else None,
        total_file_size_bytes=raw.get("total_file_size_bytes"),
        total_transferred_file_size_bytes=raw.get("total_transferred_file_size_bytes"),
    )


def fmt_bytes(b: int | float | None) -> str:
    if b is None:
        return "n/a"
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    x = float(b)
    u = 0
    while x >= 1024 and u < len(units) - 1:
        x /= 1024
        u += 1
    return f"{x:,.2f} {units[u]}"


def parse_archive_path(archive: Path, compressed_root: Path) -> tuple[str, str, str]:
    """Recover (patient_id, study_uid, series_uid) from archive layout.

    Layout:
      compressed_root/{patient_id}/{study_uid}/{study_name}/{series_uid}/DICOM.tar.zst
    """
    rel = archive.resolve().relative_to(compressed_root.resolve())
    parts = rel.parts
    # parts[-1] == "DICOM.tar.zst"
    patient_id = parts[0]
    study_uid = parts[1] if len(parts) > 1 else ""
    series_uid = parts[-2] if len(parts) >= 2 else ""
    return patient_id, study_uid, series_uid


# ---------------------------------------------------------------------------
# Phase 1 — Storage footprint
# ---------------------------------------------------------------------------


def phase_storage(
    loose_root: Path, compressed_root: Path
) -> list[PatientStorage]:
    loose_ids = set(patient_ids_on_disk(loose_root))
    comp_ids = set(patient_ids_on_disk(compressed_root))
    all_ids = sorted(loose_ids | comp_ids)
    print(
        f"[STORAGE] loose={len(loose_ids)} compressed={len(comp_ids)} "
        f"union={len(all_ids)} patients"
    )
    results: list[PatientStorage] = []
    for i, pid in enumerate(all_ids, 1):
        loose_bytes, loose_files = (0, 0)
        if pid in loose_ids:
            loose_bytes, loose_files = tree_stats(loose_root / pid)
        comp_bytes, comp_archives = (0, 0)
        if pid in comp_ids:
            comp_bytes, comp_archives = archive_bytes_for_patient(
                compressed_root / pid
            )
        ratio = (comp_bytes / loose_bytes) if loose_bytes else None
        results.append(
            PatientStorage(
                patient_id=pid,
                loose_bytes=loose_bytes,
                loose_files=loose_files,
                compressed_bytes=comp_bytes,
                compressed_archives=comp_archives,
                ratio=ratio,
            )
        )
        if i % 10 == 0 or i == len(all_ids):
            print(
                f"  [{i}/{len(all_ids)}] {pid}: "
                f"{fmt_bytes(loose_bytes)} loose / "
                f"{fmt_bytes(comp_bytes)} compressed "
                f"({loose_files} files -> {comp_archives} archives)"
            )
    return results


# ---------------------------------------------------------------------------
# Phase 2 — Rsync
# ---------------------------------------------------------------------------


def select_first_n_patients(limit: int) -> list[str]:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT patient_id
                FROM image_series
                WHERE patient_id IS NOT NULL AND patient_id != ''
                ORDER BY patient_id ASC
                LIMIT %s
                """,
                (limit,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def phase_rsync(
    loose_root: Path,
    compressed_root: Path,
    dest_root: Path,
    n: int,
) -> list[PatientRsync]:
    db_ids = select_first_n_patients(n)
    disk_ids = set(patient_ids_on_disk(loose_root))
    comp_ids = set(patient_ids_on_disk(compressed_root))
    # Keep only patients actually present in both trees so measurements are comparable.
    selected = [p for p in db_ids if p in disk_ids and p in comp_ids]
    if len(selected) < n:
        print(
            f"[RSYNC] Warning: DB returned {len(db_ids)} patients, "
            f"{len(selected)} present in both trees (wanted {n})."
        )
    else:
        print(f"[RSYNC] Patients: {selected}")

    results: list[PatientRsync] = []
    for i, pid in enumerate(selected, 1):
        src_loose = loose_root / pid
        src_comp = compressed_root / pid
        dst_loose = dest_root / "loose" / pid
        dst_comp = dest_root / "compressed" / pid

        print(f"\n[RSYNC] [{i}/{len(selected)}] Patient {pid}")

        print(f"  loose  full: {src_loose}/ -> {dst_loose}/")
        ensure_empty_dir(dst_loose)
        loose_full = _rsync_to_run(_run_rsync(src_loose, dst_loose))
        print(
            f"    {loose_full.elapsed_s:.2f}s "
            f"({loose_full.number_of_files} files, "
            f"{fmt_bytes(loose_full.total_file_size_bytes)})"
        )

        print(f"  comp   full: {src_comp}/ -> {dst_comp}/")
        ensure_empty_dir(dst_comp)
        comp_full = _rsync_to_run(_run_rsync(src_comp, dst_comp))
        print(
            f"    {comp_full.elapsed_s:.2f}s "
            f"({comp_full.number_of_files} files, "
            f"{fmt_bytes(comp_full.total_file_size_bytes)})"
        )

        # No-op runs (destination is now populated and matches)
        print(f"  loose  noop: {src_loose}/ -> {dst_loose}/ (already synced)")
        loose_noop = _rsync_to_run(_run_rsync(src_loose, dst_loose))
        print(f"    {loose_noop.elapsed_s:.2f}s")

        print(f"  comp   noop: {src_comp}/ -> {dst_comp}/ (already synced)")
        comp_noop = _rsync_to_run(_run_rsync(src_comp, dst_comp))
        print(f"    {comp_noop.elapsed_s:.2f}s")

        # Clean up so we don't leave ~150 GiB of scratch behind.
        shutil.rmtree(dst_loose, ignore_errors=True)
        shutil.rmtree(dst_comp, ignore_errors=True)

        results.append(
            PatientRsync(
                patient_id=pid,
                loose_full=loose_full,
                compressed_full=comp_full,
                loose_noop=loose_noop,
                compressed_noop=comp_noop,
            )
        )
    # Best-effort cleanup of parent scratch dirs.
    shutil.rmtree(dest_root, ignore_errors=True)
    return results


# ---------------------------------------------------------------------------
# Phase 3 — Decompression scatter
# ---------------------------------------------------------------------------


def pick_decompress_patients(
    storage_results: list[PatientStorage], count: int
) -> list[str]:
    ranked = sorted(
        [r for r in storage_results if r.loose_bytes > 0 and r.compressed_archives > 0],
        key=lambda r: r.loose_bytes,
        reverse=True,
    )
    n = len(ranked)
    if n <= count:
        return [r.patient_id for r in ranked]
    picks: list[str] = []
    seen: set[str] = set()
    for j in range(count):
        idx = min(n - 1, int((j + 0.5) * n / count))
        pid = ranked[idx].patient_id
        if pid in seen:
            continue
        seen.add(pid)
        picks.append(pid)
    return picks


def phase_decompress(
    compressed_root: Path,
    scratch: Path,
    patient_ids: list[str],
) -> tuple[list[SeriesDecompress], list[StudyDecompress]]:
    series_results: list[SeriesDecompress] = []
    study_results: list[StudyDecompress] = []

    for i, pid in enumerate(patient_ids, 1):
        pdir = compressed_root / pid
        if not pdir.is_dir():
            print(f"[DECOMPRESS] SKIP {pid}: no compressed dir")
            continue
        archives = sorted(pdir.rglob("*.tar.zst"))
        print(f"\n[DECOMPRESS] [{i}/{len(patient_ids)}] {pid} — {len(archives)} archives")

        # Group by study (second path component)
        by_study: dict[str, list[Path]] = {}
        for arch in archives:
            _, study_uid, _ = parse_archive_path(arch, compressed_root)
            by_study.setdefault(study_uid, []).append(arch)

        # -------- Series-level --------
        for arch in archives:
            _, study_uid, series_uid = parse_archive_path(arch, compressed_root)
            dest = scratch / "series" / series_uid
            try:
                elapsed = untar_zst_to(arch, dest)
                ext_bytes, ext_files = tree_stats(dest)
            except Exception as e:
                print(f"    series FAIL {series_uid[:20]}: {e}")
                shutil.rmtree(dest, ignore_errors=True)
                continue
            arch_bytes = arch.stat().st_size
            series_results.append(
                SeriesDecompress(
                    patient_id=pid,
                    study_uid=study_uid,
                    series_uid=series_uid,
                    archive_bytes=arch_bytes,
                    extracted_bytes=ext_bytes,
                    extracted_files=ext_files,
                    elapsed_s=elapsed,
                )
            )
            shutil.rmtree(dest, ignore_errors=True)

        n_series_done = sum(1 for s in series_results if s.patient_id == pid)
        print(f"    {n_series_done} series measured")

        # -------- Study-level --------
        for study_uid, arch_list in by_study.items():
            study_dest = scratch / "study" / study_uid
            ensure_empty_dir(study_dest)
            total_elapsed = 0.0
            total_arch = 0
            total_ext_bytes = 0
            total_ext_files = 0
            try:
                for arch in arch_list:
                    _, _, series_uid = parse_archive_path(arch, compressed_root)
                    sdest = study_dest / series_uid
                    total_elapsed += untar_zst_to(arch, sdest)
                    total_arch += arch.stat().st_size
                    b, f = tree_stats(sdest)
                    total_ext_bytes += b
                    total_ext_files += f
            except Exception as e:
                print(f"    study FAIL {study_uid[:20]}: {e}")
                shutil.rmtree(study_dest, ignore_errors=True)
                continue
            study_results.append(
                StudyDecompress(
                    patient_id=pid,
                    study_uid=study_uid,
                    n_series=len(arch_list),
                    archive_bytes=total_arch,
                    extracted_bytes=total_ext_bytes,
                    extracted_files=total_ext_files,
                    elapsed_s=total_elapsed,
                )
            )
            shutil.rmtree(study_dest, ignore_errors=True)

        n_studies_done = sum(1 for s in study_results if s.patient_id == pid)
        print(f"    {n_studies_done} studies measured")

    shutil.rmtree(scratch, ignore_errors=True)
    return series_results, study_results


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------


def _mpl():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def fig_storage_ratio(storage: list[PatientStorage], out: Path) -> None:
    plt = _mpl()
    items = [r for r in storage if r.ratio is not None]
    items.sort(key=lambda r: r.ratio or 1.0)
    ratios = [r.ratio for r in items]
    labels = [r.patient_id for r in items]

    total_loose = sum(r.loose_bytes for r in storage)
    total_comp = sum(r.compressed_bytes for r in storage)
    overall = (total_comp / total_loose) if total_loose else None

    fig, ax = plt.subplots(figsize=(10, max(4, len(items) * 0.18)))
    ax.barh(range(len(items)), ratios, color="steelblue", height=0.8)
    ax.set_yticks(range(len(items)))
    ax.set_yticklabels(labels, fontsize=7)
    ax.set_xlabel("Compressed / Loose ratio")
    ax.set_title(
        f"Per-patient tar.zst compression ratio (n={len(items)})\n"
        f"overall: {overall:.3f} ({(1-overall)*100:.1f}% saved)"
        if overall
        else "Per-patient tar.zst compression ratio"
    )
    if overall:
        ax.axvline(overall, color="crimson", linestyle="--", label=f"overall={overall:.3f}")
        ax.legend(loc="lower right")
    ax.set_xlim(0, max(ratios + [overall or 0, 1.0]) * 1.05)
    ax.grid(axis="x", linestyle=":", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)


def fig_rsync(rsync_results: list[PatientRsync], mode: str, out: Path) -> None:
    """mode: 'full' or 'noop'"""
    plt = _mpl()
    labels = [r.patient_id for r in rsync_results]
    if mode == "full":
        loose = [r.loose_full.elapsed_s if r.loose_full else 0 for r in rsync_results]
        comp = [r.compressed_full.elapsed_s if r.compressed_full else 0 for r in rsync_results]
        title = "Rsync full cold copy (empty destination)"
    else:
        loose = [r.loose_noop.elapsed_s if r.loose_noop else 0 for r in rsync_results]
        comp = [r.compressed_noop.elapsed_s if r.compressed_noop else 0 for r in rsync_results]
        title = "Rsync no-op (already synced, metadata walk only)"

    import numpy as np

    x = np.arange(len(labels))
    w = 0.4
    fig, ax = plt.subplots(figsize=(max(8, len(labels) * 0.35), 5))
    ax.bar(x - w / 2, loose, w, label="loose", color="tomato")
    ax.bar(x + w / 2, comp, w, label="compressed", color="steelblue")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=60, ha="right", fontsize=8)
    ax.set_ylabel("Elapsed time (s)")
    mean_loose = sum(loose) / len(loose) if loose else 0
    mean_comp = sum(comp) / len(comp) if comp else 0
    speedup = (mean_loose / mean_comp) if mean_comp else float("nan")
    ax.set_title(
        f"{title}\n"
        f"mean loose={mean_loose:.2f}s, mean compressed={mean_comp:.2f}s "
        f"(speedup ≈ {speedup:.1f}×)"
    )
    ax.legend()
    ax.grid(axis="y", linestyle=":", alpha=0.4)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)


def _scatter(
    points: list[tuple[float, float, str]],
    title: str,
    out: Path,
    y_unit: str = "s",
) -> None:
    """points: list of (size_MiB, elapsed_s, patient_id)"""
    plt = _mpl()
    if not points:
        return
    import numpy as np

    sizes = np.array([p[0] for p in points])
    times = np.array([p[1] for p in points])
    pids = [p[2] for p in points]

    unique = sorted(set(pids))
    cmap = plt.get_cmap("tab20" if len(unique) > 10 else "tab10")
    colors = {pid: cmap(i % cmap.N) for i, pid in enumerate(unique)}

    fig, ax = plt.subplots(figsize=(9, 6))
    for pid in unique:
        mask = [p == pid for p in pids]
        ax.scatter(
            sizes[mask], times[mask], s=30, alpha=0.75, color=colors[pid], label=pid
        )

    # Best-fit line through origin (y = k*x)
    k = None
    if sizes.size > 1 and sizes.sum() > 0:
        k = float(np.sum(times * sizes) / np.sum(sizes * sizes))
        xx = np.linspace(0, sizes.max(), 50)
        ax.plot(
            xx, k * xx, color="black", linestyle="--", linewidth=1,
            label=f"fit: {1/k:.1f} MiB/{y_unit} ({k*1000:.2f} ms/MiB)"
            if k > 0 else "fit",
        )

    ax.set_xlabel("Extracted size (MiB)")
    ax.set_ylabel(f"Elapsed ({y_unit})")
    ax.set_title(title)
    ax.grid(linestyle=":", alpha=0.4)
    ax.legend(fontsize=7, ncol=2, loc="upper left")
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)


def fig_decompress_series(series: list[SeriesDecompress], out: Path) -> None:
    pts = [
        (s.extracted_bytes / (1024 * 1024), s.elapsed_s, s.patient_id)
        for s in series
    ]
    _scatter(pts, f"Decompression time vs size — series (n={len(series)})", out)


def fig_decompress_study(studies: list[StudyDecompress], out: Path) -> None:
    pts = [
        (s.extracted_bytes / (1024 * 1024), s.elapsed_s, s.patient_id)
        for s in studies
    ]
    _scatter(pts, f"Decompression time vs size — study (n={len(studies)})", out)


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def render_markdown(payload: dict[str, Any], out_md: Path) -> None:
    lines: list[str] = []
    w = lines.append

    ts = payload.get("ts", "")
    host = payload.get("host", "")
    args = payload.get("args", {})

    w(f"# Cold storage benchmark report")
    w("")
    w(f"- Host: `{host}`")
    w(f"- Timestamp: {ts}")
    w(f"- Loose root: `{args.get('loose_root')}`")
    w(f"- Compressed root: `{args.get('compressed_root')}`")
    w(f"- Rsync scratch: `{args.get('rsync_dest')}` (same filesystem as sources)")
    w("")

    # --- Summary ---
    storage = payload.get("storage", [])
    rsync = payload.get("rsync", [])
    series = payload.get("decompress_series", [])
    studies = payload.get("decompress_study", [])

    total_loose = sum(r["loose_bytes"] for r in storage)
    total_comp = sum(r["compressed_bytes"] for r in storage)
    total_files = sum(r["loose_files"] for r in storage)
    total_arch = sum(r["compressed_archives"] for r in storage)
    overall = (total_comp / total_loose) if total_loose else None

    w("## Summary")
    w("")
    if overall is not None:
        w(f"- **Storage saved:** {fmt_bytes(total_loose - total_comp)} "
          f"({(1-overall)*100:.1f}% of loose footprint), "
          f"ratio = {overall:.3f}")
    w(f"- **Loose footprint:** {fmt_bytes(total_loose)} across "
      f"{len(storage)} patients / {total_files:,} DICOM files")
    w(f"- **Compressed footprint:** {fmt_bytes(total_comp)} across "
      f"{total_arch:,} tar.zst archives")
    if rsync:
        def _mean(key: str) -> float:
            vals = [r[key]["elapsed_s"] for r in rsync if r.get(key)]
            return sum(vals) / len(vals) if vals else 0.0
        lf = _mean("loose_full")
        cf = _mean("compressed_full")
        ln = _mean("loose_noop")
        cn = _mean("compressed_noop")
        w(f"- **Rsync full (mean, n={len(rsync)}):** loose {lf:.2f}s, "
          f"compressed {cf:.2f}s → {lf/cf:.1f}× speedup" if cf else
          f"- **Rsync full (mean, n={len(rsync)}):** loose {lf:.2f}s")
        w(f"- **Rsync no-op (mean, n={len(rsync)}):** loose {ln:.2f}s, "
          f"compressed {cn:.2f}s → {ln/cn:.1f}× speedup" if cn else
          f"- **Rsync no-op (mean, n={len(rsync)}):** loose {ln:.2f}s")
    if series:
        total_s_bytes = sum(s["extracted_bytes"] for s in series)
        total_s_time = sum(s["elapsed_s"] for s in series)
        if total_s_time:
            thr = total_s_bytes / total_s_time / (1024 * 1024)
            w(f"- **Decompression (series, n={len(series)}):** "
              f"{thr:.1f} MiB/s mean throughput")
    if studies:
        total_st_bytes = sum(s["extracted_bytes"] for s in studies)
        total_st_time = sum(s["elapsed_s"] for s in studies)
        if total_st_time:
            thr = total_st_bytes / total_st_time / (1024 * 1024)
            w(f"- **Decompression (study, n={len(studies)}):** "
              f"{thr:.1f} MiB/s mean throughput")
    w("")

    # --- Storage ---
    w("## 1. Storage footprint")
    w("")
    w("Walks both trees on disk; no DB dependency. Archive format is per-series "
      "`*.tar.zst` under the compressed root; loose tree has one file per DICOM slice.")
    w("")
    w(f"![Per-patient compression ratio](figures/storage_ratio_per_patient.png)")
    w("")
    w("### Aggregate")
    w("")
    w("| metric | loose | compressed |")
    w("|---|---|---|")
    w(f"| total bytes | {fmt_bytes(total_loose)} | {fmt_bytes(total_comp)} |")
    w(f"| items | {total_files:,} files | {total_arch:,} archives |")
    if overall is not None:
        w(f"| ratio (c/l) | — | **{overall:.3f}** ({(1-overall)*100:.1f}% saved) |")
    w("")
    w("### Per-patient")
    w("")
    w("| patient_id | loose | files | compressed | archives | ratio |")
    w("|---|---:|---:|---:|---:|---:|")
    for r in sorted(storage, key=lambda x: x["patient_id"]):
        rat = f"{r['ratio']:.3f}" if r["ratio"] is not None else "—"
        w(
            f"| `{r['patient_id']}` | {fmt_bytes(r['loose_bytes'])} | "
            f"{r['loose_files']:,} | {fmt_bytes(r['compressed_bytes'])} | "
            f"{r['compressed_archives']:,} | {rat} |"
        )
    w("")

    # --- Rsync ---
    if rsync:
        w("## 2. Backup speed (rsync)")
        w("")
        w("First 30 patient_ids (alphabetical) common to both trees. Each patient "
          "is rsynced to a scratch directory on the **same filesystem** as the "
          "source (`/DATA2`), twice: once cold (empty destination → full copy), "
          "and once no-op (destination already populated → metadata walk only).")
        w("")
        w("**Caveats:**")
        w("- Same-fs rsync captures CPU, metadata-walk, and local IO costs but **not** "
          "network throughput. Absolute speeds understate wins for remote backups.")
        w("- The loose-vs-compressed **ratio** is still meaningful: the compressed "
          "tree has O(archives) files vs. O(slices), and DICOM backups are usually "
          "metadata-bound at scale.")
        w("")
        w(f"![rsync full](figures/rsync_full_first30.png)")
        w("")
        w(f"![rsync no-op](figures/rsync_noop_first30.png)")
        w("")
        w("### Full cold copy")
        w("")
        w("| patient_id | loose (s) | files | compressed (s) | archives | speedup |")
        w("|---|---:|---:|---:|---:|---:|")
        for r in rsync:
            lf = r.get("loose_full") or {}
            cf = r.get("compressed_full") or {}
            lf_s = lf.get("elapsed_s", 0)
            cf_s = cf.get("elapsed_s", 0)
            sp = f"{lf_s/cf_s:.1f}×" if cf_s else "—"
            w(
                f"| `{r['patient_id']}` | {lf_s:.2f} | "
                f"{lf.get('number_of_files', '—')} | {cf_s:.2f} | "
                f"{cf.get('number_of_files', '—')} | {sp} |"
            )
        w("")
        w("### No-op (metadata walk)")
        w("")
        w("| patient_id | loose (s) | compressed (s) | speedup |")
        w("|---|---:|---:|---:|")
        for r in rsync:
            ln = (r.get("loose_noop") or {}).get("elapsed_s", 0)
            cn = (r.get("compressed_noop") or {}).get("elapsed_s", 0)
            sp = f"{ln/cn:.1f}×" if cn else "—"
            w(f"| `{r['patient_id']}` | {ln:.2f} | {cn:.2f} | {sp} |")
        w("")

    # --- Decompression ---
    if series or studies:
        w("## 3. Decompression cost")
        w("")
        w("10 patients evenly spaced across the loose-footprint size distribution. "
          "Each series is extracted individually to a scratch dir (isolated); each "
          "study is extracted as a sequential bundle of its series (mimics the "
          "`cache_manager.warm_study` flow).")
        w("")
        w(f"![Series decompression scatter](figures/decompression_series_scatter.png)")
        w("")
        w(f"![Study decompression scatter](figures/decompression_study_scatter.png)")
        w("")
        if series:
            w("### Series — throughput distribution")
            w("")
            throughputs = [
                (s["extracted_bytes"] / s["elapsed_s"]) / (1024 * 1024)
                for s in series if s["elapsed_s"] > 0
            ]
            if throughputs:
                throughputs.sort()
                n = len(throughputs)
                p50 = throughputs[n // 2]
                p10 = throughputs[max(0, n // 10)]
                p90 = throughputs[min(n - 1, (9 * n) // 10)]
                w(f"- n = {n}")
                w(f"- p10 / p50 / p90 throughput: "
                  f"{p10:.1f} / {p50:.1f} / {p90:.1f} MiB/s")
            w("")
        if studies:
            w("### Study — throughput distribution")
            w("")
            throughputs = [
                (s["extracted_bytes"] / s["elapsed_s"]) / (1024 * 1024)
                for s in studies if s["elapsed_s"] > 0
            ]
            if throughputs:
                throughputs.sort()
                n = len(throughputs)
                p50 = throughputs[n // 2]
                p10 = throughputs[max(0, n // 10)]
                p90 = throughputs[min(n - 1, (9 * n) // 10)]
                w(f"- n = {n}")
                w(f"- p10 / p50 / p90 throughput: "
                  f"{p10:.1f} / {p50:.1f} / {p90:.1f} MiB/s")
            w("")

    w("## Method")
    w("")
    w(f"- Script: `benchmarks/cold_storage_report.py`")
    w(f"- Reused primitives from `benchmarks/cold_storage_evaluation.py`")
    w(f"- CLI args:")
    w("```json")
    w(json.dumps(args, indent=2))
    w("```")

    out_md.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="Cold storage benchmark report")
    ap.add_argument("--loose-root", type=Path, default=DEFAULT_LOOSE_ROOT)
    ap.add_argument("--compressed-root", type=Path, default=DEFAULT_COMPRESSED_ROOT)
    ap.add_argument("--rsync-dest", type=Path, default=DEFAULT_RSYNC_DEST)
    ap.add_argument("--decompress-scratch", type=Path, default=DEFAULT_DECOMPRESS_SCRATCH)
    ap.add_argument("--rsync-n", type=int, default=30)
    ap.add_argument("--decompress-n", type=int, default=10)
    ap.add_argument(
        "--phases",
        nargs="*",
        default=["storage", "rsync", "decompress", "report"],
        choices=["storage", "rsync", "decompress", "report"],
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=BENCH_DIR / "cold_storage_report_results.json",
    )
    ap.add_argument(
        "--out-md",
        type=Path,
        default=BENCH_DIR / "cold_storage_report.md",
    )
    args = ap.parse_args()

    FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing JSON if only running subset of phases.
    payload: dict[str, Any] = {}
    if args.out_json.exists():
        try:
            payload = json.loads(args.out_json.read_text())
        except Exception:
            payload = {}

    payload.update(
        {
            "host": os.uname().nodename,
            "ts": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "args": {
                "loose_root": str(args.loose_root.resolve()),
                "compressed_root": str(args.compressed_root.resolve()),
                "rsync_dest": str(args.rsync_dest.resolve()),
                "decompress_scratch": str(args.decompress_scratch.resolve()),
                "rsync_n": args.rsync_n,
                "decompress_n": args.decompress_n,
                "phases": args.phases,
            },
        }
    )

    # ------------------ Phase 1 ------------------
    if "storage" in args.phases:
        print("\n=== PHASE 1: storage footprint ===")
        storage = phase_storage(args.loose_root, args.compressed_root)
        payload["storage"] = [asdict(r) for r in storage]
        args.out_json.write_text(json.dumps(payload, indent=2))

    # ------------------ Phase 2 ------------------
    if "rsync" in args.phases:
        print("\n=== PHASE 2: rsync ===")
        if not DB_CONFIG.get("user"):
            print("DB_USER not set in .env — cannot select patients.", file=sys.stderr)
            return 1
        args.rsync_dest.parent.mkdir(parents=True, exist_ok=True)
        rsync = phase_rsync(
            args.loose_root, args.compressed_root, args.rsync_dest, args.rsync_n
        )
        payload["rsync"] = [
            {
                "patient_id": r.patient_id,
                "loose_full": asdict(r.loose_full) if r.loose_full else None,
                "compressed_full": asdict(r.compressed_full) if r.compressed_full else None,
                "loose_noop": asdict(r.loose_noop) if r.loose_noop else None,
                "compressed_noop": asdict(r.compressed_noop) if r.compressed_noop else None,
            }
            for r in rsync
        ]
        args.out_json.write_text(json.dumps(payload, indent=2))

    # ------------------ Phase 3 ------------------
    if "decompress" in args.phases:
        print("\n=== PHASE 3: decompression ===")
        storage_list = payload.get("storage")
        if not storage_list:
            print("No storage data in JSON — run --phases storage first.",
                  file=sys.stderr)
            return 1
        storage_objs = [PatientStorage(**r) for r in storage_list]
        patients = pick_decompress_patients(storage_objs, args.decompress_n)
        print(f"[DECOMPRESS] Patients: {patients}")
        args.decompress_scratch.mkdir(parents=True, exist_ok=True)
        series, studies = phase_decompress(
            args.compressed_root, args.decompress_scratch, patients
        )
        payload["decompress_series"] = [asdict(r) for r in series]
        payload["decompress_study"] = [asdict(r) for r in studies]
        args.out_json.write_text(json.dumps(payload, indent=2))

    # ------------------ Phase 4: figures + markdown ------------------
    if "report" in args.phases:
        print("\n=== PHASE 4: figures + markdown ===")
        if "storage" in payload:
            storage_objs = [PatientStorage(**r) for r in payload["storage"]]
            fig_storage_ratio(storage_objs, FIGURES_DIR / "storage_ratio_per_patient.png")
            print("  wrote figures/storage_ratio_per_patient.png")
        if "rsync" in payload and payload["rsync"]:
            def _to_rsync(d: dict) -> PatientRsync:
                def _r(k: str) -> RsyncRun | None:
                    v = d.get(k)
                    return RsyncRun(**v) if v else None
                return PatientRsync(
                    patient_id=d["patient_id"],
                    loose_full=_r("loose_full"),
                    compressed_full=_r("compressed_full"),
                    loose_noop=_r("loose_noop"),
                    compressed_noop=_r("compressed_noop"),
                )
            rsync_objs = [_to_rsync(d) for d in payload["rsync"]]
            fig_rsync(rsync_objs, "full", FIGURES_DIR / "rsync_full_first30.png")
            fig_rsync(rsync_objs, "noop", FIGURES_DIR / "rsync_noop_first30.png")
            print("  wrote figures/rsync_full_first30.png, rsync_noop_first30.png")
        if payload.get("decompress_series"):
            series = [SeriesDecompress(**r) for r in payload["decompress_series"]]
            fig_decompress_series(series, FIGURES_DIR / "decompression_series_scatter.png")
            print("  wrote figures/decompression_series_scatter.png")
        if payload.get("decompress_study"):
            studies = [StudyDecompress(**r) for r in payload["decompress_study"]]
            fig_decompress_study(studies, FIGURES_DIR / "decompression_study_scatter.png")
            print("  wrote figures/decompression_study_scatter.png")

        render_markdown(payload, args.out_md)
        print(f"  wrote {args.out_md}")

    print(f"\nDone. JSON: {args.out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
