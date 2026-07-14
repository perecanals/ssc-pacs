"""Cold storage: warm/evict series into their original dicom paths.

The series is the unit of cache state: ``series_cache_state`` is keyed by
``seriesinstanceuid``; ``warm_series``/``evict_series`` are the primitives and
study/patient operations are thin wrappers (a study is ``hot`` only when all
its series are hot). Warm/evict is index-neutral — Orthanc's index rows persist
across eviction (``RemoveMissingFiles: false``).

Invariants:

* Every ``status='warming'`` row has ``warming_started_at`` set; the watchdog in
  :func:`warm_series` re-warms rows older than ``WARMING_TIMEOUT_MINUTES``.
* :func:`warm_series` refuses to extract without
  ``WARMING_DISK_SAFETY_FACTOR * compressed + WARMING_DISK_MIN_FREE_BYTES`` free
  at the target; the series row is reset to ``cold`` and
  :class:`InsufficientDiskSpaceError` raised (the route precheck surfaces 507).
* :func:`evict_series` deletes a cache row only after its ``rmtree`` succeeds;
  a failed eviction leaves the row intact for retry.
* Each series is warmed under its own advisory lock, acquired and released
  sequentially — at most one lock held at a time, so a whole-study warm cannot
  deadlock against a concurrent warm.
"""

from __future__ import annotations

import logging
import shutil
import tarfile
import time
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
import zstandard as zstd

from config import (
    COLD_ARCHIVE_ROOT,
    DICOM_DATA_ROOT,
    EVICTION_TTL_HOURS,
    STORAGE_MODE,
    WARMING_DISK_MIN_FREE_BYTES,
    WARMING_DISK_SAFETY_FACTOR,
    WARMING_TIMEOUT_MINUTES,
)
from db import get_conn
from orthanc_client import (
    orthanc_series_id,
    rebuild_series_metadata_cache,
    series_metadata_cache_is_healthy,
)

logger = logging.getLogger(__name__)

ADV_LOCK_KEY = 8741002


def _repair_metadata_cache(seriesinstanceuid: str, studyinstanceuid: str | None,
                           log_extra: dict[str, str]) -> None:
    """Rebuild the series' DICOMweb metadata cache if it is missing or poisoned.

    Called only once a series is warm (files on disk), which is the one moment
    the cache can be built correctly — see the invariant in orthanc_client. A
    cache first computed while the series was cold holds an empty array and
    400s forever, hanging OHIF; warming is what gives us the chance to fix it.

    Best-effort: a failure here must never fail the warm (the pixels are on
    disk regardless), so everything is swallowed and logged.
    """
    if not studyinstanceuid:
        return
    try:
        oid = orthanc_series_id(seriesinstanceuid)
        if not oid:
            return
        if series_metadata_cache_is_healthy(oid):
            return
        ok = rebuild_series_metadata_cache(studyinstanceuid, seriesinstanceuid, oid)
        if ok:
            logger.info("warm_series: rebuilt DICOMweb metadata cache", extra=log_extra)
        else:
            logger.warning(
                "warm_series: DICOMweb metadata cache rebuild did not yield a "
                "non-empty result — OHIF may still fail on this series",
                extra=log_extra,
            )
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "warm_series: DICOMweb metadata cache repair raised", extra=log_extra
        )


def effective_status_sql(alias: str = "cs.") -> str:
    """SQL fragment: a series' *effective* cache status, where a
    'warming'/'queued' row stuck past the warming timeout reads 'cold'
    (abandoned warm — worker died, or a queue dropped on app restart).
    LEFT JOIN-friendly: a missing row also reads 'cold'. Takes one %s
    param: the timeout in minutes. ``alias`` prefixes the columns
    (e.g. ``"cs."``; ``""`` when querying series_cache_state directly).
    """
    return (
        f"CASE WHEN {alias}status IN ('warming', 'queued') "
        f"AND {alias}warming_started_at < now() - (%s * interval '1 minute') "
        f"THEN 'cold' ELSE COALESCE({alias}status, 'cold') END"
    )


# Status row returned when a series/study has no cache state at all.
_COLD_STATUS_ROW = {
    "status": "cold",
    "error_message": None,
    "warmed_at": None,
    "last_accessed_at": None,
    "warming_started_at": None,
    "cache_path": None,
}


class InsufficientDiskSpaceError(RuntimeError):
    """Raised when the target filesystem cannot fit the estimated extraction.

    Carries `required_bytes` and `available_bytes` for the API layer.
    """

    def __init__(self, required_bytes: int, available_bytes: int, target: Path) -> None:
        self.required_bytes = int(required_bytes)
        self.available_bytes = int(available_bytes)
        self.target = target
        super().__init__(
            f"Insufficient disk space at {target}: required≈{required_bytes} bytes, "
            f"available={available_bytes} bytes"
        )


