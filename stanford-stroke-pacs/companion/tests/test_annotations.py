"""Tests for annotation CRUD at all three levels (patient/study/series)."""

import pytest


@pytest.fixture()
def _cleanup_annotations(db_conn):
    """Delete annotations created during the test."""
    yield
    with db_conn.cursor() as cur:
        cur.execute("DELETE FROM annotations WHERE created_by = 'testuser'")
    db_conn.commit()


# ---------------------------------------------------------------------------
# Patient-level annotations
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_cleanup_annotations")
class TestPatientAnnotations:
    def test_create_patient_annotation(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/annotations",
            json={
                "level": "patient",
                "patient_id": "P-0001",
                "label": "test_flag",
                "value": "yes",
            },
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["level"] == "patient"
        assert data["label"] == "test_flag"
        assert data["value"] == "yes"

    def test_read_patient_annotations_via_listing(self, logged_in_client):
        # Create an annotation first.
        logged_in_client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": "P-0001", "label": "read_test", "value": "v1"},
        )
        resp = logged_in_client.get("/api/patients", params={"patient_id": "P-0001"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) >= 1
        ann_labels = [a["label"] for a in items[0].get("annotations", [])]
        assert "read_test" in ann_labels

    def test_delete_annotation(self, logged_in_client):
        create = logged_in_client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": "P-0001", "label": "del_me", "value": "x"},
        )
        ann_id = create.json()["id"]
        resp = logged_in_client.delete(f"/api/annotations/{ann_id}")
        assert resp.status_code == 204

    def test_upsert_overwrites_value(self, logged_in_client):
        logged_in_client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": "P-0001", "label": "upsert_test", "value": "v1"},
        )
        resp = logged_in_client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": "P-0001", "label": "upsert_test", "value": "v2"},
        )
        assert resp.status_code == 201
        assert resp.json()["value"] == "v2"


# ---------------------------------------------------------------------------
# Study-level annotations
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_cleanup_annotations")
class TestStudyAnnotations:
    def test_create_study_annotation(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/annotations",
            json={
                "level": "study",
                "studyinstanceuid": "1.2.3.4.5",
                "patient_id": "P-0001",
                "label": "study_flag",
                "value": "good",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["level"] == "study"


# ---------------------------------------------------------------------------
# Series-level annotations
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_cleanup_annotations")
class TestSeriesAnnotations:
    def test_create_series_annotation(self, logged_in_client):
        resp = logged_in_client.post(
            "/api/annotations",
            json={
                "level": "series",
                "seriesinstanceuid": "1.2.3.4.5.6",
                "studyinstanceuid": "1.2.3.4.5",
                "patient_id": "P-0001",
                "label": "series_flag",
                "value": "marked",
            },
        )
        assert resp.status_code == 201
        assert resp.json()["level"] == "series"

    def test_inherited_annotations_on_series(self, logged_in_client):
        """Series listing should include parent-level annotations."""
        # Create a patient-level annotation.
        logged_in_client.post(
            "/api/annotations",
            json={"level": "patient", "patient_id": "P-0001", "label": "inherit_test", "value": "parent"},
        )
        resp = logged_in_client.get("/api/series", params={"patient_id": "P-0001"})
        assert resp.status_code == 200
        series_items = resp.json()["series"]
        assert len(series_items) >= 1
        inherited = series_items[0].get("inherited_annotations", [])
        inherited_labels = [a["label"] for a in inherited]
        assert "inherit_test" in inherited_labels


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_annotation_requires_level_id(logged_in_client):
    """Missing required ID for the level should 400."""
    resp = logged_in_client.post(
        "/api/annotations",
        json={"level": "patient", "label": "oops"},
    )
    assert resp.status_code == 400


def test_annotation_invalid_level(logged_in_client):
    resp = logged_in_client.post(
        "/api/annotations",
        json={"level": "galaxy", "label": "x", "patient_id": "P-0001"},
    )
    assert resp.status_code == 400
