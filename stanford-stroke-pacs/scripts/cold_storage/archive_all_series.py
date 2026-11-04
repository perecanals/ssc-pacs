#!/usr/bin/env python3
"""
Offline job: build one tar.zst per series from image_series.dicom_dir_path and
populate dicom_archive_path. Does not delete loose DICOMs.

Dry-run by default (prints the pending series + sample archive paths);
``--execute`` writes archives and updates image_series. Idempotent: existing
archives are skipped, so it doubles as the retry path for compression failures.

Default layout matches the evaluation script:
  archive = <volume root> / relpath(dicom_dir, DICOM_ROOT).parent / f"{leaf}.tar.zst"

Archive volumes: a series goes to the volume its study's archives are already
on (one study, one volume); a study with none archived yet goes to --volume
(default: config.toml default_archive_volume). Every target volume must be
online (its .ssc-volume sentinel readable) before anything is written.
"""

from __future__ import annotations

import argparse
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

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

# Paths from repo-root config.toml (see web-app/config.py)
sys.path.insert(0, str(REPO_ROOT / "web-app"))
from archive_volumes import (  # noqa: E402
    ArchiveVolumeError,
    configured_volumes,
    default_volume,
    get_volume,
    status,
    volume_for_path,
    volume_of_archives,
)
from common import subject_for_patient_arg  # noqa: E402
from db import DB_CONFIG  # noqa: E402

from cache_manager import archive_path_for_series_dir  # noqa: E402
from config import DICOM_DATA_ROOT  # noqa: E402

DEFAULT_DICOM_ROOT = DICOM_DATA_ROOT


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


def compress_one_job(args: tuple[str, str, str, str, int]) -> dict[str, Any]:
    """Worker: (series_uid, dicom_dir_str, data_root_str, cold_root_str, zstd_level)."""
    series_uid, dicom_dir_s, data_root_s, cold_root_s, zstd_level = args
    dicom_dir = Path(dicom_dir_s)
    cold_root = Path(cold_root_s)
    data_root = Path(data_root_s)
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
        arch = archive_path_for_series_dir(dicom_dir, data_root, cold_root)
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


def fetch_rows(conn, subject_id: str | None, new_study_root: Path) -> list[tuple[str, str, Path]]:
    """``(series_uid, dicom_dir, archive root)`` per series with a loose dir.

    The root is the volume the study's archives already live on, else
    ``new_study_root``. A study whose archives are outside every volume or span
    volumes is skipped (reported) rather than re-filed on a guess.
    """
    q = (
        "SELECT s.seriesinstanceuid, s.dicom_dir_path, s.studyinstanceuid, "
        "       (SELECT array_agg(o.dicom_archive_path) FROM image_series o "
        "        WHERE o.studyinstanceuid = s.studyinstanceuid "
        "          AND coalesce(o.dicom_archive_path, '') <> '') "
        "FROM image_series s "
        "WHERE s.dicom_dir_path IS NOT NULL AND s.dicom_dir_path != ''"
    )
    params: list[Any] = []
    if subject_id:
        q += " AND s.subject_id = %s"
        params.append(subject_id)
    with conn.cursor() as cur:
        cur.execute(q, params)
        raw = cur.fetchall()
    out = []
    for suid, dpath, study_uid, archives in raw:
        try:
            vol = volume_of_archives(archives or [])
        except ArchiveVolumeError as exc:
            print(f"  SKIP {suid} (study {study_uid}): {exc}", file=sys.stderr)
            continue
        out.append((suid, dpath, vol.root if vol else new_study_root))
    return out


def update_archive_path(conn, series_uid: str, archive_path: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE image_series SET dicom_archive_path = %s WHERE seriesinstanceuid = %s",
            (archive_path, series_uid),
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--dicom-root", "--legacy-root", dest="dicom_root", type=Path,
        default=DEFAULT_DICOM_ROOT,
        help="Uncompressed DICOM root (config.toml dicom_data_root). "
             "--legacy-root is a deprecated alias.",
    )
    target = ap.add_mutually_exclusive_group()
    target.add_argument("--volume", help="Archive volume for studies with no archive yet "
                                         "(default: config.toml default_archive_volume)")
    target.add_argument("--cold-root", type=Path,
                        help="Deprecated: a configured volume's root (use --volume)")
    ap.add_argument("--patient",
                    help="Only this patient's series: a patient_key, or a patient_id "
                         "that names one person")
    ap.add_argument(
        "--execute", action="store_true",
        help="Write archives and update image_series. Default is a dry-run preview.",
    )
    ap.add_argument("--workers", type=int, default=1, help="Process workers (1 = sequential in main)")
    ap.add_argument("--zstd-level", type=int, default=6)
    args = ap.parse_args()

    if not DB_CONFIG.get("user"):
        print("DB_USER not set", file=sys.stderr)
        return 1

    data_root = args.dicom_root.resolve()
    try:
        if args.cold_root:
            vol = volume_for_path(args.cold_root.resolve() / "x")
            if vol is None or vol.root != args.cold_root.resolve():
                raise ArchiveVolumeError(f"--cold-root {args.cold_root} is not a configured archive "
                                         f"volume root; configured: {sorted(configured_volumes())}")
        else:
            vol = get_volume(args.volume) if args.volume else default_volume()
    except ArchiveVolumeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"New studies go to archive volume {vol.name} ({vol.root})")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        rows = fetch_rows(
            conn, subject_for_patient_arg(args.patient) if args.patient else None, vol.root
        )
    finally:
        conn.close()

    # Never write onto an unmounted disk (its empty mount point).
    by_root = {v.root: v for v in configured_volumes().values()}
    offline = [(v, st) for v in {by_root[r] for _, _, r in rows if r in by_root}
               if not (st := status(v, max_age=0)).online]
    for v, st in offline:
        print(f"Error: archive volume {v.name} ({v.root}) is offline: {st.reason}", file=sys.stderr)
    if offline and args.execute:
        return 1

    print(f"Series to process: {len(rows)}")
    if not args.execute:
        for suid, dpath, cold_root in rows[:20]:
            try:
                arch = archive_path_for_series_dir(Path(dpath), data_root, cold_root)
                print(f"  {suid} -> {arch}")
            except Exception as e:
                print(f"  {suid} SKIP {e}")
        if len(rows) > 20:
            print(f"  ... and {len(rows) - 20} more")
        print("Dry-run (default): nothing written. Re-run with --execute to archive.")
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
            for i, (suid, dpath, cold_root) in enumerate(rows):
                dicom_dir = Path(dpath)
                try:
                    arch = archive_path_for_series_dir(dicom_dir, data_root, cold_root)
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
            (suid, dpath, str(data_root), str(cold_root), args.zstd_level)
            for suid, dpath, cold_root in rows
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