def _log_extra(studyinstanceuid: str) -> dict[str, str]:
    return {"study_uid": studyinstanceuid}


def _log_extra_series(seriesinstanceuid: str) -> dict[str, str]:
    return {"series_uid": seriesinstanceuid}


# ---------------------------------------------------------------------------
# Archive resolution / extraction primitives
# ---------------------------------------------------------------------------


def archive_path_for_series_dir(dicom_dir: Path, data_root: Path, cold_root: Path) -> Path:
    dicom_dir = dicom_dir.resolve()
    data_root = data_root.resolve()
    rel = dicom_dir.relative_to(data_root)
    return cold_root / rel.parent / f"{rel.name}.tar.zst"


def resolve_series_archive(dicom_archive_path: str | None, dicom_dir_path: str | None) -> Path | None:
    if dicom_archive_path:
        p = Path(dicom_archive_path)
        if p.is_file():
            return p
    if not dicom_dir_path:
        return None
    dicom_dir = Path(dicom_dir_path)
    try:
        return archive_path_for_series_dir(dicom_dir, DICOM_DATA_ROOT, COLD_ARCHIVE_ROOT)
    except ValueError:
        logger.warning("dicom_dir_path not under DICOM_DATA_ROOT: %s", dicom_dir_path)
        return None


def untar_zst(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dctx = zstd.ZstdDecompressor()
    with archive.open("rb") as f_in:
        with dctx.stream_reader(f_in) as z_in:
            with tarfile.open(fileobj=z_in, mode="r|") as tf:
                # filter="data" rejects absolute paths / ".." arcnames (defense in
                # depth — archives are produced internally) and is required to avoid
                # the extractall default becoming a hard error in Python 3.14.
                tf.extractall(dest, filter="data")


def _is_series_dir_warm(dicom_dir_path: str) -> bool:
    d = Path(dicom_dir_path)
    try:
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def _extract_one_series(dicom_dir_path: str, arch_path: Path, touched_tmp_dirs: list[Path]) -> float:
    """Extract one series archive to its original dicom_dir_path. Returns extract seconds.

    Extracts into a sibling ``*.warming`` temp dir, then atomically renames it
    over the destination. ``touched_tmp_dirs`` accumulates the temp paths so the
    caller can clean them up on failure. Raises on extraction error (the caller
    marks the series ``error``). A no-op (returns 0.0) if the dir is already warm.
    """
    dicom_dir = Path(dicom_dir_path)
    # .warming sibling lives at the same level: .../SeriesUID/DICOM.warming
    tmp_container = dicom_dir.with_name(dicom_dir.name + ".warming")
    touched_tmp_dirs.append(tmp_container)

    # Remove stale .warming dir from a prior crash.
    if tmp_container.exists():
        shutil.rmtree(tmp_container)

    # Skip if already extracted (partial warm from a previous run).
    if _is_series_dir_warm(dicom_dir_path):
        return 0.0

    t0 = time.perf_counter()
    # Archives use flat structure (files directly at archive root, matching
    # archive_all_series.py). Extract into tmp_container, then atomic rename
    # tmp_container → dicom_dir.
    untar_zst(arch_path, tmp_container)
    elapsed = time.perf_counter() - t0

    # Remove empty dicom_dir if it exists (rename requires destination absent or
    # empty on Linux).
    if dicom_dir.is_dir():
        shutil.rmtree(dicom_dir)
    tmp_container.replace(dicom_dir)
    return elapsed


def _advisory_lock(cur, key_text: str) -> None:
    cur.execute(
        "SELECT pg_advisory_lock(%s, (abs(hashtext(%s::text)))::integer)",
        (ADV_LOCK_KEY, key_text),
    )


def _advisory_unlock(cur, key_text: str) -> None:
    cur.execute(
        "SELECT pg_advisory_unlock(%s, (abs(hashtext(%s::text)))::integer)",
        (ADV_LOCK_KEY, key_text),
    )


def disk_usage_at(path: Path):
    """shutil.disk_usage for the filesystem holding `path` (walks up to the
    nearest existing ancestor, so it works while the path is transiently absent)."""
    p = path
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return shutil.disk_usage(p)


def disk_free_bytes(path: Path) -> int:
    return disk_usage_at(path).free


def _estimate_required_bytes(archives: list[tuple[str, Path]]) -> int:
    """Estimate total bytes needed to extract `archives` to disk.

    Heuristic: `safety_factor * sum(compressed_size)` + min-free headroom.
    Reading per-frame uncompressed sizes from .zst headers is unreliable
    (frames written without `Frame_Content_Size` report 0), so we use the
    conservative compressed-size multiplier from config.
    """
    compressed_total = 0
    for _, arch in archives:
        try:
            compressed_total += arch.stat().st_size
        except OSError:
            # If we can't stat the archive we'll fail later for a clearer
            # reason; assume the worst (3× the safety floor).
            compressed_total += int(WARMING_DISK_MIN_FREE_BYTES)
    return int(compressed_total * WARMING_DISK_SAFETY_FACTOR) + int(WARMING_DISK_MIN_FREE_BYTES)


# ---------------------------------------------------------------------------
# Small DB helpers (series ↔ study mapping)
# ---------------------------------------------------------------------------


def _series_for_studies(study_uids: list[str]) -> list[str]:
    """Return every seriesinstanceuid belonging to the given studies."""
    if not study_uids:
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seriesinstanceuid FROM image_series WHERE studyinstanceuid = ANY(%s)",
                (list(study_uids),),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _series_for_patient(patient_id: str) -> list[str]:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seriesinstanceuid FROM image_series WHERE patient_id = %s",
                (patient_id,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def list_patient_study_uids(patient_id: str) -> list[str]:
    """Return every studyinstanceuid belonging to a patient (warm-all fan-out)."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT studyinstanceuid FROM image_study WHERE patient_id = %s",
                (patient_id,),
            )
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def _study_path(study_uid: str) -> str | None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT study_path FROM image_study WHERE studyinstanceuid = %s",
                (study_uid,),
            )
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    finally:
        conn.close()


def _collapse_status(effs: list[str]) -> str:
    """Collapse per-series effective statuses to one study status: ``hot`` only
    when *all* series are hot; else warming > queued > error > cold."""
    if not effs:
        return "cold"
    if all(e == "hot" for e in effs):
        return "hot"
    if "warming" in effs:
        return "warming"
    if "queued" in effs:
        return "queued"
    if "error" in effs:
        return "error"
    return "cold"


# ---------------------------------------------------------------------------
# Disk-space prechecks (route-level, synchronous)
# ---------------------------------------------------------------------------


def _estimate_for_series_rows(series_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    archives: list[tuple[str, Path]] = []
    for r in series_rows:
        arch = resolve_series_archive(r.get("dicom_archive_path"), r.get("dicom_dir_path"))
        if arch and arch.is_file():
            archives.append((r["dicom_dir_path"], arch))

    archives_to_extract = [(dp, ap) for dp, ap in archives if not _is_series_dir_warm(dp)]
    if not archives_to_extract:
        return None

    required = _estimate_required_bytes(archives_to_extract)
    target = Path(archives_to_extract[0][0]).parent
    available = disk_free_bytes(target)
    return {"required_bytes": required, "available_bytes": available, "target": target}


def estimate_warm_disk_space(studyinstanceuid: str) -> dict[str, Any] | None:
    """Synchronous disk precheck for the study warm route handler.

    Resolves the study's archives, filters out series already warm on disk, and
    compares estimated required bytes against the free space at the extraction
    target. Does **not** write to ``series_cache_state``. Returns ``None`` if no
    extraction would be performed; otherwise a dict with ``required_bytes``,
    ``available_bytes``, and ``target`` (Path).
    """
    if STORAGE_MODE != "cold_path_cache":
        return None
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT dicom_dir_path, dicom_archive_path "
                "FROM image_series WHERE studyinstanceuid = %s",
                (studyinstanceuid,),
            )
            series_rows = cur.fetchall()
    finally:
        conn.close()
    return _estimate_for_series_rows([dict(r) for r in series_rows])


def estimate_warm_series_disk_space(series_uids: list[str]) -> dict[str, Any] | None:
    """Synchronous disk precheck for the series warm route handler (see above)."""
    if STORAGE_MODE != "cold_path_cache" or not series_uids:
        return None
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT dicom_dir_path, dicom_archive_path "
                "FROM image_series WHERE seriesinstanceuid = ANY(%s)",
                (list(series_uids),),
            )
            series_rows = cur.fetchall()
    finally:
        conn.close()
    return _estimate_for_series_rows([dict(r) for r in series_rows])


# ---------------------------------------------------------------------------
# Status reads (series source of truth; study/patient as aggregates)
# ---------------------------------------------------------------------------


def get_series_cache_status(seriesinstanceuid: str) -> dict[str, Any]:
    """Per-series cache status row (the same dict shape the study endpoint returns)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT status, error_message, warmed_at, last_accessed_at, "
                "       warming_started_at, cache_path "
                "FROM series_cache_state WHERE seriesinstanceuid = %s",
                (seriesinstanceuid,),
            )
            row = cur.fetchone()
        if not row:
            return dict(_COLD_STATUS_ROW)
        out = dict(row)
        for k in ("warmed_at", "last_accessed_at", "warming_started_at"):
            if out.get(k):
                out[k] = out[k].isoformat()
        return out
    finally:
        conn.close()


def get_batch_series_status(series_uids: list[str]) -> dict[str, str]:
    """Map each requested series UID to its effective cache status (default 'cold')."""
    out = {uid: "cold" for uid in series_uids}
    if not series_uids:
        return out
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT seriesinstanceuid, {effective_status_sql(alias='')} "
                "FROM series_cache_state WHERE seriesinstanceuid = ANY(%s)",
                (WARMING_TIMEOUT_MINUTES, list(series_uids)),
            )
            for uid, status in cur.fetchall():
                out[uid] = status
        return out
    finally:
        conn.close()


