"""Clean deletion of a study or series across all three layers it lives in.

A study/series exists in three independent places, none of which cascade:

1. ``stanford-stroke`` DB — ``image_study`` / ``image_series`` plus the
   soft-linked side tables (``series_cache_state``, ``series_dicom_tags``,
   ``annotations``) and the ``*_labelled`` mirrors.
2. Orthanc — the ``orthanc_db`` index and DICOMweb caches (removed by the REST
   ``DELETE /studies|series/{id}``), **plus** the Folder-Indexer's private
   ``indexer-plugin.db`` Files rows. The REST delete does NOT clean those Files
   rows (the plugin only reacts to Orthanc start/stop), so they are purged
   separately via a Force ``POST /indexer/scan`` of the now-empty loose subtree
   (see :func:`purge_indexer_rows`).
3. On disk — the loose ``dicom_dir_path`` tree and the cold ``dicom_archive_path``
   archives, both under ``<root>/<patient>/<studyUID>/<series>/…``.

This module is the single source of truth for that removal, imported by both the
admin CLI (``scripts/admin/delete_study.py``) and the admin HTTP endpoints
(``routes/data_admin.py``).

**No privilege escalation needed.** The web-app service user owns both storage
roots (it already deletes loose files during cold-cache eviction), so the same
process can run every layer — Orthanc, DB, and on-disk file removal. The safety
gate for irreversible file deletion is the path-safety guard in
:func:`_assert_within_root` (target must be ≥ ``<patient>/<studyUID>`` under a
configured root), plus admin-only auth on the endpoint — not OS permissions.

**Annotations are discarded, not migrated.** Deleting the ``annotations`` rows
fires the append-only ``annotations_history`` trigger, so every removed value is
still recoverable from history (attributed to ``app.audit_user``).

Each step is idempotent, so a partial failure is repaired by re-running.
"""

from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Any

import psycopg2.extras
import requests

from config import COLD_ARCHIVE_ROOT, DICOM_DATA_ROOT
from orthanc_client import (
    ORTHANC_PASS,
    ORTHANC_URL,
    ORTHANC_USER,
    delete_orthanc_series,
    delete_orthanc_study,
    orthanc_series_id,
    orthanc_study_id,
)

logger = logging.getLogger(__name__)

# The docker-compose bind always mounts the host DICOM tree at /dicom-data in the
# Orthanc container — the path prefix the Folder Indexer stores in indexer-plugin.db.
INDEXER_CONTAINER_ROOT = "/dicom-data"

# Tables cleared for every series (soft-linked, no cascading FK). Guarded by
# to_regclass so a not-yet-migrated DB (or a stripped test fixture) is tolerated.
_SERIES_SIDE_TABLES = ("series_cache_state", "series_dicom_tags")


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #
def _assert_within_root(path: Path, root: Path) -> Path:
    """Return ``path`` resolved, or raise if it is not safely under ``root``.

    Guards against ever removing a storage root or a whole-patient directory:
    the target must be a strict descendant of ``root`` with **at least** a
    ``<patient>/<studyUID>`` tail (≥2 path components below the root).
    """
    resolved = path.resolve()
    root = root.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError(f"refusing to remove {resolved}: not under {root}")
    rel_parts = resolved.relative_to(root).parts
    if len(rel_parts) < 2:
        raise ValueError(
            f"refusing to remove {resolved}: too shallow "
            f"(would delete a storage root or a whole patient)"
        )
    return resolved


def _prune_empty_parents(start: Path, stop_at: Path) -> list[str]:
    """rmdir empty directories from ``start`` upward, stopping before ``stop_at``.

    Removes now-empty ``<series>`` / ``<studyUID>`` shells left after a series
    deletion, but never the ``<patient>`` directory (``stop_at``) or above.
    """
    removed: list[str] = []
    stop_at = stop_at.resolve()
    current = start.resolve()
    while current != stop_at and stop_at in current.parents:
        try:
            current.rmdir()  # only succeeds if empty
        except OSError:
            break
        removed.append(str(current))
        current = current.parent
    return removed


