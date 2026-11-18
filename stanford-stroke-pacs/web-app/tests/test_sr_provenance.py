"""SR ownership is server-attributed, complete, and tied to the account lifetime."""
from datetime import timedelta
from unittest.mock import MagicMock

import psycopg2
import pytest
from fastapi import HTTPException

import sr_provenance as provenance
from tests.conftest import TEST_USER, USER_CRISP, USER_NONE, login_as

STUDY = "1.2.3.4.5"
SERIES = "9.30.1"
SOP = "9.30.1.1"
ITEM = {"study": STUDY, "series": SERIES, "sop": SOP, "sr": True}


@pytest.fixture()
def report(db_conn, seeded_db, monkeypatch):
    monkeypatch.setattr(provenance, "get_conn", lambda: psycopg2.connect(**seeded_db))
    with db_conn.cursor() as cur:
        cur.execute("SELECT created_at FROM users WHERE username=%s", (USER_CRISP,))
        created = cur.fetchone()[0]
        cur.execute("UPDATE users SET is_staff=true WHERE username=%s", (USER_CRISP,))
        cur.execute("""INSERT INTO image_series
            (seriesinstanceuid, studyinstanceuid, patient_id, modality, storage_source)
            VALUES (%s,%s,'P-0001','SR','orthanc')""", (SERIES, STUDY))
    db_conn.commit()
    monkeypatch.setattr(provenance, "series_instances", lambda *a: [SOP])
    monkeypatch.setattr(provenance, "orthanc_json", lambda *a, **kw: [])
    yield created
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM sr_provenance WHERE series_uid=%s", (SERIES,))
        cur.execute("DELETE FROM image_series_labelled WHERE seriesinstanceuid=%s", (SERIES,))
        cur.execute("DELETE FROM image_series WHERE seriesinstanceuid=%s", (SERIES,))
        cur.execute("UPDATE users SET is_staff=false WHERE username=%s", (USER_CRISP,))
    db_conn.commit()