def get_cache_status(studyinstanceuid: str) -> dict[str, Any]:
    """Study cache status, aggregated over the study's series rows
    (``hot`` only when every series is hot; same dict shape as the series read)."""
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"SELECT {effective_status_sql()} AS eff, "
                "       cs.warmed_at, cs.last_accessed_at, cs.warming_started_at, cs.error_message "
                "FROM image_series s "
                "LEFT JOIN series_cache_state cs ON cs.seriesinstanceuid = s.seriesinstanceuid "
                "WHERE s.studyinstanceuid = %s",
                (WARMING_TIMEOUT_MINUTES, studyinstanceuid),
            )
            rows = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    if not rows:
        return dict(_COLD_STATUS_ROW)

    effs = [r["eff"] for r in rows]
    status = _collapse_status(effs)

    warmed_at = max((r["warmed_at"] for r in rows if r["warmed_at"]), default=None)
    last_accessed_at = max((r["last_accessed_at"] for r in rows if r["last_accessed_at"]), default=None)
    warming_started_at = min(
        (r["warming_started_at"] for r in rows
         if r["eff"] in ("warming", "queued") and r["warming_started_at"]),
        default=None,
    )
    error_message = next(
        (r["error_message"] for r in rows if r["eff"] == "error" and r["error_message"]),
        None,
    )

    return {
        "status": status,
        "error_message": error_message if status == "error" else None,
        "warmed_at": warmed_at.isoformat() if warmed_at else None,
        "last_accessed_at": last_accessed_at.isoformat() if last_accessed_at else None,
        "warming_started_at": warming_started_at.isoformat() if warming_started_at else None,
        "cache_path": _study_path(studyinstanceuid) if status == "hot" else None,
    }