# --------------------------------------------------------------------------- #
# Plan building (read-only)
# --------------------------------------------------------------------------- #
def _table_exists(cur, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s)", (f"public.{name}",))
    return cur.fetchone()[0] is not None


def build_study_deletion_plan(conn, studyinstanceuid: str) -> dict[str, Any] | None:
    """Read-only inventory of everything a study delete would remove.

    Returns ``None`` if the study is unknown to the DB. Resolving the Orthanc ID
    is best-effort (``None`` if Orthanc doesn't know it — already gone, or never
    indexed).
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT studyinstanceuid, patient_id, studydescription "
            "FROM image_study WHERE studyinstanceuid = %s",
            (studyinstanceuid,),
        )
        study = cur.fetchone()
        if study is None:
            return None
        cur.execute(
            "SELECT seriesinstanceuid, dicom_dir_path, dicom_archive_path "
            "FROM image_series WHERE studyinstanceuid = %s "
            "ORDER BY seriesnumber NULLS LAST, seriesinstanceuid",
            (studyinstanceuid,),
        )
        series = [dict(r) for r in cur.fetchall()]
        series_uids = [s["seriesinstanceuid"] for s in series]
        n_annotations = _count_annotations(cur, studyinstanceuid, series_uids)

    patient_id = study["patient_id"]
    loose_study_dir = DICOM_DATA_ROOT / str(patient_id) / studyinstanceuid
    archive_study_dir = COLD_ARCHIVE_ROOT / str(patient_id) / studyinstanceuid

    return {
        "level": "study",
        "studyinstanceuid": studyinstanceuid,
        "patient_id": patient_id,
        "studydescription": study["studydescription"],
        "series": series,
        "series_uids": series_uids,
        "n_series": len(series),
        "n_annotations": n_annotations,
        "orthanc": {"kind": "study", "id": orthanc_study_id(studyinstanceuid)},
        # Whole study subtree in each root — removed wholesale (no ancestor prune).
        "remove_dirs": [str(loose_study_dir), str(archive_study_dir)],
        "prune_parents": [],
        # Loose subtree under the *indexed* root — its indexer-plugin.db Files rows
        # must be purged after the files are gone (the archive root is not indexed).
        "indexer_dirs": [str(loose_study_dir)],
    }


def build_series_deletion_plan(conn, seriesinstanceuid: str) -> dict[str, Any] | None:
    """Read-only inventory of everything a single-series delete would remove.

    The parent ``image_study`` row is left intact (the study may have other
    series). If this is the study's last series, Orthanc auto-removes the parent
    study resource, but the ``image_study`` DB row is deliberately preserved.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT seriesinstanceuid, studyinstanceuid, patient_id, "
            "       dicom_dir_path, dicom_archive_path "
            "FROM image_series WHERE seriesinstanceuid = %s",
            (seriesinstanceuid,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        n_annotations = _count_annotations(cur, None, [seriesinstanceuid])

    patient_id = row["patient_id"]
    remove_dirs: list[str] = []
    indexer_dirs: list[str] = []
    prune_parents: list[tuple[str, str]] = []

    # Loose + archive series subtree = the <seriesUID> dir (parent of DICOM /
    # DICOM.tar.zst). Prune now-empty ancestors up to (not incl.) the patient dir.
    if row["dicom_dir_path"]:
        series_dir = Path(row["dicom_dir_path"]).parent
        remove_dirs.append(str(series_dir))
        indexer_dirs.append(str(series_dir))  # loose tree is the indexed one
        if patient_id:
            prune_parents.append((str(series_dir.parent), str(DICOM_DATA_ROOT / str(patient_id))))
    if row["dicom_archive_path"]:
        archive_dir = Path(row["dicom_archive_path"]).parent
        remove_dirs.append(str(archive_dir))
        if patient_id:
            prune_parents.append((str(archive_dir.parent), str(COLD_ARCHIVE_ROOT / str(patient_id))))

    return {
        "level": "series",
        "seriesinstanceuid": seriesinstanceuid,
        "studyinstanceuid": row["studyinstanceuid"],
        "patient_id": patient_id,
        "series_uids": [seriesinstanceuid],
        "n_series": 1,
        "n_annotations": n_annotations,
        "orthanc": {"kind": "series", "id": orthanc_series_id(seriesinstanceuid)},
        "remove_dirs": remove_dirs,
        "prune_parents": prune_parents,
        "indexer_dirs": indexer_dirs,
    }


def _count_annotations(cur, studyinstanceuid: str | None, series_uids: list[str]) -> int:
    """Count annotation rows a delete would discard (study + series level)."""
    if studyinstanceuid is not None:
        cur.execute(
            "SELECT count(*) AS n FROM annotations "
            "WHERE studyinstanceuid = %s OR seriesinstanceuid = ANY(%s)",
            (studyinstanceuid, series_uids),
        )
    else:
        cur.execute(
            "SELECT count(*) AS n FROM annotations WHERE seriesinstanceuid = ANY(%s)",
            (series_uids,),
        )
    row = cur.fetchone()
    # Tolerate either a RealDictCursor (plan builders) or a plain cursor.
    return row["n"] if isinstance(row, dict) else row[0]


# --------------------------------------------------------------------------- #
# Layer 1 + 2: Orthanc index + DB rows (runnable as the non-root web-app)
# --------------------------------------------------------------------------- #
def delete_index_and_db(conn, plan: dict[str, Any], *, execute: bool) -> dict[str, Any]:
    """Remove the study/series from Orthanc and the ``stanford-stroke`` DB.

    Does NOT touch on-disk files (see :func:`remove_files`). The caller's ``conn``
    should already carry ``app.audit_user`` (``get_conn`` sets it from the JWT;
    the CLI sets it explicitly) so the history trigger attributes the delete.

    With ``execute=False`` nothing is changed and the returned counts are the
    plan's projected counts. On ``execute=True`` returns the actual rowcounts and
    ``orthanc_deleted`` (True unless the REST call errored — a 404 counts as True).
    """
    result: dict[str, Any] = {
        "orthanc_deleted": None,
        "annotations": 0,
        "image_series": 0,
        "image_study": 0,
    }
    if not execute:
        result["annotations"] = plan["n_annotations"]
        result["image_series"] = plan["n_series"]
        result["image_study"] = 1 if plan["level"] == "study" else 0
        return result

    # 1) Orthanc first — idempotent, online, cleans both orthanc DBs + caches.
    oid = plan["orthanc"]["id"]
    if oid:
        if plan["level"] == "study":
            result["orthanc_deleted"] = delete_orthanc_study(oid)
        else:
            result["orthanc_deleted"] = delete_orthanc_series(oid)
    else:
        result["orthanc_deleted"] = True  # nothing indexed to remove

    # 2) DB rows in one transaction.
    series_uids = plan["series_uids"]
    with conn.cursor() as cur:
        if plan["level"] == "study":
            study_uid = plan["studyinstanceuid"]
            cur.execute(
                "DELETE FROM annotations "
                "WHERE studyinstanceuid = %s OR seriesinstanceuid = ANY(%s)",
                (study_uid, series_uids),
            )
            result["annotations"] = cur.rowcount
            _delete_series_side_rows(cur, series_uids)
            if _table_exists(cur, "image_series_labelled"):
                cur.execute(
                    "DELETE FROM image_series_labelled WHERE studyinstanceuid = %s",
                    (study_uid,),
                )
            if _table_exists(cur, "image_study_labelled"):
                cur.execute(
                    "DELETE FROM image_study_labelled WHERE studyinstanceuid = %s",
                    (study_uid,),
                )
            cur.execute(
                "DELETE FROM image_series WHERE studyinstanceuid = %s", (study_uid,)
            )
            result["image_series"] = cur.rowcount
            cur.execute(
                "DELETE FROM image_study WHERE studyinstanceuid = %s", (study_uid,)
            )
            result["image_study"] = cur.rowcount
        else:
            series_uid = plan["seriesinstanceuid"]
            cur.execute(
                "DELETE FROM annotations WHERE seriesinstanceuid = %s", (series_uid,)
            )
            result["annotations"] = cur.rowcount
            _delete_series_side_rows(cur, series_uids)
            if _table_exists(cur, "image_series_labelled"):
                cur.execute(
                    "DELETE FROM image_series_labelled WHERE seriesinstanceuid = %s",
                    (series_uid,),
                )
            cur.execute(
                "DELETE FROM image_series WHERE seriesinstanceuid = %s", (series_uid,)
            )
            result["image_series"] = cur.rowcount
    conn.commit()
    return result


def _delete_series_side_rows(cur, series_uids: list[str]) -> None:
    for table in _SERIES_SIDE_TABLES:
        if _table_exists(cur, table):
            cur.execute(
                f"DELETE FROM {table} WHERE seriesinstanceuid = ANY(%s)",
                (series_uids,),
            )


# --------------------------------------------------------------------------- #
# Layer 3: on-disk files
# --------------------------------------------------------------------------- #
# The web-app service user owns both storage roots (it already deletes loose
# files during cold-cache eviction), so no privilege escalation is needed — the
# safety gate is the path-safety guard below, not OS permissions.
def remove_files(plan: dict[str, Any], *, execute: bool) -> dict[str, Any]:
    """Remove the study/series directories from both storage roots.

    Every target is validated to sit safely under ``DICOM_DATA_ROOT`` /
    ``COLD_ARCHIVE_ROOT`` (and at least ``<patient>/<studyUID>`` deep) before
    removal — that guard, not OS privilege, is what prevents a stray delete.
    """
    result = remove_dir_list(plan["remove_dirs"], execute=execute)
    pruned: list[str] = []
    if execute:
        for start_raw, stop_raw in plan.get("prune_parents", []):
            pruned.extend(_prune_empty_parents(Path(start_raw), Path(stop_raw)))
    result["pruned"] = pruned
    return result


def remove_dir_list(dirs: list[str], *, execute: bool) -> dict[str, Any]:
    """Safely remove a list of directories under the storage roots.

    Shared by :func:`remove_files` and the CLI's orphan-sweep. Every path is
    validated under a storage root with a ``<patient>/<studyUID>`` (or deeper)
    tail before removal, so it can never delete a root or a whole patient.
    """
    removed: list[str] = []
    missing: list[str] = []
    for raw in dirs:
        path = Path(raw)
        root = _root_for(path)
        target = _assert_within_root(path, root)  # raises on anything unsafe
        if not target.exists():
            missing.append(str(target))
            continue
        if execute:
            shutil.rmtree(target)
        removed.append(str(target))
    return {"removed": removed, "missing": missing}


# --------------------------------------------------------------------------- #
# Orthanc Folder-Indexer cleanup (indexer-plugin.db Files rows)
# --------------------------------------------------------------------------- #
#
# A REST DELETE of a study/series removes it from orthanc_db + its DICOMweb
# caches, but the patched Folder Indexer does NOT clean its private
# indexer-plugin.db Files rows on delete (its OnChange handler only reacts to
# Orthanc start/stop). Left alone those rows are stale tombstones — harmless
# while the files are gone, but if the files are still on disk a subsequent
# *Force* scan (or a re-ingest of the same paths) would re-register them and
# RESURRECT the study. So after removing the files we Force-scan the (now empty)
# loose subtree: RemoveFilesUnderPrefix drops the rows and the scan re-adds
# nothing. This is the only part of the delete that touches the indexer index.
def _host_dir_to_container(host_dir: str) -> str | None:
    """Rewrite a host loose dir to the container path the indexer records, or None
    if it is not under the indexed root (e.g. an archive-root dir — never indexed)."""
    root = str(DICOM_DATA_ROOT).rstrip("/")
    hd = host_dir.rstrip("/")
    if hd == root or hd.startswith(root + "/"):
        return INDEXER_CONTAINER_ROOT + hd[len(root):]
    return None


def purge_indexer_rows(
    host_loose_dirs: list[str],
    *,
    execute: bool,
    timeout_s: float = 180.0,
    poll_s: float = 2.0,
) -> dict[str, Any]:
    """Drop the indexer-plugin.db Files rows for deleted loose subtrees.

    **Call only after the files are removed.** Any dir still containing files is
    skipped (a Force scan would re-register it — the exact resurrection this
    guards against). Uses the patched ``POST /indexer/scan`` (Force) — online, no
    Orthanc restart. Returns ``{purged, skipped_nonempty, skipped_unindexed}``.
    """
    folders: list[str] = []
    skipped_nonempty: list[str] = []
    skipped_unindexed: list[str] = []
    for hd in host_loose_dirs:
        cpath = _host_dir_to_container(hd)
        if cpath is None:
            skipped_unindexed.append(hd)
            continue
        p = Path(hd)
        if p.exists() and any(p.rglob("*")):
            skipped_nonempty.append(hd)  # refuse — Force-scanning this resurrects it
            continue
        folders.append(cpath)

    result: dict[str, Any] = {
        "purged": folders,
        "skipped_nonempty": skipped_nonempty,
        "skipped_unindexed": skipped_unindexed,
        "error": None,
    }
    if not execute or not folders:
        return result

    # Best-effort: the study is already gone from Orthanc + DB + disk. If the
    # Force-scan fails (Orthanc down, timeout), the leftover Files rows are an
    # inert tombstone — re-runnable later via `delete_study.py --purge-orphan-files`.
    session = requests.Session()
    session.auth = (ORTHANC_USER, ORTHANC_PASS)
    try:
        _post_force_scan(session, folders, timeout_s=timeout_s, poll_s=poll_s)
    except (requests.RequestException, RuntimeError) as exc:
        logger.warning("indexer purge Force-scan failed for %s: %s", folders, exc)
        result["error"] = str(exc)
    return result


def _post_force_scan(session, folders, *, timeout_s, poll_s):
    """POST /indexer/scan {Force:true} and poll until idle (handles a busy 409)."""
    def _start():
        return session.post(
            f"{ORTHANC_URL}/indexer/scan",
            json={"Folders": folders, "Force": True},
            timeout=60,
        )

    deadline = time.monotonic() + timeout_s
    resp = _start()
    if resp.status_code == 409:  # another scan running — wait it out, retry once
        while time.monotonic() < deadline:
            time.sleep(poll_s)
            try:
                if not session.get(f"{ORTHANC_URL}/indexer/scan", timeout=30).json().get("busy"):
                    break
            except requests.RequestException:
                pass
        resp = _start()
    resp.raise_for_status()
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        try:
            if not session.get(f"{ORTHANC_URL}/indexer/scan", timeout=30).json().get("busy"):
                return
        except requests.RequestException:
            pass
    logger.warning("indexer Force-scan purge did not report idle within %ss", timeout_s)


def _root_for(path: Path) -> Path:
    """Which storage root a path belongs to (for the safety check)."""
    resolved = path.resolve()
    if resolved.is_relative_to(COLD_ARCHIVE_ROOT.resolve()):
        return COLD_ARCHIVE_ROOT
    if resolved.is_relative_to(DICOM_DATA_ROOT.resolve()):
        return DICOM_DATA_ROOT
    raise ValueError(
        f"refusing to remove {resolved}: outside both storage roots "
        f"({DICOM_DATA_ROOT}, {COLD_ARCHIVE_ROOT})"
    )


# --------------------------------------------------------------------------- #
# Mode 2: orphan file sweep (files whose DB rows are already gone)
# --------------------------------------------------------------------------- #
def find_orphan_study_dirs(conn) -> list[str]:
    """``<root>/<patient>/<studyUID>`` dirs with no ``image_study`` row.

    These are what a UI (index+DB) delete leaves behind for the sudo CLI to sweep.
    A study directory is orphaned iff its name (the StudyInstanceUID) is unknown
    to ``image_study``.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT studyinstanceuid FROM image_study")
        known = {r[0] for r in cur.fetchall()}

    orphans: list[str] = []
    for root in (DICOM_DATA_ROOT, COLD_ARCHIVE_ROOT):
        if not root.is_dir():
            continue
        for patient_dir in root.iterdir():
            if not patient_dir.is_dir():
                continue
            for study_dir in patient_dir.iterdir():
                if study_dir.is_dir() and study_dir.name not in known:
                    orphans.append(str(study_dir))
    return sorted(orphans)
