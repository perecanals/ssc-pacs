"""Tests for the study/series deletion core (web-app/deletion.py) + endpoints.

Covers plan building, the Orthanc+DB removal (annotations discarded to history),
the root-gated file removal, and the path-safety guards.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import psycopg2
import pytest

# orthanc_client (imported transitively by deletion) requires these at import.
os.environ.setdefault("ORTHANC_ADMIN_USER", "test")
os.environ.setdefault("ORTHANC_ADMIN_PASSWORD", "test")

import deletion  # noqa: E402
from labelled_table_sync import sync_labelled_rows  # noqa: E402
from study_metadata import refresh_study_modalities  # noqa: E402
from tests.conftest import USER_NONE, insert_patient, login_as  # noqa: E402

STUDY_UID = "1.2.999.701"
SERIES_UIDS = ["1.2.999.701.1", "1.2.999.701.2"]
PATIENT_ID = "P-DEL"


@pytest.fixture()
def del_study(seeded_db):
    """A patient/study/2-series graph with annotations + cache rows to delete."""
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.audit_user = %s", ("tester",))
            insert_patient(cur, PATIENT_ID, "lvo")
            cur.execute(
                "INSERT INTO image_study (patient_id, studyinstanceuid, study_type) "
                "VALUES (%s, %s, 'CTA') ON CONFLICT DO NOTHING",
                (PATIENT_ID, STUDY_UID),
            )
            for i, suid in enumerate(SERIES_UIDS):
                cur.execute(
                    "INSERT INTO image_series "
                    "(patient_id, studyinstanceuid, seriesinstanceuid, modality, "
                    " seriesnumber, dicom_dir_path, dicom_archive_path) "
                    "VALUES (%s, %s, %s, 'CT', %s, %s, %s) ON CONFLICT DO NOTHING",
                    (PATIENT_ID, STUDY_UID, suid, i,
                     f"/imaging/{PATIENT_ID}/{STUDY_UID}/UNNAMED/{suid}/DICOM",
                     f"/cold/{PATIENT_ID}/{STUDY_UID}/UNNAMED/{suid}/DICOM.tar.zst"),
                )
                cur.execute(
                    "INSERT INTO series_cache_state (seriesinstanceuid, status) "
                    "VALUES (%s, 'cold') ON CONFLICT DO NOTHING",
                    (suid,),
                )
            # One study-level and one series-level annotation.
            refresh_study_modalities(cur, [STUDY_UID])
            sync_labelled_rows(conn, "study", [STUDY_UID])
            cur.execute(
                "INSERT INTO annotations (level, studyinstanceuid, patient_id, label, value, created_by) "
                "VALUES ('study', %s, %s, 'timepoint', 'BL', 'tester')",
                (STUDY_UID, PATIENT_ID),
            )
            cur.execute(
                "INSERT INTO annotations "
                "(level, seriesinstanceuid, studyinstanceuid, patient_id, label, value, created_by) "
                "VALUES ('series', %s, %s, %s, 'series_type', 'NCCT', 'tester')",
                (SERIES_UIDS[0], STUDY_UID, PATIENT_ID),
            )
        conn.commit()
    finally:
        conn.close()

    yield {"study_uid": STUDY_UID, "series_uids": SERIES_UIDS, "patient_id": PATIENT_ID}

    # Idempotent cleanup (rows may already be gone if the test deleted them).
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.audit_user = %s", ("tester",))
            cur.execute("DELETE FROM annotations WHERE studyinstanceuid = %s", (STUDY_UID,))
            cur.execute("DELETE FROM series_cache_state WHERE seriesinstanceuid = ANY(%s)", (SERIES_UIDS,))
            cur.execute("DELETE FROM image_series WHERE studyinstanceuid = %s", (STUDY_UID,))
            cur.execute("DELETE FROM image_study WHERE studyinstanceuid = %s", (STUDY_UID,))
            cur.execute("DELETE FROM image_study_labelled WHERE studyinstanceuid = %s", (STUDY_UID,))
            cur.execute("DELETE FROM patient WHERE patient_id = %s", (PATIENT_ID,))
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Plan building
# --------------------------------------------------------------------------- #
def test_build_study_plan(del_study, seeded_db):
    conn = psycopg2.connect(**seeded_db)
    try:
        with patch.object(deletion, "orthanc_study_id", return_value="oid-xyz"):
            plan = deletion.build_study_deletion_plan(conn, STUDY_UID)
    finally:
        conn.close()
    assert plan is not None
    assert plan["level"] == "study"
    assert plan["n_series"] == 2
    assert plan["n_annotations"] == 2  # one study-level + one series-level
    assert plan["orthanc"]["id"] == "oid-xyz"
    assert len(plan["remove_dirs"]) == 2  # loose + archive study dirs


def test_build_study_plan_missing(seeded_db):
    conn = psycopg2.connect(**seeded_db)
    try:
        assert deletion.build_study_deletion_plan(conn, "does.not.exist") is None
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Orthanc + DB removal
# --------------------------------------------------------------------------- #
def test_delete_index_and_db_execute(del_study, seeded_db):
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute("SET app.audit_user = %s", ("tester",))
        with patch.object(deletion, "orthanc_study_id", return_value="oid-xyz"), \
             patch.object(deletion, "delete_orthanc_study", return_value=True) as m_del:
            plan = deletion.build_study_deletion_plan(conn, STUDY_UID)
            result = deletion.delete_index_and_db(conn, plan, execute=True)

        m_del.assert_called_once_with("oid-xyz")
        assert result["orthanc_deleted"] is True
        assert result["annotations"] == 2
        assert result["image_series"] == 2
        assert result["image_study"] == 1

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM image_study WHERE studyinstanceuid = %s", (STUDY_UID,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM image_series WHERE studyinstanceuid = %s", (STUDY_UID,))
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM annotations WHERE studyinstanceuid = %s", (STUDY_UID,))
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT count(*) FROM series_cache_state WHERE seriesinstanceuid = ANY(%s)",
                (SERIES_UIDS,),
            )
            assert cur.fetchone()[0] == 0
            # The discarded annotations survive as delete rows in history.
            cur.execute(
                "SELECT count(*) FROM annotations_history "
                "WHERE entity_id = %s AND operation = 'D'",
                (STUDY_UID,),
            )
            assert cur.fetchone()[0] >= 1
            # A different seeded study is untouched.
            cur.execute("SELECT count(*) FROM image_study WHERE studyinstanceuid = '1.2.3.4.5'")
            assert cur.fetchone()[0] == 1
    finally:
        conn.close()


def test_delete_index_and_db_dry_run(del_study, seeded_db):
    conn = psycopg2.connect(**seeded_db)
    try:
        with patch.object(deletion, "orthanc_study_id", return_value=None), \
             patch.object(deletion, "delete_orthanc_study") as m_del:
            plan = deletion.build_study_deletion_plan(conn, STUDY_UID)
            result = deletion.delete_index_and_db(conn, plan, execute=False)
        m_del.assert_not_called()
        assert result["annotations"] == 2
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM image_study WHERE studyinstanceuid = %s", (STUDY_UID,))
            assert cur.fetchone()[0] == 1  # still there
    finally:
        conn.close()


@pytest.mark.parametrize("distinct", [False, True])
def test_series_deletion_refreshes_parent_and_mirror(del_study, seeded_db, distinct):
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            if distinct:
                cur.execute("UPDATE image_series SET modality = 'SR' WHERE seriesinstanceuid = %s",
                            (SERIES_UIDS[0],))
            refresh_study_modalities(cur, [STUDY_UID])
            sync_labelled_rows(conn, "study", [STUDY_UID])
        conn.commit()
        for index, uid in enumerate(SERIES_UIDS):
            with patch.object(deletion, "orthanc_series_id", return_value=None):
                plan = deletion.build_series_deletion_plan(conn, uid)
                deletion.delete_index_and_db(conn, plan, execute=True)
            with conn.cursor() as cur:
                for table in ("image_study", "image_study_labelled"):
                    cur.execute(f"SELECT modalities FROM {table} WHERE studyinstanceuid = %s",
                                (STUDY_UID,))
                    assert cur.fetchone()[0] == (["CT"] if index == 0 else None)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Path safety + file removal
# --------------------------------------------------------------------------- #
def test_path_safety_guards():
    root = Path("/media/x/imaging")
    # Too shallow — would delete the root, a dataset or a whole patient.
    for shallow in (root, root / "P-1", root / "lvo", root / "lvo" / "P-1",
                    root / "lvo" / "P-1" / "NOT_A_UID"):
        with pytest.raises(ValueError):
            deletion._assert_within_root(shallow, root)
    # Outside the root.
    with pytest.raises(ValueError):
        deletion._assert_within_root(Path("/etc/passwd"), root)
    # Valid: a study dir in either layout, or below one.
    for ok in (root / "lvo" / "P-1" / "1.2.3", root / "P-1" / "1.2.3",
               root / "lvo" / "P-1" / "1.2.3" / "CTA" / "1.2.3.4"):
        assert deletion._assert_within_root(ok, root) == ok


def test_remove_files_execute():
    """No privilege escalation — removal works as the ordinary service user."""
    tmp = Path(tempfile.mkdtemp(prefix="del_files_"))
    try:
        dicom_root = tmp / "imaging"
        cold_root = tmp / "cold"
        study_loose = dicom_root / PATIENT_ID / STUDY_UID
        study_cold = cold_root / PATIENT_ID / STUDY_UID
        (study_loose / "UNNAMED").mkdir(parents=True)
        (study_cold / "UNNAMED").mkdir(parents=True)
        plan = {
            "remove_dirs": [str(study_loose), str(study_cold)],
            "prune_parents": [],
        }
        with patch.object(deletion, "DICOM_DATA_ROOT", dicom_root), \
             patch.object(deletion, "COLD_ARCHIVE_ROOT", cold_root):
            res = deletion.remove_files(plan, execute=True)
        assert not study_loose.exists()
        assert not study_cold.exists()
        assert len(res["removed"]) == 2
        # Patient dirs preserved (never pruned by a study delete).
        assert (dicom_root / PATIENT_ID).exists()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Orthanc Folder-Indexer purge (indexer-plugin.db Files rows)
# --------------------------------------------------------------------------- #
def test_purge_indexer_skips_nonempty_and_unindexed():
    """A dir still holding files (would resurrect) and archive-root dirs are skipped."""
    tmp = Path(tempfile.mkdtemp(prefix="del_idx_"))
    try:
        dicom_root = tmp / "imaging"
        cold_root = tmp / "cold"
        gone = dicom_root / "P" / "S-gone"          # under indexed root, absent → purge
        still = dicom_root / "P" / "S-still"        # under indexed root, has a file → skip
        arch = cold_root / "P" / "S-arch"           # archive root → never indexed
        (still).mkdir(parents=True)
        (still / "0.dcm").write_bytes(b"x")
        with patch.object(deletion, "DICOM_DATA_ROOT", dicom_root):
            res = deletion.purge_indexer_rows(
                [str(gone), str(still), str(arch)], execute=False,
            )
        assert res["purged"] == [f"{deletion.INDEXER_CONTAINER_ROOT}/P/S-gone"]
        assert str(still) in res["skipped_nonempty"]
        assert str(arch) in res["skipped_unindexed"]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_purge_indexer_posts_force_scan():
    """Executing posts a Force scan for the empty indexed dir and polls to idle."""
    tmp = Path(tempfile.mkdtemp(prefix="del_idx2_"))
    try:
        dicom_root = tmp / "imaging"
        gone = dicom_root / "P" / "S-gone"  # absent → eligible
        session = _FakeSession()
        with patch.object(deletion, "DICOM_DATA_ROOT", dicom_root), \
             patch.object(deletion.requests, "Session", return_value=session):
            res = deletion.purge_indexer_rows([str(gone)], execute=True, poll_s=0)
        assert res["purged"] == [f"{deletion.INDEXER_CONTAINER_ROOT}/P/S-gone"]
        # A Force scan was posted for exactly that folder.
        assert session.posted == [{
            "Folders": [f"{deletion.INDEXER_CONTAINER_ROOT}/P/S-gone"],
            "Force": True,
        }]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


class _FakeResp:
    def __init__(self, payload=None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


class _FakeSession:
    """Minimal requests.Session stand-in: records POSTs, reports idle on GET."""
    def __init__(self):
        self.auth = None
        self.posted = []

    def post(self, url, json=None, timeout=None):
        self.posted.append(json)
        return _FakeResp({"status": "started"})

    def get(self, url, timeout=None):
        return _FakeResp({"busy": False})


# --------------------------------------------------------------------------- #
# Endpoints (admin gating)
# --------------------------------------------------------------------------- #
def test_delete_study_endpoint_requires_admin(client, del_study):
    login_as(client, USER_NONE)
    resp = client.delete(f"/api/admin/studies/{STUDY_UID}")
    assert resp.status_code == 403


def test_delete_study_endpoint_404(logged_in_client):
    resp = logged_in_client.delete("/api/admin/studies/does.not.exist")
    assert resp.status_code == 404


def test_delete_study_endpoint_ok(logged_in_client, del_study):
    resp = logged_in_client.get(f"/api/admin/studies/{STUDY_UID}/deletion-plan")
    assert resp.status_code == 200
    assert resp.json()["n_series"] == 2

    # Stub the disk + indexer layers so the endpoint test stays hermetic (the
    # fixture's paths aren't real, and the indexer purge would hit live Orthanc).
    import routes.data_admin as data_admin
    with patch.object(data_admin, "remove_files",
                      return_value={"removed": ["d1", "d2"], "missing": [], "pruned": []}) as m_rm, \
         patch.object(data_admin, "purge_indexer_rows",
                      return_value={"purged": ["/dicom-data/x"], "skipped_nonempty": [],
                                    "skipped_unindexed": [], "error": None}) as m_idx:
        resp = logged_in_client.delete(f"/api/admin/studies/{STUDY_UID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["image_study"] == 1
    assert body["files_removed"] == ["d1", "d2"]
    assert body["indexer_purged"] == ["/dicom-data/x"]
    assert body["indexer_error"] is None
    m_rm.assert_called_once()
    m_idx.assert_called_once()


# --------------------------------------------------------------------------- #
# Dataset layout (<root>/<slug>/<patient>/<study>) — plans use stored paths
# --------------------------------------------------------------------------- #
def _set_study_path(seeded_db, path):
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE image_study SET study_path = %s WHERE studyinstanceuid = %s",
                        (path, STUDY_UID))
        conn.commit()
    finally:
        conn.close()


def test_study_plan_uses_the_stored_path(del_study, seeded_db, tmp_path):
    loose, cold = tmp_path / "imaging", tmp_path / "cold"
    # Stored under crisp2 although owned by lvo (moved after ingestion): the
    # plan removes where the files are, not where the layout would put them.
    _set_study_path(seeded_db, str(loose / "crisp2" / PATIENT_ID / STUDY_UID))
    conn = psycopg2.connect(**seeded_db)
    try:
        with patch.object(deletion, "DICOM_DATA_ROOT", loose), \
             patch.object(deletion, "COLD_ARCHIVE_ROOT", cold), \
             patch.object(deletion, "orthanc_study_id", return_value=None):
            plan = deletion.build_study_deletion_plan(conn, STUDY_UID)
            _set_study_path(seeded_db, "")
            fallback = deletion.build_study_deletion_plan(conn, STUDY_UID)
    finally:
        conn.close()
    assert plan["remove_dirs"] == [str(loose / "crisp2" / PATIENT_ID / STUDY_UID),
                                   str(cold / "crisp2" / PATIENT_ID / STUDY_UID)]
    # No stored path: the owner's slug.
    assert fallback["remove_dirs"][0] == str(loose / "lvo" / PATIENT_ID / STUDY_UID)


def test_series_plan_prunes_up_to_the_patient_dir(del_study, seeded_db):
    conn = psycopg2.connect(**seeded_db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE image_series SET dicom_dir_path = %s, dicom_archive_path = %s "
                "WHERE seriesinstanceuid = %s",
                (f"/imaging/lvo/{PATIENT_ID}/{STUDY_UID}/CTA/{SERIES_UIDS[0]}/DICOM",
                 f"/cold/lvo/{PATIENT_ID}/{STUDY_UID}/CTA/{SERIES_UIDS[0]}/DICOM.tar.zst",
                 SERIES_UIDS[0]),
            )
        conn.commit()
        with patch.object(deletion, "orthanc_series_id", return_value=None):
            plan = deletion.build_series_deletion_plan(conn, SERIES_UIDS[0])
    finally:
        conn.close()
    assert plan["prune_parents"] == [
        (f"/imaging/lvo/{PATIENT_ID}/{STUDY_UID}/CTA", f"/imaging/lvo/{PATIENT_ID}"),
        (f"/cold/lvo/{PATIENT_ID}/{STUDY_UID}/CTA", f"/cold/lvo/{PATIENT_ID}"),
    ]


def test_orphan_scan_reads_dataset_and_legacy_dirs(del_study, seeded_db, tmp_path):
    loose, cold = tmp_path / "imaging", tmp_path / "cold"
    known = loose / "lvo" / PATIENT_ID / STUDY_UID
    orphan_new = loose / "lvo" / PATIENT_ID / "1.2.999.702"
    orphan_legacy = loose / "P-OLD" / "1.2.999.703"
    for d in (known, orphan_new, orphan_legacy, loose / "crisp2" / "P-0001"):
        d.mkdir(parents=True)
    conn = psycopg2.connect(**seeded_db)
    try:
        with patch.object(deletion, "DICOM_DATA_ROOT", loose), \
             patch.object(deletion, "COLD_ARCHIVE_ROOT", cold):
            orphans = deletion.find_orphan_study_dirs(conn)
    finally:
        conn.close()
    # A dataset dir is never read as a patient (P-0001 is not an orphan study).
    assert orphans == sorted([str(orphan_legacy), str(orphan_new)])
