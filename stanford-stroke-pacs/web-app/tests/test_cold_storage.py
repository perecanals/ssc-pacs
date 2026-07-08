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
      - A temporary dir as the "dicom data root"
      - A tar.zst archive containing a single dummy DICOM file
      - An image_series row pointing at both paths
      - An image_study row for the study
    """
    tmpdir = tempfile.mkdtemp(prefix="cold_test_")
    dicom_root = Path(tmpdir) / "imaging"
    cold_root = Path(tmpdir) / "cold"
    dicom_root.mkdir()
    cold_root.mkdir()

    study_uid = "1.2.999.test.cold"
    series_uid = "1.2.999.test.cold.1"
    patient_id = "P-COLD"

    # The dicom_dir_path where files would be extracted.
    dicom_dir = dicom_root / patient_id / study_uid / "Axial" / series_uid / "DICOM"
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
                (patient_id, study_uid, str(dicom_root / patient_id / study_uid)),
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
        "dicom_root": dicom_root,
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
            cur.execute(
                "DELETE FROM series_cache_state WHERE seriesinstanceuid = %s", (series_uid,)
            )
            cur.execute("DELETE FROM image_series WHERE seriesinstanceuid = %s", (series_uid,))
            cur.execute("DELETE FROM image_study WHERE studyinstanceuid = %s", (study_uid,))
            cur.execute("DELETE FROM lvo_clinical_data WHERE study_id = %s", (patient_id,))
            cur.execute("DELETE FROM patient WHERE patient_id = %s", (patient_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def cold_env_multi(db_conn, seeded_db):
    """A two-series study, both cold, with real tar.zst archives.

    Used to prove the behaviour-preservation contract: warming one series leaves
    the other cold and the *study* non-Ready (binary readiness); warming the whole
    study (the wrapper) decompresses both and the study reads hot.
    """
    tmpdir = tempfile.mkdtemp(prefix="cold_multi_")
    dicom_root = Path(tmpdir) / "imaging"
    cold_root = Path(tmpdir) / "cold"
    dicom_root.mkdir()
    cold_root.mkdir()

    study_uid = "1.2.999.test.multi"
    patient_id = "P-MULTI"
    series = []
    for n in (1, 2):
        suid = f"1.2.999.test.multi.{n}"
        dicom_dir = dicom_root / patient_id / study_uid / f"S{n}" / suid / "DICOM"
        dicom_dir.mkdir(parents=True, exist_ok=True)
        archive_path = cold_root / patient_id / study_uid / f"S{n}" / suid / "DICOM.tar.zst"
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        dummy = dicom_dir / "00001.dcm"
        dummy.write_bytes(b"DICM" + b"\x00" * 100)
        cctx = zstd.ZstdCompressor()
        with archive_path.open("wb") as fout:
            with cctx.stream_writer(fout) as zout:
                with tarfile.open(fileobj=zout, mode="w|") as tf:
                    tf.add(str(dummy), arcname="00001.dcm")
        dummy.unlink()
        dicom_dir.rmdir()
        series.append({"uid": suid, "dicom_dir": dicom_dir, "archive": archive_path})

    import psycopg2

    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO patient (patient_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (patient_id,),
            )
            cur.execute(
                "INSERT INTO image_study (patient_id, studyinstanceuid, study_type, study_path) "
                "VALUES (%s, %s, 'CTA', %s) ON CONFLICT DO NOTHING",
                (patient_id, study_uid, str(dicom_root / patient_id / study_uid)),
            )
            for s in series:
                cur.execute(
                    "INSERT INTO image_series "
                    "(patient_id, studyinstanceuid, seriesinstanceuid, modality, "
                    " dicom_dir_path, dicom_archive_path) "
                    "VALUES (%s, %s, %s, 'CT', %s, %s) ON CONFLICT DO NOTHING",
                    (patient_id, study_uid, s["uid"], str(s["dicom_dir"]), str(s["archive"])),
                )
        conn.commit()
    finally:
        conn.close()

    yield {"dicom_root": dicom_root, "cold_root": cold_root,
           "study_uid": study_uid, "patient_id": patient_id, "series": series}

    import shutil

    shutil.rmtree(tmpdir, ignore_errors=True)
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            for s in series:
                cur.execute(
                    "DELETE FROM series_cache_state WHERE seriesinstanceuid = %s", (s["uid"],)
                )
                cur.execute("DELETE FROM image_series WHERE seriesinstanceuid = %s", (s["uid"],))
            cur.execute("DELETE FROM image_study WHERE studyinstanceuid = %s", (study_uid,))
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
        patch.object(cm, "DICOM_DATA_ROOT", cold_env["dicom_root"]),
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
        patch.object(cm, "DICOM_DATA_ROOT", cold_env["dicom_root"]),
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
        patch.object(cm, "DICOM_DATA_ROOT", cold_env["dicom_root"]),
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


# ---------------------------------------------------------------------------
# Series-level (single source of truth) + study aggregate behaviour
# ---------------------------------------------------------------------------


def test_warm_evict_series_cycle(cold_env, seeded_db):
    """Warm/evict a single series directly; the study aggregate tracks it."""
    import cache_manager as cm

    study_uid = cold_env["study_uid"]
    series_uid = cold_env["series_uid"]
    dicom_dir = cold_env["dicom_dir"]

    with (
        patch.object(cm, "STORAGE_MODE", "cold_path_cache"),
        patch.object(cm, "DICOM_DATA_ROOT", cold_env["dicom_root"]),
        patch.object(cm, "COLD_ARCHIVE_ROOT", cold_env["cold_root"]),
    ):
        assert cm.get_series_cache_status(series_uid)["status"] == "cold"

        result = cm.warm_series([series_uid])
        assert result["ok"] is True
        assert result["series_count"] == 1
        assert dicom_dir.is_dir() and any(dicom_dir.iterdir())

        assert cm.get_series_cache_status(series_uid)["status"] == "hot"
        # Study aggregate over its (single) series is hot too.
        assert cm.get_cache_status(study_uid)["status"] == "hot"

        # Re-warm short-circuits.
        again = cm.warm_series([series_uid])
        assert again.get("already_hot") is True

        cm.evict_series([series_uid])
        assert not dicom_dir.exists()
        assert cm.get_series_cache_status(series_uid)["status"] == "cold"
        assert cm.get_cache_status(study_uid)["status"] == "cold"


def test_single_series_warm_leaves_siblings_cold_and_study_not_ready(cold_env_multi, seeded_db):
    """Binary readiness + per-series independence (the core new guarantee).

    Warming one series of a two-series study makes only that series hot; its
    sibling stays cold and the *study* is not Ready. Warming the whole study (the
    wrapper) then decompresses both and the study reads hot — study behaviour
    preserved.
    """
    import cache_manager as cm

    study_uid = cold_env_multi["study_uid"]
    s1, s2 = cold_env_multi["series"]

    with (
        patch.object(cm, "STORAGE_MODE", "cold_path_cache"),
        patch.object(cm, "DICOM_DATA_ROOT", cold_env_multi["dicom_root"]),
        patch.object(cm, "COLD_ARCHIVE_ROOT", cold_env_multi["cold_root"]),
    ):
        # Warm only series 1.
        cm.warm_series([s1["uid"]])
        assert s1["dicom_dir"].is_dir() and any(s1["dicom_dir"].iterdir())
        assert not s2["dicom_dir"].exists()  # sibling untouched
        assert cm.get_series_cache_status(s1["uid"])["status"] == "hot"
        assert cm.get_series_cache_status(s2["uid"])["status"] == "cold"
        # Study is NOT hot until all series are hot (binary readiness).
        assert cm.get_cache_status(study_uid)["status"] != "hot"
        assert cm.get_batch_cache_status([study_uid])[study_uid] != "hot"

        # Whole-study warm (wrapper) decompresses every series.
        result = cm.warm_study(study_uid)
        assert result["ok"] is True
        assert s1["dicom_dir"].is_dir() and s2["dicom_dir"].is_dir()
        assert cm.get_cache_status(study_uid)["status"] == "hot"
        assert cm.get_batch_cache_status([study_uid])[study_uid] == "hot"


def test_patient_aggregate_counts_studies(cold_env_multi, seeded_db):
    """Patient summary counts studies (not series), aggregated over series state."""
    import cache_manager as cm

    study_uid = cold_env_multi["study_uid"]
    patient_id = cold_env_multi["patient_id"]
    s1 = cold_env_multi["series"][0]

    with (
        patch.object(cm, "STORAGE_MODE", "cold_path_cache"),
        patch.object(cm, "DICOM_DATA_ROOT", cold_env_multi["dicom_root"]),
        patch.object(cm, "COLD_ARCHIVE_ROOT", cold_env_multi["cold_root"]),
    ):
        summary = cm.get_patient_cache_status(patient_id)
        assert summary["total"] == 1  # one study
        assert summary["cold"] == 1

        # Partially warming the study (one series) keeps the study non-hot, so the
        # patient still counts it as a single non-hot study.
        cm.warm_series([s1["uid"]])
        summary = cm.get_patient_cache_status(patient_id)
        assert summary["total"] == 1
        assert summary["hot"] == 0

        cm.warm_study(study_uid)
        summary = cm.get_patient_cache_status(patient_id)
        assert summary["hot"] == 1


def test_series_warm_endpoint_returns_202_and_eventually_hot(cold_env, logged_in_client):
    """POST /api/series/{uid}/warm returns 202; the worker flips it to hot."""
    import time

    import cache_manager as cm

    series_uid = cold_env["series_uid"]
    dicom_dir = cold_env["dicom_dir"]

    with (
        patch.object(cm, "STORAGE_MODE", "cold_path_cache"),
        patch.object(cm, "DICOM_DATA_ROOT", cold_env["dicom_root"]),
        patch.object(cm, "COLD_ARCHIVE_ROOT", cold_env["cold_root"]),
    ):
        resp = logged_in_client.post(f"/api/series/{series_uid}/warm")
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["ok"] is True and body["queued"] is True
        assert body["seriesinstanceuid"] == series_uid

        deadline = time.monotonic() + 30
        status = None
        while time.monotonic() < deadline:
            r = logged_in_client.get(f"/api/series/{series_uid}/cache-status")
            assert r.status_code == 200
            status = r.json().get("status")
            if status == "hot":
                break
            if status == "error":
                pytest.fail(f"series cache-status reported error: {r.json()}")
            time.sleep(0.2)

        assert status == "hot", f"final status was {status!r}"
        assert dicom_dir.is_dir() and any(dicom_dir.iterdir())


# ---------------------------------------------------------------------------
# ohif-link: membership 404 ordering + stale-hot repair (single-connection flow)
# ---------------------------------------------------------------------------


def test_ohif_link_membership_404_before_orthanc_lookup(cold_env, logged_in_client):
    """A series not in the study 404s without ever calling Orthanc."""
    import routes.studies as studies_mod

    def _must_not_be_called(_uid):
        raise AssertionError("orthanc_lookup must not run for a bad series")

    with (
        patch.object(studies_mod, "STORAGE_MODE", "legacy"),
        patch.object(studies_mod, "orthanc_lookup", _must_not_be_called),
    ):
        resp = logged_in_client.get(
            f"/api/ohif-link/{cold_env['study_uid']}",
            params={"seriesinstanceuid": "9.9.9.not.in.study"},
        )
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Series not found in study"


def test_ohif_link_stale_hot_row_repaired_to_cold(cold_env, logged_in_client, seeded_db):
    """A 'hot' cache row with no files on disk is deleted and reported cold."""
    import psycopg2

    import routes.studies as studies_mod

    series_uid = cold_env["series_uid"]
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO series_cache_state "
                "(seriesinstanceuid, status, warmed_at, last_accessed_at, cache_path) "
                "VALUES (%s, 'hot', now(), now(), %s)",
                (series_uid, str(cold_env["dicom_dir"])),
            )
        conn.commit()
    finally:
        conn.close()

    with patch.object(studies_mod, "STORAGE_MODE", "cold_path_cache"):
        resp = logged_in_client.get(
            f"/api/ohif-link/{cold_env['study_uid']}",
            params={"seriesinstanceuid": series_uid},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "cold"
    assert body["url"] is None
    assert "stale" in body["detail"].lower()

    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM series_cache_state WHERE seriesinstanceuid = %s",
                (series_uid,),
            )
            assert cur.fetchone() is None, "stale hot row must be deleted"
    finally:
        conn.close()


def test_ohif_link_hot_series_returns_ready_url(cold_env, logged_in_client):
    """A genuinely hot series resolves to a ready OHIF URL."""
    import cache_manager as cm
    import routes.studies as studies_mod

    series_uid = cold_env["series_uid"]
    with (
        patch.object(cm, "STORAGE_MODE", "cold_path_cache"),
        patch.object(cm, "DICOM_DATA_ROOT", cold_env["dicom_root"]),
        patch.object(cm, "COLD_ARCHIVE_ROOT", cold_env["cold_root"]),
        patch.object(studies_mod, "STORAGE_MODE", "cold_path_cache"),
        patch.object(studies_mod, "orthanc_lookup", lambda _uid: [{"Type": "Study"}]),
    ):
        assert cm.warm_series([series_uid])["ok"] is True
        resp = logged_in_client.get(
            f"/api/ohif-link/{cold_env['study_uid']}",
            params={"seriesinstanceuid": series_uid},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert series_uid in body["url"]