def get_batch_cache_status(uids: list[str]) -> dict[str, str]:
    """Map each requested study UID to its aggregated cache status (default 'cold').

    A single round-trip for the table's visible rows. Studies with no warm series
    are reported ``cold``; a study is ``hot`` only when all its series are hot.
    """
    out = {uid: "cold" for uid in uids}
    if not uids:
        return out
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT s.studyinstanceuid, {effective_status_sql()} AS eff "
                "FROM image_series s "
                "LEFT JOIN series_cache_state cs ON cs.seriesinstanceuid = s.seriesinstanceuid "
                "WHERE s.studyinstanceuid = ANY(%s)",
                (WARMING_TIMEOUT_MINUTES, list(uids)),
            )
            by_study: dict[str, list[str]] = {}
            for study_uid, eff in cur.fetchall():
                by_study.setdefault(study_uid, []).append(eff)
        for study_uid, effs in by_study.items():
            out[study_uid] = _collapse_status(effs)
        return out
    finally:
        conn.close()


def _patient_status_counts(patient_ids: list[str]) -> dict[str, dict[str, int]]:
    """Per-patient {status: study_count} aggregated over each study's series.

    Two-level aggregation done in Python for clarity: collapse each study's series
    to a binary study status, then count studies by status per patient. A study
    with no series counts as one ``cold`` study (LEFT JOIN keeps it present).
    """
    counts = {
        pid: {"cold": 0, "queued": 0, "warming": 0, "hot": 0, "error": 0}
        for pid in patient_ids
    }
    if not patient_ids:
        return counts
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT st.patient_id, st.studyinstanceuid, {effective_status_sql()} AS eff "
                "FROM image_study st "
                "LEFT JOIN image_series s ON s.studyinstanceuid = st.studyinstanceuid "
                "LEFT JOIN series_cache_state cs ON cs.seriesinstanceuid = s.seriesinstanceuid "
                "WHERE st.patient_id = ANY(%s)",
                (WARMING_TIMEOUT_MINUTES, list(patient_ids)),
            )
            # patient_id -> study_uid -> [eff, ...]
            per_study: dict[str, dict[str, list[str]]] = {}
            for pid, study_uid, eff in cur.fetchall():
                per_study.setdefault(pid, {}).setdefault(study_uid, []).append(eff)
    finally:
        conn.close()

    for pid, studies in per_study.items():
        entry = counts.setdefault(pid, {"cold": 0, "queued": 0, "warming": 0, "hot": 0, "error": 0})
        for effs in studies.values():
            entry[_collapse_status(effs)] += 1
    return counts


