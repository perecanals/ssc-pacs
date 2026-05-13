"""Cold storage: warm study into original dicom paths, eviction.

Robustness invariants (see maintenance/workstreams/05-cold-storage-robustness.md):

* Every `cache_state` row that holds `status='warming'` also has
  `warming_started_at = now()`. The watchdog in `warm_study()` treats
  rows older than `WARMING_TIMEOUT_MINUTES` as recoverable and proceeds
  to re-warm them.
* `warm_study()` refuses to extract if the target filesystem cannot
  fit `WARMING_DISK_SAFETY_FACTOR * compressed` bytes plus
  `WARMING_DISK_MIN_FREE_BYTES` of headroom. On insufficient space the
  row is reset to `status='cold'` and an `InsufficientDiskSpaceError`
  is raised.
* `evict_study()` only deletes the `cache_state` row after `rmtree`
  succeeds. A failed eviction leaves the row intact so the operator
  can retry.
* Every warm/evict log line carries the study UID via
  `extra={'study_uid': uid}`.
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
    EVICTION_TTL_HOURS,
    LEGACY_DICOM_ROOT,
    STORAGE_MODE,
    WARMING_DISK_MIN_FREE_BYTES,
    WARMING_DISK_SAFETY_FACTOR,
    WARMING_TIMEOUT_MINUTES,
)
from db import get_conn

logger = logging.getLogger(__name__)

_conn = get_conn  # Backward-compat alias used throughout this module.

ADV_LOCK_KEY = 8741002


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


def archive_path_for_series_dir(dicom_dir: Path, legacy_root: Path, cold_root: Path) -> Path:
    dicom_dir = dicom_dir.resolve()
    legacy_root = legacy_root.resolve()
    rel = dicom_dir.relative_to(legacy_root)
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
        return archive_path_for_series_dir(dicom_dir, LEGACY_DICOM_ROOT, COLD_ARCHIVE_ROOT)
    except ValueError:
        logger.warning("dicom_dir_path not under LEGACY_DICOM_ROOT: %s", dicom_dir_path)
        return None


def iter_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def untar_zst(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    dctx = zstd.ZstdDecompressor()
    with archive.open("rb") as f_in:
        with dctx.stream_reader(f_in) as z_in:
            with tarfile.open(fileobj=z_in, mode="r|") as tf:
                tf.extractall(dest)


def _is_series_dir_warm(dicom_dir_path: str) -> bool:
    d = Path(dicom_dir_path)
    try:
        return d.is_dir() and any(d.iterdir())
    except OSError:
        return False


def _advisory_unlock(cur, studyinstanceuid: str) -> None:
    cur.execute(
        "SELECT pg_advisory_unlock(%s, (abs(hashtext(%s::text)))::integer)",
        (ADV_LOCK_KEY, studyinstanceuid),
    )


def _disk_free_bytes(path: Path) -> int:
    """Return free bytes on the filesystem holding `path` (or its nearest existing ancestor)."""
    p = path
    while not p.exists():
        if p.parent == p:
            break
        p = p.parent
    return shutil.disk_usage(p).free


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


def estimate_warm_disk_space(studyinstanceuid: str) -> dict[str, Any] | None:
    """Synchronous precheck for the warm route handler.

    Resolves the study's archives, filters out series that are already
    warm on disk, and compares estimated required bytes against the
    free space at the extraction target. Does **not** write to
    ``cache_state`` — the caller decides whether to surface 507 or
    proceed.

    Returns ``None`` if no extraction would be performed (no archives
    found, or every series is already warm). Otherwise a dict with
    ``required_bytes``, ``available_bytes``, and ``target`` (Path).

    The defensive disk-space check inside :func:`warm_study` runs again
    inside the worker — this precheck is the first-line guard so the
    route can return 507 synchronously instead of letting the worker
    discover the problem after a thread submission.
    """
    if STORAGE_MODE != "cold_path_cache":
        return None

    conn = _conn()
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

    archives: list[tuple[str, Path]] = []
    for r in series_rows:
        arch = resolve_series_archive(r.get("dicom_archive_path"), r.get("dicom_dir_path"))
        if arch and arch.is_file():
            archives.append((r["dicom_dir_path"], arch))

    archives_to_extract = [
        (dp, ap) for dp, ap in archives if not _is_series_dir_warm(dp)
    ]
    if not archives_to_extract:
        return None

    required = _estimate_required_bytes(archives_to_extract)
    target = Path(archives_to_extract[0][0]).parent
    available = _disk_free_bytes(target)
    return {
        "required_bytes": required,
        "available_bytes": available,
        "target": target,
    }


def get_cache_status(studyinstanceuid: str) -> dict[str, Any]:
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT status, error_message, warmed_at, last_accessed_at, "
                "       warming_started_at, cache_path "
                "FROM cache_state WHERE studyinstanceuid = %s",
                (studyinstanceuid,),
            )
            row = cur.fetchone()
        if not row:
            return {
                "status": "cold",
                "error_message": None,
                "warmed_at": None,
                "last_accessed_at": None,
                "warming_started_at": None,
                "cache_path": None,
            }
        out = dict(row)
        for k in ("warmed_at", "last_accessed_at", "warming_started_at"):
            if out.get(k):
                out[k] = out[k].isoformat()
        return out
    finally:
        conn.close()


def touch_access(studyinstanceuid: str) -> None:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE cache_state SET last_accessed_at = now() WHERE studyinstanceuid = %s",
                (studyinstanceuid,),
            )
        conn.commit()
    finally:
        conn.close()


def warm_study(studyinstanceuid: str) -> dict[str, Any]:
    """Extract all series archives for a study to their original dicom_dir_path locations.

    Orthanc's Folder Indexer has already recorded these paths in its index.
    Restoring files to the same paths makes OHIF work immediately — no re-ingestion needed.
    """
    if STORAGE_MODE != "cold_path_cache":
        return {"ok": True, "skipped": True, "reason": "not_cold_path_cache_mode"}

    conn = _conn()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    touched_tmp_dirs: list[Path] = []
    log_extra = _log_extra(studyinstanceuid)

    def finish() -> None:
        conn.commit()
        _advisory_unlock(cur, studyinstanceuid)
        conn.commit()

    try:
        cur.execute(
            "SELECT pg_advisory_lock(%s, (abs(hashtext(%s::text)))::integer)",
            (ADV_LOCK_KEY, studyinstanceuid),
        )

        cur.execute(
            "SELECT status, warming_started_at FROM cache_state WHERE studyinstanceuid = %s",
            (studyinstanceuid,),
        )
        cs_row = cur.fetchone()

        cur.execute(
            "SELECT seriesinstanceuid, dicom_dir_path, dicom_archive_path "
            "FROM image_series WHERE studyinstanceuid = %s",
            (studyinstanceuid,),
        )
        series_rows = cur.fetchall()

        # Watchdog: a row stuck in 'warming' past the timeout is treated as
        # recoverable. The advisory lock above means no other process is
        # currently warming this study, so the previous warmer must have
        # crashed. Log a warning and fall through to re-warm.
        if cs_row and cs_row["status"] == "warming":
            started = cs_row.get("warming_started_at")
            timeout_sec = WARMING_TIMEOUT_MINUTES * 60.0
            if started is None:
                logger.warning(
                    "warm_study: stale warming row with no warming_started_at — re-warming",
                    extra=log_extra,
                )
            else:
                cur.execute("SELECT now()")
                now_ts = cur.fetchone()["now"]
                age = (now_ts - started).total_seconds()
                if age > timeout_sec:
                    logger.warning(
                        "warm_study: warming watchdog fired (age=%.0fs > timeout=%.0fs) — re-warming",
                        age, timeout_sec, extra=log_extra,
                    )
                else:
                    # Another warm is genuinely in progress (can't happen
                    # under the advisory lock, but be defensive). Surface
                    # an explicit error rather than racing.
                    finish()
                    return {
                        "ok": False,
                        "error": "warm_in_progress",
                        "warming_age_seconds": age,
                    }

        # Hot-check short-circuit: already warm and all files present.
        if cs_row and cs_row["status"] == "hot":
            non_null_dirs = [r["dicom_dir_path"] for r in series_rows if r.get("dicom_dir_path")]
            if non_null_dirs and all(_is_series_dir_warm(d) for d in non_null_dirs):
                cur.execute(
                    "UPDATE cache_state SET last_accessed_at = now() WHERE studyinstanceuid = %s",
                    (studyinstanceuid,),
                )
                conn.commit()
                _advisory_unlock(cur, studyinstanceuid)
                conn.commit()
                return {"ok": True, "already_hot": True}

        # Resolve archives for each series.
        archives: list[tuple[str, Path]] = []
        for r in series_rows:
            arch = resolve_series_archive(r.get("dicom_archive_path"), r.get("dicom_dir_path"))
            if arch and arch.is_file():
                archives.append((r["dicom_dir_path"], arch))

        if not archives:
            cur.execute(
                """
                INSERT INTO cache_state (studyinstanceuid, status, error_message, warming_started_at)
                VALUES (%s, 'error', %s, NULL)
                ON CONFLICT (studyinstanceuid) DO UPDATE
                SET status = 'error', error_message = EXCLUDED.error_message,
                    warming_started_at = NULL
                """,
                (studyinstanceuid, "no_archives_for_study"),
            )
            finish()
            logger.warning("warm_study: no archives for study", extra=log_extra)
            return {"ok": False, "error": "no_archives_for_study"}

        # Pre-extraction disk-space check. Estimate against the legacy root
        # since every dicom_dir_path lives under it (warm extracts in-place).
        # Skip archives whose dest is already warm — they won't be extracted.
        archives_to_extract = [
            (dp, ap) for dp, ap in archives if not _is_series_dir_warm(dp)
        ]
        if archives_to_extract:
            required = _estimate_required_bytes(archives_to_extract)
            target_for_check = Path(archives_to_extract[0][0]).parent
            available = _disk_free_bytes(target_for_check)
            if available < required:
                # Mark cold (not 'warming') and surface a clear error.
                cur.execute(
                    """
                    INSERT INTO cache_state (studyinstanceuid, status, error_message, warming_started_at)
                    VALUES (%s, 'cold', %s, NULL)
                    ON CONFLICT (studyinstanceuid) DO UPDATE
                    SET status = 'cold', error_message = EXCLUDED.error_message,
                        warming_started_at = NULL
                    """,
                    (studyinstanceuid, f"insufficient_disk_space:required={required},available={available}"),
                )
                finish()
                logger.error(
                    "warm_study: insufficient disk space (required=%d, available=%d, target=%s)",
                    required, available, target_for_check, extra=log_extra,
                )
                raise InsufficientDiskSpaceError(required, available, target_for_check)

        # Mark warming (and stamp warming_started_at for the watchdog).
        cur.execute(
            """
            INSERT INTO cache_state (studyinstanceuid, status, error_message, warming_started_at)
            VALUES (%s, 'warming', NULL, now())
            ON CONFLICT (studyinstanceuid) DO UPDATE
            SET status = 'warming', error_message = NULL, warming_started_at = now()
            """,
            (studyinstanceuid,),
        )
        conn.commit()
        logger.info("warm_study: starting extraction (%d archives)", len(archives), extra=log_extra)

        # Per-series extraction.
        t_extract = 0.0
        for dicom_dir_path, arch_path in archives:
            dicom_dir = Path(dicom_dir_path)
            # .warming sibling lives at the same level: .../SeriesUID/DICOM.warming
            tmp_container = dicom_dir.with_name(dicom_dir.name + ".warming")
            touched_tmp_dirs.append(tmp_container)

            try:
                # Remove stale .warming dir from a prior crash.
                if tmp_container.exists():
                    shutil.rmtree(tmp_container)

                # Skip if already extracted (partial warm from a previous run).
                if _is_series_dir_warm(dicom_dir_path):
                    continue

                t0 = time.perf_counter()
                # Archives use flat structure (files directly at archive root,
                # matching archive_all_series.py). Extract into tmp_container,
                # then atomic rename tmp_container → dicom_dir.
                untar_zst(arch_path, tmp_container)
                t_extract += time.perf_counter() - t0

                # Remove empty dicom_dir if it exists (rename requires destination absent
                # or empty on Linux).
                if dicom_dir.is_dir():
                    shutil.rmtree(dicom_dir)
                tmp_container.replace(dicom_dir)

            except Exception as e:
                logger.warning(
                    "warm_study: extraction failed for %s: %s",
                    dicom_dir_path, e, extra=log_extra,
                )

        # Verify at least one series warmed successfully.
        warm_count = sum(1 for dp, _ in archives if _is_series_dir_warm(dp))
        if warm_count == 0:
            cur.execute(
                "UPDATE cache_state SET status = 'error', error_message = %s, "
                "warming_started_at = NULL "
                "WHERE studyinstanceuid = %s",
                ("extraction_produced_no_warm_series", studyinstanceuid),
            )
            finish()
            logger.error("warm_study: extraction produced no warm series", extra=log_extra)
            return {"ok": False, "error": "extraction_produced_no_warm_series"}

        # Fetch study_path to use as cache_path record.
        cur.execute(
            "SELECT study_path FROM image_study WHERE studyinstanceuid = %s",
            (studyinstanceuid,),
        )
        study_row = cur.fetchone()
        cache_path = study_row["study_path"] if study_row and study_row.get("study_path") else None

        cur.execute(
            """
            UPDATE cache_state
            SET status = 'hot',
                warmed_at = now(),
                last_accessed_at = now(),
                cache_path = %s,
                error_message = NULL,
                warming_started_at = NULL
            WHERE studyinstanceuid = %s
            """,
            (cache_path, studyinstanceuid),
        )
        finish()
        logger.info(
            "warm_study: hot (series=%d, extract_seconds=%.2f)",
            warm_count, t_extract, extra=log_extra,
        )

        return {
            "ok": True,
            "extract_seconds": t_extract,
            "series_count": warm_count,
            "cache_path": cache_path,
        }

    except InsufficientDiskSpaceError:
        # Already logged and the row was reset to 'cold' inside the
        # try-block. Skip the generic error handler so we don't overwrite
        # the cold-marker with status='error'.
        try:
            _advisory_unlock(cur, studyinstanceuid)
            conn.commit()
        except Exception:
            pass
        # Best-effort cleanup of any tmp dirs (none should exist yet).
        for tmp in touched_tmp_dirs:
            try:
                if tmp.exists():
                    shutil.rmtree(tmp)
            except Exception as cleanup_err:
                logger.warning(
                    "warm_study: tmp-dir cleanup failed for %s: %s",
                    tmp, cleanup_err, extra=log_extra,
                )
        raise
    except Exception as e:
        conn.rollback()
        # Clean up any .warming temp dirs created during this run; surface
        # cleanup errors instead of swallowing them so orphaned dirs are
        # detectable in the logs (and via cold_storage_health.py).
        for tmp in touched_tmp_dirs:
            try:
                if tmp.exists():
                    shutil.rmtree(tmp)
            except Exception as cleanup_err:
                logger.warning(
                    "warm_study: tmp-dir cleanup failed for %s: %s",
                    tmp, cleanup_err, extra=log_extra,
                )
        try:
            cur.execute(
                """
                INSERT INTO cache_state (studyinstanceuid, status, error_message, warming_started_at)
                VALUES (%s, 'error', %s, NULL)
                ON CONFLICT (studyinstanceuid) DO UPDATE
                SET status = 'error', error_message = EXCLUDED.error_message,
                    warming_started_at = NULL
                """,
                (studyinstanceuid, str(e)[:2000]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        try:
            _advisory_unlock(cur, studyinstanceuid)
            conn.commit()
        except Exception:
            pass
        logger.exception("warm_study failed", extra=log_extra)
        return {"ok": False, "error": str(e)}
    finally:
        cur.close()
        conn.close()


def evict_study(studyinstanceuid: str) -> dict[str, Any]:
    """Delete extracted DICOM files from each series dicom_dir_path for a study.

    Transactional: the `cache_state` row is only deleted after every
    `rmtree` succeeds. If any rmtree raises, the row is left intact and
    the exception is re-raised so the operator can intervene (chmod,
    free space, kill the holding process, etc.) and retry.
    """
    log_extra = _log_extra(studyinstanceuid)
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT dicom_dir_path FROM image_series WHERE studyinstanceuid = %s",
                (studyinstanceuid,),
            )
            rows = cur.fetchall()

        # Phase 1: filesystem deletion. Don't touch the DB until every
        # path is gone — a partial filesystem with a deleted cache_state
        # row is the orphan state we are trying to prevent.
        for row in rows:
            dp = row.get("dicom_dir_path")
            if not dp:
                continue
            p = Path(dp)
            if not p.exists():
                continue
            try:
                shutil.rmtree(p)
            except Exception as rm_err:
                logger.exception(
                    "evict_study: rmtree failed for %s: %s — leaving cache_state intact",
                    p, rm_err, extra=log_extra,
                )
                raise

        # Phase 2: DB delete. Single transaction.
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM cache_state WHERE studyinstanceuid = %s",
                (studyinstanceuid,),
            )
        conn.commit()
        logger.info("evict_study: evicted", extra=log_extra)
        return {"ok": True, "evicted": studyinstanceuid}
    finally:
        conn.close()


def run_eviction() -> list[str]:
    if STORAGE_MODE != "cold_path_cache":
        return []
    conn = _conn()
    evicted: list[str] = []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT studyinstanceuid FROM cache_state
                WHERE status = 'hot'
                  AND last_accessed_at IS NOT NULL
                  AND last_accessed_at < (now() - (%s * interval '1 hour'))
                """,
                (EVICTION_TTL_HOURS,),
            )
            uids = [r[0] for r in cur.fetchall()]
        for uid in uids:
            try:
                evict_study(uid)
                evicted.append(uid)
            except Exception as e:
                logger.warning(
                    "run_eviction: evict_study failed: %s", e,
                    extra=_log_extra(uid),
                )
        return evicted
    finally:
        conn.close()
