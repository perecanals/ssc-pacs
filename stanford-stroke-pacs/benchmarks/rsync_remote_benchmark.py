#!/usr/bin/env python3
"""
Remote rsync benchmark — run this on a CLIENT machine that pulls from the PACS
server over SSH.

Motivation
----------
The local benchmark (`cold_storage_report.py`) rsyncs on the same filesystem as
the sources, so it only captures file-count / metadata-walk cost.  The real
headline question — "how much faster is backing up tar.zst over the network?" —
needs a real remote transfer.  Server→laptop is blocked by the firewall but
laptop→server SSH is open, so this script pulls.

What it does
------------
For each patient in the sample list, pull both the loose and compressed trees
over `rsync -az -e ssh` and record:
  1. **Full cold copy** — destination empty, measure wall-clock, transferred
     bytes, file count.
  2. **No-op pass** — same rsync again with destination populated, measures
     the metadata-walk only.

Every patient is cleaned up from the local scratch before moving to the next
one (laptops don't have 200 GiB to spare).  Between patients a short pause
lets the network settle.

Outputs
-------
JSON (default `rsync_remote_benchmark_results.json`) with per-patient
elapsed, transferred bytes, file count, network round-trip probe, and client
metadata.  Upload this back to the server to fold into the benchmark report.

Requirements on the client
--------------------------
- Python 3.10+
- `rsync` in PATH
- `ssh` configured for the target server (keyfile-based login preferred so
  rsync doesn't block for passwords)

Usage
-----
    python rsync_remote_benchmark.py \\
        --server user@stroke.stanford.edu \\
        --loose-root /DATA2/pacs_imaging_data_loose_backup \\
        --compressed-root /DATA2/pacs_imaging_data_compressed \\
        --scratch /tmp/pacs_rsync_bench \\
        --patients-n 5

    # Or pick explicit patients:
    python rsync_remote_benchmark.py --server user@... \\
        --patients 1-017 2-592 4-0003
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Default 30 patients alphabetical (same as cold_storage_report.py rsync phase).
# Generated from `SELECT DISTINCT patient_id FROM image_series ORDER BY patient_id`.
DEFAULT_PATIENTS: list[str] = [
    "1-017", "2-592", "2-599", "2-601", "2-603", "2-610", "2-611",
    "4-0003", "4-0012", "4-0049", "4-0056", "4-0060", "4-0110", "4-0112",
    "4-0117", "4-0118", "4-0121", "4-0127", "4-0128", "4-0130", "4-0137",
    "4-0157", "4-0168", "4-0171", "4-0185", "4-0200", "4-0213", "4-0231",
    "4-0237", "4-0240",
]


def parse_rsync_stats(stdout: str, stderr: str) -> dict[str, Any]:
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
            stats["total_transferred_file_size_bytes"] = int(
                m.group(1).replace(",", "")
            )
        m = re.match(r"Total bytes sent: ([\d,]+)", line)
        if m:
            stats["total_bytes_sent"] = int(m.group(1).replace(",", ""))
        m = re.match(r"Total bytes received: ([\d,]+)", line)
        if m:
            stats["total_bytes_received"] = int(m.group(1).replace(",", ""))
    return stats


def run_rsync(
    remote_src: str, local_dst: Path, ssh_opts: list[str]
) -> dict[str, Any]:
    cmd = [
        "rsync",
        "-a",
        "--stats",
        "-e",
        " ".join(["ssh"] + ssh_opts),
        remote_src,
        f"{str(local_dst).rstrip('/')}/",
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    elapsed = time.perf_counter() - t0
    result: dict[str, Any] = {
        "cmd": " ".join(cmd),
        "exit_code": proc.returncode,
        "elapsed_s": elapsed,
    }
    if proc.stdout:
        result["stdout_tail"] = proc.stdout[-3000:]
    if proc.stderr and proc.returncode != 0:
        result["stderr_tail"] = proc.stderr[-1500:]
    result.update(parse_rsync_stats(proc.stdout or "", proc.stderr or ""))
    return result


def ensure_empty(p: Path) -> None:
    if p.exists():
        shutil.rmtree(p)
    p.mkdir(parents=True, exist_ok=True)


def ssh_probe(server: str, ssh_opts: list[str]) -> dict[str, Any]:
    """Round-trip echo test so we can correlate wins with RTT/throughput."""
    try:
        t0 = time.perf_counter()
        proc = subprocess.run(
            ["ssh", *ssh_opts, server, "echo ok"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        dt = time.perf_counter() - t0
        return {"rtt_s": dt, "stdout": (proc.stdout or "").strip(), "ok": proc.returncode == 0}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main() -> int:
    ap = argparse.ArgumentParser(description="Remote rsync benchmark (client-side)")
    ap.add_argument(
        "--server",
        required=True,
        help="SSH target, e.g. user@stroke.stanford.edu",
    )
    ap.add_argument(
        "--loose-root",
        default="/DATA2/pacs_imaging_data_loose_backup",
        help="Remote path to the loose DICOM tree.",
    )
    ap.add_argument(
        "--compressed-root",
        default="/DATA2/pacs_imaging_data_compressed",
        help="Remote path to the compressed tree.",
    )
    ap.add_argument(
        "--scratch",
        type=Path,
        default=Path.home() / "pacs_rsync_bench_scratch",
        help="Local scratch directory (must have tens of GiB free).",
    )
    ap.add_argument(
        "--patients",
        nargs="+",
        help="Explicit patient_id list. Default: first 5 of the 30-patient "
        "alphabetical sample (use --patients-n to override count).",
    )
    ap.add_argument(
        "--patients-n",
        type=int,
        default=5,
        help="If --patients not given, take the first N of the default list. "
        "Default 5 (≈20-40 GiB loose, ≈10-15 GiB compressed per transfer set).",
    )
    ap.add_argument(
        "--ssh-opt",
        action="append",
        default=[],
        help="Extra SSH option, repeatable. e.g. --ssh-opt='-o BatchMode=yes' "
        "--ssh-opt='-i /path/to/key'",
    )
    ap.add_argument(
        "--skip-noop",
        action="store_true",
        help="Only run the full cold copy (don't time the metadata-walk pass).",
    )
    ap.add_argument(
        "--keep-scratch",
        action="store_true",
        help="Don't delete the scratch copies between patients (needs space!).",
    )
    ap.add_argument(
        "--pause-between-s",
        type=float,
        default=1.0,
        help="Sleep this many seconds between patients to let the link settle.",
    )
    ap.add_argument(
        "--out-json",
        type=Path,
        default=Path.cwd() / "rsync_remote_benchmark_results.json",
    )
    args = ap.parse_args()

    patients: list[str] = (
        list(args.patients)
        if args.patients
        else DEFAULT_PATIENTS[: args.patients_n]
    )
    ssh_opts: list[str] = list(args.ssh_opt)

    args.scratch.mkdir(parents=True, exist_ok=True)
    print(f"Scratch: {args.scratch}")
    print(f"Server:  {args.server}")
    print(f"Patients ({len(patients)}): {patients}")

    probe = ssh_probe(args.server, ssh_opts)
    print(f"SSH probe: {probe}")
    if not probe.get("ok"):
        print("ERROR: could not SSH to server. Fix the connection and retry.",
              file=sys.stderr)
        return 1

    results: list[dict[str, Any]] = []
    t_start = time.perf_counter()

    for i, pid in enumerate(patients, 1):
        print(f"\n{'='*60}\n[{i}/{len(patients)}] Patient {pid}\n{'='*60}")

        loose_src = f"{args.server}:{args.loose_root.rstrip('/')}/{pid}/"
        comp_src = f"{args.server}:{args.compressed_root.rstrip('/')}/{pid}/"
        loose_dst = args.scratch / "loose" / pid
        comp_dst = args.scratch / "compressed" / pid

        # --- Loose: full cold ---
        print(f"  loose  full: {loose_src} -> {loose_dst}/")
        ensure_empty(loose_dst)
        loose_full = run_rsync(loose_src, loose_dst, ssh_opts)
        print(
            f"    {loose_full['elapsed_s']:.2f}s, "
            f"sent={loose_full.get('total_bytes_sent')}, "
            f"recv={loose_full.get('total_bytes_received')}, "
            f"files={loose_full.get('number_of_files')}, "
            f"exit={loose_full['exit_code']}"
        )

        # --- Compressed: full cold ---
        print(f"  comp   full: {comp_src} -> {comp_dst}/")
        ensure_empty(comp_dst)
        comp_full = run_rsync(comp_src, comp_dst, ssh_opts)
        print(
            f"    {comp_full['elapsed_s']:.2f}s, "
            f"sent={comp_full.get('total_bytes_sent')}, "
            f"recv={comp_full.get('total_bytes_received')}, "
            f"files={comp_full.get('number_of_files')}, "
            f"exit={comp_full['exit_code']}"
        )

        loose_noop: dict[str, Any] | None = None
        comp_noop: dict[str, Any] | None = None
        if not args.skip_noop:
            print(f"  loose  noop: {loose_src} -> {loose_dst}/ (already synced)")
            loose_noop = run_rsync(loose_src, loose_dst, ssh_opts)
            print(f"    {loose_noop['elapsed_s']:.2f}s")

            print(f"  comp   noop: {comp_src} -> {comp_dst}/ (already synced)")
            comp_noop = run_rsync(comp_src, comp_dst, ssh_opts)
            print(f"    {comp_noop['elapsed_s']:.2f}s")

        if not args.keep_scratch:
            shutil.rmtree(loose_dst, ignore_errors=True)
            shutil.rmtree(comp_dst, ignore_errors=True)

        results.append(
            {
                "patient_id": pid,
                "loose_full": loose_full,
                "compressed_full": comp_full,
                "loose_noop": loose_noop,
                "compressed_noop": comp_noop,
            }
        )

        # Persist results incrementally so a crash doesn't lose hours of work.
        payload = {
            "client_host": socket.gethostname(),
            "client_platform": platform.platform(),
            "python": sys.version.split()[0],
            "server": args.server,
            "loose_root": args.loose_root,
            "compressed_root": args.compressed_root,
            "scratch": str(args.scratch.resolve()),
            "ts": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "ssh_probe": probe,
            "patients_requested": patients,
            "skip_noop": args.skip_noop,
            "results": results,
        }
        args.out_json.write_text(json.dumps(payload, indent=2))

        if args.pause_between_s:
            time.sleep(args.pause_between_s)

    if not args.keep_scratch:
        shutil.rmtree(args.scratch, ignore_errors=True)

    elapsed = time.perf_counter() - t_start
    print(f"\nDone in {elapsed:.1f}s across {len(patients)} patients.")
    print(f"Results: {args.out_json.resolve()}")

    def _mean(key: str) -> float:
        vs = [r[key]["elapsed_s"] for r in results if r.get(key)]
        return sum(vs) / len(vs) if vs else 0.0

    lf, cf = _mean("loose_full"), _mean("compressed_full")
    print(f"  mean loose full={lf:.2f}s  compressed full={cf:.2f}s  "
          f"speedup={lf/cf:.1f}x" if cf else f"  mean loose full={lf:.2f}s")
    if not args.skip_noop:
        ln, cn = _mean("loose_noop"), _mean("compressed_noop")
        print(f"  mean loose noop={ln:.2f}s compressed noop={cn:.2f}s  "
              f"speedup={ln/cn:.1f}x" if cn else f"  mean loose noop={ln:.2f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