def get_patient_cache_status(patient_id: str) -> dict[str, Any]:
    """Aggregate cache status across all of a patient's studies (counts studies)."""
    counts = _patient_status_counts([patient_id])[patient_id]
    total = sum(counts.values())
    return {"patient_id": patient_id, "total": total, **counts}


def get_patients_cache_status(patient_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Aggregate cache status for many patients (one query, study-counted summaries)."""
    counts = _patient_status_counts(patient_ids)
    out: dict[str, dict[str, Any]] = {}
    for pid, c in counts.items():
        out[pid] = {"patient_id": pid, "total": sum(c.values()), **c}
    return out


# ---------------------------------------------------------------------------
# Queue / reap / touch (series-keyed)
# ---------------------------------------------------------------------------


def mark_queued_series(series_uids: list[str]) -> None:
    """Persist a 'queued' marker for series submitted to the warm executor.

    Makes the Queued state durable across reloads and visible to other users.
    Only promotes rows that are currently cold/error/absent; never downgrades a
    'hot' or in-progress 'warming' row.
    """
    if STORAGE_MODE != "cold_path_cache" or not series_uids:
        return
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO series_cache_state (seriesinstanceuid, status, warming_started_at)
                VALUES (%s, 'queued', now())
                ON CONFLICT (seriesinstanceuid) DO UPDATE
                SET status = 'queued', warming_started_at = now(), error_message = NULL
                WHERE series_cache_state.status IN ('cold', 'error')
                """,
                [(u,) for u in series_uids],
            )
        conn.commit()
    finally:
        conn.close()


def mark_queued(studyinstanceuids: list[str]) -> None:
    """Queue every series of the given studies (study/patient warm entry point)."""
    if STORAGE_MODE != "cold_path_cache" or not studyinstanceuids:
        return
    mark_queued_series(_series_for_studies(studyinstanceuids))


def reap_stale_warming(min_age_minutes: float | None = None) -> list[str]:
    """Reset abandoned 'warming'/'queued' series rows back to 'cold'.

    A warm whose worker died mid-extraction leaves its row stuck in 'warming'
    forever; likewise a 'queued' row whose enqueue never ran (the in-memory
    executor queue is dropped on restart). This sweep returns such series to
    'cold' so they can be decompressed again.

    ``min_age_minutes`` defaults to ``WARMING_TIMEOUT_MINUTES``. Pass ``0`` at app
    startup: a freshly started process holds no warm locks, so every warming/queued
    row is orphaned. Each candidate is only reset if ``pg_try_advisory_lock``
    succeeds, so a genuinely-running warm (which holds that series lock) is never
    clobbered.
    """
    if STORAGE_MODE != "cold_path_cache":
        return []
    age = WARMING_TIMEOUT_MINUTES if min_age_minutes is None else min_age_minutes
    reaped: list[str] = []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT seriesinstanceuid FROM series_cache_state "
                "WHERE status IN ('warming', 'queued') "
                "AND warming_started_at < now() - (%s * interval '1 minute')",
                (age,),
            )
            candidates = [r[0] for r in cur.fetchall()]
            for uid in candidates:
                cur.execute(
                    "SELECT pg_try_advisory_lock(%s, (abs(hashtext(%s::text)))::integer)",
                    (ADV_LOCK_KEY, uid),
                )
                if not cur.fetchone()[0]:
                    continue  # a warm is genuinely still in progress — leave it
                try:
                    cur.execute(
                        "UPDATE series_cache_state SET status = 'cold', "
                        "warming_started_at = NULL, error_message = NULL "
                        "WHERE seriesinstanceuid = %s AND status IN ('warming', 'queued')",
                        (uid,),
                    )
                    if cur.rowcount:
                        reaped.append(uid)
                    conn.commit()
                finally:
                    _advisory_unlock(cur, uid)
                    conn.commit()
    finally:
        conn.close()
    return reaped


def touch_access_series(seriesinstanceuid: str) -> None:
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE series_cache_state SET last_accessed_at = now() "
                "WHERE seriesinstanceuid = %s",
                (seriesinstanceuid,),
            )
        conn.commit()
    finally:
        conn.close()


def touch_access(studyinstanceuid: str) -> None:
    """Touch every series of a study, so whole-study warmth ages (and is
    evicted) together."""
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE series_cache_state SET last_accessed_at = now() "
                "WHERE seriesinstanceuid IN "
                "(SELECT seriesinstanceuid FROM image_series WHERE studyinstanceuid = %s)",
                (studyinstanceuid,),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Warm primitive (series source of truth)
