#!/usr/bin/env python3
"""
Prune stale duplicate path rows from Orthanc's Folder Indexer index
(``indexer-plugin.db`` table ``Files(path PK, time, size, isDicom, instanceId)``).

Why this exists
---------------
With ``RemoveMissingFiles: false`` the patched indexer never prunes rows for
vanished files, so an instance whose files once sat under a wrong directory can
carry two ``Files`` rows -- one valid, one stale. The plugin takes the *first*
row on lookup, so it intermittently serves the dead path -> Orthanc 500 -> blank
OHIF pane. Invariant: a row is **stale** iff its dir != the DB-canonical dir for
the instance's true series.

Detection (warm-state independent)
----------------------------------
  * instance -> true SeriesInstanceUID: Orthanc ``POST /tools/find`` (Level=Series,
    Query {PatientID}, Expand) -- the UID comes from the DICOM header, so it is
    correct regardless of where the file sits on disk.
  * SeriesInstanceUID -> canonical dir: ``image_series.dicom_dir_path`` with the
    host prefix ``DICOM_DATA_ROOT`` rewritten to the container mount (``/dicom-data``).
Because it never consults the filesystem, this also cleans stale rows for studies
that are currently cold/evicted, leaving their *valid* (currently-missing) rows intact.

Safety
------
  * Dry-run by default; ``--execute`` required for any mutation.
  * Never deletes an instance's last ``Files`` row (would orphan its ``Attachments``
    row, which does NOT cascade). Instances whose *every* row is stale are reported
    and only removed with ``--delete-orphans`` (via Orthanc REST, which cleans both
    tables properly).
  * Edits happen only while Orthanc is stopped; the pre-edit DB is backed up first
    and an attachment-orphan invariant is asserted before the edited DB is restored.
  * Idempotent: a second run finds 0 stale.

Usage
-----
  # Report only (default) -- all patients
  python scripts/cold_storage/prune_stale_index_paths.py

  # Report a single patient
  python scripts/cold_storage/prune_stale_index_paths.py --patient 24-012

  # Apply for one patient (brief Orthanc restart)
  python scripts/cold_storage/prune_stale_index_paths.py --patient 24-012 --execute --yes

  # Apply globally
  python scripts/cold_storage/prune_stale_index_paths.py --execute
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(REPO_ROOT / ".env")

# NOTE: web-app modules (config/db/orthanc_client) are imported lazily inside
# main() so the pure detection helpers below stay importable in unit tests
# without requiring the runtime .env (orthanc_client raises on missing creds).

INDEX_DB_CONTAINER_PATH = "/var/lib/orthanc/db/indexer-plugin.db"
REPORTS_DIR = REPO_ROOT.parent / "maintenance" / "index-prune-reports"
BACKUPS_DIR = REPORTS_DIR / "backups"
ORTHANC_JSON = REPO_ROOT / "orthanc.json"


# --------------------------------------------------------------------------- #
# Pure detection logic (unit-tested in tests/test_prune_stale_index_paths.py)
# --------------------------------------------------------------------------- #
def container_dicom_root() -> str:
    """Container mount that the indexer scans (orthanc.json Indexer.Folders[0])."""
    try:
        cfg = json.loads(ORTHANC_JSON.read_text())
        folders = cfg.get("Indexer", {}).get("Folders") or []
        if folders:
            return str(folders[0]).rstrip("/")
    except (OSError, ValueError):
        pass
    return "/dicom-data"


def host_dir_to_container(host_dir: str, host_root: str, container_root: str) -> str:
    """Rewrite a host dicom_dir_path to the container path the indexer records."""
    host_dir = host_dir.rstrip("/")
    host_root = host_root.rstrip("/")
    if host_dir == host_root or host_dir.startswith(host_root + "/"):
        return container_root + host_dir[len(host_root):]
    return host_dir


def classify(
    rows: list[tuple[str, str]],
    inst2suid: dict[str, str],
    suid2canondir: dict[str, str],
) -> dict[str, Any]:
    """Classify indexer Files rows for one patient.

    ``rows`` is a list of ``(container_path, instanceId)``. ``inst2suid`` maps an
    Orthanc instance id to its true SeriesInstanceUID; ``suid2canondir`` maps a
    SeriesInstanceUID to its canonical *container* directory.

    Returns a dict with:
      * ``stale_paths``   -- paths safe to raw-delete (instance keeps >=1 other row)
      * ``orphan_paths``  -- paths whose instance would be fully orphaned (handle
                             via Orthanc REST, never raw-delete)
      * ``orphan_instances`` -- instance ids that are fully stale
      * ``valid``, ``unknown_inst``, ``orphan_series`` -- counts for reporting
      * ``sample_instances`` -- a few affected instance ids (for post-fix verification)
    """
    per_inst_total: dict[str, int] = {}
    per_inst_stale: dict[str, list[str]] = {}
    valid = 0
    unknown_inst = 0
    orphan_series = 0

    for path, iid in rows:
        per_inst_total[iid] = per_inst_total.get(iid, 0) + 1
        suid = inst2suid.get(iid)
        if suid is None:
            unknown_inst += 1
            continue
        canon = suid2canondir.get(suid)
        if canon is None:
            orphan_series += 1
            continue
        row_dir = os.path.dirname(path)
        if row_dir != canon:
            per_inst_stale.setdefault(iid, []).append(path)
        else:
            valid += 1

    stale_paths: list[str] = []
    orphan_paths: list[str] = []
    orphan_instances: list[str] = []
    for iid, paths in per_inst_stale.items():
        if len(paths) >= per_inst_total[iid]:
            # Every row for this instance is stale -> raw-deleting all of them
            # would orphan the instance's Attachments row.
            orphan_instances.append(iid)
            orphan_paths.extend(paths)
        else:
            stale_paths.extend(paths)

    return {
        "stale_paths": stale_paths,
        "orphan_paths": orphan_paths,
        "orphan_instances": orphan_instances,
        "valid": valid,
        "unknown_inst": unknown_inst,
        "orphan_series": orphan_series,
        "affected_instances": len(per_inst_stale),
        "sample_instances": list(per_inst_stale.keys())[:5],
    }


# --------------------------------------------------------------------------- #
# I/O helpers
# --------------------------------------------------------------------------- #
def _docker(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *args], check=check, capture_output=capture, text=True
    )


def list_patients(conn, patient: str | None) -> list[str]:
    if patient:
        return [patient]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT patient_id FROM image_series "
            "WHERE patient_id IS NOT NULL ORDER BY patient_id"
        )
        return [r[0] for r in cur.fetchall()]


def db_series_dirs(conn, patient: str, host_root: str, container_root: str) -> dict[str, str]:
    """SeriesInstanceUID -> canonical container dir, for one patient."""
    out: dict[str, str] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT seriesinstanceuid, dicom_dir_path FROM image_series "
            "WHERE patient_id = %s AND dicom_dir_path IS NOT NULL "
            "  AND dicom_dir_path <> ''",
            (patient,),
        )
        for r in cur.fetchall():
            out[r["seriesinstanceuid"]] = host_dir_to_container(
                r["dicom_dir_path"], host_root, container_root
            )
    return out


def orthanc_inst2suid(session: requests.Session, orthanc_url: str, patient: str) -> dict[str, str]:
    """instanceId -> SeriesInstanceUID for every series of a patient."""
    resp = session.post(
        f"{orthanc_url}/tools/find",
        json={"Level": "Series", "Query": {"PatientID": patient}, "Expand": True},
        timeout=120,
    )
    resp.raise_for_status()
    out: dict[str, str] = {}
    for s in resp.json():
        suid = s.get("MainDicomTags", {}).get("SeriesInstanceUID")
        if not suid:
            continue
        for iid in s.get("Instances", []):
            out[iid] = suid
    return out


def index_rows_for_patient(
    idx: sqlite3.Connection, container_root: str, patient: str
) -> list[tuple[str, str]]:
    cur = idx.execute(
        "SELECT path, instanceId FROM Files WHERE isDicom = 1 AND path LIKE ?",
        (f"{container_root}/{patient}/%",),
    )
    return [(p, i) for p, i in cur.fetchall()]


def snapshot_index_db(container: str, dest: Path) -> None:
    _docker("cp", f"{container}:{INDEX_DB_CONTAINER_PATH}", str(dest))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def analyze(
    conn,
    session: requests.Session,
    orthanc_url: str,
    idx: sqlite3.Connection,
    patients: list[str],
    host_root: str,
    container_root: str,
    quiet: bool,
    maps_cache: dict[str, tuple[dict[str, str], dict[str, str]]] | None = None,
) -> dict[str, Any]:
    """Run detection over all patients; return aggregated results.

    ``maps_cache`` (optional) memoizes per-patient ``(inst2suid, suid2dir)`` so the
    post-edit verification pass can re-classify the edited DB **without** calling
    Orthanc (which is stopped during the edit). Pass the same dict to both calls.
    """
    per_patient: dict[str, dict[str, Any]] = {}
    all_stale: list[str] = []
    all_orphan_paths: list[str] = []
    all_orphan_instances: list[str] = []
    sample_instances: list[str] = []
    tot_valid = tot_unknown = tot_orphan_series = 0

    for pat in patients:
        rows = index_rows_for_patient(idx, container_root, pat)
        if not rows:
            continue
        if maps_cache is not None and pat in maps_cache:
            inst2suid, suid2dir = maps_cache[pat]
        else:
            inst2suid = orthanc_inst2suid(session, orthanc_url, pat)
            suid2dir = db_series_dirs(conn, pat, host_root, container_root)
            if maps_cache is not None:
                maps_cache[pat] = (inst2suid, suid2dir)
        res = classify(rows, inst2suid, suid2dir)
        n_stale = len(res["stale_paths"])
        n_orphan = len(res["orphan_instances"])
        if n_stale or n_orphan or res["unknown_inst"] or res["orphan_series"]:
            per_patient[pat] = {
                "rows": len(rows),
                "valid": res["valid"],
                "stale_rows": n_stale,
                "orphan_instances": n_orphan,
                "unknown_inst": res["unknown_inst"],
                "orphan_series": res["orphan_series"],
            }
            if not quiet and n_stale:
                print(
                    f"  {pat:10s} stale_rows={n_stale:6d} "
                    f"orphan_inst={n_orphan:3d} "
                    f"unknown={res['unknown_inst']} orphan_series={res['orphan_series']}"
                )
        all_stale.extend(res["stale_paths"])
        all_orphan_paths.extend(res["orphan_paths"])
        all_orphan_instances.extend(res["orphan_instances"])
        if len(sample_instances) < 10:
            sample_instances.extend(res["sample_instances"][: 10 - len(sample_instances)])
        tot_valid += res["valid"]
        tot_unknown += res["unknown_inst"]
        tot_orphan_series += res["orphan_series"]

    return {
        "per_patient": per_patient,
        "stale_paths": all_stale,
        "orphan_paths": all_orphan_paths,
        "orphan_instances": all_orphan_instances,
        "sample_instances": sample_instances,
        "totals": {
            "patients_affected": len(per_patient),
            "stale_rows": len(all_stale),
            "orphan_instances": len(all_orphan_instances),
            "valid_rows": tot_valid,
            "unknown_inst": tot_unknown,
            "orphan_series": tot_orphan_series,
        },
    }


def apply_deletes(working_db: Path, stale_paths: list[str], vacuum: bool) -> None:
    """Delete stale rows from the working copy; assert no new attachment orphans."""
    conn = sqlite3.connect(str(working_db))
    try:
        baseline = conn.execute(
            "SELECT count(*) FROM Attachments a "
            "WHERE NOT EXISTS (SELECT 1 FROM Files f WHERE f.instanceId = a.instanceId)"
        ).fetchone()[0]
        conn.executemany(
            "DELETE FROM Files WHERE path = ?", [(p,) for p in stale_paths]
        )
        after = conn.execute(
            "SELECT count(*) FROM Attachments a "
            "WHERE NOT EXISTS (SELECT 1 FROM Files f WHERE f.instanceId = a.instanceId)"
        ).fetchone()[0]
        if after != baseline:
            conn.rollback()
            raise RuntimeError(
                f"attachment-orphan invariant violated: orphans {baseline} -> {after}; "
                "aborting without modifying the live index"
            )
        conn.commit()
        if vacuum:
            conn.execute("VACUUM")
            conn.commit()
    finally:
        conn.close()


def wait_for_orthanc(session: requests.Session, orthanc_url: str, timeout_s: int = 60) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if session.get(f"{orthanc_url}/system", timeout=3).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--execute", action="store_true", help="Apply changes (default: dry-run)")
    ap.add_argument("--patient", help="Limit to a single patient_id")
    ap.add_argument(
        "--delete-orphans",
        action="store_true",
        help="Also remove fully-stale instances via Orthanc REST (default: report only)",
    )
    ap.add_argument("--container", default="ssc-orthanc", help="Orthanc container name")
    ap.add_argument("--json", action="store_true", help="Write a JSON report")
    ap.add_argument("--vacuum", action="store_true", help="VACUUM the index DB after deletes")
    ap.add_argument("--yes", action="store_true", help="Skip the stop-Orthanc confirmation prompt")
    args = ap.parse_args()

    # Lazy import of web-app runtime modules (need .env; orthanc_client raises on
    # missing creds). Kept out of module scope so unit tests can import the pure
    # helpers above without a configured environment.
    sys.path.insert(0, str(REPO_ROOT / "web-app"))
    from db import DB_CONFIG  # noqa: E402
    from orthanc_client import ORTHANC_PASS, ORTHANC_URL, ORTHANC_USER  # noqa: E402

    from config import DICOM_DATA_ROOT, STORAGE_MODE  # noqa: E402

    if STORAGE_MODE != "cold_path_cache":
        print(
            f"WARNING: STORAGE_MODE is '{STORAGE_MODE}' (not 'cold_path_cache'); "
            "this tool targets the cold-storage index layout. Aborting.",
            file=sys.stderr,
        )
        return 2
    if not DB_CONFIG.get("user"):
        print("DB_USER not set in .env", file=sys.stderr)
        return 1

    host_root = str(DICOM_DATA_ROOT)
    container_root = container_dicom_root()
    orthanc_url = ORTHANC_URL
    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASS)

    print(f"Mode: {'EXECUTE' if args.execute else 'DRY RUN'}")
    print(f"Container: {args.container}   DICOM root: {host_root} -> {container_root}")

    # --- snapshot + analyze ------------------------------------------------- #
    conn = psycopg2.connect(**DB_CONFIG)
    tmpdir = Path(tempfile.mkdtemp(prefix="prune-index-"))
    snap = tmpdir / "indexer-plugin.snapshot.db"
    try:
        print("Snapshotting indexer-plugin.db from container ...")
        snapshot_index_db(args.container, snap)
        idx = sqlite3.connect(f"file:{snap}?mode=ro", uri=True)
        patients = list_patients(conn, args.patient)
        print(f"Analyzing {len(patients)} patient(s) ...")
        maps_cache: dict[str, tuple[dict[str, str], dict[str, str]]] = {}
        result = analyze(conn, session, orthanc_url, idx, patients, host_root,
                         container_root, quiet=False, maps_cache=maps_cache)
        idx.close()

        t = result["totals"]
        print("\n" + "=" * 64)
        print(f"Patients affected:        {t['patients_affected']}")
        print(f"Stale rows (deletable):   {t['stale_rows']}")
        print(f"Fully-stale instances:    {t['orphan_instances']}  "
              f"({'will REST-delete' if args.delete_orphans else 'reported only — use --delete-orphans'})")
        print(f"Valid rows (kept):        {t['valid_rows']}")
        if t["unknown_inst"] or t["orphan_series"]:
            print(f"Out-of-scope (skipped):   unknown_inst={t['unknown_inst']} "
                  f"orphan_series={t['orphan_series']}")

        if args.json:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y-%m-%dT%H%M%S")
            out = REPORTS_DIR / f"{ts}.json"
            out.write_text(json.dumps(
                {"timestamp": ts, "mode": "execute" if args.execute else "dry-run",
                 "patient_filter": args.patient, "totals": t,
                 "per_patient": result["per_patient"]},
                indent=2, default=str))
            print(f"Report written to {out}")

        if not args.execute:
            print("\nDRY RUN — nothing changed. Re-run with --execute to apply.")
            return 0

        if t["stale_rows"] == 0 and not (args.delete_orphans and t["orphan_instances"]):
            print("\nNothing to do.")
            return 0

        # --- execute -------------------------------------------------------- #
        # The index-DB edit (which needs an Orthanc stop) is only required when
        # there are stale duplicate-path rows. Orphan-only cleanup is pure REST
        # and needs no outage.
        need_index_edit = bool(result["stale_paths"])

        if not args.yes and need_index_edit:
            print(f"\nThis will STOP the Orthanc container '{args.container}' briefly "
                  f"(OHIF/Explorer outage ~1-3 min), edit its index, and restart it.")
            if input("Type 'yes' to proceed: ").strip().lower() != "yes":
                print("Aborted.")
                return 0

        # Fully-stale instances: remove via REST while Orthanc is up (no outage).
        if args.delete_orphans and result["orphan_instances"]:
            print(f"REST-deleting {len(result['orphan_instances'])} fully-stale instance(s) ...")
            for iid in result["orphan_instances"]:
                r = session.delete(f"{orthanc_url}/instances/{iid}", timeout=30)
                if r.status_code not in (200, 404):
                    print(f"  WARN: DELETE /instances/{iid} -> {r.status_code}", file=sys.stderr)

        if not need_index_edit:
            print("No stale rows to remove (orphan-only run) — skipping Orthanc stop.")
            print("\nDone.")
            return 0

        print(f"Stopping {args.container} ...")
        _docker("stop", args.container)
        try:
            BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y-%m-%dT%H%M%S")
            backup = BACKUPS_DIR / f"indexer-plugin.{ts}.db"
            print(f"Backing up index DB -> {backup}")
            snapshot_index_db(args.container, backup)
            working = tmpdir / "indexer-plugin.working.db"
            shutil.copy2(backup, working)

            # Authoritative pass: recompute the stale set from the now-FROZEN
            # post-stop index (the indexer may have added rows between the initial
            # snapshot and the stop). Uses the cached Orthanc/DB maps so it does not
            # call the (now stopped) Orthanc. This is the same DB we edit + verify,
            # so there is no snapshot gap.
            widx = sqlite3.connect(f"file:{working}?mode=ro", uri=True)
            wres = analyze(conn, session, orthanc_url, widx, patients, host_root,
                           container_root, quiet=True, maps_cache=maps_cache)
            widx.close()
            stale_paths = wres["stale_paths"]

            print(f"Deleting {len(stale_paths)} stale row(s) (frozen index) ...")
            apply_deletes(working, stale_paths, args.vacuum)

            # Verify on the edited copy using the cached maps — Orthanc is stopped
            # here, so this pass must not call it.
            vidx = sqlite3.connect(f"file:{working}?mode=ro", uri=True)
            vres = analyze(conn, session, orthanc_url, vidx, patients, host_root,
                           container_root, quiet=True, maps_cache=maps_cache)
            vidx.close()
            remaining = vres["totals"]["stale_rows"]
            if remaining:
                raise RuntimeError(
                    f"post-edit verification still finds {remaining} stale rows; "
                    f"NOT restoring edited DB (backup preserved at {backup})"
                )

            print("Pushing edited index DB back into the container ...")
            _docker("cp", str(working), f"{args.container}:{INDEX_DB_CONTAINER_PATH}")
        finally:
            print(f"Starting {args.container} ...")
            _docker("start", args.container)

        if wait_for_orthanc(session, orthanc_url):
            ok = 0
            for iid in result["sample_instances"]:
                try:
                    if session.get(f"{orthanc_url}/instances/{iid}/file", timeout=15).status_code == 200:
                        ok += 1
                except requests.RequestException:
                    pass
            print(f"Orthanc back up. Sample instance reads OK: {ok}/{len(result['sample_instances'])}")
        else:
            print("WARNING: Orthanc did not report healthy within timeout.", file=sys.stderr)

        print("\nDone.")
        return 0
    finally:
        conn.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
