#!/usr/bin/env python3
"""
Offline job: build one tar.zst per series from image_series.dicom_dir_path and
populate dicom_archive_path. Does not delete loose DICOMs.

Default layout matches the evaluation script:
  archive = COLD_ROOT / relpath(dicom_dir, LEGACY_ROOT).parent / f"{leaf}.tar.zst"
"""

from __future__ import annotations

import argparse
import os
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
import zstandard as zstd
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

# Paths from repo-root config.toml (see companion/config.py)
sys.path.insert(0, str(REPO_ROOT / "companion"))
from config import COLD_ARCHIVE_ROOT, LEGACY_DICOM_ROOT  # noqa: E402
from db import DB_CONFIG, get_conn  # noqa: E402

DEFAULT_LEGACY = LEGACY_DICOM_ROOT
DEFAULT_COLD = COLD_ARCHIVE_ROOT


def iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def tar_zst_dir(src: Path, out_path: Path, level: int) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    cctx = zstd.ZstdCompressor(level=level, threads=-1)
    with out_path.open("wb") as f_out:
        with cctx.stream_writer(f_out) as z_out:
            with tarfile.open(fileobj=z_out, mode="w|") as tf:
                for f in iter_files(src):
                    tf.add(f, arcname=str(f.relative_to(src)))


def archive_path_for_series_dir(dicom_dir: Path, legacy_root: Path, cold_root: Path) -> Path:
    dicom_dir = dicom_dir.resolve()
    legacy_root = legacy_root.resolve()
    rel = dicom_dir.relative_to(legacy_root)
    return cold_root / rel.parent / f"{rel.name}.tar.zst"


def compress_one_job(args: tuple[str, str, str, str, int]) -> dict[str, Any]:
    """Worker: (series_uid, dicom_dir_str, legacy_root_str, cold_root_str, zstd_level)."""
    series_uid, dicom_dir_s, legacy_root_s, cold_root_s, zstd_level = args
    dicom_dir = Path(dicom_dir_s)
    cold_root = Path(cold_root_s)
    legacy_root = Path(legacy_root_s)
    t0 = time.perf_counter()
    out: dict[str, Any] = {
        "seriesinstanceuid": series_uid,
        "ok": False,
        "archive_path": None,
        "seconds": 0.0,
        "error": None,
    }
    try:
        if not dicom_dir.is_dir():
            out["error"] = "not_a_dir"
            return out
        arch = archive_path_for_series_dir(dicom_dir, legacy_root, cold_root)
        if arch.is_file() and arch.stat().st_size > 0:
            out["ok"] = True
            out["archive_path"] = str(arch)
            out["seconds"] = 0.0
            out["skipped"] = True
            return out
        tar_zst_dir(dicom_dir, arch, level=zstd_level)
        out["ok"] = True
        out["archive_path"] = str(arch)
        out["seconds"] = time.perf_counter() - t0
    except Exception as e:
        out["error"] = str(e)
    return out


def fetch_rows(conn, patient: str | None) -> list[tuple[str, str]]:
    q = (
        "SELECT seriesinstanceuid, dicom_dir_path FROM image_series "
        "WHERE dicom_dir_path IS NOT NULL AND dicom_dir_path != ''"
    )
    params: list[Any] = []
    if patient:
        q += " AND patient_id = %s"
        params.append(patient)
    with conn.cursor() as cur:
        cur.execute(q, params)
        return [(r[0], r[1]) for r in cur.fetchall()]


