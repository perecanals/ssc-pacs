"""Staff/admin imaging exports with safe headers and private temporary files."""

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import psycopg2.extras
from fastapi import HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from zipstream import ZipStream

from cache_manager import resolve_series_archive, untar_zst
from common import ensure_series_access
from db import get_conn
from download_headers import content_disposition, safe_name
from download_response import DownloadResponse

logger = logging.getLogger(__name__)
_slots = threading.BoundedSemaphore(2)
CONVERSION_TIMEOUT = 300
CONVERTER = Path(__file__).resolve().parents[1] / "scripts/dicom/dicom_to_nifti.py"


def series_record(uid, scope):
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            ensure_series_access(cur, uid, scope)
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT patient_id, acquisitiondatetime, seriesdescription, dicom_dir_path, dicom_archive_path "
                "FROM image_series WHERE seriesinstanceuid=%s LIMIT 1",
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                raise HTTPException(404, "Series not found")
            return row
    finally:
        conn.close()


def source_directory(row, temporary):
    archive = resolve_series_archive(row.get("dicom_archive_path"), row.get("dicom_dir_path"))
    if archive and archive.is_file():
        source = temporary / "dicom"
        source.mkdir()
        untar_zst(archive, source)
        return source
    source = Path(row["dicom_dir_path"]) if row.get("dicom_dir_path") else None
    if source is None or not source.is_dir():
        raise HTTPException(404, "DICOM files are unavailable for this series")
    return source


def imaging_download(uid, scope, *, nifti=False):
    row = series_record(uid, scope)
    if not _slots.acquire(blocking=False):
        raise HTTPException(429, "Two imaging downloads are already running; try again shortly")
    temporary = None
    cleaned = False

    def cleanup():
        nonlocal cleaned
        if cleaned:
            return
        cleaned = True
        if temporary is not None:
            shutil.rmtree(temporary, ignore_errors=True)
        _slots.release()

    try:
        temporary = Path(tempfile.mkdtemp(prefix="imaging-download-"))
        source = source_directory(row, temporary)
        date = row["acquisitiondatetime"].strftime("%Y%m%d") if row.get("acquisitiondatetime") else "nodate"
        name = safe_name(f"{row.get('patient_id') or 'unknown'}-{date}-{row.get('seriesdescription') or 'series'}")
        headers = {
            "Content-Disposition": content_disposition(name + (".nii.gz" if nifti else ".zip")),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        if nifti:
            output = temporary / "volume.nii.gz"
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(CONVERTER),
                        "--dir",
                        str(source),
                        "--out",
                        str(output),
                        "--dicom-series-uid",
                        uid,
                    ],
                    capture_output=True,
                    timeout=CONVERSION_TIMEOUT,
                    check=False,
                    env={**os.environ, "ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS": "2"},
                )
            except subprocess.TimeoutExpired:
                raise HTTPException(504, "NIfTI conversion timed out; try downloading the DICOM ZIP") from None
            if result.returncode != 0 or not output.is_file() or output.stat().st_size == 0:
                logger.warning("NIfTI conversion failed (exit code %s)", result.returncode)
                raise HTTPException(422, "This series could not be converted to NIfTI; download the DICOM ZIP instead")
            return DownloadResponse(FileResponse(output, media_type="application/gzip", headers=headers), cleanup)
        stream = ZipStream.from_path(
            str(source),
            arcname=safe_name(f"{row.get('patient_id') or 'unknown'}_{row.get('seriesdescription') or 'series'}"),
        )
        headers["Content-Length"] = str(len(stream))

        return DownloadResponse(StreamingResponse(iter(stream), media_type="application/zip", headers=headers), cleanup)
    except BaseException:
        cleanup()
        raise
