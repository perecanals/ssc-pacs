#!/usr/bin/env python3
"""
Benchmark ZIP create/extract times for DICOM directory trees.

This script is designed to simulate a compressed-at-rest + hot-cache workflow:
- Cold store: one ZIP archive per series/study tree
- Hot cache: extracted files to a cache directory

It records:
- input bytes and file counts
- zip bytes
- zip create time
- unzip time (first + repeat)

Notes:
- True "cold cache" is hard to guarantee without root (drop_caches). We report
  both the first extraction and a repeat extraction after deleting the output.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class BenchResult:
    label: str
    target_path: str
    kind: str  # "study" | "series"
    input_bytes: int
    input_files: int
    zip_path: str
    zip_bytes: int
    zip_create_s: float
    unzip_1_s: float
    unzip_2_s: float


def iter_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def tree_stats(root: Path) -> tuple[int, int]:
    total = 0
    n = 0
    for p in iter_files(root):
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        total += st.st_size
        n += 1
    return total, n


def ensure_empty_dir(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)


def zip_dir(src: Path, zip_path: Path, compresslevel: int) -> float:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    t0 = time.perf_counter()
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=compresslevel,
        allowZip64=True,
    ) as zf:
        for f in iter_files(src):
            zf.write(f, f.relative_to(src))
    return time.perf_counter() - t0


def unzip_to(zip_path: Path, dst: Path) -> float:
    ensure_empty_dir(dst)
    t0 = time.perf_counter()
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dst)
    return time.perf_counter() - t0


def bench_one(
    *,
    label: str,
    kind: str,
    target: Path,
    out_dir: Path,
    cache_dir: Path,
    compresslevel: int,
) -> BenchResult:
    if not target.exists():
        raise FileNotFoundError(str(target))
    if not target.is_dir():
        raise ValueError(f"Target must be a directory: {target}")

    input_bytes, input_files = tree_stats(target)
    zip_path = out_dir / f"{label}.zip"

    zip_create_s = zip_dir(target, zip_path, compresslevel=compresslevel)
    zip_bytes = zip_path.stat().st_size

    # Two extraction runs, deleting output in between.
    dst1 = cache_dir / f"{label}.unzip1"
    dst2 = cache_dir / f"{label}.unzip2"
    unzip_1_s = unzip_to(zip_path, dst1)
    unzip_2_s = unzip_to(zip_path, dst2)

    return BenchResult(
        label=label,
        target_path=str(target),
        kind=kind,
        input_bytes=input_bytes,
        input_files=input_files,
        zip_path=str(zip_path),
        zip_bytes=zip_bytes,
        zip_create_s=zip_create_s,
        unzip_1_s=unzip_1_s,
        unzip_2_s=unzip_2_s,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Output directory for zip files and results JSON")
    ap.add_argument("--cache", required=True, help="Output directory for extracted trees (hot cache simulation)")
    ap.add_argument("--compresslevel", type=int, default=6, help="ZIP deflate compression level (0-9)")
    ap.add_argument("--label", action="append", default=[], help="Label for a benchmark item (repeatable)")
    ap.add_argument("--kind", action="append", default=[], help="Kind for each item: study|series (repeatable)")
    ap.add_argument("--path", action="append", default=[], help="Directory path for each item (repeatable)")
    args = ap.parse_args()

    if not (len(args.label) == len(args.kind) == len(args.path)):
        raise SystemExit("Must pass matching counts of --label, --kind, and --path")

    out_dir = Path(args.out).resolve()
    cache_dir = Path(args.cache).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    results: list[BenchResult] = []
    for label, kind, path in zip(args.label, args.kind, args.path):
        target = Path(path).resolve()
        results.append(
            bench_one(
                label=label,
                kind=kind,
                target=target,
                out_dir=out_dir,
                cache_dir=cache_dir,
                compresslevel=args.compresslevel,
            )
        )

    payload = {
        "host": os.uname().nodename,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "compresslevel": args.compresslevel,
        "results": [asdict(r) for r in results],
    }
    out_json = out_dir / "dicom_archive_benchmark_results.json"
    out_json.write_text(json.dumps(payload, indent=2))

    # Print a compact summary for terminal consumption.
    def fmt_bytes(n: int) -> str:
        for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
            if n < 1024 or unit == "TiB":
                return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
            n /= 1024
        return f"{n:.1f}TiB"

    print(f"Wrote: {out_json}")
    print("label\tkind\tinput\tfiles\tzip\tzip_s\tunzip1_s\tunzip2_s")
    for r in results:
        print(
            f"{r.label}\t{r.kind}\t{fmt_bytes(r.input_bytes)}\t{r.input_files}\t"
            f"{fmt_bytes(r.zip_bytes)}\t{r.zip_create_s:.2f}\t{r.unzip_1_s:.2f}\t{r.unzip_2_s:.2f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