# ---------------------------------------------------------------------------


def _upsert_series_status(
    cur, seriesinstanceuid: str, status: str, *,
    error_message: str | None = None, stamp_warming: bool = False,
) -> None:
    """INSERT/UPDATE a series_cache_state row to `status` (warming_started_at handling inline)."""
    warming_started = "now()" if stamp_warming else "NULL"
    cur.execute(
        f"""
        INSERT INTO series_cache_state (seriesinstanceuid, status, error_message, warming_started_at)
        VALUES (%s, %s, %s, {warming_started})
        ON CONFLICT (seriesinstanceuid) DO UPDATE
        SET status = EXCLUDED.status, error_message = EXCLUDED.error_message,
            warming_started_at = {warming_started}
        """,
        (seriesinstanceuid, status, error_message),
    )


def _warm_one_series(cur, conn, seriesinstanceuid: str, row: dict[str, Any] | None,
                     log_extra: dict[str, str]) -> dict[str, Any]:
    """Warm a single series. The caller must already hold this series' advisory lock.

    Manages its own commits. Returns a result dict; raises
    :class:`InsufficientDiskSpaceError` (after marking the series 'cold') so the
    caller can record/surface it.
    """
    touched_tmp_dirs: list[Path] = []
    dir_path = row.get("dicom_dir_path") if row else None
    archive = resolve_series_archive(row.get("dicom_archive_path"), dir_path) if row else None

    cur.execute(
        "SELECT status, warming_started_at FROM series_cache_state WHERE seriesinstanceuid = %s",
        (seriesinstanceuid,),
    )
    cs_row = cur.fetchone()

    # Watchdog: a row stuck in 'warming' past the timeout is recoverable. The
    # advisory lock means no other process is currently warming this series, so
    # the previous warmer must have crashed — fall through to re-warm.
    if cs_row and cs_row["status"] == "warming":
        started = cs_row.get("warming_started_at")
        timeout_sec = WARMING_TIMEOUT_MINUTES * 60.0
        if started is None:
            logger.warning(
                "warm_series: stale warming row with no warming_started_at — re-warming",
                extra=log_extra,
            )
        else:
            cur.execute("SELECT now()")
            now_ts = cur.fetchone()["now"]
            age = (now_ts - started).total_seconds()
            if age > timeout_sec:
                logger.warning(
                    "warm_series: warming watchdog fired (age=%.0fs > timeout=%.0fs) — re-warming",
                    age, timeout_sec, extra=log_extra,
                )
            else:
                conn.commit()
                return {"ok": False, "error": "warm_in_progress", "warming_age_seconds": age}

    # Hot-check short-circuit: already warm and files present.
    if cs_row and cs_row["status"] == "hot" and dir_path and _is_series_dir_warm(dir_path):
        cur.execute(
            "UPDATE series_cache_state SET last_accessed_at = now() WHERE seriesinstanceuid = %s",
            (seriesinstanceuid,),
        )
        conn.commit()
        # Files are on disk: repair a cache poisoned before this series was
        # ever warmed (already-hot series can carry one just as cold ones do).
        _repair_metadata_cache(
            seriesinstanceuid, (row or {}).get("studyinstanceuid"), log_extra
        )
        return {"ok": True, "already_hot": True, "extract_seconds": 0.0}

    if not archive or not archive.is_file():
        _upsert_series_status(cur, seriesinstanceuid, "error", error_message="no_archive_for_series")
        conn.commit()
        logger.warning("warm_series: no archive for series", extra=log_extra)
        return {"ok": False, "error": "no_archive_for_series"}

    # Pre-extraction disk-space check (skip if already warm — it won't extract).
    if not _is_series_dir_warm(dir_path):
        required = _estimate_required_bytes([(dir_path, archive)])
        target = Path(dir_path).parent
        available = disk_free_bytes(target)
        if available < required:
            _upsert_series_status(
                cur, seriesinstanceuid, "cold",
                error_message=f"insufficient_disk_space:required={required},available={available}",
            )
            conn.commit()
            logger.error(
                "warm_series: insufficient disk space (required=%d, available=%d, target=%s)",
                required, available, target, extra=log_extra,
            )
            raise InsufficientDiskSpaceError(required, available, target)

    # Mark warming (and stamp warming_started_at for the watchdog).
    _upsert_series_status(cur, seriesinstanceuid, "warming", stamp_warming=True)
    conn.commit()

    try:
        secs = _extract_one_series(dir_path, archive, touched_tmp_dirs)
    except Exception as e:
        for tmp in touched_tmp_dirs:
            try:
                if tmp.exists():
                    shutil.rmtree(tmp)
            except Exception as cleanup_err:
                logger.warning(
                    "warm_series: tmp-dir cleanup failed for %s: %s",
                    tmp, cleanup_err, extra=log_extra,
                )
        conn.rollback()
        _upsert_series_status(cur, seriesinstanceuid, "error", error_message=str(e)[:2000])
        conn.commit()
        logger.exception("warm_series: extraction failed", extra=log_extra)
        return {"ok": False, "error": str(e)}

    if not _is_series_dir_warm(dir_path):
        _upsert_series_status(
            cur, seriesinstanceuid, "error", error_message="extraction_produced_no_warm_series"
        )
        conn.commit()
        logger.error("warm_series: extraction produced no warm series", extra=log_extra)
        return {"ok": False, "error": "extraction_produced_no_warm_series"}

    cur.execute(
        """
        UPDATE series_cache_state
        SET status = 'hot', warmed_at = now(), last_accessed_at = now(),
            cache_path = %s, error_message = NULL, warming_started_at = NULL
        WHERE seriesinstanceuid = %s
        """,
        (dir_path, seriesinstanceuid),
    )
    conn.commit()
    # The series is hot and its files are on disk — the only moment the
    # DICOMweb metadata cache can be built correctly. Repair it if it was
    # poisoned (empty array) by an eviction-time computation.
    _repair_metadata_cache(
        seriesinstanceuid, (row or {}).get("studyinstanceuid"), log_extra
    )
    logger.info("warm_series: hot (extract_seconds=%.2f)", secs, extra=log_extra)
    return {"ok": True, "extract_seconds": secs}


