#!/usr/bin/env python3
"""
Verify every *.tar.zst under the cold archive root and optionally rebuild any
corrupt archives from the loose DICOM backup.

Verify:
  Streams the archive through zstd + tarfile and reads each member's payload
  in 1 MiB chunks. Catches truncation, zstd frame corruption, and mid-member
  data errors. Does not write anything to disk.

Repair (--repair):
  For each corrupt archive, locate the matching loose directory under
  --loose-root (same relative path, sans .tar.zst), re-run the tar+zstd
  compression to a temp file next to the original, atomically replace.
  Refuses to repair if the loose dir is missing.

Layout assumption (matches archive_all_series.py):
  compressed: {cold_root}/{patient}/{study}/{study_name}/{series}/DICOM.tar.zst
  loose:      {loose_root}/{patient}/{study}/{study_name}/{series}/DICOM/
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import zstandard as zstd
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

DEFAULT_COLD_ROOT = Path("/DATA2/pacs_imaging_data_compressed")
DEFAULT_LOOSE_ROOT = Path("/DATA2/pacs_imaging_data_loose_backup")
CHUNK = 1 << 20  # 1 MiB


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass
class VerifyResult:
    archive: str
    ok: bool
    bytes_read: int
    members: int
    elapsed_s: float
    error: str | None = None


def verify_archive(path: Path) -> VerifyResult:
    t0 = time.perf_counter()
    bytes_read = 0
    members = 0
    try:
        dctx = zstd.ZstdDecompressor()
        with path.open("rb") as f_in:
            with dctx.stream_reader(f_in) as z_in:
                with tarfile.open(fileobj=z_in, mode="r|") as tf:
                    for m in tf:
                        members += 1
                        if not m.isfile():
                            continue
                        ef = tf.extractfile(m)
                        if ef is None:
                            continue
                        while True:
                            chunk = ef.read(CHUNK)
                            if not chunk:
                                break
                            bytes_read += len(chunk)
        return VerifyResult(
            archive=str(path),
            ok=True,
            bytes_read=bytes_read,
            members=members,
            elapsed_s=time.perf_counter() - t0,
        )
    except Exception as e:
        return VerifyResult(
            archive=str(path),
            ok=False,
            bytes_read=bytes_read,
            members=members,
            elapsed_s=time.perf_counter() - t0,
            error=f"{type(e).__name__}: {e}",
        )


def verify_worker(path_str: str) -> dict[str, Any]:
    return asdict(verify_archive(Path(path_str)))


# ---------------------------------------------------------------------------
# Repair
# ---------------------------------------------------------------------------


def loose_dir_for_archive(
    archive: Path, cold_root: Path, loose_root: Path
) -> Path:
    """archive = .../{patient}/.../{series}/DICOM.tar.zst
    →       loose = .../{patient}/.../{series}/DICOM/
    """
    rel = archive.resolve().relative_to(cold_root.resolve())
    # Strip .tar.zst (two suffixes)
    name = rel.name
    if name.endswith(".tar.zst"):
        name = name[: -len(".tar.zst")]
    return loose_root / rel.parent / name


def rebuild_archive(src_dir: Path, out_path: Path, level: int) -> None:
    """Write tar.zst atomically: create .rebuild, then rename over out_path."""
    tmp = out_path.with_suffix(out_path.suffix + ".rebuild")
    if tmp.exists():
        tmp.unlink()
    cctx = zstd.ZstdCompressor(level=level, threads=-1)
    with tmp.open("wb") as f_out:
        with cctx.stream_writer(f_out) as z_out:
            with tarfile.open(fileobj=z_out, mode="w|") as tf:
                for p in src_dir.rglob("*"):
                    if p.is_file():
                        tf.add(p, arcname=str(p.relative_to(src_dir)))
    os.replace(tmp, out_path)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cold-root", type=Path, default=DEFAULT_COLD_ROOT)
    ap.add_argument("--loose-root", type=Path, default=DEFAULT_LOOSE_ROOT)
    ap.add_argument(
        "--patient",
        help="Limit to one patient (directory name under cold-root).",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=max(1, (os.cpu_count() or 4) // 2),
        help="Parallel verifiers (default: half the cores).",
    )
    ap.add_argument("--zstd-level", type=int, default=6)
    ap.add_argument(
        "--repair",
        action="store_true",
        help="Rebuild corrupt archives from the loose backup.",
    )
    ap.add_argument(
        "--execute",
        action="store_true",
        help="Without this, --repair is a dry-run (just lists actions).",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=REPO_ROOT / "scripts" / "verify_and_repair_archives_results.json",
    )
    args = ap.parse_args()

    cold_root: Path = args.cold_root.resolve()
    loose_root: Path = args.loose_root.resolve()

    if not cold_root.is_dir():
        print(f"Cold root missing: {cold_root}", file=sys.stderr)
        return 1

    scan_root = cold_root / args.patient if args.patient else cold_root
    if not scan_root.is_dir():
        print(f"Scan root missing: {scan_root}", file=sys.stderr)
        return 1

    archives = sorted(scan_root.rglob("*.tar.zst"))
    print(
        f"Scanning {len(archives):,} archives under {scan_root} "
        f"with {args.workers} workers..."
    )
    if not archives:
        return 0

    t_start = time.perf_counter()
    bad: list[VerifyResult] = []
    total_bytes = 0
    done = 0
    last_print = 0.0

    if args.workers <= 1:
        for p in archives:
            r = verify_archive(p)
            done += 1
            total_bytes += r.bytes_read
            if not r.ok:
                bad.append(r)
                print(f"  CORRUPT  {p}  {r.error}")
            now = time.perf_counter()
            if now - last_print > 5 or done == len(archives):
                last_print = now
                el = now - t_start
                rate = total_bytes / el / (1024 * 1024) if el > 0 else 0
                print(
                    f"  [{done:,}/{len(archives):,}] "
                    f"{done/len(archives)*100:5.1f}% | "
                    f"corrupt={len(bad)} | {rate:.0f} MiB/s",
                    flush=True,
                )
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(verify_worker, str(p)): p for p in archives}
            for fut in as_completed(futs):
                rd = fut.result()
                r = VerifyResult(**rd)
                done += 1
                total_bytes += r.bytes_read
                if not r.ok:
                    bad.append(r)
                    print(f"  CORRUPT  {r.archive}  {r.error}")
                now = time.perf_counter()
                if now - last_print > 5 or done == len(archives):
                    last_print = now
                    el = now - t_start
                    rate = total_bytes / el / (1024 * 1024) if el > 0 else 0
                    print(
                        f"  [{done:,}/{len(archives):,}] "
                        f"{done/len(archives)*100:5.1f}% | "
                        f"corrupt={len(bad)} | {rate:.0f} MiB/s",
                        flush=True,
                    )

    elapsed = time.perf_counter() - t_start
    print(
        f"\nVerify done in {int(elapsed)}s. "
        f"Scanned {len(archives):,} archives, "
        f"read {total_bytes / (1024**3):.2f} GiB. "
        f"CORRUPT: {len(bad)}"
    )

    # Plan repairs
    repair_plan: list[dict[str, Any]] = []
    for r in bad:
        arch = Path(r.archive)
        ldir = loose_dir_for_archive(arch, cold_root, loose_root)
        have_loose = ldir.is_dir() and any(ldir.iterdir())
        repair_plan.append(
            {
                "archive": str(arch),
                "loose_dir": str(ldir),
                "loose_present": have_loose,
                "error": r.error,
            }
        )
        print(
            f"  {'REPAIRABLE' if have_loose else 'NO LOOSE  '}  "
            f"{arch}  <- {ldir}"
        )

    # Execute repairs
    repair_results: list[dict[str, Any]] = []
    if args.repair and repair_plan:
        if not args.execute:
            print("\n--repair --dry-run: listing planned actions only (pass --execute to apply).")
        else:
            print(f"\nRepairing {sum(1 for p in repair_plan if p['loose_present'])} archives...")
            for p in repair_plan:
                if not p["loose_present"]:
                    repair_results.append({**p, "repaired": False, "reason": "no_loose_dir"})
                    continue
                arch = Path(p["archive"])
                ldir = Path(p["loose_dir"])
                try:
                    t0 = time.perf_counter()
                    rebuild_archive(ldir, arch, level=args.zstd_level)
                    # Re-verify post-rebuild
                    post = verify_archive(arch)
                    if post.ok:
                        repair_results.append(
                            {
                                **p,
                                "repaired": True,
                                "new_bytes": arch.stat().st_size,
                                "rebuild_s": time.perf_counter() - t0,
                                "verify_s": post.elapsed_s,
                                "members": post.members,
                            }
                        )
                        print(
                            f"  OK   {arch}  "
                            f"({arch.stat().st_size / (1024*1024):.1f} MiB, "
                            f"{post.members} members) "
                            f"in {time.perf_counter()-t0:.2f}s"
                        )
                    else:
                        repair_results.append(
                            {**p, "repaired": False, "reason": f"post_verify_failed: {post.error}"}
                        )
                        print(f"  FAIL {arch}  post-rebuild verify failed: {post.error}")
                except Exception as e:
                    repair_results.append(
                        {**p, "repaired": False, "reason": f"{type(e).__name__}: {e}"}
                    )
                    print(f"  FAIL {arch}  {e}")

    # Write JSON summary
    payload = {
        "host": os.uname().nodename,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "cold_root": str(cold_root),
        "loose_root": str(loose_root),
        "patient_filter": args.patient,
        "archives_scanned": len(archives),
        "bytes_read": total_bytes,
        "elapsed_s": elapsed,
        "workers": args.workers,
        "corrupt": [asdict(r) for r in bad],
        "repair_plan": repair_plan,
        "repair_executed": args.repair and args.execute,
        "repair_results": repair_results,
    }
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.write_text(json.dumps(payload, indent=2))
    print(f"\nResults written to: {args.out_json}")

    return 0 if not bad or all(r.get("repaired") for r in repair_results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
