#!/usr/bin/env python3
"""Register specific series into Orthanc via the patched indexer's on-demand scan.

Why this exists
---------------
The patched Folder Indexer only discovers new files by re-walking every directory in
``orthanc.json`` ``Indexer.Folders`` each ``Interval``. With the whole ``/dicom-data``
tree configured (millions of files over virtiofs) a pass is glacial and does not scale.
But in ``cold_path_cache`` mode the ONLY event that ever needs indexing is ingestion of
new data — whose paths we already know from ``image_series.dicom_dir_path``.

The SSC fork exposes ``POST /indexer/scan`` (see orthanc-indexer-patched/PATCHES.md):
it scans exactly the folders you give it and registers their DICOMs — cost O(new data),
independent of the total index size. This module is a thin client over that endpoint:
no config edits, no Orthanc restarts, nothing to leave in a dirty state.

- ``Force`` (optional) drops the target folders' existing index rows first, so
  "orphaned-row" series (a row exists but the instance was never registered, e.g. a
  POST that failed during an OOM) get re-registered. Harmless (0-row delete) otherwise.
- The plugin's own DICOM cache is bounded (~0.35 GiB plateau), but Orthanc CORE's
  working set grows during sustained registration: one uninterrupted scan over
  hundreds of thousands of new instances can push the Colima VM past its memory
  ceiling (VM-global OOM; ``docker inspect`` ``OOMKilled`` reads false). Large batches
  must therefore go through ``register_in_bounded_passes`` — bounded scans with a
  settle between them so memory returns to baseline (~1 GiB/pass).

Consumers: ``reindex_missing_series.py`` (backfill) and the ingestion
pipeline (``index_case_into_orthanc`` / end-of-run sanity pass).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "web-app"))

# Container mount the indexer scans (stable; the docker-compose bind is always /dicom-data).
CONTAINER_ROOT = "/dicom-data"


@dataclass(frozen=True)
class SeriesTarget:
    suid: str
    dicom_dir_path: str          # host path from image_series.dicom_dir_path
    n_instances: int = 0         # number_of_slices; 0 if unknown


def host_dir_to_container(host_dir: str, host_root: str) -> str:
    """Rewrite a host dicom_dir_path to the container path the indexer scans."""
    host_dir = host_dir.rstrip("/")
    host_root = host_root.rstrip("/")
    if host_dir == host_root or host_dir.startswith(host_root + "/"):
        return CONTAINER_ROOT + host_dir[len(host_root):]
    return host_dir


def target_container_dir(t: SeriesTarget, host_root: str, granularity: str) -> str:
    """Map a target's host dicom_dir_path to the container subtree to scan.

    granularity: 'series' -> .../{seriesuid}/DICOM ; 'study' -> .../{patient}/{studyuid} ;
    'patient' -> .../{patient}. dicom_dir_path is
    {root}/{patient}/{studyuid}/{seriesdesc}/{seriesuid}/DICOM.
    """
    cpath = host_dir_to_container(t.dicom_dir_path, host_root).rstrip("/")
    if granularity == "series":
        return cpath
    parts = cpath.split("/")
    if granularity == "study" and len(parts) >= 4:
        return "/".join(parts[:-3])
    if granularity == "patient" and len(parts) >= 5:
        return "/".join(parts[:-4])
    return cpath


def minimal_folder_set(dirs: Iterable[str]) -> list[str]:
    """Dedupe and drop any dir that is a subpath of another (prefix-collapse)."""
    uniq = sorted({d.rstrip("/") for d in dirs if d})
    out: list[str] = []
    for d in uniq:
        if out and (d == out[-1] or d.startswith(out[-1] + "/")):
            continue
        out.append(d)
    return out


def dir_max_file_bytes(dirpath: str) -> int:
    """Largest single file (bytes) under `dirpath`; 0 if empty/unreadable.

    Registering a DICOM file transiently costs Orthanc core ~2-3x the file
    size in RAM (read buffer + DCMTK parse + storage copy). A single
    multi-GB multiframe file (e.g. an XA angio run) can therefore OOM the
    whole Colima VM on its own, no matter how small the pass — callers use
    this to fence off such series before scanning.
    """
    mx = 0
    for root, _dirs, files in os.walk(dirpath):
        for f in files:
            try:
                mx = max(mx, os.path.getsize(os.path.join(root, f)))
            except OSError:
                pass
    return mx


def verify_registered(session, url, suids: Iterable[str]) -> list[str]:
    """Return the subset of `suids` that Orthanc's index currently resolves."""
    verified = []
    for s in suids:
        try:
            r = session.post(f"{url}/tools/lookup", data=s, timeout=15)
            if r.ok and any(x.get("Type") == "Series" for x in r.json()):
                verified.append(s)
        except requests.RequestException:
            pass
    return verified