def warm_series(series_uids: list[str]) -> dict[str, Any]:
    """Extract one or more series archives to their original dicom_dir_path locations.

    The single warm primitive. Each series is warmed under its own advisory lock,
    acquired and released sequentially (never nested), so warming a whole study
    (the :func:`warm_study` wrapper) cannot deadlock against a concurrent warm.
    Returns an aggregate result: ``ok`` is True if at least one series ended hot.
    """
    if STORAGE_MODE != "cold_path_cache":
        return {"ok": True, "skipped": True, "reason": "not_cold_path_cache_mode"}
    if not series_uids:
        return {"ok": True, "series_count": 0, "extract_seconds": 0.0, "errors": []}

    # Holds this pooled connection for the whole extraction — bounded by
    # WARM_WORKERS (2) ≪ POOL_MAX (20), so the pool cannot be starved.
    conn = get_conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        cur.execute(
            "SELECT seriesinstanceuid, studyinstanceuid, dicom_dir_path, "
            "dicom_archive_path "
            "FROM image_series WHERE seriesinstanceuid = ANY(%s)",
            (list(series_uids),),
        )
        meta = {r["seriesinstanceuid"]: dict(r) for r in cur.fetchall()}

        total_extract = 0.0
        warm_count = 0
        already_hot_count = 0
        errors: list[dict[str, str]] = []

        for suid in series_uids:
            row = meta.get(suid)
            log_extra = _log_extra_series(suid)
            _advisory_lock(cur, suid)
            try:
                res = _warm_one_series(cur, conn, suid, row, log_extra)
            except InsufficientDiskSpaceError:
                # Already marked 'cold' + logged inside. Record and continue so a
                # whole-study warm still attempts its other series.
                res = {"ok": False, "error": "insufficient_disk_space"}
            except Exception as e:  # pragma: no cover - defensive
                conn.rollback()
                logger.exception("warm_series: unexpected error", extra=log_extra)
                res = {"ok": False, "error": str(e)}
            finally:
                _advisory_unlock(cur, suid)
                conn.commit()

            if res.get("ok"):
                warm_count += 1
                total_extract += res.get("extract_seconds", 0.0) or 0.0
                if res.get("already_hot"):
                    already_hot_count += 1
            else:
                errors.append({"series": suid, "error": res.get("error", "unknown")})

        out: dict[str, Any] = {
            "ok": warm_count > 0,
            "series_count": warm_count,
            "extract_seconds": total_extract,
            "errors": errors,
        }
        if warm_count > 0 and not errors and already_hot_count == warm_count:
            out["already_hot"] = True
        if not out["ok"]:
            out["error"] = errors[0]["error"] if errors else "no_series"
        return out
    finally:
        cur.close()
        conn.close()


# ---------------------------------------------------------------------------
# Study / patient warm wrappers
# ---------------------------------------------------------------------------