def update_archive_path(conn, series_uid: str, archive_path: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE image_series SET dicom_archive_path = %s WHERE seriesinstanceuid = %s",
            (archive_path, series_uid),
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--legacy-root", type=Path, default=DEFAULT_LEGACY)
    ap.add_argument("--cold-root", type=Path, default=DEFAULT_COLD)
    ap.add_argument("--patient", help="Only series for this patient_id")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=1, help="Process workers (1 = sequential in main)")
    ap.add_argument("--zstd-level", type=int, default=6)
    args = ap.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set", file=sys.stderr)
        return 1

    legacy_root = args.legacy_root.resolve()
    cold_root = args.cold_root.resolve()
    cold_root.mkdir(parents=True, exist_ok=True)

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        rows = fetch_rows(conn, args.patient)
    finally:
        conn.close()

    print(f"Series to process: {len(rows)}")
    if args.dry_run:
        for suid, dpath in rows[:20]:
            try:
                arch = archive_path_for_series_dir(Path(dpath), legacy_root, cold_root)
                print(f"  {suid} -> {arch}")
            except Exception as e:
                print(f"  {suid} SKIP {e}")
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")
        return 0

    total = len(rows)
    ok = skip = err = 0
    t_start = time.perf_counter()

    def _progress_line() -> str:
        done = ok + skip + err
        elapsed = time.perf_counter() - t_start
        pct = done / total * 100 if total else 0
        rate = done / elapsed if elapsed > 0 else 0
        eta_s = (total - done) / rate if rate > 0 else 0
        eta_m, eta_sec = divmod(int(eta_s), 60)
        eta_h, eta_m = divmod(eta_m, 60)
        return (
            f"[{done}/{total} {pct:5.1f}%] "
            f"ok={ok} skip={skip} err={err} | "
            f"{rate:.1f} series/s | "
            f"elapsed {int(elapsed)}s | "
            f"ETA {eta_h}h{eta_m:02d}m{eta_sec:02d}s"
        )

    if args.workers <= 1:
        conn = psycopg2.connect(**DB_CONFIG)
        try:
            for i, (suid, dpath) in enumerate(rows):
                dicom_dir = Path(dpath)
                try:
                    arch = archive_path_for_series_dir(dicom_dir, legacy_root, cold_root)
                    if arch.is_file() and arch.stat().st_size > 0:
                        update_archive_path(conn, suid, str(arch))
                        conn.commit()
                        skip += 1
                        if skip % 100 == 0:
                            print(f"  SKIP (existing) {suid}  {_progress_line()}")
                        continue
                    if not dicom_dir.is_dir():
                        print(f"  ERR  {suid} missing dir {dicom_dir}")
                        err += 1
                        continue
                    t0 = time.perf_counter()
                    tar_zst_dir(dicom_dir, arch, level=args.zstd_level)
                    update_archive_path(conn, suid, str(arch))
                    conn.commit()
                    ok += 1
                    print(f"  OK   {suid} {time.perf_counter() - t0:.2f}s -> {arch.name}")
                except Exception as e:
                    conn.rollback()
                    print(f"  ERR  {suid} {e}", file=sys.stderr)
                    err += 1
                if (i + 1) % 50 == 0:
                    print(_progress_line(), flush=True)
        finally:
            conn.close()
    else:
        jobs = [
            (suid, dpath, str(legacy_root), str(cold_root), args.zstd_level)
            for suid, dpath in rows
        ]
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(compress_one_job, j): j[0] for j in jobs}
            conn = psycopg2.connect(**DB_CONFIG)
            try:
                for fut in as_completed(futs):
                    suid = futs[fut]
                    try:
                        r = fut.result()
                    except Exception as e:
                        print(f"  ERR  {suid} {e}", file=sys.stderr)
                        err += 1
                        print(_progress_line(), flush=True)
                        continue
                    if r.get("ok") and r.get("archive_path"):
                        update_archive_path(conn, r["seriesinstanceuid"], r["archive_path"])
                        conn.commit()
                        if r.get("skipped"):
                            skip += 1
                        else:
                            ok += 1
                            print(
                                f"  OK   {r['seriesinstanceuid']} "
                                f"{r['seconds']:.2f}s -> {Path(r['archive_path']).name}",
                                flush=True,
                            )
                    else:
                        conn.rollback()
                        err += 1
                        print(f"  ERR  {suid} {r.get('error')}", file=sys.stderr)
                    done = ok + skip + err
                    if done % 50 == 0 or done == total:
                        print(_progress_line(), flush=True)
            finally:
                conn.close()

    elapsed_total = time.perf_counter() - t_start
    m, s = divmod(int(elapsed_total), 60)
    h, m = divmod(m, 60)
    print(f"\nDone: ok={ok} skipped_existing={skip} errors={err} | total time {h}h{m:02d}m{s:02d}s")
    return 0 if err == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
