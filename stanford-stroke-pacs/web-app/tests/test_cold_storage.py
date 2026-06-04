"""Tests for the cold-storage warm/evict cycle.

Uses a mock archive file (tar.zst) to exercise the warm → hot → evict → cold
state machine without touching real DICOM data.
"""

import tarfile
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import zstandard as zstd


@pytest.fixture()
def cold_env(db_conn, seeded_db):
    """Set up a temporary cold-storage environment with a mock archive.

    Creates:
      - A temporary dir as the "legacy dicom root"
      - A tar.zst archive containing a single dummy DICOM file
      - An image_series row pointing at both paths
      - An image_study row for the study
    """
    tmpdir = tempfile.mkdtemp(prefix="cold_test_")
    legacy_root = Path(tmpdir) / "imaging"
    cold_root = Path(tmpdir) / "cold"
    legacy_root.mkdir()
    cold_root.mkdir()

    study_uid = "1.2.999.test.cold"
    series_uid = "1.2.999.test.cold.1"
    patient_id = "P-COLD"

    # The dicom_dir_path where files would be extracted.
    dicom_dir = legacy_root / patient_id / study_uid / "Axial" / series_uid / "DICOM"
    dicom_dir.mkdir(parents=True, exist_ok=True)

    # Create a mock tar.zst archive with one dummy file.
    archive_path = cold_root / patient_id / study_uid / "Axial" / series_uid / "DICOM.tar.zst"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    dummy_file = dicom_dir / "00001.dcm"
    dummy_file.write_bytes(b"DICM" + b"\x00" * 100)

    cctx = zstd.ZstdCompressor()
    with archive_path.open("wb") as fout:
        with cctx.stream_writer(fout) as zout:
            with tarfile.open(fileobj=zout, mode="w|") as tf:
                tf.add(str(dummy_file), arcname="00001.dcm")

    # Remove the extracted file so the series is "cold".
    dummy_file.unlink()
    # Remove the empty dirs so _is_series_dir_warm returns False.
    dicom_dir.rmdir()

    # Insert DB rows.
    import psycopg2

    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lvo_clinical_data (study_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (patient_id,),
            )
            cur.execute(
                "INSERT INTO patient (patient_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (patient_id,),
            )
            cur.execute(
                "INSERT INTO image_study (patient_id, studyinstanceuid, study_type, study_path) "
                "VALUES (%s, %s, 'CTA', %s) ON CONFLICT DO NOTHING",
                (patient_id, study_uid, str(legacy_root / patient_id / study_uid)),
            )
            cur.execute(
                "INSERT INTO image_series "
                "(patient_id, studyinstanceuid, seriesinstanceuid, modality, "
                " dicom_dir_path, dicom_archive_path) "
                "VALUES (%s, %s, %s, 'CT', %s, %s) ON CONFLICT DO NOTHING",
                (patient_id, study_uid, series_uid, str(dicom_dir), str(archive_path)),
            )
        conn.commit()
    finally:
        conn.close()

    yield {
        "tmpdir": tmpdir,
        "legacy_root": legacy_root,
        "cold_root": cold_root,
        "study_uid": study_uid,
        "series_uid": series_uid,
        "dicom_dir": dicom_dir,
        "archive_path": archive_path,
    }

    # Cleanup: remove temp files and DB rows.
    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM cache_state WHERE studyinstanceuid = %s", (study_uid,))
            cur.execute("DELETE FROM image_series WHERE seriesinstanceuid = %s", (series_uid,))
            cur.execute("DELETE FROM image_study WHERE studyinstanceuid = %s", (study_uid,))
            cur.execute("DELETE FROM lvo_clinical_data WHERE study_id = %s", (patient_id,))
            cur.execute("DELETE FROM patient WHERE patient_id = %s", (patient_id,))
        conn.commit()
    finally:
        conn.close()


def test_warm_evict_cycle(cold_env, seeded_db):
    """Exercise the warm → hot → evict → cold state machine."""
    import cache_manager as cm

    study_uid = cold_env["study_uid"]
    dicom_dir = cold_env["dicom_dir"]

    # Patch config paths + storage mode.
    with (
        patch.object(cm, "STORAGE_MODE", "cold_path_cache"),
        patch.object(cm, "LEGACY_DICOM_ROOT", cold_env["legacy_root"]),
        patch.object(cm, "COLD_ARCHIVE_ROOT", cold_env["cold_root"]),
    ):
        # 1. Status should be cold.
        status = cm.get_cache_status(study_uid)
        assert status["status"] == "cold"

        # 2. Warm the study.
        result = cm.warm_study(study_uid)
        assert result["ok"] is True
        assert dicom_dir.is_dir()
        assert any(dicom_dir.iterdir())

        # 3. Status should be hot.
        status = cm.get_cache_status(study_uid)
        assert status["status"] == "hot"

        # 4. Re-warm should short-circuit (already hot).
        result2 = cm.warm_study(study_uid)
        assert result2["ok"] is True
        assert result2.get("already_hot") is True

        # 5. Evict.
        evict_result = cm.evict_study(study_uid)
        assert evict_result["ok"] is True
        assert not dicom_dir.exists()

        # 6. Status should be cold again.
        status = cm.get_cache_status(study_uid)
        assert status["status"] == "cold"


def test_warm_no_archives_returns_error(cold_env, seeded_db):
    """Warming a study with no resolvable archives marks status as error."""
    import cache_manager as cm

    study_uid = cold_env["study_uid"]

    # Delete the archive file so there's nothing to extract.
    cold_env["archive_path"].unlink()

    with (
        patch.object(cm, "STORAGE_MODE", "cold_path_cache"),
        patch.object(cm, "LEGACY_DICOM_ROOT", cold_env["legacy_root"]),
        patch.object(cm, "COLD_ARCHIVE_ROOT", cold_env["cold_root"]),
    ):
        result = cm.warm_study(study_uid)
        assert result["ok"] is False
        assert "no_archives" in result.get("error", "")


def test_warm_endpoint_returns_202_and_eventually_hot(cold_env, logged_in_client):
    """POST /api/studies/{uid}/warm returns 202 quickly; the worker pool
    runs the extraction in the background and `cache_state` flips to 'hot'
    shortly after.
    """
    import time

    import cache_manager as cm

    study_uid = cold_env["study_uid"]
    dicom_dir = cold_env["dicom_dir"]

    with (
        patch.object(cm, "STORAGE_MODE", "cold_path_cache"),
        patch.object(cm, "LEGACY_DICOM_ROOT", cold_env["legacy_root"]),
        patch.object(cm, "COLD_ARCHIVE_ROOT", cold_env["cold_root"]),
    ):
        t0 = time.perf_counter()
        resp = logged_in_client.post(f"/api/studies/{study_uid}/warm")
        elapsed = time.perf_counter() - t0

        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["queued"] is True
        assert body["studyinstanceuid"] == study_uid
        # The POST must return without waiting for the extraction.
        assert elapsed < 2.0, f"POST took {elapsed:.2f}s — extraction was not backgrounded"

        # Poll cache-status until the worker reports 'hot' (or the test
        # gives up after 30s).
        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            status_resp = logged_in_client.get(f"/api/studies/{study_uid}/cache-status")
            assert status_resp.status_code == 200
            status = status_resp.json().get("status")
            if status == "hot":
                break
            if status == "error":
                pytest.fail(f"cache-status reported error: {status_resp.json()}")
            time.sleep(0.2)

        assert status == "hot", f"final status was {status!r}"
        assert dicom_dir.is_dir()
        assert any(dicom_dir.iterdir())
