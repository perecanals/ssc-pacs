"""Per-user dataset (cohort) access control.

Seeded fixture geometry (conftest.py):
  patients   P-0001 dataset={lvo,crisp2},  P-0002 dataset={lvo}
  studies    1.2.3.4.5 (P-0001),           2.2.2.2.2 (P-0002)
  series     1.2.3.4.5.6 (P-0001)
  users      testuser (admin),
             user_lvo {lvo}, user_crisp {crisp2}, user_none {} (deny-by-default)
"""

import pytest

from tests.conftest import USER_CRISP, USER_LVO, USER_NONE, login_as

P1_STUDY = "1.2.3.4.5"
P1_SERIES = "1.2.3.4.5.6"
P2_STUDY = "2.2.2.2.2"


def _patient_ids(resp):
    return {it["patient_id"] for it in resp.json()["items"]}


# ---------------------------------------------------------------------------
# Authentication regression: browsing endpoints must reject anonymous calls.
# ---------------------------------------------------------------------------

class TestUnauthenticated:
    @pytest.mark.parametrize("path", [
        "/api/patients",
        "/api/studies",
        "/api/series",
        "/api/datasets",
        "/api/study-import-labels",
        "/api/patients/P-0001/studies",
        f"/api/studies/{P1_STUDY}/series",
        f"/api/series/{P1_SERIES}/annotations",
        "/api/labels",
        "/api/labels/summary",
        "/api/labels/x/values",
        "/api/label-definitions",
        "/api/instruments",
        "/api/storage-mode",
        f"/api/studies/{P1_STUDY}/cache-status",
        f"/api/series/{P1_SERIES}/cache-status",
        "/api/patients/P-0001/cache-status",
    ])
    def test_get_requires_login(self, client, path):
        assert client.get(path).status_code == 401


# ---------------------------------------------------------------------------
# List filtering
# ---------------------------------------------------------------------------

class TestListFiltering:
    def test_admin_sees_all_patients(self, logged_in_client):
        resp = logged_in_client.get("/api/patients")
        assert {"P-0001", "P-0002"} <= _patient_ids(resp)

    def test_lvo_user_sees_both(self, client):
        resp = login_as(client, USER_LVO).get("/api/patients")
        assert {"P-0001", "P-0002"} <= _patient_ids(resp)

    def test_crisp_user_sees_only_p0001(self, client):
        resp = login_as(client, USER_CRISP).get("/api/patients")
        ids = _patient_ids(resp)
        assert "P-0001" in ids
        assert "P-0002" not in ids

    def test_no_grants_sees_nothing(self, client):
        resp = login_as(client, USER_NONE).get("/api/patients")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 0
        assert body["items"] == []

    def test_studies_filtered(self, client):
        resp = login_as(client, USER_CRISP).get("/api/studies")
        uids = {it["studyinstanceuid"] for it in resp.json()["items"]}
        assert P1_STUDY in uids
        assert P2_STUDY not in uids

    def test_series_filtered(self, client):
        login_as(client, USER_CRISP)
        resp = client.get("/api/series")
        pids = {s["patient_id"] for s in resp.json()["series"]}
        assert pids <= {"P-0001"}
        resp = login_as(client, USER_NONE).get("/api/series")
        assert resp.json()["total"] == 0

    def test_datasets_intersected_with_scope(self, client):
        assert login_as(client, USER_CRISP).get("/api/datasets").json() == ["crisp2"]

    def test_datasets_empty_for_ungranted(self, client):
        assert login_as(client, USER_NONE).get("/api/datasets").json() == []

    def test_datasets_full_for_admin(self, logged_in_client):
        assert logged_in_client.get("/api/datasets").json() == ["crisp2", "lvo"]


# ---------------------------------------------------------------------------
# Detail endpoints: out-of-scope ids are 404 (indistinguishable from absent).
# ---------------------------------------------------------------------------