def warm_study(studyinstanceuid: str) -> dict[str, Any]:
    """Warm every series of a study — one :func:`warm_series` call, one executor
    task; returns a study-shaped result dict."""
    if STORAGE_MODE != "cold_path_cache":
        return {"ok": True, "skipped": True, "reason": "not_cold_path_cache_mode"}

    res = warm_series(_series_for_studies([studyinstanceuid]))
    if res.get("skipped"):
        return res

    out: dict[str, Any] = {
        "ok": res["ok"],
        "series_count": res.get("series_count", 0),
        "extract_seconds": res.get("extract_seconds", 0.0),
    }
    if res.get("already_hot"):
        out["already_hot"] = True
    if res["ok"]:
        out["cache_path"] = _study_path(studyinstanceuid)
    else:
        errs = res.get("errors", [])
        if errs and all(e["error"] == "no_archive_for_series" for e in errs):
            out["error"] = "no_archives_for_study"
        else:
            out["error"] = res.get("error", "warm_failed")
    return out


def warm_patient(patient_id: str) -> dict[str, Any]:
    """Warm every series under a patient (used by the patient warm fan-out)."""
    return warm_series(_series_for_patient(patient_id))


# ---------------------------------------------------------------------------
# Evict (series source of truth; study/patient wrappers)
# ---------------------------------------------------------------------------


def evict_series(series_uids: list[str]) -> dict[str, Any]:
    """Delete extracted DICOM files for the given series, then their cache rows.

    Two-phase and transactional, matching the previous study-level guarantee: all
    ``rmtree`` calls run first; the ``series_cache_state`` rows are only deleted
    after every deletion succeeds. If any ``rmtree`` raises, no rows are deleted
    and the exception propagates so an operator can intervene and retry.
    """
    if not series_uids:
        return {"ok": True, "evicted": []}
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT seriesinstanceuid, dicom_dir_path FROM image_series "
                "WHERE seriesinstanceuid = ANY(%s)",
                (list(series_uids),),
            )
            rows = cur.fetchall()

        # Phase 1: filesystem deletion. Don't touch the DB until every path is
        # gone — a partial filesystem with deleted cache rows is the orphan state
        # we are preventing.
        #
        # Each rmtree runs under the same per-series advisory lock warm_series
        # takes, so an evict cannot race a concurrent warm of the same series
        # (both rmtree the series dir; whoever loses the final rmdir() sees
        # ENOENT). One lock at a time, released immediately — same discipline as
        # warm_series, so the two can never deadlock against each other.
        lock_cur = conn.cursor()
        try:
            for row in rows:
                dp = row.get("dicom_dir_path")
                if not dp:
                    continue
                suid = row["seriesinstanceuid"]
                _advisory_lock(lock_cur, suid)
                try:
                    p = Path(dp)
                    if not p.exists():
                        continue
                    try:
                        shutil.rmtree(p)
                    except FileNotFoundError:
                        # The tree vanished under us. Eviction's goal state is
                        # "files are gone", and they are — this is a no-op, not a
                        # failure. Aborting here would strand the cache rows.
                        logger.info(
                            "evict_series: %s already gone — treating as evicted",
                            p, extra=_log_extra_series(suid),
                        )
                    except Exception as rm_err:
                        logger.exception(
                            "evict_series: rmtree failed for %s: %s — leaving cache rows intact",
                            p, rm_err, extra=_log_extra_series(suid),
                        )
                        raise
                finally:
                    _advisory_unlock(lock_cur, suid)
        finally:
            lock_cur.close()

        # Phase 2: DB delete. Single transaction.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM series_cache_state WHERE seriesinstanceuid = ANY(%s)",
                (list(series_uids),),
            )
        conn.commit()
        return {"ok": True, "evicted": list(series_uids)}
    finally:
        conn.close()


def evict_study(studyinstanceuid: str) -> dict[str, Any]:
    """Evict every series of a study."""
    evict_series(_series_for_studies([studyinstanceuid]))
    logger.info("evict_study: evicted", extra=_log_extra(studyinstanceuid))
    return {"ok": True, "evicted": studyinstanceuid}


def run_eviction() -> list[str]:
    """Evict series whose hot rows have not been accessed within the TTL.

    Because :func:`touch_access` touches all of a study's series together, an
    idle study's series age out and are evicted in the same sweep.
    """
    if STORAGE_MODE != "cold_path_cache":
        return []
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT seriesinstanceuid FROM series_cache_state
                WHERE status = 'hot'
                  AND last_accessed_at IS NOT NULL
                  AND last_accessed_at < (now() - (%s * interval '1 hour'))
                """,
                (EVICTION_TTL_HOURS,),
            )
            uids = [r[0] for r in cur.fetchall()]
    finally:
        conn.close()

    evicted: list[str] = []
    for uid in uids:
        try:
            evict_series([uid])
            evicted.append(uid)
        except Exception as e:
            logger.warning(
                "run_eviction: evict_series failed: %s", e,
                extra=_log_extra_series(uid),
            )
    return evicted