def _wait_until_idle(session, url, *, timeout_s: int, poll_s: int,
                     log: Callable[[str], None]) -> bool:
    """Poll GET /indexer/scan until busy=false. Returns False on timeout."""
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if not session.get(f"{url}/indexer/scan", timeout=30).json().get("busy"):
                return True
        except requests.RequestException:
            pass  # Orthanc may be restarting; keep waiting
        time.sleep(poll_s)
    return False


def scoped_register_series(
    targets: list[SeriesTarget],
    *,
    host_root: str,
    orthanc_url: str,
    session: requests.Session,
    force: bool = False,
    granularity: str = "series",
    poll_s: int = 15,
    deadline_s: int = 172800,
    log: Callable[[str], None] = print,
) -> dict:
    """Register `targets` in one pass via POST /indexer/scan, poll until done.

    No config edits, no restarts. One call = ONE scan; callers that need to
    register very large batches must sub-divide via register_in_bounded_passes
    (a single huge uninterrupted scan can OOM Orthanc core — VM-global).
    Returns {registered, targets, registered_suids, endpoint}.
    """
    if not targets:
        return {"registered": 0, "targets": 0, "registered_suids": []}

    folders = minimal_folder_set(
        target_container_dir(t, host_root, granularity) for t in targets)
    inst = sum(t.n_instances for t in targets)
    log(f"{len(targets)} series (~{inst:,} instances) → {len(folders)} folder(s), "
        f"force={force}, granularity={granularity}")

    resp = session.post(f"{orthanc_url}/indexer/scan",
                        json={"Folders": folders, "Force": force}, timeout=60)
    if resp.status_code == 409:
        # Another scan is running (e.g. a prior pass truncated by a restart, or a
        # concurrent manual scan). Wait for the indexer to go idle, then retry once.
        log("  scan endpoint busy (409); waiting for the running scan to finish…")
        if not _wait_until_idle(session, orthanc_url,
                                timeout_s=deadline_s, poll_s=poll_s, log=log):
            raise RuntimeError("Timed out waiting for a running /indexer/scan to finish.")
        resp = session.post(f"{orthanc_url}/indexer/scan",
                            json={"Folders": folders, "Force": force}, timeout=60)
        if resp.status_code == 409:
            raise RuntimeError("/indexer/scan still busy after waiting (409).")
    resp.raise_for_status()
    log(f"scan started: {resp.json()}")

    t0 = time.time()
    last_log = 0.0
    while True:
        time.sleep(poll_s)
        st = session.get(f"{orthanc_url}/indexer/scan", timeout=30).json()
        if not st.get("busy"):
            break
        if time.time() - last_log > 120:
            log(f"  … scanning: filesProcessed={st.get('filesProcessed')} "
                f"registered={st.get('registered')}")
            last_log = time.time()
        if time.time() - t0 > deadline_s:
            log("  WARNING: deadline exceeded; stopping poll (scan may still be running).")
            break

    registered_suids = verify_registered(session, orthanc_url, [t.suid for t in targets])
    log(f"registered {len(registered_suids)}/{len(targets)} target series "
        f"(endpoint: filesProcessed={st.get('filesProcessed')}, "
        f"registered={st.get('registered')}, took {round(time.time()-t0)}s)")
    return {"registered": len(registered_suids), "targets": len(targets),
            "registered_suids": registered_suids, "endpoint": st}


# Bounded-pass defaults: proven remediation for VM-global OOM during huge
# registrations (2026-07-02: one 668k-instance scan OOM'd Orthanc; looped
# ~500-series passes with a 120 s settle stayed at ~1 GiB/pass).
MAX_SERIES_PER_PASS = 350
MAX_INSTANCES_PER_PASS = 40_000
SETTLE_S = 120


def _partition_passes(targets: list[SeriesTarget],
                      max_series: int, max_instances: int) -> list[list[SeriesTarget]]:
    """Greedy split capped by both series count and summed instance count.

    A single series above max_instances still forms its own (unsplittable) pass.
    """
    passes: list[list[SeriesTarget]] = []
    cur: list[SeriesTarget] = []
    cur_inst = 0
    for t in targets:
        n = max(t.n_instances, 0)
        if cur and (len(cur) >= max_series or cur_inst + n > max_instances):
            passes.append(cur)
            cur, cur_inst = [], 0
        cur.append(t)
        cur_inst += n
    if cur:
        passes.append(cur)
    return passes