def record(conn, created, *, confirmed=True, username=USER_CRISP, sop=SOP):
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO sr_provenance
            (sop_uid, study_uid, series_uid, saved_by, account_created_at, confirmed)
            VALUES (%s,%s,%s,%s,%s,%s)""", (sop, STUDY, SERIES, username, created, confirmed))
    conn.commit()


@pytest.mark.parametrize("case,status", [
    ("own", 200), ("unknown", 403), ("pending", 403), ("other", 403),
    ("mixed", 403), ("extra", 403), ("recreated", 403),
    ("revoked", 404), ("imaging", 403), ("not_staff", 403),
])
def test_staff_preview_and_delete_require_complete_ownership(
    client, db_conn, report, monkeypatch, case, status,
):
    from routes import data_admin

    if case != "unknown":
        record(db_conn, report + timedelta(seconds=1) if case == "recreated" else report,
               confirmed=case != "pending", username=TEST_USER if case == "other" else USER_CRISP)
    if case in ("mixed", "extra"):
        monkeypatch.setattr(provenance, "series_instances", lambda *a: [SOP, "9.30.1.2"])
        if case == "mixed":
            record(db_conn, report, username=TEST_USER, sop="9.30.1.2")
    with db_conn.cursor() as cur:
        if case == "revoked":
            cur.execute("UPDATE users SET allowed_datasets='{}' WHERE username=%s", (USER_CRISP,))
        if case == "not_staff":
            cur.execute("UPDATE users SET is_staff=false WHERE username=%s", (USER_CRISP,))
        if case == "imaging":
            cur.execute("UPDATE image_series SET storage_source='filesystem' WHERE seriesinstanceuid=%s", (SERIES,))
    db_conn.commit()
    plan = MagicMock(return_value={"preview": True})
    delete = MagicMock(return_value={"deleted": True})
    monkeypatch.setattr(data_admin, "build_series_deletion_plan", plan)
    monkeypatch.setattr(data_admin, "_plan_summary", lambda p: p)
    monkeypatch.setattr(data_admin, "_do_full_delete", delete)
    login_as(client, USER_CRISP)
    try:
        assert client.get(f"/api/admin/series/{SERIES}/deletion-plan").status_code == status
        assert client.delete(f"/api/admin/series/{SERIES}").status_code == status
        assert delete.called == (status == 200)
        assert client.delete(f"/api/admin/studies/{STUDY}").status_code == 403
    finally:
        with db_conn.cursor() as cur:
            cur.execute("UPDATE users SET allowed_datasets=ARRAY['crisp2'] WHERE username=%s", (USER_CRISP,))
        db_conn.commit()


def test_journal_precedes_save_and_only_success_confirms(db_conn, report, monkeypatch):
    monkeypatch.setattr(provenance, "series_instances", lambda *a: [])
    lock = provenance.begin_upload([ITEM], USER_CRISP)
    try:
        with db_conn.cursor() as cur:
            cur.execute("SELECT saved_by, confirmed FROM sr_provenance WHERE sop_uid=%s", (SOP,))
            assert cur.fetchone() == (USER_CRISP, False)
        provenance.confirm_upload([ITEM], USER_CRISP, set())
        with db_conn.cursor() as cur:
            cur.execute("SELECT confirmed FROM sr_provenance WHERE sop_uid=%s", (SOP,))
            assert cur.fetchone() == (False,)
        provenance.confirm_upload([ITEM], USER_CRISP, {SOP})
    finally:
        lock.close()
    monkeypatch.setattr(provenance, "series_instances", lambda *a: [SOP])
    provenance.authorize_series_delete(db_conn, SERIES, USER_CRISP, lock=True)
    db_conn.rollback()
    with db_conn.cursor() as cur:
        cur.execute("SELECT saved_at FROM sr_provenance WHERE sop_uid=%s", (SOP,))
        original = cur.fetchone()[0]
    retry = provenance.begin_upload([ITEM], USER_CRISP)
    retry.close()
    provenance.confirm_upload([ITEM], USER_CRISP, {SOP})
    with db_conn.cursor() as cur:
        cur.execute("SELECT saved_at FROM sr_provenance WHERE sop_uid=%s", (SOP,))
        assert cur.fetchone()[0] == original


@pytest.mark.parametrize("case", ["unknown", "pending", "other", "collision"])
def test_upload_cannot_claim_existing_or_uncertain_reports(db_conn, report, monkeypatch, case):
    if case == "pending":
        record(db_conn, report, confirmed=False)
    elif case == "other":
        record(db_conn, report, username=TEST_USER)
    elif case == "collision":
        monkeypatch.setattr(provenance, "series_instances", lambda *a: [])
        monkeypatch.setattr(provenance, "orthanc_json", lambda *a, **kw: [{"Type": "Instance"}])
    with pytest.raises(HTTPException) as error:
        provenance.begin_upload([ITEM], USER_CRISP)
    assert error.value.status_code == 403


def test_unknown_extra_instance_blocks_even_with_owned_summary(db_conn, report, monkeypatch):
    record(db_conn, report)
    with db_conn.cursor() as cur:
        cur.execute("UPDATE image_series SET sr_saved_by=%s WHERE seriesinstanceuid=%s", (USER_CRISP, SERIES))
    db_conn.commit()
    monkeypatch.setattr(provenance, "series_instances", lambda *a: [SOP, "unknown"])
    with pytest.raises(HTTPException):
        provenance.authorize_series_delete(db_conn, SERIES, USER_CRISP)


def test_orthanc_failure_blocks_staff_but_admin_retains_access(db_conn, report, monkeypatch):
    def offline(*a):
        raise RuntimeError("offline")
    monkeypatch.setattr(provenance, "series_instances", offline)
    with pytest.raises(RuntimeError):
        provenance.authorize_series_delete(db_conn, SERIES, USER_CRISP)
    provenance.authorize_series_delete(db_conn, SERIES, TEST_USER)
    with pytest.raises(HTTPException):
        provenance.authorize_series_delete(db_conn, SERIES, USER_NONE)


def test_save_holds_parent_lock_until_forwarding_finishes(db_conn, report, monkeypatch):
    from study_metadata import lock_study_rows

    monkeypatch.setattr(provenance, "series_instances", lambda *a: [])
    lock = provenance.begin_upload([ITEM], USER_CRISP)
    try:
        with db_conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout='100ms'")
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                lock_study_rows(cur, [STUDY])
        db_conn.rollback()
    finally:
        lock.close()
    with db_conn.cursor() as cur:
        lock_study_rows(cur, [STUDY])
    db_conn.rollback()


@pytest.mark.parametrize("username", [USER_CRISP, TEST_USER])
def test_stow_attribution_uses_session_and_successful_sops(
    client, db_conn, report, monkeypatch, username,
):
    import asyncio
    from io import BytesIO

    import httpx
    import pydicom

    import report_catalog
    from routes import proxy
    from tests.test_annotation_upload import CONTENT_TYPE, dicom, multipart

    monkeypatch.setattr(provenance, "series_instances", lambda *a: [])
    monkeypatch.setattr(report_catalog, "catalog_stow_response", lambda body: None)
    parts = [dicom(SeriesInstanceUID=SERIES), dicom(SeriesInstanceUID=SERIES)]
    sops = [str(pydicom.dcmread(BytesIO(part)).SOPInstanceUID) for part in parts]

    async def handler(request):
        assert await request.aread() == multipart(*parts)
        return httpx.Response(200, json={
            "00081199": {"Value": [{"00081155": {"Value": [sops[0]]}}]},
            "00081198": {"Value": [{"00081155": {"Value": [sops[1]]}}]},
        })

    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(proxy, "_CLIENT", stub)
    login_as(client, username)
    try:
        assert client.post("/dicom-web/studies", content=multipart(*parts),
                           headers={"Content-Type": CONTENT_TYPE}).status_code == 200
        with db_conn.cursor() as cur:
            cur.execute("SELECT sop_uid, saved_by, confirmed FROM sr_provenance WHERE series_uid=%s", (SERIES,))
            assert set(cur.fetchall()) == {(sops[0], username, True), (sops[1], username, False)}
    finally:
        asyncio.run(stub.aclose())


def test_seg_cannot_replace_an_owned_sr(db_conn, report):
    record(db_conn, report)
    with pytest.raises(HTTPException) as error:
        provenance.begin_upload([{**ITEM, "sr": False}], USER_CRISP)
    assert error.value.status_code == 409


def test_browsing_exposes_confirmed_owner_and_conservative_capability(client, db_conn, report, monkeypatch):
    from labelled_table_sync import sync_labelled_rows

    record(db_conn, report)
    provenance.refresh_series_summary(db_conn, SERIES)
    sync_labelled_rows(db_conn, "series", [SERIES])
    db_conn.commit()
    with db_conn.cursor() as cur:
        cur.execute("SELECT sr_saved_by, sr_saved_at FROM image_series_labelled WHERE seriesinstanceuid=%s", (SERIES,))
        saved_by, saved_at = cur.fetchone()
        assert saved_by == USER_CRISP and saved_at is not None
    login_as(client, USER_CRISP)
    for endpoint in (f"/api/series?seriesinstanceuid={SERIES}", f"/api/studies/{STUDY}/series"):
        response = client.get(endpoint)
        assert response.status_code == 200
        rows = response.json()
        rows = rows["series"] if isinstance(rows, dict) else rows
        row = next(r for r in rows if r["seriesinstanceuid"] == SERIES)
        assert row["sr_saved_by"] == USER_CRISP
        assert row["sr_ownership"] == "confirmed"
        assert row["can_delete"] is True
    monkeypatch.setattr(provenance, "series_instances", lambda *a: [SOP, "unknown"])
    row = client.get(f"/api/series?seriesinstanceuid={SERIES}").json()["series"][0]
    assert row["can_delete"] is False
    assert row["sr_ownership"] == "unknown"
    assert row["sr_saved_by"] is None


def test_staff_deletes_owned_report_preserving_study_and_imaging(client, db_conn, report, monkeypatch, caplog):
    import deletion
    import report_catalog

    record(db_conn, report)
    resource = {"MainDicomTags": {"SeriesInstanceUID": SERIES, "Modality": "SR"},
                "ParentStudy": "study", "Instances": ["instance"]}

    def orthanc(method, path, **kwargs):
        return {"/tools/lookup": [{"Type": "Series", "ID": "report"}],
                "/series/report": resource,
                "/studies/study": {"MainDicomTags": {"StudyInstanceUID": STUDY}}}[path]

    monkeypatch.setattr(report_catalog, "orthanc_json", orthanc)
    removed = MagicMock(return_value=True)
    monkeypatch.setattr(deletion, "delete_orthanc_series", removed)
    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM image_series WHERE studyinstanceuid=%s AND storage_source='filesystem'", (STUDY,))
        siblings = cur.fetchone()[0]
    login_as(client, USER_CRISP)
    with caplog.at_level("INFO", logger="routes.data_admin"):
        response = client.delete(f"/api/admin/series/{SERIES}")
    assert any(getattr(r, "user", None) == USER_CRISP and getattr(r, "entity", None) == SERIES
               for r in caplog.records)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    assert response.json()["files_removed"] == []
    removed.assert_called_once_with("report")
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM image_series WHERE seriesinstanceuid=%s", (SERIES,))
        assert cur.fetchone() is None
        cur.execute("SELECT 1 FROM image_study WHERE studyinstanceuid=%s", (STUDY,))
        assert cur.fetchone() is not None
        cur.execute("SELECT count(*) FROM image_series WHERE studyinstanceuid=%s AND storage_source='filesystem'", (STUDY,))
        assert cur.fetchone()[0] == siblings
        cur.execute("SELECT 1 FROM sr_provenance WHERE sop_uid=%s", (SOP,))
        assert cur.fetchone() is not None