class TestDetailAccess:
    def test_patient_studies_in_scope(self, client):
        resp = login_as(client, USER_CRISP).get("/api/patients/P-0001/studies")
        assert resp.status_code == 200

    def test_patient_studies_out_of_scope_404(self, client):
        resp = login_as(client, USER_CRISP).get("/api/patients/P-0002/studies")
        assert resp.status_code == 404

    def test_study_series_out_of_scope_404(self, client):
        resp = login_as(client, USER_CRISP).get(f"/api/studies/{P2_STUDY}/series")
        assert resp.status_code == 404

    def test_ohif_link_out_of_scope_404(self, client):
        resp = login_as(client, USER_CRISP).get(f"/api/ohif-link/{P2_STUDY}")
        assert resp.status_code == 404

    def test_cache_status_out_of_scope_404(self, client):
        login_as(client, USER_CRISP)
        assert client.get(f"/api/studies/{P2_STUDY}/cache-status").status_code == 404
        assert client.get("/api/patients/P-0002/cache-status").status_code == 404

    def test_warm_evict_out_of_scope_404(self, client):
        login_as(client, USER_CRISP)
        assert client.post(f"/api/studies/{P2_STUDY}/warm").status_code == 404
        assert client.post(f"/api/studies/{P2_STUDY}/evict").status_code == 404
        assert client.post("/api/patients/P-0002/warm").status_code == 404

    def test_series_endpoints_out_of_scope_404(self, client, db_conn):
        """A P-0002 (lvo-only) series is invisible to a crisp2-scoped user."""
        p2_series = "2.2.2.2.2.9"
        with db_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO image_series "
                "(patient_id, studyinstanceuid, seriesinstanceuid, modality) "
                "VALUES ('P-0002', %s, %s, 'CT') ON CONFLICT DO NOTHING",
                (P2_STUDY, p2_series),
            )
        db_conn.commit()
        try:
            login_as(client, USER_CRISP)
            assert client.get(f"/api/series/{p2_series}/cache-status").status_code == 404
            assert client.post(f"/api/series/{p2_series}/warm").status_code == 404
            assert client.post(f"/api/series/{p2_series}/evict").status_code == 404
        finally:
            with db_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM image_series WHERE seriesinstanceuid = %s", (p2_series,)
                )
            db_conn.commit()

    def test_batch_cache_status_drops_out_of_scope(self, client):
        login_as(client, USER_CRISP)
        resp = client.post(
            "/api/cache-status/batch",
            json={"uids": [P1_STUDY, P2_STUDY],
                  "patient_ids": ["P-0001", "P-0002"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert P2_STUDY not in body["studies"]
        assert "P-0002" not in body["patients"]


# ---------------------------------------------------------------------------
# Annotations
# ---------------------------------------------------------------------------

@pytest.fixture()
def _cleanup_annotations(db_conn):
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM annotations WHERE label LIKE 'dsperm_%'")
        cur.execute("DELETE FROM label_value_options WHERE label LIKE 'dsperm_%'")
        cur.execute("DELETE FROM label_definitions WHERE name LIKE 'dsperm_%'")
    db_conn.commit()


@pytest.mark.usefixtures("_cleanup_annotations")
class TestAnnotationScoping:
    def test_create_out_of_scope_404(self, client):
        login_as(client, USER_CRISP)
        resp = client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": "P-0002",
                  "label": "dsperm_flag", "value": "x"},
        )
        assert resp.status_code == 404

    def test_create_in_scope_ok(self, client):
        login_as(client, USER_CRISP)
        resp = client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": "P-0001",
                  "label": "dsperm_flag", "value": "x"},
        )
        assert resp.status_code == 201

    def test_delete_out_of_scope_404(self, logged_in_client, client):
        created = logged_in_client.post(
            "/api/annotations",
            json={"level": "study", "studyinstanceuid": P2_STUDY,
                  "patient_id": "P-0002", "label": "dsperm_del", "value": "x"},
        )
        ann_id = created.json()["id"]
        login_as(client, USER_CRISP)
        assert client.delete(f"/api/annotations/{ann_id}").status_code == 404

    def test_series_annotations_out_of_scope_404(self, client):
        # USER_NONE has no grants at all — even P-0001's series is hidden.
        login_as(client, USER_NONE)
        resp = client.get(f"/api/series/{P1_SERIES}/annotations")
        assert resp.status_code == 404

    def test_label_values_are_global_vocabulary(self, logged_in_client, client):
        # Select-label values form a shared controlled vocabulary: a value used
        # only on P-0002 (lvo-only) is still visible to a crisp2-scoped user.
        # Only the value string is shared — the underlying P-0002 row stays hidden
        # (enforced by the patient/study/series endpoints, not this list).
        logged_in_client.post(
            "/api/label-definitions",
            json={"name": "dsperm_sel", "level": "patient", "datatype": "select"},
        )
        logged_in_client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": "P-0002",
                  "label": "dsperm_sel", "value": "shared-val"},
        )
        login_as(client, USER_CRISP)
        assert "shared-val" in client.get("/api/labels/dsperm_sel/values").json()
        login_as(client, USER_LVO)
        assert "shared-val" in client.get("/api/labels/dsperm_sel/values").json()


# ---------------------------------------------------------------------------
# /api/me carries the grants
# ---------------------------------------------------------------------------

class TestMe:
    def test_me_includes_allowed_datasets(self, client):
        login_as(client, USER_CRISP)
        body = client.get("/api/me").json()
        assert body["allowed_datasets"] == ["crisp2"]
        assert body["is_admin"] is False

    def test_me_admin(self, logged_in_client):
        body = logged_in_client.get("/api/me").json()
        assert body["is_admin"] is True