def register_in_bounded_passes(
    targets: list[SeriesTarget],
    *,
    host_root: str,
    orthanc_url: str,
    session: requests.Session,
    force: bool = False,
    granularity: str = "series",
    max_series_per_pass: int = MAX_SERIES_PER_PASS,
    max_instances_per_pass: int = MAX_INSTANCES_PER_PASS,
    settle_s: int = SETTLE_S,
    poll_s: int = 15,
    log: Callable[[str], None] = print,
) -> dict:
    """Register `targets` as a sequence of bounded scans with a settle between.

    Keeps the "one scan = one bounded batch" principle: each pass is one
    scoped_register_series call; between passes Orthanc's working set returns
    to baseline. Aborts remaining passes if a non-empty pass verifies zero
    registrations (signature of an Orthanc restart mid-scan) — rely on
    re-detection/backfill instead of hammering a crash-looping Orthanc.

    Returns {registered, targets, passes, truncated, registered_suids}.
    """
    if not targets:
        return {"registered": 0, "targets": 0, "passes": 0,
                "truncated": False, "registered_suids": []}

    passes = _partition_passes(targets, max_series_per_pass, max_instances_per_pass)
    registered_suids: list[str] = []
    truncated = False

    if len(passes) > 1:
        log(f"Registering {len(targets)} series in {len(passes)} bounded pass(es) "
            f"(≤{max_series_per_pass} series / ≤{max_instances_per_pass:,} instances "
            f"per pass, {settle_s}s settle)")

    for i, batch in enumerate(passes, start=1):
        if len(passes) > 1:
            log(f"— pass {i}/{len(passes)}: {len(batch)} series "
                f"(~{sum(t.n_instances for t in batch):,} instances)")
        try:
            summary = scoped_register_series(
                batch, host_root=host_root, orthanc_url=orthanc_url,
                session=session, force=force, granularity=granularity,
                poll_s=poll_s, log=log)
        except (requests.RequestException, RuntimeError) as exc:
            log(f"  pass {i} failed ({exc}); aborting remaining passes")
            truncated = True
            break
        registered_suids.extend(summary["registered_suids"])
        if summary["registered"] == 0:
            log(f"  pass {i} verified 0/{len(batch)} registrations — likely an "
                f"Orthanc restart mid-scan; aborting remaining passes")
            truncated = True
            break
        if i < len(passes) and settle_s > 0:
            log(f"  settling {settle_s}s before next pass…")
            time.sleep(settle_s)

    log(f"Bounded-pass registration: {len(registered_suids)}/{len(targets)} series "
        f"across {len(passes)} pass(es)" + (" [TRUNCATED]" if truncated else ""))
    return {"registered": len(registered_suids), "targets": len(targets),
            "passes": len(passes), "truncated": truncated,
            "registered_suids": registered_suids}


# --------------------------------------------------------------------------- #
# CLI (standalone: register an explicit list of series UIDs)
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", required=True, help="Comma-separated SeriesInstanceUIDs.")
    ap.add_argument("--granularity", default="series", choices=("series", "study", "patient"))
    ap.add_argument("--force", action="store_true", help="Drop existing index rows first.")
    args = ap.parse_args()

    import psycopg2
    from config import DICOM_DATA_ROOT, STORAGE_MODE  # noqa: E402
    from db import DB_CONFIG  # noqa: E402
    from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER  # noqa: E402

    if STORAGE_MODE != "cold_path_cache":
        sys.exit(f"STORAGE_MODE is {STORAGE_MODE!r}, not cold_path_cache. Aborting.")

    suids = [s.strip() for s in args.series.split(",") if s.strip()]
    conn = psycopg2.connect(**DB_CONFIG)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT seriesinstanceuid, dicom_dir_path, COALESCE(number_of_slices,0) "
            "FROM image_series WHERE seriesinstanceuid = ANY(%s) "
            "AND dicom_dir_path IS NOT NULL",
            (suids,),
        )
        targets = [SeriesTarget(s, d, int(n)) for s, d, n in cur.fetchall()]
    conn.close()

    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASS)
    summary = scoped_register_series(
        targets, host_root=str(DICOM_DATA_ROOT), orthanc_url=ORTHANC_URL,
        session=session, force=args.force, granularity=args.granularity)
    print(f"\nDone. Registered {summary['registered']}/{summary['targets']} series.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
