#!/usr/bin/env python3
"""
Benchmark tar.zst (tar + zstd) create/extract times for DICOM directory trees.

Mirrors `benchmarks/dicom_archive_benchmark.py`, but uses a single `.tar.zst`
archive per target.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tarfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import zstandard as zstd


@dataclass
class BenchResult:
    label: str
    target_path: str
    kind: str  # "study" | "series"
    input_bytes: int
    input_files: int
    tar_zst_path: str
    tar_zst_bytes: int
    tar_zst_create_s: float
    untar_zst_1_s: float
    untar_zst_2_s: float


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


def tar_zst_dir(src: Path, out_path: Path, level: int) -> float:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    # Stream tar -> zstd to avoid creating an intermediate tar file.
    t0 = time.perf_counter()
    cctx = zstd.ZstdCompressor(level=level, threads=-1)
    with out_path.open("wb") as f_out:
        with cctx.stream_writer(f_out) as z_out:
            with tarfile.open(fileobj=z_out, mode="w|") as tf:
                # Add the directory contents without embedding absolute paths.
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


def bench_one(
    *,
    label: str,
    kind: str,
    target: Path,
    out_dir: Path,
    cache_dir: Path,
    level: int,
) -> BenchResult:
    if not target.exists():
        raise FileNotFoundError(str(target))
    if not target.is_dir():
        raise ValueError(f"Target must be a directory: {target}")

    input_bytes, input_files = tree_stats(target)
    out_path = out_dir / f"{label}.tar.zst"

    create_s = tar_zst_dir(target, out_path, level=level)
    out_bytes = out_path.stat().st_size

    dst1 = cache_dir / f"{label}.untar1"
    dst2 = cache_dir / f"{label}.untar2"
    untar_1_s = untar_zst_to(out_path, dst1)
    untar_2_s = untar_zst_to(out_path, dst2)

    return BenchResult(
        label=label,
        target_path=str(target),
        kind=kind,
        input_bytes=input_bytes,
        input_files=input_files,
        tar_zst_path=str(out_path),
        tar_zst_bytes=out_bytes,
        tar_zst_create_s=create_s,
        untar_zst_1_s=untar_1_s,
        untar_zst_2_s=untar_2_s,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="Output directory for tar.zst files and results JSON")
    ap.add_argument("--cache", required=True, help="Output directory for extracted trees (hot cache simulation)")
    ap.add_argument("--level", type=int, default=6, help="zstd compression level (1-22 typical)")
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
                level=args.level,
            )
        )

    payload = {
        "host": os.uname().nodename,
        "ts": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "zstd_level": args.level,
        "results": [asdict(r) for r in results],
    }
    out_json = out_dir / "dicom_archive_benchmark_tarzst_results.json"
    out_json.write_text(json.dumps(payload, indent=2))

    def fmt_bytes(n: int) -> str:
        for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
            if n < 1024 or unit == "TiB":
                return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
            n /= 1024
        return f"{n:.1f}TiB"

    print(f"Wrote: {out_json}")
    print("label\tkind\tinput\tfiles\ttar.zst\ttarzst_s\tuntar1_s\tuntar2_s")
    for r in results:
        print(
            f"{r.label}\t{r.kind}\t{fmt_bytes(r.input_bytes)}\t{r.input_files}\t"
            f"{fmt_bytes(r.tar_zst_bytes)}\t{r.tar_zst_create_s:.2f}\t{r.untar_zst_1_s:.2f}\t{r.untar_zst_2_s:.2f}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

