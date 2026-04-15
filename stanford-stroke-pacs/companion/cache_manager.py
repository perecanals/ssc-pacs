"""Cold storage: warm study into original dicom paths, eviction."""

from __future__ import annotations

import logging
import os
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
)

logger = logging.getLogger(__name__)

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=os.getenv("DB_PORT", "5432"),
    dbname=os.getenv("DB_NAME", "stanford-stroke"),
    user=os.getenv("DB_USER"),
    password=os.getenv("DB_PASSWORD"),
)

ADV_LOCK_KEY = 8741002


def _conn():
    return psycopg2.connect(**DB_CONFIG)


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


def get_cache_status(studyinstanceuid: str) -> dict[str, Any]:
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT status, error_message, warmed_at, last_accessed_at, cache_path "
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
                "cache_path": None,
            }
        out = dict(row)
        if out.get("warmed_at"):
            out["warmed_at"] = out["warmed_at"].isoformat()
        if out.get("last_accessed_at"):
            out["last_accessed_at"] = out["last_accessed_at"].isoformat()
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

    def finish() -> None:
        conn.commit()
        _advisory_unlock(cur, studyinstanceuid)
        conn.commit()

    try:
        cur.execute(
            "SELECT pg_advisory_lock(%s, (abs(hashtext(%s::text)))::integer)",
            (ADV_LOCK_KEY, studyinstanceuid),
        )

        # Hot-check: already warm and all files present → just touch and return.
        cur.execute(
            "SELECT status FROM cache_state WHERE studyinstanceuid = %s",
            (studyinstanceuid,),
        )
        cs_row = cur.fetchone()

        cur.execute(
            "SELECT seriesinstanceuid, dicom_dir_path, dicom_archive_path "
            "FROM image_series WHERE studyinstanceuid = %s",
            (studyinstanceuid,),
        )
        series_rows = cur.fetchall()

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

        # Mark warming.
        cur.execute(
            """
            INSERT INTO cache_state (studyinstanceuid, status, error_message)
            VALUES (%s, 'warming', NULL)
            ON CONFLICT (studyinstanceuid) DO UPDATE
            SET status = 'warming', error_message = NULL
            """,
            (studyinstanceuid,),
        )
        conn.commit()

        # Resolve archives for each series.
        archives: list[tuple[str, Path]] = []
        for r in series_rows:
            arch = resolve_series_archive(r.get("dicom_archive_path"), r.get("dicom_dir_path"))
            if arch and arch.is_file():
                archives.append((r["dicom_dir_path"], arch))

        if not archives:
            cur.execute(
                "UPDATE cache_state SET status = 'error', error_message = %s "
                "WHERE studyinstanceuid = %s",
                ("no_archives_for_study", studyinstanceuid),
            )
            finish()
            return {"ok": False, "error": "no_archives_for_study"}

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
                    dicom_dir_path, e,
                )

        # Verify at least one series warmed successfully.
        warm_count = sum(1 for dp, _ in archives if _is_series_dir_warm(dp))
        if warm_count == 0:
            cur.execute(
                "UPDATE cache_state SET status = 'error', error_message = %s "
                "WHERE studyinstanceuid = %s",
                ("extraction_produced_no_warm_series", studyinstanceuid),
            )
            finish()
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
                error_message = NULL
            WHERE studyinstanceuid = %s
            """,
            (cache_path, studyinstanceuid),
        )
        finish()

        return {
            "ok": True,
            "extract_seconds": t_extract,
            "series_count": warm_count,
            "cache_path": cache_path,
        }

    except Exception as e:
        conn.rollback()
        # Clean up any .warming temp dirs created during this run.
        for tmp in touched_tmp_dirs:
            try:
                if tmp.exists():
                    shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass
        try:
            cur.execute(
                """
                INSERT INTO cache_state (studyinstanceuid, status, error_message)
                VALUES (%s, 'error', %s)
                ON CONFLICT (studyinstanceuid) DO UPDATE
                SET status = 'error', error_message = EXCLUDED.error_message
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
        logger.exception("warm_study failed for %s", studyinstanceuid)
        return {"ok": False, "error": str(e)}
    finally:
        cur.close()
        conn.close()


def evict_study(studyinstanceuid: str) -> dict[str, Any]:
    """Delete extracted DICOM files from each series dicom_dir_path for a study."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT dicom_dir_path FROM image_series WHERE studyinstanceuid = %s",
                (studyinstanceuid,),
            )
            rows = cur.fetchall()

        for row in rows:
            dp = row.get("dicom_dir_path")
            if dp:
                shutil.rmtree(Path(dp), ignore_errors=True)

        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM cache_state WHERE studyinstanceuid = %s",
                (studyinstanceuid,),
            )
        conn.commit()
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
                logger.warning("evict_study %s failed: %s", uid, e)
        return evicted
    finally:
        conn.close()
